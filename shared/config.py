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
    phi_chunk_words: int = 120  # smaller segments: better recall, and they run in parallel

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
