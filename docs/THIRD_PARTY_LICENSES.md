# Third-party licences

This product is built so that it can be sold commercially without copyleft obligations on its own code.
Every component that processes documents, every model, and every service in the stack is under a
permissive licence. The exceptions are listed in full under *Items for legal review*.

The inventory below was produced from the built images, not from memory:
`tools/license_audit.py` reads the declared licence of every installed Python distribution, and
`npm ls --omit=dev` was used for the web bundle. Rerun both before each release.

## Document processing, OCR and models

| Component | Used for | Licence |
|---|---|---|
| PaddleOCR 3.7 / PaddleX 3.7 / PaddlePaddle 3.3 | OCR and layout parsing | Apache-2.0 |
| PP-OCRv6 small detection and recognition models | Text recognition | Apache-2.0 |
| PP-StructureV3 layout, table and formula models | Document parsing | Apache-2.0 |
| PDFium (via pypdfium2) | Reading, text extraction and rendering of PDFs | BSD-3-Clause (pypdfium2: Apache-2.0 or BSD-3-Clause) |
| reportlab | Writing redacted PDFs | BSD-3-Clause |
| Pillow | Image editing and encoding | MIT-CMU (HPND) |
| lxml | DOCX (Office Open XML) editing | BSD-3-Clause |
| rapidfuzz | Fuzzy matching of OCR text | MIT |
| Qwen3.5 4B (local fallback model) | PHI detection when the primary engine is unavailable | Apache-2.0 (confirm against the LICENSE file in the official Qwen model repository before release) |
| Ollama | Local model server | MIT |

**Removed during this rework:** PyMuPDF (AGPL-3.0 or Artifex commercial licence). It was the original
PDF engine. PDF redaction now re-renders each page with PDFium, paints the redactions into the pixels
and writes a new PDF with reportlab, with a searchable text layer that contains only unredacted words.

## Platform services

| Component | Licence |
|---|---|
| PostgreSQL 15 | PostgreSQL Licence (permissive) |
| Valkey 8 (queue) | BSD-3-Clause. Replaces Redis, which is RSALv2 / SSPLv1 from version 7.4 |
| nginx | BSD-2-Clause |
| FastAPI, Starlette, Pydantic, SQLAlchemy, Alembic, Uvicorn | MIT / BSD-3-Clause |
| pg8000 (PostgreSQL driver) | BSD-3-Clause. Replaces psycopg2 (LGPL) |
| RQ, redis-py (queue client) | BSD-2-Clause / MIT |
| PyJWT, bcrypt, httpx, boto3, anthropic SDK | MIT / Apache-2.0 / BSD-3-Clause |

## Web application

149 production packages: 139 MIT, 3 ISC, 2 Apache-2.0, 1 BSD-2-Clause, 1 0BSD, 1 MIT AND Zlib (pako),
1 MIT OR GPL-3.0 (jszip, used under MIT) and the Inter typeface (SIL Open Font License 1.1, which permits
embedding in commercial software). Notable packages: React, React Router, TanStack Query, Tailwind CSS,
Framer Motion, lucide-react (all MIT or ISC), PDF.js (Apache-2.0), docx-preview (Apache-2.0).

## Items for legal review

| Component | Licence | Why it is present | Notes |
|---|---|---|---|
| python-bidi | LGPL-3.0 | Hard requirement of PaddleOCR's text recogniser (right-to-left scripts) | Used unmodified as a separately installed package, so it can be replaced by the user, which is what LGPL requires. PaddleX refuses to build the OCR pipeline without it. |
| cssutils, encutils | LGPL-3.0 | Pulled in by premailer, which PaddleX lists as required for its `ocr` extra | Only used by PaddleX's table-to-Excel export, which this product never calls. PaddleX checks for the package before it builds PP-StructureV3, so it cannot be removed without patching PaddleX. Same LGPL position as above. |
| certifi, tqdm | MPL-2.0 | Common transitive dependencies (CA bundle, progress bars) | File-level copyleft that applies only to modified MPL files. Used unmodified. |
| NVIDIA CUDA and cuDNN runtime libraries | NVIDIA proprietary, redistributable | GPU inference (in the PaddlePaddle base image) | Redistribution is allowed under the NVIDIA CUDA and cuDNN licence terms; a CPU-only build avoids them. |
| Ubuntu base image packages | Various, including GPL | Operating system of the worker container | Distributed as separate programs in a container (mere aggregation); the product does not link to them. |

Removed from the worker image because nothing needs them: `bce-python-sdk` and `crc32c` (LGPL), which
PaddleX only uses to upload to Baidu cloud storage.

## Hosted model APIs

OpenAI, Azure OpenAI, Anthropic and Google Gemini are optional engines called over HTTPS. They are
governed by each provider's commercial terms, not by an open-source licence. If PHI is sent to a hosted
engine, a Business Associate Agreement with that provider is required under HIPAA. The local Qwen engine
keeps all document text inside your network.
