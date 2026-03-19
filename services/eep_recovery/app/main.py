"""
services/eep_recovery/app/main.py
----------------------------------
EEP Recovery — stuck-task reconciliation service.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  Reconciliation loop  → Phase 4 (Packet 4.7)
"""

from fastapi import FastAPI

from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="eep_recovery")

app = FastAPI(
    title="EEP Recovery",
    version="0.1.0",
    description=(
        "Periodic reconciliation service that compares DB page states against "
        "Redis queue contents to detect and re-enqueue abandoned, stuck, or "
        "orphaned tasks. Complements the EEP worker watchdog."
    ),
)

configure_observability(app, service_name="eep_recovery")

# Reconciliation loop implemented in Phase 4 (Packet 4.7)
