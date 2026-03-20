"""
services/eep/app/main.py
------------------------
EEP — Execution Engine Pipeline API service.
Phase 0 skeleton: health/ready/metrics are live; all business logic is stubbed.

Real implementations:
  POST /v1/auth/token          → Phase 7 (Packet 7.1)
  POST /v1/uploads/jobs/presign → LIVE (Packet 1.7b)
  POST /v1/jobs                → LIVE (Packet 1.8)
  GET  /v1/jobs/{job_id}       → LIVE (Packet 1.9)
"""

from fastapi import FastAPI

from services.eep.app.jobs.create import router as jobs_router
from services.eep.app.jobs.status import router as job_status_router
from services.eep.app.uploads import router as uploads_router
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

# Must be called before app is created so uvicorn log capture is configured
setup_logging(service_name="eep")

app = FastAPI(
    title="EEP — Execution Engine Pipeline",
    version="0.1.0",
    description=(
        "Central orchestrator for the LibraryAI processing pipeline. "
        "Owns job management, page routing, quality gates, artifact persistence, "
        "lineage recording, and all acceptance decisions."
    ),
)

configure_observability(app, service_name="eep")

# ── Phase 1 routers ────────────────────────────────────────────────────────────
app.include_router(uploads_router)
app.include_router(jobs_router)
app.include_router(job_status_router)

# ── Phase 0 placeholder endpoints ─────────────────────────────────────────────
# These stubs satisfy the Phase 0 definition of done ("EEP placeholder endpoints
# exist"). They are replaced with real implementations in the phases listed above.


@app.post("/v1/auth/token", tags=["auth"], summary="[stub] Issue JWT token")
async def auth_token_placeholder() -> dict[str, str]:
    return {"detail": "not implemented — Phase 7 (Packet 7.1)"}
