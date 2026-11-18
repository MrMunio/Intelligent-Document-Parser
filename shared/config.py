from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ---- Core infrastructure -------------------------------------------------
    database_url: str = "postgresql://idp_user:idp_password@localhost:5432/idp_db"
    redis_host: str = "localhost"
    redis_port: int = 6379
    log_level: str = "INFO"  # INFO or DEBUG
    data_dir: str = "/app/data"

    # ---- Authentication ------------------------------------------------------
    jwt_secret: str = "change-me-in-env"
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 30
    jwt_refresh_ttl_days: int = 7
    cookie_secure: bool = False  # set True when served over HTTPS
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    super_admin_email: str = "admin@idp.local"
    super_admin_password: str = ""
    super_admin_name: str = "Platform Administrator"
    cors_origins: str = ""  # comma separated; empty = same-origin only (nginx proxy)

    # ---- Request limits ------------------------------------------------------
    max_upload_mb: int = 50
    max_text_chars: int = 500_000
    sync_timeout_seconds: int = 900

    # ---- Storage -------------------------------------------------------------
    storage_mode: str = "LOCAL"
    s3_bucket_name: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    file_retention_days: int = 7

    # ---- Document parsing LLM (vision: image and chart descriptions) ----------
    # Providers: openai | azure_openai | openai_compatible | anthropic | gemini | ollama
    doc_llm_provider: str = "ollama"
    doc_llm_url: str = "http://ollama:11434"
    doc_llm_model: str = "qwen3.5:4b"
    doc_llm_api_key: str | None = None
    doc_llm_api_version: str | None = None  # azure_openai only
    doc_llm_enable_summary: bool = True
    doc_llm_fallback_provider: str | None = None
    doc_llm_fallback_url: str | None = None
    doc_llm_fallback_model: str | None = None
    doc_llm_fallback_api_key: str | None = None
    doc_llm_concurrency: int = 4

    # ---- PHI redaction LLM (text) ---------------------------------------------
    phi_llm_provider: str = "ollama"
    phi_llm_url: str = "http://ollama:11434"
    phi_llm_model: str = "qwen3.5:4b"
    phi_llm_api_key: str | None = None
    phi_llm_api_version: str | None = None  # azure_openai only
    phi_llm_fallback_provider: str | None = None
    phi_llm_fallback_url: str | None = None
    phi_llm_fallback_model: str | None = None
    phi_llm_fallback_api_key: str | None = None
    phi_llm_concurrency: int = 16  # per job; providers queue anything above their own limit
    phi_chunk_words: int = 4000  # safety ceiling per call: a page is one call unless it exceeds this many words

    # ---- Provider credentials shared by every workload ------------------------
    # A workload uses its own *_LLM_API_KEY first, then the key for its provider below, then the key of
    # another workload that is configured for the same provider.
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    model_catalog_path: str | None = None  # default: shared/model_catalog.json
    # Where engine settings come from: db (seeded from .env at first start, then edited in the web app) | env
    runtime_config_source: str = "db"

    # ---- Data extraction (document to JSON) -----------------------------------
    extract_llm_provider: str = "anthropic"
    extract_llm_url: str | None = None
    extract_llm_model: str = "claude-sonnet-5-5"
    extract_llm_api_key: str | None = None
    extract_llm_api_version: str | None = None  # azure_openai only
    extract_llm_fallback_provider: str | None = None
    extract_llm_fallback_url: str | None = None
    extract_llm_fallback_model: str | None = None
    extract_llm_fallback_api_key: str | None = None
    extract_reasoning_effort: str | None = "medium"
    # Agent sessions are compacted and restarted when the context reaches this share of the usable window.
    extract_context_threshold: float = 0.70
    # Upper bound on the usable window in tokens, whatever the model supports (0 = the full model window).
    # Keeps a long agent run from paying for a million-token prompt on every turn.
    extract_context_cap_tokens: int = 200_000
    extract_spec_min_fields: int = 1
    extract_spec_max_fields: int = 300
    extract_spec_max_depth: int = 6
    extract_batch_chunk_tokens: int = 60_000  # largest document part per call in the batch approach
    extract_batch_overlap_tokens: int = 500
    extract_agent_max_turns: int = 80
    extract_agent_max_sessions: int = 8
    extract_agent_max_tool_chars: int = 12_000  # tool results longer than this are cut, with a pointer to continue
    extract_agent_max_output_tokens: int = 16_000
    extract_toc_scan_pages: int = 10
    extract_toc_use_llm: bool = True  # legacy switch: false means EXTRACT_TOC_MODE=rules
    # How the contents (index) page is found: rules (regex only) | llm | hybrid (rules first, LLM when they fail)
    extract_toc_mode: str = "hybrid"
    # Engine for contents detection; empty provider = the data extraction engine
    extract_toc_llm_provider: str | None = None
    extract_toc_llm_url: str | None = None
    extract_toc_llm_model: str | None = None
    extract_rag_chunk_tokens: int = 450
    extract_rag_overlap_tokens: int = 60
    # Embeddings for agentic retrieval: ollama (local, default) | openai | none (keyword ranking only)
    extract_embed_provider: str = "ollama"
    extract_embed_url: str | None = "http://ollama:11434"
    extract_embed_model: str = "qwen3-embedding:0.6b"
    extract_embed_api_key: str | None = None
    extract_embed_batch: int = 32

    # ---- Shared LLM behaviour -------------------------------------------------
    llm_timeout_seconds: float = 180.0
    llm_max_retries: int = 2
    llm_reasoning_effort: str | None = "low"  # OpenAI reasoning models / Anthropic effort
    llm_failover_cooldown_seconds: int = 300
    ollama_num_ctx: int = 8192
    ollama_keep_alive: str = "30m"

    # ---- OCR (PaddleOCR 3.7 / PP-OCRv6) ---------------------------------------
    ocr_det_model: str = "PP-OCRv6_small_det"
    ocr_rec_model: str = "PP-OCRv6_small_rec"
    ocr_device: str | None = None  # e.g. "gpu:0" or "cpu"; None = auto
    ocr_batch_pages: int = 1  # measured: batching pages gives no gain, GPU recognition is the bottleneck
    ocr_rec_batch_size: int = 16
    phi_render_dpi: int = 150
    pdf_output_dpi: int = 200  # resolution of redacted PDF pages
    pdf_output_jpeg_quality: int = 82
    render_workers: int = 2  # page rasterisation processes, overlapped with OCR
    preload_models: str = "ocr"  # ocr | all | none
    worker_concurrency: int = 3  # jobs processed at the same time by one worker process
    paddle_enable_hpi: bool = False
    paddle_use_table: bool = True
    paddle_use_formula: bool = False
    paddle_use_chart: bool = False

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
