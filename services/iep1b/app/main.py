"""
services/iep1b/app/main.py
--------------------------
IEP1B — YOLOv8-pose page geometry service.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  POST /v1/geometry  → Phase 2 (Packets 2.3, 2.4)
"""

from fastapi import FastAPI

from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep1b")

app = FastAPI(
    title="IEP1B — YOLOv8-pose Geometry",
    version="0.1.0",
    description=(
        "Page geometry service using YOLOv8-pose keypoint regression. "
        "Predicts page corners directly as coordinate keypoints; provides "
        "geometry from a fundamentally different representation than IEP1A. "
        "Mock inference — real model loaded in Phase 12."
    ),
)

configure_observability(app, service_name="iep1b")

# POST /v1/geometry implemented in Phase 2 (Packet 2.3)
