"""
services/eep/app/main.py
------------------------
EEP — Execution Engine Pipeline API service.

Real implementations:
  POST /v1/auth/token          → LIVE (Packet 7.1)
  POST /v1/uploads/jobs/presign → LIVE (Packet 1.7b)
  POST /v1/jobs                → LIVE (Packet 1.8)
  GET  /v1/jobs/{job_id}       → LIVE (Packet 1.9)
  GET  /v1/jobs                → LIVE (Packet 7.3)
"""

from fastapi import FastAPI

from services.eep.app.auth import router as auth_router
from services.eep.app.correction.apply import router as correction_apply_router
from services.eep.app.correction.ptiff_qa import router as ptiff_qa_router
from services.eep.app.correction.queue import router as correction_queue_router
from services.eep.app.correction.reject import router as correction_reject_router
from services.eep.app.jobs.create import router as jobs_router
from services.eep.app.jobs.list import router as job_list_router
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

# ── Phase 7 routers ────────────────────────────────────────────────────────────
app.include_router(auth_router)

# ── Phase 1 routers ────────────────────────────────────────────────────────────
app.include_router(uploads_router)
app.include_router(jobs_router)
app.include_router(job_list_router)
app.include_router(job_status_router)

# ── Phase 5 routers ────────────────────────────────────────────────────────────
app.include_router(ptiff_qa_router)
app.include_router(correction_queue_router)
app.include_router(correction_apply_router)
app.include_router(correction_reject_router)
