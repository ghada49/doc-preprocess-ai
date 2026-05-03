"""
RunPod startup helper for on-demand retraining.

EEP owns the control plane: it creates the durable retraining trigger and job
in the database, then calls RunPod to start one GPU pod that runs a one-shot
worker. The pod receives only public callback and S3/model configuration, then
reports completion back to EEP. It does not need private AWS DB/Redis access.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_RUNPOD_PODS_URL = "https://rest.runpod.io/v1/pods"


class RunPodStartError(RuntimeError):
    """Raised when RunPod pod creation fails."""


def start_retraining_pod(trigger_id: str, *, job_id: str | None = None) -> tuple[str, str]:
    """
    Create a RunPod GPU pod for retraining.

    Returns:
        (pod_id, message)

    Raises:
        RunPodStartError when required config is missing or the API call fails.
    """
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        raise RunPodStartError("RUNPOD_API_KEY is not set.")

    image_name = os.environ.get(
        "RUNPOD_IMAGE",
        "gma51/libraryai-retraining-worker:latest",
    ).strip()
    if not image_name:
        raise RunPodStartError("RUNPOD_IMAGE is not set.")

    payload = _build_create_pod_payload(trigger_id, image_name, job_id=job_id)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=float(os.environ.get("RUNPOD_API_TIMEOUT_SECONDS", "30"))) as client:
            response = client.post(_RUNPOD_PODS_URL, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        raise RunPodStartError(
            f"RunPod create pod failed with HTTP {exc.response.status_code}: {body}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RunPodStartError(f"RunPod create pod request failed: {exc}") from exc

    pod_id = _extract_pod_id(data)
    if not pod_id:
        raise RunPodStartError(f"RunPod create pod response did not include a pod id: {data}")

    logger.info(
        "runpod_scaler: created retraining pod_id=%s image=%s trigger_id=%s",
        pod_id,
        image_name,
        trigger_id,
    )
    return pod_id, f"RunPod retraining pod created: {pod_id}."


def terminate_retraining_pod(pod_id: str) -> None:
    """Best-effort RunPod pod termination used after callback completion."""
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key or not pod_id:
        return
    try:
        response = httpx.delete(
            f"{_RUNPOD_PODS_URL}/{pod_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(os.environ.get("RUNPOD_API_TIMEOUT_SECONDS", "30")),
        )
        response.raise_for_status()
        logger.info("runpod_scaler: terminated retraining pod_id=%s", pod_id)
    except Exception:
        logger.exception("runpod_scaler: failed to terminate retraining pod_id=%s", pod_id)


def _build_create_pod_payload(
    trigger_id: str,
    image_name: str,
    *,
    job_id: str | None = None,
) -> dict[str, Any]:
    gpu_types = _csv_env(
        "RUNPOD_GPU_TYPES",
        "NVIDIA GeForce RTX 3090,NVIDIA RTX A5000,NVIDIA GeForce RTX 4090",
    )

    payload: dict[str, Any] = {
        "name": _pod_name(trigger_id),
        "imageName": image_name,
        "computeType": "GPU",
        "cloudType": os.environ.get("RUNPOD_CLOUD_TYPE", "SECURE").strip() or "SECURE",
        "gpuCount": int(os.environ.get("RUNPOD_GPU_COUNT", "1")),
        "gpuTypeIds": gpu_types,
        "gpuTypePriority": os.environ.get("RUNPOD_GPU_TYPE_PRIORITY", "availability").strip()
        or "availability",
        "interruptible": _bool_env("RUNPOD_INTERRUPTIBLE", default=False),
        "containerDiskInGb": int(os.environ.get("RUNPOD_CONTAINER_DISK_GB", "80")),
        "volumeInGb": int(os.environ.get("RUNPOD_VOLUME_GB", "50")),
        "volumeMountPath": os.environ.get("RUNPOD_VOLUME_MOUNT_PATH", "/workspace"),
        "minVCPUPerGPU": int(os.environ.get("RUNPOD_MIN_VCPU_PER_GPU", "4")),
        "minRAMPerGPU": int(os.environ.get("RUNPOD_MIN_RAM_PER_GPU", "16")),
        "env": _runpod_env(trigger_id, job_id=job_id),
    }

    registry_auth_id = os.environ.get("RUNPOD_CONTAINER_REGISTRY_AUTH_ID", "").strip()
    if registry_auth_id:
        payload["containerRegistryAuthId"] = registry_auth_id

    data_center_ids = _csv_env("RUNPOD_DATA_CENTER_IDS", "")
    if data_center_ids:
        payload["dataCenterIds"] = data_center_ids
        payload["dataCenterPriority"] = os.environ.get(
            "RUNPOD_DATA_CENTER_PRIORITY",
            "availability",
        ).strip() or "availability"

    return payload


def _runpod_env(trigger_id: str, *, job_id: str | None = None) -> dict[str, str]:
    callback_url = _callback_url()
    callback_secret = os.environ.get("RETRAINING_CALLBACK_SECRET", "").strip()
    if not callback_secret:
        raise RunPodStartError("RETRAINING_CALLBACK_SECRET is not set.")

    env: dict[str, str] = {
        "RETRAINING_WORKER_MODE": "callback_once",
        "RETRAINING_TRIGGER_ID": trigger_id,
        "RETRAINING_JOB_ID": job_id or "",
        "RETRAINING_CALLBACK_URL": callback_url,
        "RETRAINING_CALLBACK_SECRET": callback_secret,
        "PYTHONPATH": "/app",
        "HEALTH_PORT": os.environ.get("RETRAINING_HEALTH_PORT", "9104"),
        # callback_once mode supports stub retraining only — live training
        # runs via ECS db_poll mode (retraining-worker task def).
        "LIBRARYAI_RETRAINING_TRAIN": "stub",
        "LIBRARYAI_RETRAINING_GOLDEN_EVAL": "stub",
        "RETRAINING_DATASET_MODE": os.environ.get(
            "RETRAINING_DATASET_MODE",
            "corrected_hybrid",
        ),
        "RETRAINING_DATASET_REGISTRY_PATH": os.environ.get(
            "RETRAINING_DATASET_REGISTRY_PATH",
            "s3://libraryai2/retraining/dataset_registry.json",
        ),
        "RUNPOD_TERMINATE_ON_IDLE": os.environ.get("RUNPOD_TERMINATE_ON_IDLE", "true"),
        "S3_BUCKET_NAME": os.environ.get("S3_BUCKET_NAME", "libraryai2"),
        "AWS_REGION": os.environ.get("AWS_REGION", os.environ.get("S3_REGION", "eu-central-1")),
        "S3_REGION": os.environ.get("S3_REGION", os.environ.get("AWS_REGION", "eu-central-1")),
    }

    pass_through = [
        "IEP1A_WEIGHTS_URI",
        "IEP1B_WEIGHTS_URI",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_ENDPOINT_URL",
        "RETRAINING_DATASET_VERSION",
        "RETRAINING_DATASET_BUILDER_CMD",
        "RETRAINING_DATASET_BUILDER_TIMEOUT",
        "RETRAINING_TRAIN_MANIFEST",
        "RETRAINING_DEVICE",
        "RETRAINING_QUICK",
        "RETRAINING_SUBPROCESS_TIMEOUT",
        "GOLDEN_SKIP_CROSS_MODEL",
    ]

    for name in pass_through:
        value = os.environ.get(name)
        if value:
            env[name] = value

    # boto3 uses AWS_* names. Keep compatibility with the existing S3_* secret
    # names so the RunPod pod can access private S3 objects without DB access.
    if "AWS_ACCESS_KEY_ID" not in env and env.get("S3_ACCESS_KEY_ID"):
        env["AWS_ACCESS_KEY_ID"] = env["S3_ACCESS_KEY_ID"]
    if "AWS_SECRET_ACCESS_KEY" not in env and env.get("S3_SECRET_ACCESS_KEY"):
        env["AWS_SECRET_ACCESS_KEY"] = env["S3_SECRET_ACCESS_KEY"]

    return env


def _callback_url() -> str:
    base = (
        os.environ.get("RETRAINING_CALLBACK_BASE_URL", "").strip()
        or os.environ.get("PUBLIC_EEP_BASE_URL", "").strip()
    )
    if not base:
        raise RunPodStartError(
            "RETRAINING_CALLBACK_BASE_URL is not set; RunPod needs a public EEP callback URL."
        )
    return f"{base.rstrip('/')}/v1/retraining/runpod/callback"


def _extract_pod_id(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("id", "podId", "pod_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    nested = data.get("pod")
    if isinstance(nested, dict):
        return _extract_pod_id(nested)
    return None


def _pod_name(trigger_id: str) -> str:
    prefix = os.environ.get("RUNPOD_POD_NAME_PREFIX", "libraryai-retraining").strip()
    return f"{prefix}-{trigger_id[:8]}"


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
