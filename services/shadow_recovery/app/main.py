"""
services/shadow_recovery/app/main.py
--------------------------------------
Shadow Recovery — shadow task reconciliation service.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  Shadow reconciliation loop  → Phase 8 (Packet 8.4)
"""

from fastapi import FastAPI

from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="shadow_recovery")

app = FastAPI(
    title="Shadow Recovery",
    version="0.1.0",
    description=(
        "Periodic reconciliation service for shadow inference tasks. "
        "Detects and re-enqueues abandoned or stuck shadow tasks so that "
        "shadow result coverage remains complete for MLOps evaluation."
    ),
)

configure_observability(app, service_name="shadow_recovery")

# Shadow reconciliation loop implemented in Phase 8 (Packet 8.4)
