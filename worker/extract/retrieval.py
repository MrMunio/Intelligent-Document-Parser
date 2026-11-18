"""
Hybrid retrieval over document chunks: BM25 keyword ranking plus dense embeddings, fused with
reciprocal rank fusion. Keyword ranking finds exact identifiers and numbers; embeddings find passages
that say the same thing in other words.

Embeddings come from a local model through Ollama by default (EXTRACT_EMBED_PROVIDER=ollama,
EXTRACT_EMBED_MODEL=qwen3-embedding:0.6b, Apache-2.0), or from OpenAI. When the embedding engine is not
reachable the index falls back to keyword ranking and the job continues; the result says so.
The index lives in memory for the duration of one job.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

import httpx

from shared.config import settings
from shared.llm import resolve_api_key
from shared.logger import setup_logger
from shared.model_catalog import embedding_model
from worker.extract.chunking import Chunk

log = setup_logger("worker.extract.retrieval")

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-/][a-z0-9]+)*")
STOPWORDS = set("""a an and are as at be but by for from has have he her his i if in into is it its of on or our she
that the their them then there these they this to was we were what when where which who will with you your shall
may must any all such other than per upon""".split())
RRF_K = 60


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tfs = [Counter(tokenize(d)) for d in docs]
        self.lengths = [sum(tf.values()) for tf in self.tfs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = tokenize(query)
        out = []
        for tf, length in zip(self.tfs, self.lengths):
            score = 0.0
            for term in terms:
                f = tf.get(term)
                if f:
                    score += self.idf.get(term, 0.0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / self.avg))
            out.append(score)
        return out


class EmbeddingError(Exception):
    pass


class Embedder:
    """Embedding client for Ollama (/api/embed) or OpenAI (/v1/embeddings)."""

    def __init__(self, provider: str, model: str, url: Optional[str] = None, api_key: Optional[str] = None):
        self.provider = provider
        self.model = model
        self.url = (url or ("https://api.openai.com/v1" if provider == "openai" else "http://ollama:11434")).rstrip("/")
        self.api_key = resolve_api_key(provider, api_key) if provider == "openai" else api_key
        info = embedding_model(provider, model)
        self.query_prefix = info.get("query_prefix", "")
        self.document_prefix = info.get("document_prefix", "")
        self.tokens_used = 0

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        prefix = self.query_prefix if query else self.document_prefix
        inputs = [prefix + t[:8000] for t in texts]
        out: list[list[float]] = []
        batch = max(1, settings.extract_embed_batch)
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            for i in range(0, len(inputs), batch):
                part = inputs[i:i + batch]
                try:
                    if self.provider == "ollama":
                        resp = await client.post(f"{self.url}/api/embed", json={"model": self.model, "input": part,
                                                                                "keep_alive": settings.ollama_keep_alive})
                        resp.raise_for_status()
                        data = resp.json()
                        vectors = data.get("embeddings") or []
                        self.tokens_used += int(data.get("prompt_eval_count") or 0)
                    elif self.provider == "openai":
                        resp = await client.post(f"{self.url}/embeddings", json={"model": self.model, "input": part},
                                                 headers={"Authorization": f"Bearer {self.api_key}"})
                        resp.raise_for_status()
                        data = resp.json()
                        vectors = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
                        self.tokens_used += int((data.get("usage") or {}).get("total_tokens") or 0)
                    else:
                        raise EmbeddingError(f"unknown embedding provider '{self.provider}'")
                except httpx.HTTPStatusError as exc:
                    raise EmbeddingError(f"{self.label}: HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    raise EmbeddingError(f"{self.label}: {type(exc).__name__}: {exc}") from exc
                if len(vectors) != len(part):
                    raise EmbeddingError(f"{self.label}: returned {len(vectors)} vectors for {len(part)} inputs")
                out.extend(vectors)
        return out


def build_embedder() -> Optional[Embedder]:
    from shared import runtime_config
    cfg = runtime_config.get("extract_embed")
    provider = (cfg.get("provider") or "none").strip().lower()
    if provider in ("", "none", "off"):
        return None
    key = settings.extract_embed_api_key if provider == (settings.extract_embed_provider or "").lower() else None
    return Embedder(provider, cfg.get("model") or settings.extract_embed_model, cfg.get("url"), key)


@dataclass
class Hit:
    chunk: Chunk
    score: float
    keyword_rank: Optional[int]
    vector_rank: Optional[int]


class HybridIndex:
    def __init__(self, chunks: list[Chunk], embedder: Optional[Embedder] = None):
        self.chunks = chunks
        self.embedder = embedder
        self.bm25 = BM25([self._doc_text(c) for c in chunks])
        self.matrix = None  # numpy array of unit vectors
        self.mode = "keyword"
        self.note = "" if embedder else "Embeddings are disabled (EXTRACT_EMBED_PROVIDER=none): keyword ranking only."

    @staticmethod
    def _doc_text(chunk: Chunk) -> str:
        return f"{chunk.section_title or ''}\n{chunk.text}"

    async def build(self) -> None:
        if not self.embedder or not self.chunks:
            return
        import numpy as np
        try:
            vectors = await self.embedder.embed([self._doc_text(c) for c in self.chunks])
        except EmbeddingError as exc:
            self.note = f"Embedding engine unavailable ({str(exc)[:160]}): keyword ranking only."
            log.warning("[retrieval] %s", self.note)
            return
        matrix = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.matrix = matrix / norms
        self.mode = "hybrid"

    async def search(self, query: str, top_k: int = 6, page_from: Optional[int] = None,
                     page_to: Optional[int] = None, section_id: Optional[str] = None) -> list[Hit]:
        allowed = [i for i, c in enumerate(self.chunks)
                   if (page_from is None or c.page_end >= page_from) and (page_to is None or c.page_start <= page_to)
                   and (section_id is None or c.section_id == section_id)]
        if not allowed:
            return []
        kw = self.bm25.scores(query)
        kw_order = sorted((i for i in allowed if kw[i] > 0), key=lambda i: -kw[i])
        kw_rank = {i: r for r, i in enumerate(kw_order)}
        vec_rank: dict[int, int] = {}
        if self.matrix is not None:
            import numpy as np
            try:
                q = np.asarray((await self.embedder.embed([query], query=True))[0], dtype=np.float32)
                q /= (np.linalg.norm(q) or 1.0)
                sims = self.matrix[allowed] @ q
                order = [allowed[j] for j in np.argsort(-sims)[: max(top_k * 4, 20)]]
                vec_rank = {i: r for r, i in enumerate(order)}
            except EmbeddingError as exc:
                log.warning("[retrieval] query embedding failed, keyword ranking only: %s", exc)
        fused: dict[int, float] = {}
        for i, r in kw_rank.items():
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + r + 1)
        for i, r in vec_rank.items():
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + r + 1)
        best = sorted(fused, key=lambda i: -fused[i])[:top_k]
        return [Hit(self.chunks[i], fused[i], kw_rank.get(i), vec_rank.get(i)) for i in best]
