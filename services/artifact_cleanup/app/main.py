"""
services/artifact_cleanup/app/main.py
---------------------------------------
Artifact Cleanup — expired artifact garbage collection service.
Phase 0 skeleton: health/ready/metrics are live.

Real implementation:
  Cleanup loop  → Phase 9 (Packet 9.5)
"""

from fastapi import FastAPI

from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="artifact_cleanup")

app = FastAPI(
    title="Artifact Cleanup",
    version="0.1.0",
    description=(
        "Maintenance process that garbage-collects expired artifacts from "
        "object storage (MinIO / S3) after the configured grace period "
        "(artifact_cleanup_grace_hours). Removes OTIFF inputs, intermediate "
        "artifacts, and shadow inference results that are no longer needed."
    ),
)

configure_observability(app, service_name="artifact_cleanup")

# Cleanup loop implemented in Phase 9 (Packet 9.5)
