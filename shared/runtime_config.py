"""
Engine configuration per service, editable by administrators at run time.

    phi             PHI detection engine (primary and fallback)
    doc             Document parsing: figure and chart descriptions (vision engine)
    extract         Data extraction engine (primary, fallback, reasoning effort)
    extract_toc     Data extraction, contents (index) page detection: mode rules | llm | hybrid, and its engine
    extract_embed   Data extraction embeddings for retrieval

Lifecycle: at the first start of the API, every service is seeded from .env (`seed_from_env`). From then on the
database row is the source of truth; administrators change it in the web app (Administration, AI Engines) and can
reset it to the .env values. API keys are never stored here: they stay in .env and are resolved per provider.

Reads are cached per process for CACHE_SECONDS, so the API and the worker pick up a change within seconds.
With RUNTIME_CONFIG_SOURCE=env (or when the database is unreachable) the .env values are used.
"""
import logging
import time
from copy import deepcopy
from typing import Any, Optional

from shared.config import settings

log = logging.getLogger("shared.runtime_config")

CACHE_SECONDS = 5.0
WORKLOADS = ("phi", "doc", "extract", "extract_toc", "extract_embed")
LLM_PROVIDERS = ("openai", "azure_openai", "openai_compatible", "anthropic", "gemini", "ollama")
TOC_MODES = ("rules", "llm", "hybrid")
EMBED_PROVIDERS = ("ollama", "openai", "none")
EFFORTS = ("low", "medium", "high")
TITLES = {
    "phi": ("PHI detection", "Finds protected health information in every document submitted for redaction."),
    "doc": ("Document parsing: figure descriptions", "Vision model that describes figures and charts during structured parsing."),
    "extract": ("Data extraction", "Reasoning model that reads documents and fills the requested JSON fields."),
    "extract_toc": ("Data extraction: index page detection",
                    "How the table of contents is found (rules, LLM, or rules with an LLM fallback) and which model reads it."),
    "extract_embed": ("Data extraction: embeddings", "Model that embeds document chunks for semantic search."),
}

_cache: dict[str, tuple[float, Optional[dict]]] = {}


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------
# .env defaults
# ---------------------------------------------------------------------------

def _engine(provider, model, url) -> Optional[dict]:
    if not provider or not model:
        return None
    return {"provider": str(provider).strip().lower(), "model": str(model).strip(), "url": (url or None)}


def env_defaults(workload: str) -> dict:
    if workload in ("phi", "doc", "extract"):
        g = lambda name: getattr(settings, f"{workload}_llm_{name}", None)  # noqa: E731
        value: dict[str, Any] = {
            "primary": _engine(g("provider"), g("model"), g("url")),
            "fallback": _engine(g("fallback_provider"), g("fallback_model"), g("fallback_url")),
        }
        if workload == "extract":
            value["reasoning_effort"] = settings.extract_reasoning_effort
        return value
    if workload == "extract_toc":
        mode = (settings.extract_toc_mode or "hybrid").strip().lower()
        if not settings.extract_toc_use_llm:
            mode = "rules"
        return {"mode": mode if mode in TOC_MODES else "hybrid",
                "engine": _engine(settings.extract_toc_llm_provider, settings.extract_toc_llm_model,
                                  settings.extract_toc_llm_url)}
    if workload == "extract_embed":
        return {"provider": (settings.extract_embed_provider or "none").strip().lower(),
                "model": settings.extract_embed_model, "url": settings.extract_embed_url}
    raise ConfigError(f"Unknown service '{workload}'. Choose one of: {', '.join(WORKLOADS)}.")


# ---------------------------------------------------------------------------
# Database overrides
# ---------------------------------------------------------------------------

def _use_db() -> bool:
    return (settings.runtime_config_source or "db").strip().lower() == "db"


def _row(workload: str):
    from shared.database import SessionLocal
    from shared.models import PlatformSetting
    db = SessionLocal()
    try:
        return db.query(PlatformSetting).filter(PlatformSetting.key == workload).first()
    finally:
        db.close()


def _stored(workload: str) -> Optional[dict]:
    if not _use_db():
        return None
    hit = _cache.get(workload)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return deepcopy(hit[1])
    try:
        row = _row(workload)
        value = dict(row.value) if row is not None else None
    except Exception as exc:  # the platform keeps working on the .env values
        log.warning("runtime config: database unavailable (%s); using .env values", str(exc).splitlines()[0][:160])
        value = None
    _cache[workload] = (time.monotonic(), value)
    return deepcopy(value)


def clear_cache() -> None:
    _cache.clear()


def get(workload: str) -> dict:
    """Effective configuration of a service: the stored value, or the .env defaults."""
    defaults = env_defaults(workload)
    stored = _stored(workload)
    if not stored:
        return defaults
    return {**defaults, **{k: v for k, v in stored.items() if k in defaults}}


def describe(workload: str) -> dict:
    """
    Effective value with its origin, for the administration API. Read straight from the database, not from the
    per-process cache: the API runs several processes, and a change saved by one must show at once in the others.
    """
    defaults = env_defaults(workload)
    value = defaults
    meta = {"source": "env", "updated_by": None, "updated_at": None}
    if _use_db():
        try:
            row = _row(workload)
            if row is not None:
                value = {**defaults, **{k: v for k, v in dict(row.value).items() if k in defaults}}
                meta = {"source": row.source, "updated_by": row.updated_by,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None}
        except Exception:
            meta["source"] = "env (database unavailable)"
    title, description = TITLES[workload]
    return {"key": workload, "title": title, "description": description, "value": value,
            "env_defaults": env_defaults(workload), **meta}


# ---------------------------------------------------------------------------
# Validation and writes
# ---------------------------------------------------------------------------

def _validate_engine(engine: Any, label: str, required: bool, selectable: dict) -> Optional[dict]:
    """`selectable` maps provider -> set of allowed model ids, or None when any model name is accepted."""
    if engine in (None, {}, ""):
        if required:
            raise ConfigError(f"{label}: choose a provider and a model.")
        return None
    if not isinstance(engine, dict):
        raise ConfigError(f"{label}: must be an object with provider and model.")
    provider = str(engine.get("provider") or "").strip().lower()
    model = str(engine.get("model") or "").strip()
    url = (str(engine.get("url")).strip() or None) if engine.get("url") else None
    if not provider and not model and not required:
        return None
    if provider not in LLM_PROVIDERS:
        raise ConfigError(f"{label}: provider must be one of {', '.join(LLM_PROVIDERS)}.")
    if not model or len(model) > 200:
        raise ConfigError(f"{label}: model is required (up to 200 characters).")
    allowed = selectable.get(provider)
    if allowed is not None and model not in allowed:
        raise ConfigError(f"{label}: '{model}' is not offered for {provider}. Choose one of: {', '.join(sorted(allowed))}.")
    if provider in ("azure_openai", "openai_compatible") and not url:
        raise ConfigError(f"{label}: a URL is required for {provider}.")
    from shared.llm import resolve_api_key
    if provider not in ("ollama", "openai_compatible") and not resolve_api_key(provider):
        key = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}.get(provider, "an API key")
        raise ConfigError(f"{label}: no credentials for {provider}. Set {key} in .env and restart the stack.")
    if url and not url.startswith(("http://", "https://")):
        raise ConfigError(f"{label}: URL must start with http:// or https://.")
    return {"provider": provider, "model": model, "url": url}


def validate(workload: str, value: Any, selectable: dict) -> dict:
    if not isinstance(value, dict):
        raise ConfigError("The configuration must be a JSON object.")
    if workload in ("phi", "doc", "extract"):
        out: dict[str, Any] = {
            "primary": _validate_engine(value.get("primary"), "Primary engine", True, selectable),
            "fallback": _validate_engine(value.get("fallback"), "Fallback engine", False, selectable),
        }
        if out["fallback"] and out["fallback"] == out["primary"]:
            raise ConfigError("The fallback engine must differ from the primary engine.")
        if workload == "extract":
            effort = str(value.get("reasoning_effort") or "medium").lower()
            if effort not in EFFORTS:
                raise ConfigError(f"reasoning_effort must be one of {', '.join(EFFORTS)}.")
            out["reasoning_effort"] = effort
        return out
    if workload == "extract_toc":
        mode = str(value.get("mode") or "").lower()
        if mode not in TOC_MODES:
            raise ConfigError(f"mode must be one of {', '.join(TOC_MODES)}.")
        engine = _validate_engine(value.get("engine"), "Index detection engine", False, selectable)
        return {"mode": mode, "engine": engine}  # kept in rules mode too, so switching back restores it
    if workload == "extract_embed":
        provider = str(value.get("provider") or "").lower()
        if provider not in EMBED_PROVIDERS:
            raise ConfigError(f"provider must be one of {', '.join(EMBED_PROVIDERS)}.")
        model = str(value.get("model") or "").strip()
        if provider != "none" and not model:
            raise ConfigError("model is required unless embeddings are turned off.")
        url = (str(value.get("url")).strip() or None) if value.get("url") else None
        if provider == "openai":
            from shared.llm import resolve_api_key
            if not resolve_api_key("openai"):
                raise ConfigError("No OpenAI credentials. Set OPENAI_API_KEY in .env and restart the stack.")
        return {"provider": provider, "model": model or settings.extract_embed_model, "url": url}
    raise ConfigError(f"Unknown service '{workload}'.")


def save(workload: str, value: dict, actor: Optional[str], source: str = "admin") -> None:
    from shared.database import SessionLocal
    from shared.models import PlatformSetting
    db = SessionLocal()
    try:
        row = db.query(PlatformSetting).filter(PlatformSetting.key == workload).first()
        if row is None:
            db.add(PlatformSetting(key=workload, value=value, source=source, updated_by=actor))
        else:
            row.value, row.source, row.updated_by = value, source, actor
        db.commit()
    finally:
        db.close()
    _cache.pop(workload, None)


def reset(workload: str, actor: Optional[str]) -> None:
    """Back to the .env values (stored again as the env seed)."""
    save(workload, env_defaults(workload), actor, source="env")


def seed_from_env() -> list[str]:
    """First start: store the .env values for every service that has no row yet. Returns the seeded keys."""
    from shared.database import SessionLocal
    from shared.models import PlatformSetting
    seeded = []
    db = SessionLocal()
    try:
        existing = {k for (k,) in db.query(PlatformSetting.key).all()}
        for workload in WORKLOADS:
            if workload not in existing:
                db.add(PlatformSetting(key=workload, value=env_defaults(workload), source="env", updated_by="startup (.env)"))
                seeded.append(workload)
        db.commit()
    finally:
        db.close()
    return seeded
