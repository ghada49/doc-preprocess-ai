"""
services/eep_recovery/app/main.py
----------------------------------
EEP Recovery — stuck-task reconciliation service.

Packet 4.7: reconciliation loop wired up via run_reconciliation_loop().
Full integration with live Redis and DB session factory is wired at Phase 8
(Packet 8.4) when MLOps infrastructure is available.  The reconciler module
is fully implemented and testable independently.
"""

from fastapi import FastAPI

from services.eep_recovery.app.reconciler import (  # noqa: F401 — exported for Phase 8 wiring
    ReconcilerConfig,
    ReconciliationResult,
    reconcile_once,
    run_reconciliation_loop,
)
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
