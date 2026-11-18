# IDP Platform API: integration guide

This guide is the contract for systems that integrate with the IDP Platform over HTTP. It covers authentication,
the job model, every service endpoint with request and response shapes, errors, usage and cost reporting, and
limits. The machine-readable reference is the OpenAPI document at `/api/openapi.json` (interactive at
`/api/docs`); it imports directly into Postman and code generators.

Contents

1. [Overview](#1-overview)
2. [Authentication](#2-authentication)
3. [Jobs: how every request runs](#3-jobs-how-every-request-runs)
4. [PHI redaction](#4-phi-redaction)
5. [Document parsing](#5-document-parsing)
6. [Data extraction (document to JSON)](#6-data-extraction-document-to-json)
7. [Usage and cost](#7-usage-and-cost)
8. [Errors](#8-errors)
9. [Limits, retention and security](#9-limits-retention-and-security)
10. [Account and key management endpoints](#10-account-and-key-management-endpoints)
11. [End-to-end examples](#11-end-to-end-examples)
12. [Versioning and compatibility](#12-versioning-and-compatibility)

---

## 1. Overview

| Service | Scope name | What it does | Main endpoints |
|---|---|---|---|
| PHI redaction | `phi` | Finds protected health information and personal data (16 built-in types plus your own) and returns the redacted document in the same format, with a metadata JSON of every detection | `POST /phi/jobs`, `/phi/redact`, `/phi/redact-text` |
| Document parsing | `parse` | Layout-aware markdown and structured JSON, or plain text with word boxes | `POST /parse/jobs`, `/parse/sync` |
| Data extraction | `extract` | JSON that follows a spec you send with each request, from documents of any length | `POST /extract/jobs`, `/extract/sync`, `/extract/text` |

**Base URL.** Every endpoint is under `/api/v1` on the platform host, for example
`https://idp.example.com/api/v1`. On a local installation the API listens on `http://localhost:<API_PORT>`
(default 8000) and the web app also proxies `/api` on `http://localhost:<WEB_PORT>`.

**Conventions.**

* Requests with files use `multipart/form-data`; other requests and all responses use JSON (UTF-8).
* Times are ISO 8601 in UTC. Ids are UUID strings.
* Field names are `snake_case`. Unknown query parameters are ignored.

**Health probes** (no authentication): `GET /health` (process alive) and `GET /ready` (database and queue
reachable; `503` when degraded).

## 2. Authentication

Every endpoint under `/api/v1` requires one of two credentials.

### 2.1 API keys (recommended for integrations)

```
X-API-Key: idp_3fJ9...
```

The bearer form also works: `Authorization: Bearer idp_3fJ9...`.

* **Creating a key.** A user creates keys in the web app (**API Access**) or with `POST /api-keys` while signed
  in. The full key is shown once; the platform stores only a SHA-256 hash.
* **Scopes.** Each key is limited to the services chosen when it was created (`phi`, `parse`, `extract`), and
  never exceeds what its owner can do now. If the owner loses a role, their keys lose that service at once.
* **Expiry and revocation.** A key can have an expiry (`expires_in_days`, 1 to 3650) and can be revoked
  (`DELETE /api-keys/{id}`).
* **Account endpoints.** Keys cannot manage users, keys or passwords; those endpoints require a signed-in
  session.
* **Usage tracking.** `last_used_at` is updated at most once a minute.

Create a dedicated service account (a user with only the role your integration needs, see 2.3) and create the
key as that user. Store the key in a secret manager, never in source code.

### 2.2 Session tokens (interactive clients)

```http
POST /api/v1/auth/login
Content-Type: application/json

{"email": "integration@example.com", "password": "..."}
```

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "expires_in": 1800,
  "user": {"id": "...", "email": "integration@example.com", "full_name": "...", "roles": ["data_extractor"],
           "services": ["extract"], "is_active": true, "must_change_password": false}
}
```

**The access token**
* Send it as `Authorization: Bearer <access_token>`.
* It lasts `JWT_ACCESS_TTL_MINUTES` (30 by default).

**The refresh token**
* Login also sets an httpOnly, SameSite=Strict cookie scoped to `/api/v1/auth`, which lasts 7 days by default.
* `POST /auth/refresh` exchanges the cookie for a new access token.
* `POST /auth/logout` clears it.

**Lockout**
* Five failed sign-ins lock the account for 15 minutes; requests during the lockout get `429`.

**Session invalidation**
* Changing a user's roles or password, or disabling the account, invalidates all of that user's tokens at once.

**Temporary passwords**
* Accounts created with a temporary password must call `POST /auth/change-password` before any service
  accepts their token; until then services return `403`.
* API keys are not affected by this rule.

### 2.3 Roles and what they allow

| Role | Services | Notes |
|---|---|---|
| `phi_redactor` | `phi` | |
| `document_parser` | `parse` | |
| `data_extractor` | `extract` | |
| `admin` | all three | Also manages non-admin users, sees every job, system status and audit log |
| `super_admin` | all three | Also manages administrators |

`GET /auth/me` returns the current account with its `roles` and effective `services`. A request to a service
the credential cannot use returns `403` with `"Your account does not have access to the '<service>' service."`

## 3. Jobs: how every request runs

Every processing request becomes a **job**. There are two ways to run one:

* **Asynchronous** (`.../jobs` endpoints). Returns `202 Accepted` immediately with the job id. Poll
  `GET /jobs/{job_id}` until `status` is `COMPLETED` or `FAILED`. Use this for large documents and batch
  integrations.
* **Synchronous** (`/phi/redact`, `/phi/redact-text`, `/parse/sync`, `/extract/sync`, `/extract/text`). Waits
  for the job and returns the result in the same response. Synchronous jobs go to a priority queue.
  * If processing takes longer than `SYNC_TIMEOUT_SECONDS` (900 by default), the response is `504`. The job keeps
    running; poll it by the id in the error message.
  * Set your HTTP client timeout above that value.

### 3.1 Accepted response (`202`)

```json
{
  "job_id": "3f1c2a90-6d1e-4c5e-9a43-0f0b9d6f1c21",
  "status": "PENDING",
  "message": "Job queued. Poll the 'self' link until status is COMPLETED or FAILED.",
  "links": {"self": "/api/v1/jobs/3f1c2a90-6d1e-4c5e-9a43-0f0b9d6f1c21"}
}
```

### 3.2 Job object (`GET /jobs/{job_id}`)

```json
{
  "job_id": "3f1c2a90-...",
  "job_type": "EXTRACT",
  "status": "COMPLETED",
  "file_name": "provider-agreement.pdf",
  "input_format": "pdf",
  "file_size": 175241,
  "source": "api",
  "options": {"approach": "agentic", "model": null, "...": "..."},
  "progress": {"stage": "completed", "percent": 100, "detail": "Completed"},
  "stats": {"...": "service-specific counters, see each service"},
  "usage": {"total": {"calls": 6, "input_tokens": 4003, "output_tokens": 2895, "cache_read_tokens": 39000,
                      "cache_write_tokens": 12284, "cost_usd": 0.074227, "priced": true},
            "by_model": {"anthropic:claude-sonnet-5-5": {"...": "same keys as total"}}},
  "error": null,
  "created_at": "2026-11-18T12:01:02Z",
  "started_at": "2026-11-18T12:01:02Z",
  "completed_at": "2026-11-18T12:01:33Z",
  "duration_ms": 30412,
  "result_file_name": "provider-agreement.extracted.json",
  "result_media_type": "application/json",
  "result_available": true,
  "links": {"self": "/api/v1/jobs/3f1c2a90-...", "file": "/api/v1/jobs/3f1c2a90-.../file"},
  "result_data": {"...": "service-specific result"},
  "result_markdown": null
}
```

| Field | Meaning |
|---|---|
| `job_type` | `PHI`, `PARSE` (structured parsing), `OCR` (plain text) or `EXTRACT` |
| `status` | `PENDING`, then `PROCESSING`, then `COMPLETED` or `FAILED` |
| `progress` | `stage` (for example `ocr`, `detecting`, `indexing`, `agent`), `percent` 0 to 100, human-readable `detail`. Stored on the server, so any client sees the same progress |
| `usage` | Language model calls, tokens and cost for the job (section 7) |
| `error` | Present when `FAILED`: the last line of the error, safe to show to users |
| `links` | `file`, `metadata` and `markdown` appear once the corresponding output exists |
| `result_data`, `result_markdown` | Only on single-job responses (not in lists) |

**Polling.** Poll every 1 to 2 seconds for small documents and every 5 to 10 seconds for long ones. `progress`
tells you how far the job is.

### 3.3 Job endpoints

| Method and path | Description |
|---|---|
| `GET /jobs` | Your jobs, newest first. Query: `job_type`, `status`, `page` (from 1), `page_size` (1 to 100, default 20), `scope=all` (administrators: everyone's jobs). Returns `{"items": [...], "total", "page", "page_size"}` without `result_data` |
| `GET /jobs/{job_id}` | One job with its result |
| `GET /jobs/{job_id}/file` | The output file (redacted document, or the extracted JSON). `?inline=true` sets `Content-Disposition: inline`. The job id is in `X-Job-Id` |
| `GET /jobs/{job_id}/metadata` | PHI only: the redaction metadata JSON |
| `GET /jobs/{job_id}/markdown` | Parsing only: the markdown (`text/markdown`) |
| `DELETE /jobs/{job_id}` | Deletes the job and its output (`204`); `409` while it is processing |

A job is visible to its owner and to administrators. Another user's job returns `404`, as if it did not exist.

## 4. PHI redaction

Requires scope `phi`. Accepted files are PDF, DOCX, images (PNG, JPEG, TIFF, BMP, WEBP) and any text-based file
(TXT, MD, CSV, JSON, XML, HTML, YAML, LOG and others). The output keeps the input format. PHI jobs **fail
closed**: if any part of the document could not be checked, no output is produced.

### 4.1 Options (form fields)

| Field | Type | Default | Description |
|---|---|---|---|
| `file` | file | required | The document |
| `entity_types` | JSON array or comma list | all built-in types | Subset of `PATIENT_NAME`, `PROVIDER_NAME`, `DATE`, `AGE_OVER_89`, `MRN`, `SSN`, `HEALTH_PLAN_ID`, `ACCOUNT_NUMBER`, `LICENSE_NUMBER`, `DEVICE_ID`, `OTHER_ID`, `PHONE`, `EMAIL`, `ADDRESS`, `URL`, `IP_ADDRESS`. `GET /phi/entity-types` lists them with descriptions |
| `custom_entities` | JSON array | none | Up to 25 `{"label": "EMPLOYEE_ID", "description": "Internal employee numbers such as EMP-48213"}`. Labels become placeholders such as `[EMPLOYEE_ID_1]` |
| `instructions` | string | none | Extra guidance for the detector, up to 2000 characters |
| `redaction_style` | `label` or `blackout` | `label` | `label` writes `[TYPE_n]` placeholders (the same value keeps the same number); `blackout` masks without a label |
| `ocr_mode` | `auto`, `always`, `never` | `auto` | PDF only. `auto` uses the text layer where it exists and OCR for scanned pages, form fields and annotations |
| `use_pattern_detectors` | boolean | `true` | Also run deterministic patterns (email, phone, SSN, dates, URLs, IPs) |
| `scrub_metadata` | boolean | `true` | Remove document properties such as author and title |

### 4.2 Endpoints

| Method and path | Returns |
|---|---|
| `POST /phi/jobs` | `202` with the job id |
| `POST /phi/redact` | Waits. `response_format=json` (default) returns the job object; `response_format=file` returns the redacted file itself with `X-Job-Id` |
| `POST /phi/redact-text` | JSON in, JSON out (below) |
| `GET /phi/entity-types` | Entity types, styles, OCR modes, limits |

`POST /phi/redact-text` request:

```json
{
  "text": "Patient Sarah Mitchell (MRN 4481920) called from (415) 555-0134.",
  "entity_types": ["PATIENT_NAME", "MRN", "PHONE"],
  "custom_entities": [{"label": "CASE_NUMBER", "description": "Support case numbers starting with CS-"}],
  "redaction_style": "label",
  "use_pattern_detectors": true,
  "file_name": "message.txt"
}
```

Response:

```json
{
  "job_id": "...",
  "redacted_text": "Patient [PATIENT_NAME_1] (MRN [MRN_1]) called from [PHONE_1].",
  "entities": [{"text": "Sarah Mitchell", "type": "PATIENT_NAME", "label": "[PATIENT_NAME_1]", "source": "llm", "...": "..."}],
  "summary": {"total_occurrences": 3, "unique_entities": 3, "by_type": {"PATIENT_NAME": 1, "MRN": 1, "PHONE": 1}},
  "usage": {"total": {"calls": 1, "cost_usd": 0.0003, "...": "..."}, "by_model": {"...": "..."}},
  "duration_ms": 2410
}
```

### 4.3 Result

* `result_data` (and `GET /jobs/{id}/metadata`) is the metadata JSON, schema version 2.0:
  * `entities`: every detection, with value, type, label, source (`pattern` or `llm`) and location (page and
    box, or part and paragraph);
  * `summary`: counts by type;
  * `verification`: the output is re-read and must not contain any detected value;
  * `engine`, `usage`, `timings_ms`, `notes`.
* `GET /jobs/{id}/file` returns the redacted document, named `<name>.redacted.<ext>`.
* `stats` holds `entities`, `unique_entities`, `by_type`, `pages`, `pages_ocr`, `verification`, `timings_ms`,
  `llm` and `usage`.

## 5. Document parsing

Requires scope `parse`. Accepts PDF and images.

| Field | Values | Description |
|---|---|---|
| `file` | file | The document |
| `mode` | `structured` (default) or `ocr` | `structured`: layout-aware markdown plus JSON chunks (headings, paragraphs, tables, figures in reading order). `ocr`: plain text with line and word bounding boxes |
| `ai_summaries` | boolean, default `true` | Structured mode: describe figures and charts with the vision model (adds time and token cost) |

| Method and path | Returns |
|---|---|
| `POST /parse/jobs` | `202` with the job id |
| `POST /parse/sync` | The job object when finished |

**Structured mode** (`job_type` `PARSE`)
* `result_markdown`: the document as markdown.
* `result_data.chunks`: one entry per layout block, with `label` (for example `text`, `table`, `figure`),
  `page`, `grounding` (box) and `markdown`.
* `result_data.metadata`: `page_count`, `duration_s`, `figures_described`, `llm` and `usage`.

**OCR mode** (`job_type` `OCR`)
* `result_data.pages[].lines[]`: each line has `text`, `confidence` and `boundingBox`
  (`xmin`, `ymin`, `xmax`, `ymax` in pixels).
* Each line also has `words[]` with their own boxes.
* OCR mode calls no language model: its `usage` is zero.

## 6. Data extraction (document to JSON)

Requires scope `extract`. Accepts PDF, DOCX, images and text-based files. You send the document and a **spec**
that describes the JSON you want; the result is JSON in exactly that shape.

### 6.1 The spec

A spec is a JSON object whose `properties` define each output key. Every field needs a `type` and a
`description` (what the field holds, in words the document is likely to use).

```json
{
  "title": "Provider contract summary",
  "description": "Key terms of a health plan provider agreement",
  "type": "object",
  "properties": {
    "effective_date": {"type": "date", "description": "Date the agreement takes effect"},
    "auto_renewal": {"type": "boolean", "description": "Whether the agreement renews automatically"},
    "termination": {
      "type": "object", "description": "Termination terms",
      "properties": {
        "notice_days": {"type": "integer", "description": "Days of written notice to terminate without cause"}
      }
    },
    "fee_schedule": {
      "type": "array", "description": "Every service rate in the compensation schedule",
      "items": {"type": "object", "properties": {
        "code": {"type": "string", "description": "Procedure code"},
        "rate_usd": {"type": "number", "description": "Rate in US dollars"}
      }}
    },
    "party_role": {"type": "string", "description": "Role of the signing party", "enum": ["payer", "provider"]}
  },
  "required": ["effective_date"]
}
```

| Rule | Detail |
|---|---|
| Types | `string`, `number`, `integer`, `boolean`, `object`, `array`, and `date` (stored as a string, `YYYY-MM-DD`). `["string", "null"]` style unions are accepted |
| Description | Required on every field, at least 3 characters (array `items` may omit it) |
| Objects | Need a non-empty `properties` |
| Arrays | Need `items` with a `type` |
| `enum` | Optional on string, number and integer fields; values must match the type |
| Names | Letters, digits and underscores, not starting with a digit, up to 64 characters; no duplicate keys |
| Size | 1 to 300 fields (scalars and arrays count; objects are containers), nesting up to 6 levels. Limits are configurable and reported by `GET /extract/options` |
| Other keys | Ignored, reported as warnings |

**Validation before the job starts.**
* An invalid spec is rejected with `422` before the document is stored. The response lists every problem at once,
  each with its JSON path:

  ```json
  {"detail": {"message": "The extraction spec is not valid.",
              "errors": [{"path": "$.termination.notice_days", "message": "description is required: say what the field holds so the extractor can find it."},
                         {"path": "$.fee_schedule", "message": "array fields need an 'items' object that describes one element (with a type)."}],
              "warnings": []}}
  ```
* `POST /extract/validate-spec` with `{"spec": {...}}` runs the same check without a document. It always returns
  `200`, with `{"valid", "errors", "warnings", "field_count", "fields": [{"path", "type", "description"}]}`.

**Shaping of values.** Every value is coerced to the declared type:
* `"$1,250.50"` becomes `1250.5`; `"Yes"` becomes `true`.
* An enum value is matched case-insensitively.
* A value that cannot be coerced becomes `null`.
* Keys keep the order of your spec. Fields not found are `null`.

### 6.2 Approaches

| `approach` | How it works | Best for |
|---|---|---|
| `agentic` (default) | The document is mapped (table of contents from bookmarks or a contents page, with printed page numbers calibrated to physical pages), chunked by document type, and indexed with keyword and semantic search. A reasoning agent navigates it with retrieval tools, records each field with its evidence pages, and compacts its own context when it grows | Long documents; lowest cost on large files |
| `batch` | The document is read in order, in parts sized to the model context; the JSON found so far is carried into the next part | Short documents; whole-document reading |

### 6.3 Endpoints and options

| Method and path | Returns |
|---|---|
| `GET /extract/options` | Approaches, available engines (from the model catalog, with context windows and prices), the default engine, limits, the context compaction threshold and an example spec |
| `POST /extract/validate-spec` | Validation report (6.1) |
| `POST /extract/jobs` | `202` with the job id |
| `POST /extract/sync` | The job object when finished |
| `POST /extract/text` | JSON in, JSON out for text you already have |

Form fields for `/extract/jobs` and `/extract/sync`:

| Field | Type | Default | Description |
|---|---|---|---|
| `file` | file | required | The document |
| `spec` | JSON string | required | The spec (6.1) |
| `approach` | `agentic` or `batch` | `agentic` | 6.2 |
| `model` | `provider:model` | configured engine | One of `models[].key` from `GET /extract/options`, for example `anthropic:claude-sonnet-5-5` or `openai:gpt-6.2-sol` |
| `instructions` | string | none | Extra guidance, up to 4000 characters |
| `context_window` | integer | model window, capped by the server | Upper bound in tokens on what the engine may use per request (minimum 16000) |
| `ocr_mode` | `auto`, `always`, `never` | `auto` | PDF only |

`POST /extract/text` body:

```json
{"text": "Invoice INV-2207 from Acme Medical Supply, dated 2026-08-14. Total due: $4,310.75. Net 30.",
 "spec": {"properties": {"invoice_number": {"type": "string", "description": "Invoice identifier"},
                         "total_usd": {"type": "number", "description": "Total amount due in US dollars"}}},
 "approach": "batch", "model": null, "instructions": null, "context_window": null, "file_name": "invoice.txt"}
```

Response: `{"job_id", "data", "fields", "summary", "engine", "usage", "warnings", "duration_ms"}`.

### 6.4 Result (`result_data`)

| Key | Content |
|---|---|
| `data` | The extracted JSON, in the shape of your spec. Also downloadable from `GET /jobs/{id}/file` as `<name>.extracted.json` |
| `fields` | One entry per field: `path` (dotted, for example `termination.notice_days`), `type`, `status` (`found` or `not_found`), `value`, `pages` (evidence pages), `confidence` (`high`, `medium`, `low`, agentic only), `note` |
| `summary` | `total`, `found`, `not_found`, `coverage` (0 to 1) |
| `complete` | `false` when part of the document could not be processed (batch) or the agent stopped at a limit (agentic); `warnings` says why |
| `document` | Pages, OCR pages, and for `agentic` the contents map (`toc`: source, contents pages, page offset, sections with page ranges), chunking strategy and retrieval mode |
| `engine` | Model, its context window, the usable window, the compaction threshold and the fallback engine |
| `usage` | Tokens and cost (section 7) |
| `agent` | Agentic only: sessions (turns, tool calls, peak context, how each ended), compactions, handoff notes and the tool-call trace |
| `batch` | Batch only: the parts, their page ranges and the fields each one updated |
| `warnings`, `timings_ms` | |

`stats` holds `approach`, `fields`, `found`, `not_found`, `coverage`, `complete`, `pages`, `model`, `sessions`,
`compactions` and `usage`.

## 7. Usage and cost

Every job reports the language model calls it made, the tokens and an estimated cost, in the same shape for every
service. It appears in three places:
* the top-level `usage` field of the job object;
* `stats.usage`;
* the inline responses of `/phi/redact-text` and `/extract/text`.

```json
{
  "total": {"calls": 8, "input_tokens": 9021, "output_tokens": 3874, "cache_read_tokens": 40112,
            "cache_write_tokens": 13168, "cost_usd": 0.112978, "priced": true},
  "by_model": {
    "anthropic:claude-sonnet-5-5": {"calls": 8, "input_tokens": 9021, "output_tokens": 3874,
                                    "cache_read_tokens": 40112, "cache_write_tokens": 13168,
                                    "cost_usd": 0.112978, "priced": true}
  }
}
```

| Field | Meaning |
|---|---|
| `calls` | Language model requests, including retries served by a fallback engine |
| `input_tokens` | Prompt tokens not served from a cache |
| `cache_read_tokens`, `cache_write_tokens` | Prompt tokens read from or written to the provider's prompt cache (priced differently) |
| `output_tokens` | Generated tokens, including reasoning tokens where the provider reports them |
| `cost_usd` | Estimated cost in US dollars at the list prices in the platform's model catalog, including long-context price tiers and announced price changes. `null` when a model is not in the catalog |
| `priced` | `false` when `cost_usd` could not be computed for every call |
| `by_model` | The same counters per engine (`provider:model`), so a job that failed over shows both engines |

**Usage by service**

| Service | Usage |
|---|---|
| PHI redaction | Detection calls |
| Structured parsing | Figure description calls (zero when `ai_summaries=false`) |
| OCR mode | Always zero |
| Data extraction | Contents structuring plus the agent or batch calls |

Local engines (Ollama) count tokens at no cost. The figures are estimates for monitoring and chargeback; your
provider invoice is authoritative.

## 8. Errors

Errors use the FastAPI shape: `{"detail": "<message>"}`, or an object for structured errors.

| Status | When |
|---|---|
| `400` | Invalid option value (for example an unknown `approach`, a `model` that is not available, bad `custom_entities` JSON) |
| `401` | Missing, invalid, expired or revoked credentials. `WWW-Authenticate: Bearer` is set |
| `403` | The credential cannot use this service, the account must change its password first, or an API key was used on an account endpoint |
| `404` | Unknown job, or a job owned by someone else |
| `409` | The output is not available yet, or the job cannot be deleted while processing |
| `410` | The output file has expired (`FILE_RETENTION_DAYS`) |
| `413` | Upload larger than `MAX_UPLOAD_MB`, or text longer than `MAX_TEXT_CHARS` |
| `415` | File type not supported by the service (checked by content, not only by extension) |
| `422` | Request body validation failed; an invalid extraction spec (`detail.errors`); or a synchronous job that ended `FAILED`: `{"detail": {"job_id", "status": "FAILED", "error"}}` |
| `429` | Too many failed sign-ins (account locked for 15 minutes) |
| `503` | The processing queue is unavailable; retry with backoff |
| `504` | A synchronous request exceeded `SYNC_TIMEOUT_SECONDS`; the job continues and can be polled |

**Retries**
* Retry `503`, `504` (by polling the job, not by resubmitting) and network errors with exponential backoff.
* Do not retry `4xx` without changing the request.
* A job that ended `FAILED` keeps its public error in `error`.

## 9. Limits, retention and security

| Setting | Default | Meaning |
|---|---|---|
| `MAX_UPLOAD_MB` | 50 | Largest upload |
| `MAX_TEXT_CHARS` | 500000 | Largest text for `/phi/redact-text` and `/extract/text` |
| `SYNC_TIMEOUT_SECONDS` | 900 | How long synchronous endpoints wait |
| `FILE_RETENTION_DAYS` | 7 | How long outputs are kept |
| `EXTRACT_SPEC_MAX_FIELDS`, `EXTRACT_SPEC_MAX_DEPTH` | 300, 6 | Extraction spec limits |

* **Uploads.** Uploaded originals are deleted as soon as a job ends, whether it completed or failed.
* **Outputs.** Outputs are kept for the retention period, then purged. Delete a job earlier with
  `DELETE /jobs/{id}`.
* **Data sent to model providers.** Document text is sent to the configured language model engines.
  * Ask the platform administrator which engines are configured; `GET /extract/options` lists them for extraction.
  * Hosted engines that receive PHI require a Business Associate Agreement with that provider.
* **Audit.** Every submission, download and account change is written to the audit log with the actor and IP
  address.
* **Prompt injection.** Text inside documents is treated as data. Instructions found in a document are not
  followed.
* **Transport.** Use HTTPS in production. The platform sets security headers on every response.

## 10. Account and key management endpoints

These require a signed-in session (not an API key).

| Method and path | Who | Description |
|---|---|---|
| `GET /auth/me` | Any | Current account, roles, services |
| `POST /auth/change-password` | Any | `{"current_password", "new_password"}` (at least 10 characters with a letter and a digit); returns new tokens |
| `GET /api-keys` | Any | Your keys (`?all_users=true` for administrators) |
| `POST /api-keys` | Any | `{"name", "scopes": ["extract"], "expires_in_days": 90}` returns `201` with `{"api_key": "idp_...", "key": {...}}`. The `api_key` value is shown only here |
| `DELETE /api-keys/{id}` | Owner or admin | Revokes a key (`204`) |
| `GET/POST/PATCH/DELETE /users...` | Admin | User administration (see `/api/docs`) |
| `GET /admin/system`, `/admin/audit-logs` | Admin | Health of components and engines; audit trail |
| `GET /admin/llm-config` | Admin | Engine of every service (`phi`, `doc`, `extract`, `extract_toc`, `extract_embed`) with its origin (`env` or `admin`), the providers with key status and the models that can be chosen |
| `PUT /admin/llm-config/{service}` | Admin | Change a service, for example `{"primary": {"provider": "ollama", "model": "qwen3.5:4b"}, "fallback": null}` for PHI detection, or `{"mode": "hybrid", "engine": {"provider": "openai", "model": "gpt-6-luna"}}` for index detection. Applies to new jobs within seconds; `400` with the reason when a provider has no key or a model is not offered |
| `POST /admin/llm-config/{service}/reset` | Admin | Restore the .env values |
| `POST /admin/llm-config/{service}/test` | Admin | Check that the configured engines respond |

## 11. End-to-end examples

### curl: extract key terms from a contract, asynchronously

```bash
API=https://idp.example.com/api/v1
KEY=idp_...   # scope: extract

JOB=$(curl -s -X POST "$API/extract/jobs" -H "X-API-Key: $KEY" \
  -F "file=@provider-agreement.pdf" \
  -F "spec=<contract.schema.json" \
  -F "approach=agentic" | jq -r .job_id)

while :; do
  STATUS=$(curl -s "$API/jobs/$JOB" -H "X-API-Key: $KEY" | jq -r .status)
  [ "$STATUS" = COMPLETED ] || [ "$STATUS" = FAILED ] && break
  sleep 5
done

curl -s "$API/jobs/$JOB" -H "X-API-Key: $KEY" | jq '{status, data: .result_data.data, cost: .usage.total.cost_usd}'
curl -s "$API/jobs/$JOB/file" -H "X-API-Key: $KEY" -o contract.extracted.json
```

### curl: redact a Word document and keep the file

```bash
curl -X POST "$API/phi/redact" -H "X-API-Key: $PHI_KEY" \
  -F "file=@discharge-summary.docx" \
  -F 'custom_entities=[{"label":"EMPLOYEE_ID","description":"Internal employee numbers such as EMP-48213"}]' \
  -F "response_format=file" -D headers.txt -o discharge-summary.redacted.docx
grep -i x-job-id headers.txt   # then GET /jobs/<id>/metadata for the detection report
```

### Python (requests)

```python
import json
import time

import requests

API = "https://idp.example.com/api/v1"
session = requests.Session()
session.headers["X-API-Key"] = "idp_..."  # read it from your secret store


def run_job(path: str, endpoint: str, data: dict, timeout_s: int = 1800) -> dict:
    with open(path, "rb") as f:
        res = session.post(f"{API}{endpoint}", files={"file": (path, f)}, data=data, timeout=60)
    if res.status_code == 422:
        raise ValueError(res.json()["detail"])  # invalid spec: detail["errors"] lists each problem
    res.raise_for_status()
    job_id = res.json()["job_id"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = session.get(f"{API}/jobs/{job_id}", timeout=30).json()
        if job["status"] in ("COMPLETED", "FAILED"):
            return job
        time.sleep(3)
    raise TimeoutError(job_id)


spec = json.load(open("contract.schema.json"))
job = run_job("provider-agreement.pdf", "/extract/jobs", {"spec": json.dumps(spec), "approach": "agentic"})
if job["status"] == "FAILED":
    raise RuntimeError(job["error"])
print(job["result_data"]["data"])
print("not found:", [f["path"] for f in job["result_data"]["fields"] if f["status"] == "not_found"])
print("cost USD:", job["usage"]["total"]["cost_usd"])
```

### Postman

Import `/api/openapi.json`. Add a collection variable `apiKey` and the header `X-API-Key: {{apiKey}}` at the
collection level.

## 12. Versioning and compatibility

* The API is versioned in the path (`/api/v1`). Within `v1`, changes are additive: new endpoints, new optional
  request fields, new response fields. Clients must ignore response fields they do not know.
* Result documents carry a `schema_version`. PHI metadata is `2.0`; the extraction result is `1.0`.
* Engines, models and prices change over time. Read `GET /extract/options` rather than hard-coding model names.
