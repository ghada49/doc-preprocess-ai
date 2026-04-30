"""
On-demand retraining worker startup.

The admin retraining button creates a durable retraining trigger first, then
uses this helper to wake the retraining worker when configured.

Configuration:
  RETRAINING_WORKER_START_MODE     disabled | ecs_service | runpod_pod
  ECS_CLUSTER                      ECS cluster name
  RETRAINING_WORKER_ECS_SERVICE    ECS service name (default: libraryai-retraining-worker)
  RETRAINING_WORKER_DESIRED_COUNT  Desired count to set (default: 1)
  AWS_REGION                       AWS region (default: us-east-1)
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from services.eep.app.scaling.runpod_scaler import RunPodStartError, start_retraining_pod

logger = logging.getLogger(__name__)

RetrainingWorkerStartStatus = Literal["disabled", "skipped", "requested", "failed"]


def maybe_start_retraining_worker(
    trigger_id: str,
    *,
    job_id: str | None = None,
) -> tuple[RetrainingWorkerStartStatus, str, str | None]:
    """
    Start the retraining worker infrastructure when configured.

    Returns a status/message/external_id tuple. This function does not raise
    because a retraining trigger should remain queued even if worker startup is
    temporarily unavailable.
    """
    mode = os.environ.get("RETRAINING_WORKER_START_MODE", "disabled").strip().lower()
    if mode in {"", "disabled"}:
        return "disabled", "Retraining worker auto-start is disabled.", None

    if mode == "runpod_pod":
        try:
            pod_id, message = start_retraining_pod(trigger_id, job_id=job_id)
        except RunPodStartError as exc:
            message = str(exc)
            logger.error("retraining_scaler: %s", message)
            return "failed", message, None
        return "requested", message, pod_id

    if mode != "ecs_service":
        message = f"Unsupported RETRAINING_WORKER_START_MODE={mode!r}."
        logger.warning("retraining_scaler: %s", message)
        return "skipped", message, None

    cluster = os.environ.get("ECS_CLUSTER", "").strip()
    service = os.environ.get(
        "RETRAINING_WORKER_ECS_SERVICE",
        "libraryai-retraining-worker",
    ).strip()
    desired = int(os.environ.get("RETRAINING_WORKER_DESIRED_COUNT", "1"))
    region = os.environ.get("AWS_REGION", "us-east-1").strip() or "us-east-1"

    if not cluster:
        return "skipped", "ECS_CLUSTER is not set; retraining worker was not started.", None
    if not service:
        return "skipped", "RETRAINING_WORKER_ECS_SERVICE is not set.", None

    try:
        import boto3  # noqa: PLC0415

        client = boto3.client("ecs", region_name=region)
        client.update_service(
            cluster=cluster,
            service=service,
            desiredCount=desired,
        )
    except Exception as exc:  # noqa: BLE001
        message = f"Failed to start retraining worker: {exc}"
        logger.error("retraining_scaler: %s", message)
        return "failed", message, None

    message = f"Retraining worker start requested: {service} desired -> {desired}."
    logger.info(
        "retraining_scaler: cluster=%s service=%s desired=%d",
        cluster,
        service,
        desired,
    )
    return "requested", message, service
