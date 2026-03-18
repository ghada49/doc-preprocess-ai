"""
services/iep1d/app/main.py
--------------------------
IEP1D — UVDoc rectification fallback service.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  POST /v1/rectify  → Phase 4 (Packet 4.5)
"""

from fastapi import FastAPI

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

# POST /v1/rectify implemented in Phase 4 (Packet 4.5)
