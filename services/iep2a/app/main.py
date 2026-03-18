"""
services/iep2a/app/main.py
--------------------------
IEP2A — Detectron2 layout detection service.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  POST /v1/layout-detect  → Phase 6 (Packets 6.1, 6.2)
"""

from fastapi import FastAPI

from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep2a")

app = FastAPI(
    title="IEP2A — Detectron2 Layout Detection",
    version="0.1.0",
    description=(
        "Layout detection service using Detectron2 Faster R-CNN "
        "(ResNet-50-FPN, PubLayNet weights). Primary layout detector. "
        "Canonical 5-class schema: text_block, title, table, image, caption. "
        "Column structure inferred via DBSCAN on text_block x-centroids."
    ),
)

configure_observability(app, service_name="iep2a")

# POST /v1/layout-detect implemented in Phase 6 (Packet 6.1)
