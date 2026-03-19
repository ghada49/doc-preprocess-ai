"""
services/iep1a/app/main.py
--------------------------
IEP1A — YOLOv8-seg page geometry service.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  POST /v1/geometry  → Phase 2 (Packets 2.1, 2.2)
"""

from fastapi import FastAPI

from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep1a")

app = FastAPI(
    title="IEP1A — YOLOv8-seg Geometry",
    version="0.1.0",
    description=(
        "Page geometry service using YOLOv8-seg instance segmentation. "
        "Predicts page regions as segmentation masks; geometry is derived from "
        "mask contours. Mock inference — real model loaded in Phase 12."
    ),
)

configure_observability(app, service_name="iep1a")

# POST /v1/geometry implemented in Phase 2 (Packet 2.1)
