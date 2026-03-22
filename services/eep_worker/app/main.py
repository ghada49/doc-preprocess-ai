"""
services/eep_worker/app/main.py
--------------------------------
EEP Worker — page-processing background worker.
Phase 0 skeleton: health/ready/metrics are live.

Packet 4.1–4.6: worker pipeline modules implemented.
Packet 4.7: in-process watchdog wired up via TaskWatchdog.
"""

from fastapi import FastAPI

from services.eep_worker.app.watchdog import (  # noqa: F401 — exported for Phase 8 wiring
    StaleTaskReport,
    TaskWatchdog,
    WatchdogConfig,
)
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="eep_worker")

app = FastAPI(
    title="EEP Worker",
    version="0.1.0",
    description=(
        "Background worker process that dequeues page tasks from Redis, "
        "orchestrates IEP1A/IEP1B geometry, IEP1C normalization, artifact "
        "validation, IEP1D rectification rescue, and IEP2A/IEP2B layout "
        "detection. Owns all page state transitions from queued to terminal states."
    ),
)

configure_observability(app, service_name="eep_worker")
