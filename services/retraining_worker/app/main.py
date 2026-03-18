"""
services/retraining_worker/app/main.py
----------------------------------------
Retraining Worker — model retraining trigger background worker.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  Retraining trigger loop  → Phase 8 (Packet 8.7)
"""

from fastapi import FastAPI

from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="retraining_worker")

app = FastAPI(
    title="Retraining Worker",
    version="0.1.0",
    description=(
        "Background worker that consumes retraining trigger events, records "
        "them in the DB, and dispatches to the configured MLOps backend. "
        "Supports webhook-driven and drift-triggered retraining initiation."
    ),
)

configure_observability(app, service_name="retraining_worker")

# Retraining trigger loop implemented in Phase 8 (Packet 8.7)
