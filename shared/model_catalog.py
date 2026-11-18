"""
Model catalog: context windows, output limits and prices of the LLM engines, read from
shared/model_catalog.json (or MODEL_CATALOG_PATH).

The data extraction service uses it to:
  * find the context window of the running model and derive the usable window
    (min(model window, EXTRACT_CONTEXT_CAP_TOKENS), or OLLAMA_NUM_CTX for local models);
  * decide when an agent session has to be compacted (EXTRACT_CONTEXT_THRESHOLD of the usable window);
  * report the cost of a job from the token usage of every call.
"""
import json
import os
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Optional

from shared.config import settings

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_catalog.json")
UNKNOWN_CONTEXT_WINDOW = 128_000  # used for models that are not in the catalog
UNKNOWN_MAX_OUTPUT = 16_000


@dataclass
class ModelInfo:
    provider: str
    id: str
    display_name: str
    context_window: int
    max_output_tokens: int
    max_input_tokens: Optional[int] = None
    tier: str = ""
    status: str = ""
    reasoning: bool = False
    tool_calling: bool = True
    structured_outputs: bool = False
    context_verified: bool = False
    pricing: dict = field(default_factory=dict)
    scheduled_pricing: list = field(default_factory=list)
    in_catalog: bool = True
    notes: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"

    def input_limit(self) -> int:
        """Largest prompt the model accepts."""
        if self.max_input_tokens:
            return self.max_input_tokens
        return max(1, self.context_window - min(self.max_output_tokens, self.context_window // 4))

    def prices_on(self, day: Optional[date] = None) -> dict:
        """Base prices, replaced by the latest scheduled change whose date has passed."""
        day = day or date.today()
        prices = dict(self.pricing)
        for change in sorted(self.scheduled_pricing, key=lambda c: c.get("effective", "")):
            try:
                effective = date.fromisoformat(change["effective"])
            except (KeyError, ValueError):
                continue
            if effective <= day:
                prices.update({k: v for k, v in change.items() if k != "effective"})
        return prices

    def call_cost(self, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
                  cache_write_tokens: int = 0, day: Optional[date] = None) -> float:
        """
        Cost in USD of one call. `input_tokens` are uncached prompt tokens. The long-context tier applies to
        the whole call when the full prompt is larger than its threshold.
        """
        prices = self.prices_on(day)
        prompt_total = input_tokens + cache_read_tokens + cache_write_tokens
        long_tier = prices.get("long_context")
        if long_tier and prompt_total > long_tier.get("threshold_tokens", 10 ** 12):
            prices = {**prices, **{k: v for k, v in long_tier.items() if k != "threshold_tokens"}}
        p_in = float(prices.get("input", 0.0))
        p_out = float(prices.get("output", 0.0))
        p_read = float(prices.get("cached_input", p_in))
        p_write = float(prices.get("cache_write", p_in))
        cost = (input_tokens * p_in + output_tokens * p_out + cache_read_tokens * p_read + cache_write_tokens * p_write)
        return cost / 1_000_000

    def public(self) -> dict:
        return {
            "provider": self.provider, "id": self.id, "key": self.key, "display_name": self.display_name,
            "context_window": self.context_window, "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens, "tier": self.tier, "status": self.status,
            "reasoning": self.reasoning, "tool_calling": self.tool_calling,
            "structured_outputs": self.structured_outputs, "context_verified": self.context_verified,
            "pricing": self.prices_on(), "notes": self.notes,
        }


@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def catalog() -> dict:
    return _load(settings.model_catalog_path or DEFAULT_PATH)


def _info(provider: str, raw: dict) -> ModelInfo:
    return ModelInfo(
        provider=provider, id=raw["id"], display_name=raw.get("display_name", raw["id"]),
        context_window=int(raw.get("context_window") or UNKNOWN_CONTEXT_WINDOW),
        max_output_tokens=int(raw.get("max_output_tokens") or UNKNOWN_MAX_OUTPUT),
        max_input_tokens=raw.get("max_input_tokens"), tier=raw.get("tier", ""), status=raw.get("status", ""),
        reasoning=bool(raw.get("reasoning")), tool_calling=bool(raw.get("tool_calling", True)),
        structured_outputs=bool(raw.get("structured_outputs")),
        context_verified=bool(raw.get("context_verified")), pricing=raw.get("pricing") or {},
        scheduled_pricing=raw.get("scheduled_pricing") or [], notes=raw.get("notes", ""),
    )


# Providers that speak another provider's protocol and serve the same model families.
_PRICE_ALIASES = {"azure_openai": "openai"}


def models(provider: Optional[str] = None) -> list[ModelInfo]:
    out = []
    for name, entry in catalog().get("providers", {}).items():
        if provider and name != provider:
            continue
        out.extend(_info(name, raw) for raw in entry.get("models", []))
    return out


def selectable_models(provider: str) -> list[ModelInfo]:
    """Models offered for selection: the provider's `account_models` when the catalog lists them, else all current ones."""
    entry = catalog().get("providers", {}).get(provider, {})
    allowed = entry.get("account_models")
    out = [m for m in models(provider) if m.status != "deprecated"]
    if allowed:
        order = {mid: i for i, mid in enumerate(allowed)}
        out = sorted((m for m in out if m.id in order), key=lambda m: order[m.id])
    return out


def find_model(provider: str, model_id: str) -> ModelInfo:
    """Catalog entry for a model. Unknown models get conservative defaults and no price."""
    lookup = _PRICE_ALIASES.get(provider, provider)
    entry = catalog().get("providers", {}).get(lookup, {})
    for raw in entry.get("models", []):
        if raw["id"] == model_id:
            info = _info(lookup, raw)
            info.provider = provider
            return info
    # Dated snapshots and deployment names: match on the longest catalog id the name starts with.
    best = None
    for raw in entry.get("models", []):
        if model_id.startswith(raw["id"]) and (best is None or len(raw["id"]) > len(best["id"])):
            best = raw
    if best is not None:
        info = _info(lookup, best)
        info.provider, info.id = provider, model_id
        return info
    return ModelInfo(provider=provider, id=model_id, display_name=model_id, context_window=UNKNOWN_CONTEXT_WINDOW,
                     max_output_tokens=UNKNOWN_MAX_OUTPUT, in_catalog=False,
                     notes="Not in the model catalog: conservative limits are assumed and cost is not reported.")


def recommended(provider: str, purpose: str) -> Optional[str]:
    entry = catalog().get("providers", {}).get(_PRICE_ALIASES.get(provider, provider), {})
    return (entry.get("recommended") or {}).get(purpose)


def provider_names() -> list[str]:
    return list(catalog().get("providers", {}).keys())


def provider_display_name(provider: str) -> str:
    return catalog().get("providers", {}).get(provider, {}).get("display_name", provider)


def embedding_model(provider: str, model_id: str) -> dict:
    known = catalog().get("embedding_models", {}).get(provider, [])
    for raw in known:
        if raw["id"] == model_id:
            return raw
    for raw in known:  # same family, another tag (for example qwen3-embedding:4b)
        if model_id.split(":")[0] == raw["id"].split(":")[0]:
            return raw
    return {"id": model_id, "query_prefix": "", "document_prefix": "", "pricing": {"input": 0.0}}


def usable_window(info: ModelInfo, override: Optional[int] = None) -> int:
    """
    Tokens the extraction service may fill in one request. The model's input limit, capped by
    EXTRACT_CONTEXT_CAP_TOKENS, by OLLAMA_NUM_CTX for local models, and by a per-request override.
    """
    window = info.input_limit()
    if info.provider == "ollama":
        window = min(window, settings.ollama_num_ctx)
    if settings.extract_context_cap_tokens and settings.extract_context_cap_tokens > 0:
        window = min(window, settings.extract_context_cap_tokens)
    if override:
        window = min(window, int(override))
    return max(4_000, window)


def compaction_threshold() -> float:
    """EXTRACT_CONTEXT_THRESHOLD, kept in a sane range (it is a share of the usable window)."""
    return min(0.95, max(0.30, float(settings.extract_context_threshold)))
