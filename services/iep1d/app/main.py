"""
services/iep1d/app/main.py
--------------------------
IEP1D — UVDoc rectification fallback service.
Phase 0 skeleton: health/ready/metrics are live.
Packet 4.5: POST /v1/rectify pass-through mock implemented.
"""

from fastapi import FastAPI

from services.iep1d.app.rectify import router as rectify_router
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep1d")

app = FastAPI(
    title="IEP1D — UVDoc Rectification",
    version="0.1.0",
    description=(
        "Rectification fallback service using UVDoc. Rescues pages where "
        "deterministic normalization produces an artifact that fails validation "
        "(warped pages, strong curl, perspective-heavy captures). "
        "Does not decide split; does not replace IEP1A/IEP1B as geometry source."
    ),
)

configure_observability(app, service_name="iep1d")

app.include_router(rectify_router)
