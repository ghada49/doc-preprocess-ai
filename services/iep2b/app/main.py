"""
services/iep2b/app/main.py
--------------------------
IEP2B — DocLayout-YOLO layout detection service.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  POST /v1/layout-detect  → Phase 6 (Packets 6.3, 6.4)
"""

from fastapi import FastAPI

from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep2b")

app = FastAPI(
    title="IEP2B — DocLayout-YOLO Layout Detection",
    version="0.1.0",
    description=(
        "Layout detection service using DocLayout-YOLO "
        "(DocStructBench-aligned class vocabulary). Fast second-opinion detector. "
        "Maps native output classes to the canonical LibraryAI 5-class schema "
        "before returning LayoutDetectResponse."
    ),
)

configure_observability(app, service_name="iep2b")

# POST /v1/layout-detect implemented in Phase 6 (Packet 6.3)
