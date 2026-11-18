"""
Administration of the AI engines used by each service. Values are seeded from .env at the first start and stored in
the database; changes apply to new jobs within a few seconds. API keys are never accepted or returned here.
"""
import asyncio
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request

from api.deps import Principal, audit, require_admin
from shared import model_catalog, runtime_config
from shared.config import settings
from shared.database import get_db
from shared.llm import DEFAULT_URLS, build_provider, resolve_api_key
from shared.runtime_config import ConfigError, WORKLOADS

router = APIRouter(prefix="/admin/llm-config", tags=["Administration"])

PROVIDER_LABELS = {
    "anthropic": "Anthropic Claude", "openai": "OpenAI", "gemini": "Google Gemini", "ollama": "Local (Ollama)",
    "azure_openai": "Azure OpenAI", "openai_compatible": "OpenAI-compatible server",
}
FREE_TEXT_PROVIDERS = {"ollama", "azure_openai", "openai_compatible"}  # local tags and deployment names


def _model_row(info) -> dict:
    return {"id": info.id, "display_name": info.display_name, "context_window": info.context_window,
            "tier": info.tier, "pricing": info.prices_on(), "reasoning": info.reasoning}


def _selectable() -> dict:
    """provider -> allowed model ids (None = any name, for local models and deployments)."""
    out: dict[str, Any] = {p: None for p in FREE_TEXT_PROVIDERS}
    for provider in ("anthropic", "openai", "gemini"):
        allowed = {m.id for m in model_catalog.selectable_models(provider)}
        # the configured engines stay valid even when an account list narrows the choice
        for workload in ("phi", "doc", "extract"):
            for engine in (runtime_config.env_defaults(workload).get("primary"), runtime_config.env_defaults(workload).get("fallback")):
                if engine and engine.get("provider") == provider:
                    allowed.add(engine["model"])
        out[provider] = allowed
    return out


async def _ollama_installed() -> Optional[set]:
    """Names of the models downloaded on the local Ollama server, or None when the server cannot be reached."""
    url = (runtime_config.get("extract_embed").get("url") or DEFAULT_URLS["ollama"]).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            return {m.get("name", "") for m in resp.json().get("models", [])}
    except Exception:
        return None


def _is_installed(model_id: str, installed: Optional[set]) -> bool:
    """Catalog ids may omit the tag ('nomic-embed-text'); Ollama lists it as 'nomic-embed-text:latest'."""
    return installed is None or model_id in installed or (":" not in model_id and f"{model_id}:latest" in installed)


def _providers(installed: Optional[set] = None) -> list[dict]:
    rows = []
    for name in ("anthropic", "openai", "gemini", "ollama", "azure_openai", "openai_compatible"):
        models = [_model_row(m) for m in model_catalog.selectable_models(name)] if name in ("anthropic", "openai", "gemini", "ollama") else []
        if name == "ollama":  # a local model can only be chosen once it is downloaded on the server
            models = [m for m in models if _is_installed(m["id"], installed)]
        rows.append({"name": name, "label": PROVIDER_LABELS[name], "key_configured": bool(resolve_api_key(name)) or name in ("ollama", "openai_compatible"),
                     "needs_key": name not in ("ollama", "openai_compatible"), "free_text_model": name in FREE_TEXT_PROVIDERS,
                     "default_url": DEFAULT_URLS.get(name), "models": models})
    return rows


def _embedding_models(installed: Optional[set] = None) -> dict:
    cat = model_catalog.catalog().get("embedding_models", {})
    return {provider: [{"id": e["id"], "dimensions": e.get("dimensions"), "size_mb": e.get("size_mb"), "notes": e.get("notes", "")}
                       for e in items if provider != "ollama" or _is_installed(e["id"], installed)]
            for provider, items in cat.items()}


def _check(workload: str):
    if workload not in WORKLOADS:
        raise HTTPException(status_code=404, detail=f"Unknown service '{workload}'. Choose one of: {', '.join(WORKLOADS)}.")


@router.get("", summary="Engine configuration of every service, with the choices available")
async def get_config(_: Principal = Depends(require_admin)):
    installed = await _ollama_installed()
    return {
        "services": [runtime_config.describe(w) for w in WORKLOADS],
        "providers": _providers(installed),
        "embedding_models": _embedding_models(installed),
        "ollama_reachable": installed is not None,
        "toc_modes": [
            {"value": "rules", "label": "Rules", "description": "Regular expressions only: contents headings, lines ending in page numbers, dot leaders. No model cost."},
            {"value": "llm", "label": "LLM", "description": "The language model reads the first pages, finds the contents and structures it."},
            {"value": "hybrid", "label": "Hybrid", "description": "Rules first; the language model only when the rules find nothing. The model also structures the contents found."},
        ],
        "efforts": list(runtime_config.EFFORTS),
        "storage": "database" if settings.runtime_config_source == "db" else ".env only (RUNTIME_CONFIG_SOURCE=env: changes cannot be saved)",
    }


@router.put("/{workload}", summary="Change the engine configuration of a service")
def update_config(workload: str, request: Request, value: dict = Body(...), principal: Principal = Depends(require_admin),
                  db=Depends(get_db)):
    _check(workload)
    if settings.runtime_config_source != "db":
        raise HTTPException(status_code=409, detail="Engine settings are read from .env (RUNTIME_CONFIG_SOURCE=env).")
    try:
        clean = runtime_config.validate(workload, value, _selectable())
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    before = runtime_config.get(workload)
    runtime_config.save(workload, clean, principal.user.email)
    audit(db, "settings.engine_updated", actor=principal.user, target_type="service", target_id=workload,
          detail={"before": before, "after": clean}, request=request)
    return runtime_config.describe(workload)


@router.post("/{workload}/reset", summary="Restore the .env values for a service")
def reset_config(workload: str, request: Request, principal: Principal = Depends(require_admin), db=Depends(get_db)):
    _check(workload)
    if settings.runtime_config_source != "db":
        raise HTTPException(status_code=409, detail="Engine settings are read from .env (RUNTIME_CONFIG_SOURCE=env).")
    before = runtime_config.get(workload)
    runtime_config.reset(workload, principal.user.email)
    audit(db, "settings.engine_reset", actor=principal.user, target_type="service", target_id=workload,
          detail={"before": before, "after": runtime_config.get(workload)}, request=request)
    return runtime_config.describe(workload)


async def _probe(engine: dict) -> dict:
    provider = build_provider(engine["provider"], engine.get("url"), engine["model"])
    try:
        health = await asyncio.wait_for(provider.health(), timeout=30)
    except Exception as exc:
        health = {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        await provider.aclose()
    return {"engine": f"{engine['provider']}:{engine['model']}", **health}


@router.post("/{workload}/test", summary="Check that the configured engines of a service respond")
async def test_config(workload: str, _: Principal = Depends(require_admin)):
    _check(workload)
    cfg = runtime_config.get(workload)
    results = []
    if workload in ("phi", "doc", "extract"):
        for role in ("primary", "fallback"):
            if cfg.get(role):
                results.append({"role": role, **await _probe(cfg[role])})
    elif workload == "extract_toc":
        if cfg["mode"] == "rules":
            results.append({"role": "rules", "engine": "none", "ok": True, "detail": "Rules mode uses no language model."})
        elif cfg.get("engine"):
            results.append({"role": "index detection", **await _probe(cfg["engine"])})
        else:
            results.append({"role": "index detection", "engine": "extraction engine", "ok": True,
                            "detail": "Uses the data extraction engine; test that service."})
    else:
        results.append({"role": "embeddings", **await _probe_embedding(cfg)})
    return {"service": workload, "results": results}


async def _probe_embedding(cfg: dict) -> dict:
    """Embeds one short text with the configured embedding engine (the API cannot import worker code)."""
    import httpx
    provider, model = cfg.get("provider"), cfg.get("model")
    if provider == "none":
        return {"engine": "none", "ok": True, "detail": "Embeddings are off: retrieval uses keyword ranking only."}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            if provider == "ollama":
                url = (cfg.get("url") or DEFAULT_URLS["ollama"]).rstrip("/")
                resp = await client.post(f"{url}/api/embed", json={"model": model, "input": ["connection test"]})
                resp.raise_for_status()
                dims = len((resp.json().get("embeddings") or [[]])[0])
            else:
                url = (cfg.get("url") or DEFAULT_URLS["openai"]).rstrip("/")
                resp = await client.post(f"{url}/embeddings", json={"model": model, "input": ["connection test"]},
                                         headers={"Authorization": f"Bearer {resolve_api_key('openai')}"})
                resp.raise_for_status()
                dims = len(resp.json()["data"][0]["embedding"])
        return {"engine": f"{provider}:{model}", "ok": dims > 0, "detail": f"Embedding returned {dims} dimensions"}
    except httpx.HTTPStatusError as exc:
        return {"engine": f"{provider}:{model}", "ok": False,
                "detail": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"engine": f"{provider}:{model}", "ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
