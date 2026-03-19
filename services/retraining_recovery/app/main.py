"""
services/retraining_recovery/app/main.py
------------------------------------------
Retraining Recovery — retraining task reconciliation service.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  Retraining reconciliation loop  → Phase 8 (Packet 8.7)
"""

from fastapi import FastAPI

from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="retraining_recovery")

app = FastAPI(
    title="Retraining Recovery",
    version="0.1.0",
    description=(
        "Periodic reconciliation service for retraining trigger tasks. "
        "Detects and re-enqueues abandoned or stuck retraining events to "
        "ensure no trigger is silently lost."
    ),
)

configure_observability(app, service_name="retraining_recovery")

# Retraining reconciliation loop implemented in Phase 8 (Packet 8.7)
