"""
LLM engine layer.

Engines: openai, azure_openai, openai_compatible (vLLM, LM Studio, Groq, Together, ...),
anthropic, gemini, ollama.

Every engine exposes the same three coroutines:
    chat(system, user, json_mode)      -> LLMResult
    vision(prompt, image_bytes)        -> LLMResult
    health()                           -> {"ok": bool, "detail": str}

`get_llm_client(mode)` returns a FailoverLLM that wraps the configured primary engine and
an optional fallback engine. When the primary fails (bad credentials, network, exhausted
retries) the request is transparently served by the fallback, and the primary is skipped
for a cool-down period so that jobs do not pay the failure latency on every call.
"""
import asyncio
import base64
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from shared.config import settings

log = logging.getLogger("shared.llm")

PROVIDERS = ["openai", "azure_openai", "openai_compatible", "anthropic", "gemini", "ollama"]

DEFAULT_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "ollama": "http://ollama:11434",
}


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    provider: str = ""
    model: str = ""


class LLMError(Exception):
    def __init__(self, message: str, *, status: Optional[int] = None, auth: bool = False, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.auth = auth
        self.retryable = retryable


def _image_media_type(image_bytes: bytes) -> str:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


class BaseProvider:
    name = "base"

    def __init__(self, base_url: Optional[str], model: str, api_key: Optional[str] = None, api_version: Optional[str] = None):
        self.base_url = (base_url or DEFAULT_URLS.get(self.name, "")).rstrip("/")
        self.model = model
        self.api_key = api_key or ""
        self.api_version = api_version
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def ident(self) -> str:
        return f"{self.name}:{self.model}@{self.base_url}"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
        return self._client

    async def aclose(self):
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def _request(self, method: str, url: str, *, headers=None, json_body=None, retries: Optional[int] = None) -> dict:
        retries = settings.llm_max_retries if retries is None else retries
        last: Optional[LLMError] = None
        for attempt in range(retries + 1):
            try:
                resp = await self._http().request(method, url, headers=headers, json=json_body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = LLMError(f"{self.name}: network error ({type(exc).__name__}: {exc})", retryable=True)
            else:
                if resp.status_code < 400:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise LLMError(f"{self.name}: response was not JSON") from exc
                body = resp.text[:400]
                if resp.status_code in (401, 403):
                    raise LLMError(f"{self.name}: authentication failed (HTTP {resp.status_code})", status=resp.status_code, auth=True)
                if resp.status_code in (408, 409, 429) or resp.status_code >= 500:
                    last = LLMError(f"{self.name}: HTTP {resp.status_code}: {body}", status=resp.status_code, retryable=True)
                else:
                    raise LLMError(f"{self.name}: HTTP {resp.status_code}: {body}", status=resp.status_code)
            if attempt < retries:
                await asyncio.sleep(min(8.0, (2 ** attempt)) + random.uniform(0, 0.5))
        assert last is not None
        raise last

    async def chat(self, system: str, user: str, *, json_mode: bool = False, max_tokens: int = 4096) -> LLMResult:
        raise NotImplementedError

    async def vision(self, prompt: str, image_bytes: bytes, *, max_tokens: int = 1000) -> LLMResult:
        raise NotImplementedError

    async def health(self) -> dict:
        raise NotImplementedError

    def _result(self, text: str, input_tokens: int = 0, output_tokens: int = 0) -> LLMResult:
        return LLMResult(text=(text or "").strip(), input_tokens=int(input_tokens or 0),
                         output_tokens=int(output_tokens or 0), provider=self.name, model=self.model)


# ---------------------------------------------------------------------------
# OpenAI family
# ---------------------------------------------------------------------------

class OpenAIProvider(BaseProvider):
    name = "openai"
    REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")

    def _is_reasoning_model(self) -> bool:
        return any(self.model.startswith(p) for p in self.REASONING_PREFIXES)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _base_payload(self, messages: list, json_mode: bool, max_tokens: int) -> dict:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if self._is_reasoning_model():
            payload["max_completion_tokens"] = max(max_tokens, 4096)
            if settings.llm_reasoning_effort:
                payload["reasoning_effort"] = settings.llm_reasoning_effort
        else:
            payload["temperature"] = 0
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _complete(self, payload: dict) -> LLMResult:
        optional = ("reasoning_effort", "response_format", "temperature")
        try:
            data = await self._request("POST", self._chat_url(), headers=self._headers(), json_body=payload)
        except LLMError as exc:
            # Some models or gateways reject optional parameters. Drop them once and retry.
            if exc.status == 400 and any(k in payload and k in str(exc) for k in optional):
                slim = {k: v for k, v in payload.items() if k not in optional}
                data = await self._request("POST", self._chat_url(), headers=self._headers(), json_body=slim)
            else:
                raise
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.name}: unexpected response shape") from exc
        usage = data.get("usage") or {}
        return self._result(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096) -> LLMResult:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return await self._complete(self._base_payload(messages, json_mode, max_tokens))

    async def vision(self, prompt, image_bytes, *, max_tokens=1000) -> LLMResult:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        media = _image_media_type(image_bytes)
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}},
            ],
        }]
        return await self._complete(self._base_payload(messages, False, max_tokens))

    async def health(self) -> dict:
        if not self.api_key and self.name != "openai_compatible":
            return {"ok": False, "detail": "API key is not configured"}
        try:
            await self._request("GET", f"{self.base_url}/models", headers=self._headers(), retries=0)
            return {"ok": True, "detail": "Credentials accepted"}
        except LLMError as exc:
            if not exc.auth:
                return {"ok": False, "detail": str(exc)}
        # Restricted project keys may be denied the model list but still allowed to run completions.
        try:
            await self.chat("Reply with OK.", "OK", max_tokens=16)
            return {"ok": True, "detail": "Completions accepted (this key cannot list models)"}
        except LLMError as exc:
            return {"ok": False, "detail": str(exc)}


class OpenAICompatibleProvider(OpenAIProvider):
    """Any server that speaks the OpenAI chat completions protocol (vLLM, LM Studio, Groq, ...)."""
    name = "openai_compatible"

    def _is_reasoning_model(self) -> bool:
        return False

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


class AzureOpenAIProvider(OpenAIProvider):
    """`model` is the Azure deployment name, `base_url` is https://<resource>.openai.azure.com."""
    name = "azure_openai"

    def _headers(self) -> dict:
        return {"api-key": self.api_key, "Content-Type": "application/json"}

    def _api_version(self) -> str:
        return self.api_version or "2024-10-21"

    def _chat_url(self) -> str:
        return f"{self.base_url}/openai/deployments/{self.model}/chat/completions?api-version={self._api_version()}"

    async def health(self) -> dict:
        if not self.api_key:
            return {"ok": False, "detail": "API key is not configured"}
        try:
            await self._request("GET", f"{self.base_url}/openai/models?api-version={self._api_version()}",
                                headers=self._headers(), retries=0)
            return {"ok": True, "detail": "Credentials accepted"}
        except LLMError as exc:
            return {"ok": False, "detail": str(exc)}


# ---------------------------------------------------------------------------
# Anthropic (official SDK)
# ---------------------------------------------------------------------------

class AnthropicProvider(BaseProvider):
    name = "anthropic"
    # Models that support the server-side refusal fallback parameter.
    FALLBACK_MODELS = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sdk = None

    def _anthropic(self):
        if self._sdk is None:
            import anthropic  # imported lazily so other engines work without the package
            kwargs: dict[str, Any] = {"timeout": settings.llm_timeout_seconds, "max_retries": settings.llm_max_retries}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.base_url and self.base_url != DEFAULT_URLS["anthropic"]:
                kwargs["base_url"] = self.base_url
            self._sdk = anthropic.AsyncAnthropic(**kwargs)
        return self._sdk

    async def aclose(self):
        if self._sdk is not None:
            try:
                await self._sdk.close()
            finally:
                self._sdk = None
        await super().aclose()

    async def _create(self, system: Optional[str], content: Any, max_tokens: int) -> LLMResult:
        import anthropic
        client = self._anthropic()
        base: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            base["system"] = system
        # Current Claude models reject sampling parameters, so none are sent.
        rich = dict(base)
        if settings.llm_reasoning_effort:
            rich["output_config"] = {"effort": settings.llm_reasoning_effort}
        use_fallbacks = any(self.model.startswith(p) for p in self.FALLBACK_MODELS)
        try:
            try:
                if use_fallbacks:
                    resp = await client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **rich)
                else:
                    resp = await client.messages.create(**rich)
            except (TypeError, anthropic.BadRequestError) as exc:
                # Older SDKs or models without effort / fallback support: retry with the plain request.
                log.warning("[anthropic] optional parameters rejected (%s); retrying with the plain request", exc)
                resp = await client.messages.create(**base)
        except anthropic.AuthenticationError as exc:
            raise LLMError("anthropic: authentication failed", status=401, auth=True) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError("anthropic: permission denied", status=403, auth=True) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("anthropic: rate limited", status=429, retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"anthropic: HTTP {exc.status_code}: {exc.message}", status=exc.status_code,
                           retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"anthropic: network error ({exc})", retryable=True) from exc
        if getattr(resp, "stop_reason", None) == "refusal":
            raise LLMError("anthropic: the model declined this request")
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        usage = getattr(resp, "usage", None)
        return self._result(text, getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0))

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096) -> LLMResult:
        return await self._create(system, user, max(max_tokens, 16000))

    async def vision(self, prompt, image_bytes, *, max_tokens=1000) -> LLMResult:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": _image_media_type(image_bytes),
                                         "data": base64.standard_b64encode(image_bytes).decode("utf-8")}},
            {"type": "text", "text": prompt},
        ]
        return await self._create(None, content, max(max_tokens, 4096))

    async def health(self) -> dict:
        if not self.api_key:
            return {"ok": False, "detail": "API key is not configured"}
        try:
            import anthropic
            await self._anthropic().models.retrieve(self.model)
            return {"ok": True, "detail": "Credentials accepted and model is available"}
        except ImportError:
            return {"ok": False, "detail": "anthropic package is not installed"}
        except Exception as exc:  # health checks must never raise
            return {"ok": False, "detail": f"anthropic: {type(exc).__name__}: {str(exc)[:200]}"}


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------

class GeminiProvider(BaseProvider):
    name = "gemini"

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def _url(self) -> str:
        return f"{self.base_url}/v1beta/models/{self.model}:generateContent"

    async def _generate(self, body: dict) -> LLMResult:
        data = await self._request("POST", self._url(), headers=self._headers(), json_body=body)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("gemini: unexpected response shape (the request may have been blocked)") from exc
        usage = data.get("usageMetadata") or {}
        return self._result(text, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0))

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096) -> LLMResult:
        config: dict[str, Any] = {"temperature": 0, "maxOutputTokens": max(max_tokens, 8192)}
        if json_mode:
            config["responseMimeType"] = "application/json"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }
        return await self._generate(body)

    async def vision(self, prompt, image_bytes, *, max_tokens=1000) -> LLMResult:
        body = {
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": _image_media_type(image_bytes),
                                 "data": base64.b64encode(image_bytes).decode("utf-8")}},
                {"text": prompt},
            ]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": max(max_tokens, 2048)},
        }
        return await self._generate(body)

    async def health(self) -> dict:
        if not self.api_key:
            return {"ok": False, "detail": "API key is not configured"}
        try:
            await self._request("GET", f"{self.base_url}/v1beta/models/{self.model}", headers=self._headers(), retries=0)
            return {"ok": True, "detail": "Credentials accepted and model is available"}
        except LLMError as exc:
            return {"ok": False, "detail": str(exc)}


# ---------------------------------------------------------------------------
# Ollama (local models)
# ---------------------------------------------------------------------------

class OllamaProvider(BaseProvider):
    name = "ollama"

    async def _chat(self, messages: list, json_mode: bool, max_tokens: int) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,  # thinking models are slow and can break JSON output
            "keep_alive": settings.ollama_keep_alive,
            "options": {"temperature": 0, "num_ctx": settings.ollama_num_ctx, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        url = f"{self.base_url}/api/chat"
        try:
            data = await self._request("POST", url, json_body=payload)
        except LLMError as exc:
            if exc.status == 400 and "think" in str(exc).lower():
                payload.pop("think", None)  # model without a thinking mode
                data = await self._request("POST", url, json_body=payload)
            else:
                raise
        text = (data.get("message") or {}).get("content", "")
        return self._result(text, data.get("prompt_eval_count", 0), data.get("eval_count", 0))

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096) -> LLMResult:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return await self._chat(messages, json_mode, max_tokens)

    async def vision(self, prompt, image_bytes, *, max_tokens=1000) -> LLMResult:
        messages = [{"role": "user", "content": prompt, "images": [base64.b64encode(image_bytes).decode("utf-8")]}]
        return await self._chat(messages, False, max_tokens)

    async def health(self) -> dict:
        try:
            data = await self._request("GET", f"{self.base_url}/api/tags", retries=0)
        except LLMError as exc:
            return {"ok": False, "detail": str(exc)}
        names = [m.get("name", "") for m in data.get("models", [])]
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted in names or self.model in names:
            return {"ok": True, "detail": "Server reachable and model is downloaded"}
        return {"ok": False, "detail": f"Server reachable but model '{self.model}' is not downloaded yet"}


PROVIDER_CLASSES = {
    "openai": OpenAIProvider,
    "azure_openai": AzureOpenAIProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
}


def build_provider(provider: str, url: Optional[str], model: str, api_key: Optional[str] = None,
                   api_version: Optional[str] = None) -> BaseProvider:
    key = (provider or "").strip().lower()
    if key not in PROVIDER_CLASSES:
        raise ValueError(f"Unknown LLM provider '{provider}'. Supported: {', '.join(PROVIDERS)}")
    return PROVIDER_CLASSES[key](url, model, api_key, api_version)


# ---------------------------------------------------------------------------
# Failover wrapper
# ---------------------------------------------------------------------------

# provider ident -> unix time until which the provider is skipped
_COOLDOWN: dict[str, float] = {}


class FailoverLLM:
    def __init__(self, primary: BaseProvider, fallback: Optional[BaseProvider] = None):
        self.primary = primary
        self.fallback = fallback
        self.usage: dict[str, Any] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "engines": {}, "failovers": 0}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        await self.primary.aclose()
        if self.fallback:
            await self.fallback.aclose()

    def _record(self, res: LLMResult):
        self.usage["calls"] += 1
        self.usage["input_tokens"] += res.input_tokens
        self.usage["output_tokens"] += res.output_tokens
        key = f"{res.provider}:{res.model}"
        self.usage["engines"][key] = self.usage["engines"].get(key, 0) + 1

    async def _run(self, method: str, *args, **kwargs) -> LLMResult:
        primary_skipped = _COOLDOWN.get(self.primary.ident, 0) > time.time()
        if not primary_skipped or self.fallback is None:
            try:
                res = await getattr(self.primary, method)(*args, **kwargs)
                self._record(res)
                return res
            except LLMError as exc:
                if self.fallback is None:
                    raise
                _COOLDOWN[self.primary.ident] = time.time() + settings.llm_failover_cooldown_seconds
                log.warning("[llm] primary engine %s failed (%s). Switching to fallback %s for %ss.",
                            self.primary.ident, exc, self.fallback.ident, settings.llm_failover_cooldown_seconds)
        self.usage["failovers"] += 1
        res = await getattr(self.fallback, method)(*args, **kwargs)
        self._record(res)
        return res

    async def chat(self, system: str, user: str, *, json_mode: bool = False, max_tokens: int = 4096) -> LLMResult:
        return await self._run("chat", system, user, json_mode=json_mode, max_tokens=max_tokens)

    async def vision(self, prompt: str, image_bytes: bytes, *, max_tokens: int = 1000) -> LLMResult:
        return await self._run("vision", prompt, image_bytes, max_tokens=max_tokens)


def _mode_config(mode: str) -> dict:
    if mode not in ("doc", "phi"):
        raise ValueError("Mode must be either 'doc' or 'phi'")
    g = lambda name: getattr(settings, f"{mode}_llm_{name}")  # noqa: E731
    return {
        "provider": g("provider"), "url": g("url"), "model": g("model"), "api_key": g("api_key"),
        "api_version": g("api_version"),
        "fallback_provider": g("fallback_provider"), "fallback_url": g("fallback_url"),
        "fallback_model": g("fallback_model"), "fallback_api_key": g("fallback_api_key"),
    }


def get_llm_client(mode: str) -> FailoverLLM:
    """Returns the failover-wrapped engine for 'doc' (vision) or 'phi' (text) work."""
    cfg = _mode_config(mode)
    primary = build_provider(cfg["provider"], cfg["url"], cfg["model"], cfg["api_key"], cfg["api_version"])
    fallback = None
    if cfg["fallback_provider"] and cfg["fallback_model"]:
        fallback = build_provider(cfg["fallback_provider"], cfg["fallback_url"], cfg["fallback_model"],
                                  cfg["fallback_api_key"])
    return FailoverLLM(primary, fallback)


HEALTH_CACHE_SECONDS = 300
_HEALTH_CACHE: dict[str, tuple[float, dict]] = {}


async def _cached_health(prov: BaseProvider) -> dict:
    """Health checks can cost a few tokens, so results are reused for a few minutes."""
    hit = _HEALTH_CACHE.get(prov.ident)
    if hit and time.time() - hit[0] < HEALTH_CACHE_SECONDS:
        return hit[1]
    try:
        health = await asyncio.wait_for(prov.health(), timeout=20)
    except Exception as exc:  # health must not break the page
        health = {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    _HEALTH_CACHE[prov.ident] = (time.time(), health)
    return health


async def describe_llm(mode: str) -> dict:
    """Configuration and live health for the admin system page. Never includes secrets."""
    out: dict[str, Any] = {"mode": mode, "engines": []}
    client = get_llm_client(mode)
    try:
        for role, prov in (("primary", client.primary), ("fallback", client.fallback)):
            if prov is None:
                continue
            health = await _cached_health(prov)
            out["engines"].append({
                "role": role, "provider": prov.name, "model": prov.model, "url": prov.base_url,
                "key_configured": bool(prov.api_key), "ok": health["ok"], "detail": health["detail"],
            })
    finally:
        await client.aclose()
    active = next((e for e in out["engines"] if e["ok"]), None)
    out["active"] = active["role"] if active else None
    return out
