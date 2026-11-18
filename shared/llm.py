"""
LLM engine layer.

Engines: openai, azure_openai, openai_compatible (vLLM, LM Studio, Groq, Together, ...),
anthropic, gemini, ollama.

Every engine exposes the same coroutines:
    chat(system, user, json_mode, json_schema) -> LLMResult
    vision(prompt, image_bytes)                -> LLMResult
    tool_chat(system, history, tools)          -> AgentTurn   (one turn of a tool-using agent)
    health()                                   -> {"ok": bool, "detail": str}

`tool_chat` takes a provider-neutral conversation (see AgentTurn) and converts it to each provider's
native tool-calling format, so one agent harness runs on every engine. Assistant turns keep the
provider's native message (`AgentTurn.native`) and are replayed unchanged, which preserves Claude
thinking blocks and Gemini thought signatures between turns.

`get_llm_client(mode)` returns a FailoverLLM that wraps the configured primary engine and
an optional fallback engine. When the primary fails (bad credentials, network, exhausted
retries) the request is transparently served by the fallback, and the primary is skipped
for a cool-down period so that jobs do not pay the failure latency on every call.
"""
import asyncio
import base64
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
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
    input_tokens: int = 0  # uncached prompt tokens
    output_tokens: int = 0
    provider: str = ""
    model: str = ""
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    native_id: Optional[str] = None  # provider call id when it differs from `id` (Gemini)


@dataclass
class AgentTurn:
    """
    One assistant turn of a tool-using conversation.

    The neutral history passed to `tool_chat` is a list of:
        {"role": "user", "content": str}
        {"role": "assistant", "turn": AgentTurn}
        {"role": "tool", "results": [{"id", "name", "content": str, "is_error": bool}], "text": optional str}
    """
    text: str
    tool_calls: list = field(default_factory=list)
    native: Any = None  # provider-native assistant message, replayed as-is
    provider: str = ""
    model: str = ""
    input_tokens: int = 0  # uncached prompt tokens
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str = ""

    @property
    def context_tokens(self) -> int:
        """Size of the conversation after this turn: the full prompt plus the reply."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens + self.output_tokens


def _call_id() -> str:
    return "call_" + uuid.uuid4().hex[:16]


def _parse_arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"value": value}
    except (TypeError, ValueError):
        return {"__unparsed__": str(raw)[:2000]}


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

    def __init__(self, base_url: Optional[str], model: str, api_key: Optional[str] = None, api_version: Optional[str] = None,
                 effort: Optional[str] = None):
        self.base_url = (base_url or DEFAULT_URLS.get(self.name, "")).rstrip("/")
        self.model = model
        self.api_key = api_key or ""
        self.api_version = api_version
        self.effort = effort  # reasoning effort; None = LLM_REASONING_EFFORT
        self._client: Optional[httpx.AsyncClient] = None

    def _effort(self) -> Optional[str]:
        return self.effort or settings.llm_reasoning_effort

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

    async def chat(self, system: str, user: str, *, json_mode: bool = False, max_tokens: int = 4096,
                   json_schema: Optional[dict] = None) -> LLMResult:
        raise NotImplementedError

    async def vision(self, prompt: str, image_bytes: bytes, *, max_tokens: int = 1000) -> LLMResult:
        raise NotImplementedError

    async def tool_chat(self, system: str, history: list, tools: list, *, max_tokens: int = 16000) -> "AgentTurn":
        raise NotImplementedError

    def _replay(self, turn: "AgentTurn") -> bool:
        """Native messages are replayed only to the provider and model that produced them."""
        return turn.native is not None and turn.provider == self.name and turn.model == self.model

    async def health(self) -> dict:
        raise NotImplementedError

    def _result(self, text: str, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0,
                cache_write: int = 0) -> LLMResult:
        return LLMResult(text=(text or "").strip(), input_tokens=int(input_tokens or 0),
                         output_tokens=int(output_tokens or 0), provider=self.name, model=self.model,
                         cache_read_tokens=int(cache_read or 0), cache_write_tokens=int(cache_write or 0))


# ---------------------------------------------------------------------------
# OpenAI family
# ---------------------------------------------------------------------------

class OpenAIProvider(BaseProvider):
    name = "openai"
    REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt-6")

    def _is_reasoning_model(self) -> bool:
        return any(self.model.startswith(p) for p in self.REASONING_PREFIXES)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _base_payload(self, messages: list, json_mode: bool, max_tokens: int, json_schema: Optional[dict] = None) -> dict:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if self._is_reasoning_model():
            payload["max_completion_tokens"] = max(max_tokens, 4096)
            if self._effort():
                payload["reasoning_effort"] = self._effort()
        else:
            payload["temperature"] = 0
            payload["max_tokens"] = max_tokens
        if json_schema:
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "extraction", "schema": json_schema, "strict": True}}
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _post(self, payload: dict) -> dict:
        optional = ("reasoning_effort", "response_format", "temperature")
        try:
            return await self._request("POST", self._chat_url(), headers=self._headers(), json_body=payload)
        except LLMError as exc:
            # Some models or gateways reject optional parameters. Drop them once and retry.
            if exc.status == 400 and any(k in payload and k in str(exc) for k in optional):
                slim = {k: v for k, v in payload.items() if k not in optional}
                return await self._request("POST", self._chat_url(), headers=self._headers(), json_body=slim)
            raise

    @staticmethod
    def _usage(data: dict) -> tuple[int, int, int]:
        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        return max(0, prompt - cached), int(usage.get("completion_tokens") or 0), cached

    async def _complete(self, payload: dict) -> LLMResult:
        data = await self._post(payload)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.name}: unexpected response shape") from exc
        uncached, output, cached = self._usage(data)
        return self._result(text, uncached, output, cache_read=cached)

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096, json_schema=None) -> LLMResult:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return await self._complete(self._base_payload(messages, json_mode, max_tokens, json_schema))

    def _tool_messages(self, system: str, history: list) -> list:
        messages: list[dict] = [{"role": "system", "content": system}]
        for item in history:
            role = item["role"]
            if role == "user":
                messages.append({"role": "user", "content": item["content"]})
            elif role == "assistant":
                turn: AgentTurn = item["turn"]
                if self._replay(turn):
                    messages.append(turn.native)
                else:
                    msg: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
                    if turn.tool_calls:
                        msg["tool_calls"] = [{"id": c.id, "type": "function",
                                              "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                             for c in turn.tool_calls]
                    messages.append(msg)
            elif role == "tool":
                for r in item["results"]:
                    messages.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
                if item.get("text"):
                    messages.append({"role": "user", "content": item["text"]})
        return messages

    def _uses_responses_api(self) -> bool:
        # OpenAI reasoning models only accept function tools together with reasoning effort on /v1/responses.
        return self.name == "openai" and self._is_reasoning_model()

    def _responses_input(self, history: list) -> list:
        items: list[dict] = []
        for item in history:
            role = item["role"]
            if role == "user":
                items.append({"role": "user", "content": item["content"]})
            elif role == "assistant":
                turn: AgentTurn = item["turn"]
                if self._replay(turn) and isinstance(turn.native, list):
                    items.extend(turn.native)  # reasoning items (encrypted), messages and function calls
                else:
                    if turn.text:
                        items.append({"role": "assistant", "content": turn.text})
                    items.extend({"type": "function_call", "call_id": c.id, "name": c.name,
                                  "arguments": json.dumps(c.arguments)} for c in turn.tool_calls)
            elif role == "tool":
                items.extend({"type": "function_call_output", "call_id": r["id"], "output": r["content"]}
                             for r in item["results"])
                if item.get("text"):
                    items.append({"role": "user", "content": item["text"]})
        return items

    async def _responses_tool_chat(self, system, history, tools, max_tokens) -> AgentTurn:
        payload: dict[str, Any] = {
            "model": self.model, "instructions": system, "input": self._responses_input(history),
            "tools": [{"type": "function", "name": t["name"], "description": t["description"],
                       "parameters": t["parameters"]} for t in tools],
            "max_output_tokens": max(max_tokens, 4096),
            # Stateless: reasoning is returned encrypted and sent back with the next turn.
            "store": False, "include": ["reasoning.encrypted_content"],
        }
        if self._effort():
            payload["reasoning"] = {"effort": self._effort()}
        data = await self._request("POST", f"{self.base_url}/responses", headers=self._headers(), json_body=payload)
        output = data.get("output") or []
        text, calls = [], []
        for out in output:
            kind = out.get("type")
            if kind == "message":
                text.extend(part.get("text", "") for part in out.get("content") or []
                            if part.get("type") == "output_text")
            elif kind == "function_call":
                calls.append(ToolCall(id=out.get("call_id") or _call_id(), name=out.get("name", ""),
                                      arguments=_parse_arguments(out.get("arguments"))))
        usage = data.get("usage") or {}
        prompt = int(usage.get("input_tokens") or 0)
        cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        stop = (data.get("incomplete_details") or {}).get("reason") or data.get("status") or ""
        return AgentTurn(text="".join(text), tool_calls=calls, native=output, provider=self.name, model=self.model,
                         input_tokens=max(0, prompt - cached), output_tokens=int(usage.get("output_tokens") or 0),
                         cache_read_tokens=cached, stop_reason=stop)

    async def tool_chat(self, system, history, tools, *, max_tokens=16000) -> AgentTurn:
        if self._uses_responses_api():
            return await self._responses_tool_chat(system, history, tools, max_tokens)
        payload = self._base_payload(self._tool_messages(system, history), False, max_tokens)
        payload["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": t["parameters"]}} for t in tools]
        data = await self._post(payload)
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.name}: unexpected response shape") from exc
        calls = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function") or {}
            calls.append(ToolCall(id=raw.get("id") or _call_id(), name=fn.get("name", ""),
                                  arguments=_parse_arguments(fn.get("arguments"))))
        native: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            native["tool_calls"] = msg["tool_calls"]
        uncached, output, cached = self._usage(data)
        return AgentTurn(text=msg.get("content") or "", tool_calls=calls, native=native, provider=self.name,
                         model=self.model, input_tokens=uncached, output_tokens=output, cache_read_tokens=cached,
                         stop_reason=choice.get("finish_reason") or "")

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

    async def _send(self, rich: dict, base: dict):
        """Sends a Messages request with the optional parameters, falling back to the plain request."""
        import anthropic
        client = self._anthropic()
        use_fallbacks = any(self.model.startswith(p) for p in self.FALLBACK_MODELS)
        try:
            try:
                if use_fallbacks:
                    resp = await client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **rich)
                else:
                    resp = await client.messages.create(**rich)
            except (TypeError, anthropic.BadRequestError) as exc:
                # Older SDKs or models without effort / fallback / format support: retry with the plain request.
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
        return resp

    @staticmethod
    def _usage(resp) -> tuple[int, int, int, int]:
        usage = getattr(resp, "usage", None)

        def get(name):
            return int(getattr(usage, name, 0) or 0)

        return (get("input_tokens"), get("output_tokens"), get("cache_read_input_tokens"),
                get("cache_creation_input_tokens"))

    async def _create(self, system: Optional[str], content: Any, max_tokens: int,
                      json_schema: Optional[dict] = None) -> LLMResult:
        base: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            base["system"] = system
        # Current Claude models reject sampling parameters, so none are sent.
        rich = dict(base)
        output_config: dict[str, Any] = {}
        if self._effort():
            output_config["effort"] = self._effort()
        if json_schema:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            rich["output_config"] = output_config
        resp = await self._send(rich, base)
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        inp, out, read, write = self._usage(resp)
        return self._result(text, inp, out, cache_read=read, cache_write=write)

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096, json_schema=None) -> LLMResult:
        return await self._create(system, user, max(max_tokens, 16000), json_schema)

    def _tool_messages(self, history: list) -> list:
        messages: list[dict] = []
        for item in history:
            role = item["role"]
            if role == "user":
                messages.append({"role": "user", "content": item["content"]})
            elif role == "assistant":
                turn: AgentTurn = item["turn"]
                if self._replay(turn):
                    content = turn.native  # keeps thinking blocks exactly as returned
                else:
                    content = ([{"type": "text", "text": turn.text}] if turn.text else []) + [
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in turn.tool_calls]
                    content = content or [{"type": "text", "text": "(no output)"}]
                messages.append({"role": "assistant", "content": content})
            elif role == "tool":
                blocks: list[dict] = [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                                       "is_error": bool(r.get("is_error"))} for r in item["results"]]
                if item.get("text"):
                    blocks.append({"type": "text", "text": item["text"]})
                messages.append({"role": "user", "content": blocks})
        return messages

    async def tool_chat(self, system, history, tools, *, max_tokens=16000) -> AgentTurn:
        base: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens, "system": system,
            "messages": self._tool_messages(history),
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                      for t in tools],
        }
        rich = dict(base)
        if self._effort():
            rich["output_config"] = {"effort": self._effort()}
        # Automatic prompt caching: each turn re-reads the growing conversation from the cache.
        rich["cache_control"] = {"type": "ephemeral"}
        resp = await self._send(rich, base)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        calls = [ToolCall(id=b.id, name=b.name, arguments=_parse_arguments(b.input))
                 for b in resp.content if getattr(b, "type", "") == "tool_use"]
        inp, out, read, write = self._usage(resp)
        return AgentTurn(text=text, tool_calls=calls, native=list(resp.content), provider=self.name, model=self.model,
                         input_tokens=inp, output_tokens=out, cache_read_tokens=read, cache_write_tokens=write,
                         stop_reason=getattr(resp, "stop_reason", "") or "")

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

    @staticmethod
    def _usage(data: dict) -> tuple[int, int, int]:
        usage = data.get("usageMetadata") or {}
        prompt = int(usage.get("promptTokenCount") or 0)
        cached = int(usage.get("cachedContentTokenCount") or 0)
        output = int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
        return max(0, prompt - cached), output, cached

    async def _generate(self, body: dict) -> LLMResult:
        data = await self._request("POST", self._url(), headers=self._headers(), json_body=body)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("gemini: unexpected response shape (the request may have been blocked)") from exc
        uncached, output, cached = self._usage(data)
        return self._result(text, uncached, output, cache_read=cached)

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096, json_schema=None) -> LLMResult:
        config: dict[str, Any] = {"temperature": 0, "maxOutputTokens": max(max_tokens, 8192)}
        if json_mode or json_schema:
            config["responseMimeType"] = "application/json"
        if json_schema:
            config["responseJsonSchema"] = json_schema
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }
        try:
            return await self._generate(body)
        except LLMError as exc:
            if json_schema and exc.status == 400:  # schema features this model does not accept
                config.pop("responseJsonSchema", None)
                return await self._generate(body)
            raise

    async def tool_chat(self, system, history, tools, *, max_tokens=16000) -> AgentTurn:
        contents: list[dict] = []
        for item in history:
            role = item["role"]
            if role == "user":
                contents.append({"role": "user", "parts": [{"text": item["content"]}]})
            elif role == "assistant":
                turn: AgentTurn = item["turn"]
                if self._replay(turn):
                    contents.append(turn.native)  # keeps thought signatures
                else:
                    parts = ([{"text": turn.text}] if turn.text else []) + [
                        {"functionCall": {"name": c.name, "args": c.arguments}} for c in turn.tool_calls]
                    contents.append({"role": "model", "parts": parts or [{"text": "(no output)"}]})
            elif role == "tool":
                parts = []
                for r in item["results"]:
                    response: dict[str, Any] = {"name": r["name"], "response": {"result": r["content"]}}
                    if r.get("native_id"):
                        response["id"] = r["native_id"]
                    parts.append({"functionResponse": response})
                if item.get("text"):
                    parts.append({"text": item["text"]})
                contents.append({"role": "user", "parts": parts})
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "tools": [{"functionDeclarations": [{"name": t["name"], "description": t["description"],
                                                 "parameters": t["parameters"]} for t in tools]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        data = await self._request("POST", self._url(), headers=self._headers(), json_body=body)
        try:
            candidate = data["candidates"][0]
            content = candidate.get("content") or {"role": "model", "parts": []}
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("gemini: unexpected response shape (the request may have been blocked)") from exc
        text, calls = [], []
        for part in content.get("parts") or []:
            if "functionCall" in part:
                fc = part["functionCall"]
                calls.append(ToolCall(id=fc.get("id") or _call_id(), name=fc.get("name", ""),
                                      arguments=_parse_arguments(fc.get("args")), native_id=fc.get("id")))
            elif part.get("text") and not part.get("thought"):
                text.append(part["text"])
        content.setdefault("role", "model")
        uncached, output, cached = self._usage(data)
        return AgentTurn(text="".join(text), tool_calls=calls, native=content, provider=self.name, model=self.model,
                         input_tokens=uncached, output_tokens=output, cache_read_tokens=cached,
                         stop_reason=candidate.get("finishReason", ""))

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

    async def _chat(self, messages: list, json_mode: bool, max_tokens: int, json_schema: Optional[dict] = None,
                    tools: Optional[list] = None, raw: bool = False):
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,  # thinking models are slow and can break JSON output
            "keep_alive": settings.ollama_keep_alive,
            "options": {"temperature": 0, "num_ctx": settings.ollama_num_ctx, "num_predict": max_tokens},
        }
        if json_schema:
            payload["format"] = json_schema
        elif json_mode:
            payload["format"] = "json"
        if tools:
            payload["tools"] = tools
        url = f"{self.base_url}/api/chat"
        try:
            data = await self._request("POST", url, json_body=payload)
        except LLMError as exc:
            if exc.status == 400 and "think" in str(exc).lower():
                payload.pop("think", None)  # model without a thinking mode
                data = await self._request("POST", url, json_body=payload)
            else:
                raise
        if raw:
            return data
        text = (data.get("message") or {}).get("content", "")
        return self._result(text, data.get("prompt_eval_count", 0), data.get("eval_count", 0))

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096, json_schema=None) -> LLMResult:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return await self._chat(messages, json_mode, max_tokens, json_schema)

    async def tool_chat(self, system, history, tools, *, max_tokens=16000) -> AgentTurn:
        messages: list[dict] = [{"role": "system", "content": system}]
        for item in history:
            role = item["role"]
            if role == "user":
                messages.append({"role": "user", "content": item["content"]})
            elif role == "assistant":
                turn: AgentTurn = item["turn"]
                if self._replay(turn):
                    messages.append(turn.native)
                else:
                    msg: dict[str, Any] = {"role": "assistant", "content": turn.text or ""}
                    if turn.tool_calls:
                        msg["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}}
                                             for c in turn.tool_calls]
                    messages.append(msg)
            elif role == "tool":
                for r in item["results"]:
                    messages.append({"role": "tool", "content": r["content"], "tool_name": r["name"]})
                if item.get("text"):
                    messages.append({"role": "user", "content": item["text"]})
        specs = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                    "parameters": t["parameters"]}} for t in tools]
        data = await self._chat(messages, False, max_tokens, tools=specs, raw=True)
        msg = data.get("message") or {}
        calls = [ToolCall(id=_call_id(), name=(c.get("function") or {}).get("name", ""),
                          arguments=_parse_arguments((c.get("function") or {}).get("arguments")))
                 for c in msg.get("tool_calls") or []]
        native: dict[str, Any] = {"role": "assistant", "content": msg.get("content", "")}
        if msg.get("tool_calls"):
            native["tool_calls"] = msg["tool_calls"]
        return AgentTurn(text=msg.get("content", "") or "", tool_calls=calls, native=native, provider=self.name,
                         model=self.model, input_tokens=int(data.get("prompt_eval_count") or 0),
                         output_tokens=int(data.get("eval_count") or 0), stop_reason=data.get("done_reason", ""))

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
                   api_version: Optional[str] = None, effort: Optional[str] = None) -> BaseProvider:
    key = (provider or "").strip().lower()
    if key not in PROVIDER_CLASSES:
        raise ValueError(f"Unknown LLM provider '{provider}'. Supported: {', '.join(PROVIDERS)}")
    return PROVIDER_CLASSES[key](url, model, resolve_api_key(key, api_key), api_version, effort)


PROVIDER_KEY_SETTINGS = {"openai": "openai_api_key", "anthropic": "anthropic_api_key", "gemini": "gemini_api_key"}
WORKLOADS = ("phi", "doc", "extract")


def resolve_api_key(provider: str, explicit: Optional[str] = None) -> Optional[str]:
    """
    Credential for a provider: the explicit key, then OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY,
    then the key of any workload configured for the same provider. Lets one key serve every workload.
    """
    if explicit:
        return explicit
    attr = PROVIDER_KEY_SETTINGS.get(provider)
    if attr and getattr(settings, attr, None):
        return getattr(settings, attr)
    for mode in WORKLOADS:
        for prefix in ("", "fallback_"):
            if (getattr(settings, f"{mode}_llm_{prefix}provider", None) or "").lower() == provider:
                key = getattr(settings, f"{mode}_llm_{prefix}api_key", None)
                if key:
                    return key
    return None


# ---------------------------------------------------------------------------
# Failover wrapper
# ---------------------------------------------------------------------------

# provider ident -> unix time until which the provider is skipped
_COOLDOWN: dict[str, float] = {}


class FailoverLLM:
    def __init__(self, primary: BaseProvider, fallback: Optional[BaseProvider] = None):
        self.primary = primary
        self.fallback = fallback
        self.usage: dict[str, Any] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                                      "cache_write_tokens": 0, "engines": {}, "failovers": 0}
        self.call_log: list[LLMResult] = []  # token counts of every call, for per-model cost reporting

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
        self.usage["cache_read_tokens"] += res.cache_read_tokens
        self.usage["cache_write_tokens"] += res.cache_write_tokens
        self.call_log.append(LLMResult(text="", input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                                       provider=res.provider, model=res.model, cache_read_tokens=res.cache_read_tokens,
                                       cache_write_tokens=res.cache_write_tokens))
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

    async def chat(self, system: str, user: str, *, json_mode: bool = False, max_tokens: int = 4096,
                   json_schema: Optional[dict] = None) -> LLMResult:
        if json_schema is None:  # keeps compatibility with engines and stubs that predate structured output
            return await self._run("chat", system, user, json_mode=json_mode, max_tokens=max_tokens)
        return await self._run("chat", system, user, json_mode=json_mode, max_tokens=max_tokens,
                               json_schema=json_schema)

    async def vision(self, prompt: str, image_bytes: bytes, *, max_tokens: int = 1000) -> LLMResult:
        return await self._run("vision", prompt, image_bytes, max_tokens=max_tokens)


def _mode_config(mode: str) -> dict:
    """
    Engine settings of a workload: the configuration administrators set in the web app (seeded from .env at the
    first start), see shared/runtime_config.py. Keys always come from .env.
    """
    if mode not in WORKLOADS:
        raise ValueError("Mode must be 'doc', 'phi' or 'extract'")
    from shared import runtime_config
    cfg = runtime_config.get(mode)
    primary, fallback = cfg.get("primary") or {}, cfg.get("fallback") or {}

    def env_value(prefix: str, provider: Optional[str], name: str):
        # A workload's own *_API_KEY / *_API_VERSION apply only to the provider it was configured with in .env.
        env_provider = (getattr(settings, f"{mode}_llm_{prefix}provider", None) or "").lower()
        return getattr(settings, f"{mode}_llm_{prefix}{name}", None) if provider and provider == env_provider else None

    return {
        "provider": primary.get("provider"), "url": primary.get("url"), "model": primary.get("model"),
        "api_key": env_value("", primary.get("provider"), "api_key"),
        "api_version": env_value("", primary.get("provider"), "api_version"),
        "fallback_provider": fallback.get("provider"), "fallback_url": fallback.get("url"),
        "fallback_model": fallback.get("model"),
        "fallback_api_key": env_value("fallback_", fallback.get("provider"), "api_key"),
        "reasoning_effort": cfg.get("reasoning_effort"),
    }


def get_llm_client(mode: str) -> FailoverLLM:
    """Returns the failover-wrapped engine for 'doc' (vision), 'phi' (text) or 'extract' work."""
    cfg = _mode_config(mode)
    effort = cfg.get("reasoning_effort") if mode == "extract" else None
    primary = build_provider(cfg["provider"], cfg["url"], cfg["model"], cfg["api_key"], cfg["api_version"], effort)
    fallback = None
    if cfg["fallback_provider"] and cfg["fallback_model"]:
        fallback = build_provider(cfg["fallback_provider"], cfg["fallback_url"], cfg["fallback_model"],
                                  cfg["fallback_api_key"], effort=effort)
    return FailoverLLM(primary, fallback)


# ---------------------------------------------------------------------------
# Data extraction engines (per-request model choice)
# ---------------------------------------------------------------------------

def _local_ollama_url() -> Optional[str]:
    for mode in WORKLOADS:
        for prefix in ("", "fallback_"):
            if (getattr(settings, f"{mode}_llm_{prefix}provider", None) or "").lower() == "ollama":
                return getattr(settings, f"{mode}_llm_{prefix}url", None) or DEFAULT_URLS["ollama"]
    return None


def available_extract_models() -> dict:
    """
    "provider:model" -> ModelInfo for every engine a request may choose: catalog models of providers that
    have credentials, local models when an Ollama server is configured, and the configured engines.
    """
    from shared import model_catalog
    out: dict = {}
    for provider in ("anthropic", "openai", "gemini"):
        if resolve_api_key(provider):
            for info in model_catalog.selectable_models(provider):
                out[info.key] = info
    if _local_ollama_url():
        for info in model_catalog.models("ollama"):
            out[info.key] = info
    cfg = _mode_config("extract")
    for prefix in ("", "fallback_"):
        provider, model = cfg[f"{prefix}provider"], cfg[f"{prefix}model"]
        if provider and model:
            out.setdefault(f"{provider}:{model}", model_catalog.find_model(provider, model))
    return out


def build_extract_engines(model_key: Optional[str] = None) -> tuple[BaseProvider, Optional[BaseProvider]]:
    """
    Primary and fallback engines for one extraction job. Without `model_key` the configured EXTRACT_LLM_*
    engine is used. A chosen model keeps the configured fallback unless it is the same engine.
    """
    cfg = _mode_config("extract")
    effort = cfg.get("reasoning_effort") or settings.extract_reasoning_effort
    configured = build_provider(cfg["provider"], cfg["url"], cfg["model"], cfg["api_key"], cfg["api_version"], effort)
    fallback = None
    if cfg["fallback_provider"] and cfg["fallback_model"]:
        fallback = build_provider(cfg["fallback_provider"], cfg["fallback_url"], cfg["fallback_model"],
                                  cfg["fallback_api_key"], effort=effort)
    if not model_key or model_key == f"{configured.name}:{configured.model}":
        primary = configured
    elif fallback is not None and model_key == f"{fallback.name}:{fallback.model}":
        primary, fallback = fallback, configured
    else:
        provider, _, model = model_key.partition(":")
        url = cfg["url"] if provider == cfg["provider"] else (_local_ollama_url() if provider == "ollama" else None)
        primary = build_provider(provider, url, model, None, None, effort)
        if fallback is None or fallback.ident == primary.ident:
            fallback = configured
    if fallback is not None and fallback.ident == primary.ident:
        fallback = None
    return primary, fallback


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
