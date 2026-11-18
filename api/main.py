import logging

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from api.routers import admin, api_keys, auth, jobs, parse, phi, users
from shared.config import settings
from shared.database import SessionLocal
from shared.queue import redis_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger("api")

DESCRIPTION = """
Intelligent Document Processing platform: **PHI redaction** and **document parsing**.

**Authentication.** Every endpoint under `/api/v1` requires one of:

* `X-API-Key: idp_...` for service-to-service integration (create keys under *API keys*), or
* `Authorization: Bearer <access token>` obtained from `POST /api/v1/auth/login`.

**Working with jobs.** Submit with a `.../jobs` endpoint (returns `202` and a job id) and poll
`GET /api/v1/jobs/{job_id}`, or use the synchronous variants (`/phi/redact`, `/phi/redact-text`,
`/parse/sync`) which wait for the result.
"""

app = FastAPI(
    title="IDP Platform API",
    description=DESCRIPTION,
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Job-Id", "Content-Disposition"])

v1 = APIRouter(prefix="/api/v1")
for module in (auth, users, api_keys, phi, parse, jobs, admin):
    v1.include_router(module.router)
app.include_router(v1)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.get("/health", tags=["Health"], summary="Liveness probe")
def health():
    return {"status": "ok"}


@app.get("/ready", tags=["Health"], summary="Readiness probe (database and queue)")
def ready():
    checks = {"database": False, "redis": False}
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            checks["database"] = True
        finally:
            db.close()
    except Exception as exc:
        log.warning("readiness: database check failed: %s", exc)
    try:
        checks["redis"] = bool(redis_conn.ping())
    except Exception as exc:
        log.warning("readiness: redis check failed: %s", exc)
    ok = all(checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", **checks}, status_code=200 if ok else 503)
