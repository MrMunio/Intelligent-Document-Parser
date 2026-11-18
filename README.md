# IDP Platform

Intelligent document processing with two services:

* **PHI redaction.** Detects protected health information and personal data, plus any custom entity
  types described in the request, and returns the redacted document **in the same format** (PDF, DOCX,
  PNG / JPEG / TIFF, TXT, MD, CSV, JSON, XML, HTML and other text files) together with a metadata JSON
  that lists every detection.
* **Document parsing.** Converts scanned or digital PDFs and images into layout-aware markdown and
  structured JSON, or plain text with word coordinates.

It ships as a React web app and a standalone REST API. External systems integrate through the API with
API keys; the web app is one client of that API and is not required.

## Quick start

Requirements: Docker with Compose, and an NVIDIA GPU with the NVIDIA Container Toolkit (8 GB is enough
for OCR plus the local fallback model).

```bash
cp .env.example .env
# Set JWT_SECRET, SUPER_ADMIN_PASSWORD and, if you use a hosted engine, PHI_LLM_API_KEY / DOC_LLM_API_KEY.
docker compose up -d --build
```

* Web app: `http://localhost:3000`. Sign in with `SUPER_ADMIN_EMAIL` / `SUPER_ADMIN_PASSWORD`.
* API reference: `http://localhost:3000/api/docs` (also on the API port, default 8000).
* The first start downloads the OCR models and, with the `ollama` profile, the local Qwen model.

## Architecture

```
Browser ──> web (nginx: React app, proxies /api) ──> api (FastAPI) ──> PostgreSQL (users, keys, jobs, audit)
External systems ─────────────────────────────────> api              ──> Valkey (job queue)
                                                                             │
                                worker (GPU) <───────────────────────────────┘
                                  PaddleOCR PP-OCRv6 + PP-StructureV3, PDFium, reportlab, lxml
                                  └──> LLM engine: primary (e.g. OpenAI) with automatic fallback (local Ollama)
```

* **api**: authentication, role-based access, validation, job records, file delivery, audit log.
  Migrations run automatically at start-up (Alembic).
* **worker**: one process loads the models once and runs `WORKER_CONCURRENCY` jobs at a time. Most of a
  job is spent waiting for the language model, so concurrent jobs share the GPU models without extra
  GPU memory.
* **Progress** is stored on the job record, so it survives a page refresh. Uploaded originals are deleted
  as soon as a job finishes; outputs are deleted after `FILE_RETENTION_DAYS`.

## Roles

| Role | PHI redaction | Document parsing | Manage users | System status and audit log |
|---|---|---|---|---|
| Super admin | Yes | Yes | All users, including administrators | Yes |
| Admin | Yes | Yes | Non-administrator users | Yes |
| PHI redactor | Yes | | | |
| Document parser | | Yes | | |

A user can hold several roles. Access is enforced by the API on every request. New users get a
temporary password and must change it at first sign-in. Changing a user's roles, disabling them or
resetting their password signs out their existing sessions immediately.

**Authentication.** Email and password sign-in issues a 30-minute JWT access token (kept in memory by
the web app) and a 7-day refresh token in an httpOnly, SameSite=Strict cookie. Five failed sign-ins lock
the account for 15 minutes. API keys (`X-API-Key: idp_...`) act as their owner, limited to the services
selected for the key, and are stored only as a SHA-256 hash.

## API

All endpoints are under `/api/v1`. The complete reference is at `/api/docs`, and the OpenAPI document at
`/api/openapi.json` imports directly into Postman.

| Endpoint | Purpose |
|---|---|
| `POST /phi/redact` | Redact a file and wait. `response_format=file` returns the redacted file itself |
| `POST /phi/jobs` | Redact a file asynchronously; returns a job id |
| `POST /phi/redact-text` | JSON in, JSON out, for short text |
| `GET /phi/entity-types` | Built-in entity types and accepted options |
| `POST /parse/sync`, `POST /parse/jobs` | Parse a document (`mode=structured` or `mode=ocr`) |
| `GET /jobs`, `GET /jobs/{id}` | List jobs, poll status and progress |
| `GET /jobs/{id}/file`, `/metadata`, `/markdown` | Download results |
| `POST /auth/login`, `/auth/refresh`, `GET /auth/me` | Sessions |
| `/users`, `/api-keys`, `/admin/system`, `/admin/audit-logs` | Administration |

Example: redact a Word document with a custom entity and save the result.

```bash
curl -X POST "http://localhost:8000/api/v1/phi/redact" \
  -H "X-API-Key: $IDP_API_KEY" \
  -F "file=@discharge-summary.docx" \
  -F 'custom_entities=[{"label":"EMPLOYEE_ID","description":"Internal employee numbers such as EMP-48213"}]' \
  -F "response_format=file" -D headers.txt -o discharge-summary.redacted.docx
```

### Per-request PHI options

| Field | Meaning |
|---|---|
| `entity_types` | Built-in types to detect (default: all 16). See `GET /phi/entity-types` |
| `custom_entities` | Your own types for this request: `[{"label": "...", "description": "..."}]`, up to 25 |
| `instructions` | Extra guidance for the detector |
| `redaction_style` | `label` writes `[TYPE_n]` placeholders (same value, same placeholder); `blackout` masks without a label |
| `ocr_mode` | PDF only: `auto` (text layer where present, OCR for scanned pages), `always`, `never` |
| `use_pattern_detectors` | Deterministic rules for emails, phones, SSNs, dates, URLs and IP addresses (default on) |
| `scrub_metadata` | Remove document properties such as author and title (default on) |

### How each format is redacted

* **PDF**: pages with a text layer use its exact character positions; scanned pages, and pages with form
  fields or annotations, are read with OCR. The output is a sanitised PDF: every page is re-rendered with
  the redactions painted into the pixels, and a new searchable text layer is written that holds only the
  unredacted words. Hidden text, form values, annotations, attachments and metadata from the source are
  not carried over. The written file is re-read and checked before it is released.
* **DOCX**: edited in place at the XML level, keeping all formatting. Covers body text, tables, headers,
  footers, footnotes, comments, text boxes, tracked deletions, field codes and hyperlink targets, including
  values split across formatting runs. Text inside embedded pictures is not scanned; the metadata says so.
* **Images**: OCR, then boxes painted into the pixels; the image format is kept and EXIF metadata is dropped.
* **Text files**: replaced in place, keeping the encoding and byte order mark.

If any part of a document could not be checked by the detector, the job fails rather than returning a
partially redacted file.

## LLM engines

Set a primary and an optional fallback engine separately for PHI detection and for figure descriptions.
Supported: OpenAI, Azure OpenAI, any OpenAI-compatible server (vLLM, LM Studio, Groq and others),
Anthropic Claude, Google Gemini and Ollama. When the primary fails (bad credentials, outage or exhausted
retries) the job continues on the fallback, and the primary is retried after five minutes. The System
Status page shows the live state of every engine.

**Accuracy and privacy.** Measured on synthetic test documents, `gpt-5-mini` at low
reasoning effort and the local `qwen3.5:4b` both find every patient identifier. At low effort,
`gpt-5-mini` sometimes misses names of care-team staff listed in tables; `LLM_REASONING_EFFORT=medium`
removes that at about 2.5 times the detection time. The local Qwen engine keeps document text inside your
network and needs no BAA; hosted engines that receive PHI require a Business Associate Agreement.

## OCR

PaddleOCR 3.7 with **PP-OCRv6 small**, released June 2026. On the 12-page scanned test document it
recognises a page in 0.41 s on an RTX 4070 Laptop GPU, 1.9 times faster than PP-OCRv5 server (the previous
model). `PP-OCRv6_tiny_*` is 2.5 times faster again, `PP-OCRv6_medium_*` is the most accurate; choose with
`OCR_DET_MODEL` and `OCR_REC_MODEL`.

## Tests

```bash
# Unit tests (no GPU or model needed)
docker compose run --rm --no-deps -v ./tests:/app/tests -v ./pytest.ini:/app/pytest.ini worker \
  python -m pytest tests/unit
# Integration tests against the running stack. They read test documents from IDP_FIXTURES
# (default dev_space/test_docs) and write raw outputs to IDP_RESULTS (default dev_space/results).
docker compose run --rm --no-deps -v ./tests:/app/tests -v ./dev_space:/app/dev_space \
  -v ./pytest.ini:/app/pytest.ini worker python -m pytest tests/integration
```

Build with `INSTALL_DEV=true` to include the test tools in the images.

## Licensing

Every component that handles documents, every model and every service is under a permissive licence; see
[THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md), including the short list of items for legal review.
Check the build with `python tools/license_audit.py` inside each image.

## Project structure

```
api/        FastAPI app: routers, auth and RBAC dependencies, job service, start-up migrations
shared/     Settings, database models, LLM engines, file-format rules, PHI catalogue
worker/     Job runner, OCR, document parsing, PHI redaction (pdf, docx, image, text)
frontend/   React + Vite + Tailwind web app, served by nginx
migrations/ Alembic database migrations
tests/      Unit and integration tests
tools/      Licence audit
docs/       Architecture documentation
```

AWS: `ec2_setup.sh` prepares an Amazon Linux 2023 GPU instance (drivers, Docker, NVIDIA Container Toolkit).
