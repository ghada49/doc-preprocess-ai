"""
services/shadow_worker/app/main.py
------------------------------------
Shadow Worker — shadow inference background worker.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  Shadow inference loop  → Phase 8 (Packet 8.4)
"""

from fastapi import FastAPI

from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="shadow_worker")

app = FastAPI(
    title="Shadow Worker",
    version="0.1.0",
    description=(
        "Background worker that runs candidate model versions in shadow mode "
        "alongside the production pipeline. Results are stored for offline "
        "comparison but never surface to end users. Feeds the MLOps promotion "
        "and rollback workflow."
    ),
)

configure_observability(app, service_name="shadow_worker")

# Shadow inference loop implemented in Phase 8 (Packet 8.4)
