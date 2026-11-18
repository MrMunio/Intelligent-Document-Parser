# IDP Platform architecture

This document describes how the platform is built: the runtime services, the back end, the web app,
the processing worker, the PHI redaction, document parsing and data extraction pipelines, the OCR models
and the LLM engines. Settings named in `CAPITALS` are environment variables read by `shared/config.py`; defaults
are in `.env.example`.

Contents

1. [System overview](#1-system-overview)
2. [Runtime services](#2-runtime-services)
3. [Request and job lifecycle](#3-request-and-job-lifecycle)
4. [Back end (API)](#4-back-end-api)
5. [Security model](#5-security-model)
6. [Data model](#6-data-model)
7. [Processing worker](#7-processing-worker)
8. [PHI redaction service](#8-phi-redaction-service)
9. [Document parsing and OCR services](#9-document-parsing-and-ocr-services)
9a. [Data extraction service](#9a-data-extraction-service)
10. [OCR models](#10-ocr-models)
11. [LLM engines](#11-llm-engines)
12. [Front end (web app)](#12-front-end-web-app)
13. [Configuration](#13-configuration)
14. [Performance design](#14-performance-design)
15. [Testing](#15-testing)
16. [Deployment](#16-deployment)
17. [Extension points](#17-extension-points)

---

## 1. System overview

```
                      +-------------------------------------------+
 Browser  ----------> | web   nginx: React SPA, proxies /api      |
                      +---------------------+---------------------+
                                            |
 External systems (X-API-Key) ------------->|
                                            v
                      +-------------------------------------------+
                      | api   FastAPI                             |
                      |  auth, RBAC, validation, job records,     |
                      |  file delivery, audit log, migrations     |
                      +----+--------------------------+-----------+
                           |                          |
                   SQL (pg8000)                 enqueue (RQ)
                           v                          v
                  +----------------+         +----------------+
                  | postgres 15    |         | valkey 8       |
                  | users, keys,   |         | job queues,    |
                  | jobs, audit    |         | login lockout, |
                  +--------^-------+         | heartbeat      |
                           |                 +-------+--------+
                           |  status, progress       |  dequeue
                           |                         v
                  +--------+-----------------------------------+
                  | worker (GPU)                               |
                  |  WORKER_CONCURRENCY job slots (threads)     |
                  |  PaddleOCR PP-OCRv6, PP-StructureV3         |
                  |  PDFium, reportlab, Pillow, lxml            |
                  +--------+-----------------------------------+
                           |  HTTPS / HTTP
                           v
                  +--------------------------------------------+
                  | LLM engines: primary (OpenAI, Azure,        |
                  | Claude, Gemini, OpenAI-compatible) with     |
                  | automatic fallback (local Ollama, Qwen3.5)  |
                  +--------------------------------------------+

 Shared volume idp_data: uploads (deleted when a job ends) and outputs (FILE_RETENTION_DAYS)
```

Three business services are exposed:

| Service | Code name | Job type | Purpose |
|---|---|---|---|
| PHI redaction | `phi` | `PHI` | Detect PHI/PII and custom entities; return the redacted file in the same format plus a metadata JSON |
| Document parsing | `parse` | `PARSE` (structured), `OCR` (plain text) | Layout-aware markdown and JSON, or text with word coordinates |
| Data extraction | `extract` | `EXTRACT` | JSON that follows a spec sent with each request, from documents of any length (batch or agentic approach) |

The API is the product; the web app is one client of it and is optional.

## 2. Runtime services

Defined in `docker-compose.yml` on the `idp_network` bridge network.

| Service | Image / build | Role | Port | Volumes |
|---|---|---|---|---|
| `postgres` | `postgres:15` | Relational store, schema `idp_schema` | internal | `postgres_data` |
| `redis` | `valkey/valkey:8-alpine` | RQ queues, login lockout counters, worker heartbeat (service name kept for compatibility) | internal | `valkey_data` |
| `api` | `Dockerfile.api` (python:3.12-slim, non-root) | REST API; runs `api/prestart.py` (wait for DB, migrate, seed super admin) then Uvicorn | `API_PORT` (8000) | `idp_data` |
| `worker` | `Dockerfile.worker` (PaddlePaddle 3.3 GPU base) | Job execution, OCR, redaction, parsing | none | `idp_data`, model caches |
| `web` | `frontend/Dockerfile` (node build, nginx serve) | React SPA, `/api` reverse proxy with 960 s timeouts | `WEB_PORT` (3000) | none |
| `ollama` | `ollama/ollama` | Local LLM server (profile `ollama`) | internal | `ollama_models` |
| `ollama-init` | `ollama/ollama` | One-shot pull of `OLLAMA_PULL_MODELS` | none | `ollama_models` |

Worker container tuning: `shm_size: 2gb` for the render pool, and glibc `MALLOC_ARENA_MAX=2`,
`MALLOC_MMAP_THRESHOLD_` and `MALLOC_TRIM_THRESHOLD_` at 1 MiB, which keep resident memory flat
(about 1.3 GB) across long runs.

## 3. Request and job lifecycle

Every processing request becomes a job, whether it is synchronous or asynchronous.

```
client              api                          postgres         valkey          worker
  | POST /phi/jobs   |                              |               |               |
  |----------------->| authenticate, authorise      |               |               |
  |                  | normalise options            |               |               |
  |                  | stream upload to disk        |               |               |
  |                  |  (MAX_UPLOAD_MB cap)         |               |               |
  |                  | INSERT job (PENDING) ------->|               |               |
  |                  | enqueue process_job(id) ---------------------->|              |
  |<-- 202 {job_id} -|                              |               |-- dequeue --->|
  |                  |                              |<-- PROCESSING, progress ------|
  | GET /jobs/{id}   |                              |               |  run service  |
  |----------------->| read status + progress ----->|               |               |
  |                  |                              |<-- COMPLETED, stats, result --|
  | GET /jobs/{id}/file, /metadata, /markdown       |               |  delete upload|
```

* **Synchronous endpoints** (`POST /phi/redact`, `/phi/redact-text`, `/parse/sync`, `/extract/sync`,
  `/extract/text`) create the same job
  and then poll the database until it finishes or `SYNC_TIMEOUT_SECONDS` passes
  (`api/job_service.py: wait_for_job`). `response_format=file` streams the output file directly.
* **Progress** (`stage`, `percent`, `detail`) is written to the job row by `worker/tasks.py:
  ProgressWriter`, so it survives page reloads and is visible through the API.
* **Failure**: the worker records a public error message (last line only, no stack trace) and status
  `FAILED`. PHI jobs fail closed: if any part of the document could not be checked, no output is
  produced.
* **Retention**: originals are deleted as soon as the job ends; outputs are removed after
  `FILE_RETENTION_DAYS` (`worker/storage.py`).
* **Queues**: `priority` and `default`. Synchronous requests go to `priority`.

## 4. Back end (API)

FastAPI application in `api/`, sharing models and settings with the worker through `shared/`. The contract for
external integrators is [API_GUIDE.md](API_GUIDE.md).

### Module map

| Module | Responsibility |
|---|---|
| `api/main.py` | App factory; mounts routers under `/api/v1`; `/health` and `/ready`; security headers; CORS; OpenAPI at `/api/docs` and `/api/openapi.json` |
| `api/prestart.py` | Start-up: wait for PostgreSQL, run Alembic migrations, seed the first super admin, refuse to start if `JWT_SECRET` is shorter than 32 characters |
| `api/deps.py` | `Principal` (user or API key), `get_principal`, `require_service`, `require_user_session`, `require_admin`, `audit()` |
| `api/schemas.py` | Pydantic request and response models (custom e-mail validator that accepts `.local` domains) |
| `api/job_service.py` | `save_upload` (streamed, size-capped), `create_job`, `wait_for_job`, `job_out`, file download helpers |
| `api/routers/auth.py` | `login`, `refresh`, `logout`, `me`, `change-password` |
| `api/routers/users.py` | User CRUD, role assignment, enable/disable, password reset |
| `api/routers/api_keys.py` | Create, list and revoke API keys |
| `api/routers/phi.py` | `entity-types`, `jobs`, `redact`, `redact-text` |
| `api/routers/parse.py` | `jobs`, `sync` (`mode=structured` or `ocr`) |
| `api/routers/extract.py` | `options`, `validate-spec`, `jobs`, `sync`, `text`; the spec is validated before the upload is stored |
| `api/routers/jobs.py` | List, get, delete; download `file`, `metadata`, `markdown` |
| `api/routers/admin.py` | `/admin/system` (component health, engines, queue depth, 24 h stats), `/admin/audit-logs` |
| `api/routers/llm_config.py` | `/admin/llm-config`: engine per service (read, change, reset to .env, test) |

### Endpoint summary

| Method and path | Access |
|---|---|
| `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout` | Public (rate limited by lockout) |
| `GET /auth/me`, `POST /auth/change-password` | Signed-in user |
| `GET /phi/entity-types`, `POST /phi/jobs`, `/phi/redact`, `/phi/redact-text` | Service `phi` |
| `POST /parse/jobs`, `/parse/sync` | Service `parse` |
| `GET /extract/options`, `POST /extract/validate-spec`, `/extract/jobs`, `/extract/sync`, `/extract/text` | Service `extract` |
| `GET /jobs`, `/jobs/{id}`, `/jobs/{id}/file`, `/metadata`, `/markdown`; `DELETE /jobs/{id}` | Owner of the job, or an admin |
| `/api-keys` | Signed-in user (session only, not via API key) |
| `/users` | Admin (super admin for administrator accounts) |
| `/admin/system`, `/admin/audit-logs` | Admin |

### Shared library (`shared/`)

| Module | Responsibility |
|---|---|
| `config.py` | All settings (pydantic-settings), single source of configuration |
| `database.py` | SQLAlchemy engine; forces the `postgresql+pg8000` driver; sets `search_path` to `idp_schema` |
| `models.py` | ORM models; `ROLE_SERVICES`, `JOB_SERVICE`, `services_for_roles` |
| `security.py` | bcrypt hashing, password policy, JWT create and decode, API key generation and hashing |
| `queue.py` | Valkey connection and RQ queues |
| `llm.py` | LLM provider abstraction, tool calling, failover, health checks (section 11) |
| `model_catalog.py`, `model_catalog.json` | Context window, output limit, prices and capabilities of every known model; usable window and cost |
| `runtime_config.py` | Engine settings per service: .env defaults, database overrides edited by administrators, validation, seeding |
| `usage.py` | Token and cost report of a job (same shape for every service), priced from the model catalog |
| `extract_spec.py` | Extraction spec validation and normalisation, field flattening, type coercion, state merging, strict output schema |
| `formats.py` | File classification by extension and content, safe file names, media types, accept lists |
| `phi_catalog.py` | 16 built-in entity types and `normalize_phi_options` (validation of per-request options) |
| `logger.py` | Logging set-up |

### Database migrations

Alembic (`alembic.ini`, `migrations/`). `0001_platform_baseline.py` is idempotent and brings both
new and pre-existing databases to the current schema. `db_init/init_db.sql` only creates the schema
on first PostgreSQL start. Add new migrations under `migrations/versions/`; they run automatically at
API start.

## 5. Security model

**Roles and services**

| Role | Services | Administration |
|---|---|---|
| `super_admin` | `phi`, `parse`, `extract` | All users, including admins |
| `admin` | `phi`, `parse`, `extract` | Non-admin users; system status; audit log |
| `phi_redactor` | `phi` | none |
| `document_parser` | `parse` | none |
| `data_extractor` | `extract` | none |

A user may hold several roles. `services_for_roles` turns roles into allowed services, and every
route checks them on the server (`require_service`, `require_admin`). The last active super admin
cannot be removed or demoted.

**Sessions**

* Login with e-mail and password returns a JWT access token (`JWT_ACCESS_TTL_MINUTES`, 30) and sets an
  httpOnly, SameSite=Strict refresh cookie scoped to `/api/v1/auth` (`JWT_REFRESH_TTL_DAYS`, 7).
* The web app keeps the access token in memory only and refreshes it silently.
* Each user has a `token_version`. Changing roles, disabling the account, resetting or changing the
  password increments it, which invalidates all existing tokens immediately.
* `LOGIN_MAX_FAILURES` failures lock the account for `LOGIN_LOCKOUT_MINUTES` (counter in Valkey).
* New and reset accounts must change their temporary password at first sign-in.
* Password policy: at least 10 characters with a letter and a digit; bcrypt hashes.

**API keys**

* Format `idp_<random>`; shown once; stored as SHA-256 hash with a display prefix.
* Sent as `X-API-Key`. The key acts as its owner, limited to the intersection of the key's scopes and
  the owner's current roles, so removing a role from a user also removes it from their keys.
* API keys cannot manage keys, users or passwords (`require_user_session`).

**Other controls**

* Security headers on every response (`X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: no-referrer` and related).
* Uploads are streamed to disk with a size cap; file type is checked by content, not only extension.
* Every security-relevant action is written to `audit_logs` (actor, action, target, IP, detail).
* Secrets live only in `.env`; nothing is hard-coded.
* The LLM prompt treats document text as data (prompt-injection guard, rule 6 of `phi_prompt.txt`; the same rule
  in `extract_batch_prompt.txt` and `extract_agent_prompt.txt`). Extraction agent tools are read-only views of the
  uploaded document plus writes to the job's own result; they reach no network, file system or database.

## 6. Data model

Schema `idp_schema` in PostgreSQL.

| Table | Key columns |
|---|---|
| `users` | id, email (unique), full_name, password_hash, roles, is_active, must_change_password, token_version, last_login_at, created_by, created_at, updated_at |
| `api_keys` | id, user_id, name, prefix, key_hash, scopes, expires_at, revoked_at, last_used_at, created_at |
| `job_requests` | id, job_type, status, original_file_name, result_data, result_markdown, result_file_url, error_details, user_id, api_key_id, source (web/api), input_format, input_path, file_size, options, progress, stats, result_file_name, result_media_type, created_at, updated_at, started_at, completed_at, duration_ms |
| `audit_logs` | id, created_at, actor_id, actor_email, action, target_type, target_id, detail, ip_address |
| `platform_settings` | key (service), value (engine settings JSON), source (`env` or `admin`), updated_by, updated_at |

Files are not stored in the database. The worker writes outputs to
`DATA_DIR/output/<job_id>/` (or S3 when `STORAGE_MODE=S3`).

## 7. Processing worker

### Module map

| Module | Responsibility |
|---|---|
| `worker/main.py` | Entry point: preload models (`PRELOAD_MODELS`), OCR warm-up, heartbeat to Valkey (`idp:worker:heartbeat`, shown on System Status), starts `WORKER_CONCURRENCY` job slots |
| `worker/tasks.py` | `process_job(job_id)`: loads the job, dispatches to `_run_phi`, `_run_parse` or `_run_ocr`, records progress, stats, result and errors |
| `worker/storage.py` | Per-job output folders, uploads cleanup, retention purge, optional S3 |
| `worker/ocr_instance.py` | Single PaddleOCR instance; `ocr_images` (batched predict under a GPU lock); `parse_ocr_result` to words with boxes |
| `worker/render_pool.py` | Spawned process pool that rasterises PDF pages with PDFium (`RENDER_WORKERS`) |
| `worker/pdf_util.py` | PDFium helpers: open (detects encrypted files), page words with positions, image coverage, annotation detection, render, text, metadata; `PDFIUM_LOCK` |
| `worker/engine_parse.py` | PP-StructureV3 document parsing, figure descriptions with the vision LLM |
| `worker/engine_ocr.py` | Plain OCR output (text, lines, word boxes) |
| `worker/phi/` | PHI redaction service (section 8) |
| `worker/extract/` | Data extraction service (section 9a) |
| `worker/prompts/` | `phi_prompt.txt`, `image_prompt.txt`, `chart_prompt.txt`, `extract_batch_prompt.txt`, `extract_agent_prompt.txt` |

### Concurrency model

```
worker process
 +-- slot 1 (thread, own Valkey connection) --+
 +-- slot 2                                   +--> shared models in one process
 +-- slot 3                                   +    - PaddleOCR predict serialised by _PREDICT_LOCK (GPU)
                                                   - PDFium calls serialised by PDFIUM_LOCK (not thread-safe)
                                                   - per-job asyncio loop for LLM calls (PHI_LLM_CONCURRENCY in flight)
 +-- render pool (separate processes) ---------> page rasterisation
 +-- ENCODE_EXECUTOR (4 threads) --------------> output page image encoding
```

Slots are RQ `SimpleWorker` subclasses (`ThreadSlotWorker`) with a timer-based job timeout, so
several jobs run in one process and share the loaded models without extra GPU memory. Most of a PHI
job is spent waiting for the LLM, which is why concurrent slots scale throughput.

## 8. PHI redaction service

Entry point `worker/phi/engine.py: run_phi`. It normalises options, builds the detector, dispatches
by format and writes the metadata JSON.

```
 input file
     |
     v
 formats.classify  -->  pdf.py | docx.py | image.py | text.py
                              |
                 extract text units with positions
                 (text layer, OCR, XML runs, characters)
                              |
                              v
                  detector.Detector.detect
                  - pattern detectors (regex): email, phone, SSN, dates, URLs, IPs ...
                  - LLM: one call per page (split only above the PHI_CHUNK_WORDS ceiling), pages in parallel up to PHI_LLM_CONCURRENCY
                  - fail closed if any chunk could not be checked
                              |
                              v
                  matching: locate every detected value in the units
                  (exact, squashed, fuzzy for OCR), propagate across pages
                              |
                              v
                  LabelRegistry: [TYPE_n] per distinct value, or blackout
                              |
                              v
                  write output in the same format, verify, metadata JSON
```

### Components

| Module | Responsibility |
|---|---|
| `detector.py` | Pattern rules, `build_system_prompt` (built-in types, custom entities, instructions), `chunk_text`, `parse_llm_entities`, `Detector` |
| `matching.py` | `Word`, `WordIndex` (squashed matching across OCR tokens with letter/digit and case boundaries), `find_spans`, `apply_spans`, `match_page`, `LabelRegistry`, `summarise` |
| `pdf.py` | PDF redaction (below) |
| `docx.py` | DOCX redaction at the XML level |
| `image.py` | PNG, JPEG, TIFF, BMP, WEBP: OCR then boxes painted into pixels, EXIF dropped |
| `text.py` | TXT, MD, CSV, JSON, XML, HTML, YAML, LOG and other text: in-place replacement keeping encoding and BOM |
| `engine.py` | Dispatch, output naming, metadata schema 2.0 |

### Per-request options (`shared/phi_catalog.py`)

| Option | Values |
|---|---|
| `entity_types` | Subset of the 16 built-in types (default all) |
| `custom_entities` | Up to 25 `{label, description}` pairs, injected into the prompt |
| `instructions` | Free-text guidance for the detector |
| `redaction_style` | `label` (`[TYPE_n]`) or `blackout` |
| `ocr_mode` | PDF: `auto`, `always`, `never` |
| `use_pattern_detectors` | default true |
| `scrub_metadata` | default true |

### PDF pipeline (`worker/phi/pdf.py`)

1. **Analyse** (`_analyse`): each page is classified. Pages with a usable text layer use PDFium
   character positions; scanned pages, and pages with form fields or annotations, are OCR'd
   (`ocr_mode=auto`).
2. **OCR**: pages are rendered at `PHI_RENDER_DPI` by the render pool and recognised in batches.
3. **Detect** on the whole-document text, then **match** values back to word boxes per page.
4. **Write** (`_write_page`): the page is rendered at `PDF_OUTPUT_DPI`, redaction boxes are painted
   into the pixels with padding scaled to text height (`_padded`), the image is encoded (16-level
   grayscale PNG for text pages, JPEG for scans, in parallel on `ENCODE_EXECUTOR`), and reportlab
   writes a new PDF page with the image, an invisible text layer containing only unredacted words,
   and visible labels.
5. **Verify** (`_verify`): the output is re-read with PDFium and must not contain any detected value;
   otherwise the job fails.

The output is a sanitised rebuild: hidden text, form values, annotations, attachments, JavaScript and
metadata from the source are not carried over.

### DOCX pipeline (`worker/phi/docx.py`)

`DocxText` walks every part of the package: body, tables, headers, footers, footnotes, endnotes,
comments, text boxes, tracked deletions (`w:delText`), field codes (`w:instrText`) and hyperlink
relationships. Paragraph text is reconstructed across formatting runs (table cells joined with tabs,
rows with newlines for context), spans are mapped back to runs, and runs are split so only the PHI is
replaced while formatting is kept. Core and app properties are scrubbed when `scrub_metadata` is on.

### Metadata JSON

Returned by `GET /jobs/{id}/metadata` (and inline in synchronous responses): schema version, input
and output formats, page or unit counts, each detection (value, type, label, source: pattern or llm,
location: page and box, or part and paragraph), counts by type, engine used, timings and notes
(for example, text inside embedded pictures not scanned).

## 9. Document parsing and OCR services

**Structured parsing** (`worker/engine_parse.py`, job type `PARSE`): PP-StructureV3 performs layout
detection, reading order, table recognition (`PADDLE_USE_TABLE`), optional formulas and charts. Pages
are fed one by one from PDFium. Figures and charts are optionally described by the document vision LLM
(`DOC_LLM_*`, `DOC_LLM_ENABLE_SUMMARY`). Output: markdown (`/jobs/{id}/markdown`) and structured JSON.

**Plain OCR** (`worker/engine_ocr.py`, job type `OCR`, `mode=ocr`): text per page with line and word
boxes, using the same PP-OCRv6 instance.

## 9a. Data extraction service

Entry point `worker/extract/engine.py: run_extract`. A request carries a document and a **spec**: a JSON object
whose `properties` define each output key with its `type` and a `description`. The output is JSON in exactly
that shape, plus the status and evidence pages of every field.

### Spec validation (`shared/extract_spec.py`)

Checked by the API before the upload is stored; all problems are returned together with `422` and their JSON
path (`POST /extract/validate-spec` runs the same check without a document). Rules: a JSON object with a
non-empty `properties`; field names are identifiers; every field has a supported type (`string`, `number`,
`integer`, `boolean`, `object`, `array`, or the alias `date`) and a description of at least 3 characters;
objects have `properties`, arrays have `items` with a type; `enum` values match the type; no duplicate keys;
`EXTRACT_SPEC_MIN_FIELDS` to `EXTRACT_SPEC_MAX_FIELDS` fields; nesting up to `EXTRACT_SPEC_MAX_DEPTH`.
Objects are containers: the units the extractor fills ("fields") are scalars and arrays, addressed by dotted
paths. Unknown keys are kept as warnings. Model answers are coerced to the declared types (`conform`).

### Approach 1: document batch processing (`batch.py`)

```
 document text with page markers
     |  parts of min(EXTRACT_BATCH_CHUNK_TOKENS,
     |           usable window x EXTRACT_CONTEXT_THRESHOLD - prompt - state - output reserve) tokens
     v
 for each part, in order:  schema + JSON so far (state) + part  -->  LLM (structured output)  -->  merge
     |                      (a null never erases a found value; arrays merged without duplicates)
     v
 final state = result; pages where each field was set = evidence
```

### Approach 2: agentic retrieval (`toc.py`, `chunking.py`, `retrieval.py`, `agent.py`)

```
 document
   |-- table of contents: PDF bookmarks > contents page found by rules in the first EXTRACT_TOC_SCAN_PAGES pages
   |   > contents page found by an LLM scan > outline from headings. Contents pages are structured by the LLM
   |   (titles, levels, printed pages, document type, summary); printed page numbers are calibrated to physical
   |   pages (the offset that puts most titles on the page where they appear, then per-entry snapping);
   |   each entry becomes a section with an id and a page range.
   |-- chunking strategy by document type: section (map available), tabular (CSV / delimited rows, header
   |   repeated), page (short forms, images), recursive (paragraph-aware with overlap). Chunks keep pages and section.
   |-- hybrid index: BM25 + local embeddings (Ollama qwen3-embedding:0.6b), reciprocal rank fusion.
   |   Without the embedding engine it falls back to keyword ranking and says so in the result.
   v
 agent loop (provider-neutral tool calling, any engine)
   tools: get_document_map, search_document, find_text, read_pages, read_section, read_chunk,
          record_fields, get_pending_fields, finish
   state: field values live outside the conversation (record_fields), with pages, confidence and notes
   context: after every turn, size = prompt + cache reads/writes + reply (as reported by the provider)
            + tool results about to be sent. At 80 % of the limit the agent is asked to save findings;
            at EXTRACT_CONTEXT_THRESHOLD x usable window it writes handoff notes, the conversation is dropped
            and a new session starts from a checkpoint (recorded, not found, pending fields; searches made;
            pages and sections read; notes). Raw tool output is never carried over.
   engine failure: the session restarts on the fallback engine from the same checkpoint.
   tool output: about 15 % of the usable window per result (at most EXTRACT_AGENT_MAX_TOOL_CHARS), so small
            windows still leave room to think and record
   loops: a read already done in this session is pointed to; a read from an earlier session is served once more
          with a reminder to record from it, then refused with a pointer to find_text
   not_found: accepted only after the field was searched for (words from its description) or its cited pages
              were examined
   limits: EXTRACT_AGENT_MAX_TURNS, EXTRACT_AGENT_MAX_SESSIONS; unresolved fields end as not_found.
```

**Usable window.** `shared/model_catalog.py: usable_window` = the model's input limit from
`shared/model_catalog.json`, capped by `EXTRACT_CONTEXT_CAP_TOKENS` (default 200000, so long runs do not pay
for million-token prompts), by `OLLAMA_NUM_CTX` for local models and by the per-request `context_window`.

**Engines.** `EXTRACT_LLM_*` (default Claude Sonnet 5.5, fallback GPT-6.2 Sol). A request may choose any
`provider:model` listed by `GET /extract/options`: catalog models of providers with credentials, local models
and the configured engines. `EXTRACT_REASONING_EFFORT` sets the effort. Claude calls use adaptive thinking
with automatic prompt caching; OpenAI reasoning models use the Responses API (stateless, encrypted reasoning
replayed); Gemini thought signatures are replayed.

**Result** (`result_data`, schema 1.0): `data` (the JSON), `fields` (path, type, status, value, pages,
confidence, note), `summary` (found, not found, coverage), `document` (pages, OCR, contents map, chunking,
retrieval), `engine` (model, windows, threshold, fallback), `usage` (tokens and cost per model, from catalog
prices), `agent` (sessions, compactions, handoff notes, tool trace) or `batch` (parts), `warnings`, timings.
`GET /jobs/{id}/file` downloads `data` as `<name>.extracted.json`.

### Model catalog (`shared/model_catalog.json`)

Providers (Anthropic, OpenAI, Gemini, Ollama) with recommended models per purpose, and per model: context
window, max input and output tokens, prices (input, output, cache read, cache write, long-context tier,
scheduled changes), capabilities and whether the context window was verified at first-party sources. Also the
embedding models with their query and document prefixes. The compaction threshold is not in the file; it is
`EXTRACT_CONTEXT_THRESHOLD`. Update the file when providers release models; `MODEL_CATALOG_PATH` points to
another copy.

## 10. OCR models

| Setting | Default | Alternatives |
|---|---|---|
| `OCR_DET_MODEL` | `PP-OCRv6_small_det` | `PP-OCRv6_tiny_det` (fastest), `PP-OCRv6_medium_det` (most accurate), `PP-OCRv5_server_det` |
| `OCR_REC_MODEL` | `PP-OCRv6_small_rec` | as above |

Engine: PaddleOCR 3.7 / PaddleX 3.7 on PaddlePaddle 3.3 (GPU). Measured recognition time per page on an
RTX 4070 Laptop GPU: v6 tiny 0.17 s, v6 small 0.41 s, v6 medium 0.64 s, v5 server 0.77 s. Models are
cached in the `paddle_models` and `paddle_ocr_models` volumes. `PADDLE_ENABLE_HPI` enables the optional
high-performance inference plugin. Document parsing uses the PP-StructureV3 layout, table and formula
models. All models are Apache-2.0.

## 11. LLM engines

`shared/llm.py` provides one interface (`chat`, `vision`, `health`) over all providers.

| Provider id | Class | Transport | Notes |
|---|---|---|---|
| `openai` | `OpenAIProvider` | Chat Completions over httpx | Reasoning models get `reasoning_effort` (`LLM_REASONING_EFFORT`) |
| `azure_openai` | `AzureOpenAIProvider` | Azure deployment URL, `api-key` header | `*_API_VERSION` |
| `openai_compatible` | `OpenAICompatibleProvider` | Any OpenAI-protocol server (vLLM, LM Studio, Groq, Together) | Optional key |
| `anthropic` | `AnthropicProvider` | Official `anthropic` SDK | Claude models; effort mapping |
| `gemini` | `GeminiProvider` | Generative Language REST API | JSON mode |
| `ollama` | `OllamaProvider` | Ollama `/api/chat` | `format: json`, thinking off, `OLLAMA_KEEP_ALIVE` |

**Workloads.** Three independent configurations: `PHI_LLM_*` (text, PHI detection), `DOC_LLM_*`
(vision, figure descriptions) and `EXTRACT_LLM_*` (data extraction). Each has a primary and an optional
fallback. Credentials resolve as: the workload's own key, then `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` /
`GEMINI_API_KEY`, then the key of another workload on the same provider (`resolve_api_key`).

**Tool calling.** `tool_chat(system, history, tools)` takes a provider-neutral conversation and returns an
`AgentTurn` (text, tool calls, native message, token usage). Native assistant messages are replayed unchanged
to the same model, so Claude thinking blocks, OpenAI encrypted reasoning and Gemini thought signatures are
preserved. OpenAI reasoning models go through `/v1/responses`, because Chat Completions does not accept
function tools together with reasoning effort on them. `chat(..., json_schema=...)` asks for structured
output (OpenAI strict JSON schema, Claude output format, Gemini response schema, Ollama format).

**Failover** (`FailoverLLM`): calls go to the primary; on an authentication error, outage or exhausted
retries the call is repeated on the fallback, and the primary is skipped for
`LLM_FAILOVER_COOLDOWN_SECONDS`. `get_llm_client(mode)` builds the client.

**Usage and cost.** `FailoverLLM.call_log` records the tokens of every call; `shared/usage.py` turns it into a
report (calls, uncached input, cache reads and writes, output, cost per model and in total) for PHI redaction,
structured parsing and data extraction. It is stored in `stats.usage`, returned as `usage` on every job, and shown
on each review page and in Job History.

**Health** (`describe_llm`): each engine is probed (model listing, or a one-token completion for
restricted keys) and the result cached for five minutes; `/admin/system` shows which engine is in use.

**Engine settings per service (control panel).** The engine of each service is stored in `platform_settings`:
`phi`, `doc`, `extract` (primary, fallback, reasoning effort), `extract_toc` (index detection mode and engine) and
`extract_embed` (embedding provider and model). At the first start `api/prestart.py` seeds every row from .env;
from then on administrators change them under **Administration, AI Engines** (`/admin/llm-config`), with save,
connection test and reset to the .env values, each written to the audit log. `shared/runtime_config.py` serves the
effective values to the API and the worker with a 5 second cache, so new jobs use a change within seconds; it falls
back to .env when the database is unreachable or `RUNTIME_CONFIG_SOURCE=env`. API keys are never stored there.
Selection lists come from the model catalog; a provider's `account_models` limits what is offered.

**Index (contents) detection modes** (`extract_toc`): `rules` (regular expressions, no model), `llm` (the model
looks for the contents pages and structures them), `hybrid` (rules first, the model when they find nothing). PDF
bookmarks are used first in every mode. The engine can differ from the extraction engine (for example a small,
cheap model for indexing and a reasoning model for extraction).

**Default local model:** `qwen3.5:4b` (Apache-2.0) pulled by `ollama-init`, with `qwen3-embedding:0.6b` for
retrieval. `OLLAMA_NUM_CTX` (32768) is the context of every local request; 8192 is too small for agentic extraction
with a 4B model (3 to 11 of 16 fields, against 16 of 16 at 32768). `OLLAMA_NUM_PARALLEL` (2) bounds the KV memory,
and `OLLAMA_MAX_LOADED_MODELS` (2) keeps the chat and embedding models loaded together, which avoids a model reload on
every agent turn (606 s down to 78 s on the 100-page test contract).

## 12. Front end (web app)

React 18 + TypeScript, built with Vite 6, styled with Tailwind 3, served by nginx.

### Libraries

| Concern | Library |
|---|---|
| Routing | react-router 6 (lazy-loaded review pages) |
| Server state | TanStack Query |
| Animation | Framer Motion (subtle transitions only) |
| Icons | lucide-react |
| PDF rendering | pdfjs-dist 4 |
| DOCX rendering | docx-preview |
| Markdown | react-markdown with rehype-sanitize |
| Typeface | Inter (`@fontsource-variable/inter`) |

### Structure

| Path | Responsibility |
|---|---|
| `src/main.tsx`, `src/App.tsx` | Bootstrap, routes and guards (`RequireAuth`, `RequireService`, `RequireAdmin`) |
| `src/lib/api.ts` | Fetch wrapper: bearer token, silent refresh on 401, `ApiError` |
| `src/lib/auth.tsx` | Auth context: session, roles, services, forced password change |
| `src/lib/types.ts`, `format.ts`, `hooks.ts` | API types, formatting helpers, shared hooks |
| `src/components/ui.tsx` | Design system: Page, PageHeader, Section, Button, Alert, Tag, StatusBadge, Stepper, FileDrop, Modal, Toast, Pagination, Segmented, Tabs, Spinner |
| `src/components/Layout.tsx` | Sidebar navigation filtered by role, page shell |
| `src/components/viewers.tsx` | Document viewers: PDF, DOCX (placeholders highlighted), text, markdown, image; side-by-side original and redacted |
| `src/components/JobsTable.tsx`, `JobProgress.tsx` | Job lists and live progress |
| `src/pages/*` | Screens (below) |

### Screens

| Route | Page | Access |
|---|---|---|
| `/login` | Login | Public |
| `/` | Overview | Signed in |
| `/phi`, `/phi/jobs/:id` | PHI redaction: Upload, Configure (entity types, custom entities, style), Process, Review | `phi` |
| `/parse`, `/parse/jobs/:id` | Document parsing: Upload, Configure, Process, Review | `parse` |
| `/extract`, `/extract/jobs/:id` | Data extraction: Upload, Define output (spec editor with live validation), Process, Review (fields with evidence pages, JSON, document map, agent activity) | `extract` |
| `/jobs` | Job history | Signed in |
| `/api-access` | API keys and integration examples | Signed in |
| `/account` | Profile and password | Signed in |
| `/admin/users`, `/admin/system`, `/admin/engines`, `/admin/audit` | Administration (AI Engines: engine per service) | Admin |

Design rules: light theme, white background, blue accents, Inter, no emojis, titles with a subtitle,
subtle animation only.

## 13. Configuration

All configuration comes from environment variables (`.env`, template `.env.example`), loaded by
`shared/config.py`. Groups: stack and ports; database; authentication and super admin seed; limits and
storage; PHI, document and extraction LLM engines with fallbacks; provider keys; extraction limits,
context threshold and cap, chunking and embeddings; OCR models and rendering; worker concurrency. Model facts
(context windows, prices) are data, in `shared/model_catalog.json`.
Nothing is hard-coded and secrets are never logged.

## 14. Performance design

| Technique | Where | Effect (measured) |
|---|---|---|
| Parallel LLM calls per job (`PHI_LLM_CONCURRENCY=16`) across pages (one call per page) | `detector.py` | 30-page PDF detection 55 s to 15.5 s |
| Concurrent job slots sharing models (`WORKER_CONCURRENCY=3`) | `worker/main.py` | about 55 pages/min under load, 3.2x serial |
| PP-OCRv6 small instead of v5 server | `ocr_instance.py` | 1.9x faster OCR |
| OCR warm-up at start | `worker/main.py` | no first-job penalty |
| Parallel output encoding, 16-level PNG for text pages | `pdf.py` | smaller files, faster writes |
| glibc malloc tuning | `docker-compose.yml` | flat worker memory |

GPU OCR is the serial bottleneck; more render processes or larger OCR batches did not help.

## 15. Testing

| Suite | Location | Needs |
|---|---|---|
| Unit | `tests/unit` | Nothing external (stub LLM) |
| Integration | `tests/integration` | Running stack; test documents from `IDP_FIXTURES` |

Development scripts (fixture generator, benchmarks, browser walkthrough) and reports live in the
git-ignored `dev_space/` folder; see `CLAUDE.md`.

## 16. Deployment

* `docker compose up -d --build` runs the whole stack; the first start downloads OCR and Ollama models.
* GPU: NVIDIA driver and Container Toolkit. `ec2_setup.sh` prepares an Amazon Linux 2023 GPU host.
* Put TLS in front of `web` and set `COOKIE_SECURE=true`.
* Scale out by running more `worker` replicas against the same Valkey and PostgreSQL.
* Licences: `THIRD_PARTY_LICENSES.md`; check with `tools/license_audit.py`.

## 17. Extension points

| To add | Change |
|---|---|
| An LLM provider | Subclass `BaseProvider` in `shared/llm.py`, register it in the provider factory, add config fields |
| A built-in PHI type | `BUILTIN_ENTITIES` in `shared/phi_catalog.py` (and a pattern in `detector.py` if deterministic) |
| A redaction format | New module in `worker/phi/`, a branch in `engine.py: run_phi`, the type in `shared/formats.py` |
| A role or service | `ROLE_SERVICES` and `JOB_SERVICE` in `shared/models.py`; a migration if stored values change |
| A model or a price change | `shared/model_catalog.json` (no code change) |
| An extraction agent tool | `TOOL_SPECS` and `DocumentTools.execute` in `worker/extract/agent.py` |
| An API route | Router in `api/routers/`, mounted in `api/main.py`, guarded with `require_service` / `require_admin` |
| A screen | Page in `frontend/src/pages/`, route in `App.tsx`, nav entry in `Layout.tsx` |
| A schema change | New Alembic revision in `migrations/versions/` |
