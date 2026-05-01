"""
Central service status reporter for Grafana.

The EEP API is the stable service Prometheus can already scrape.  It reports
whether the live ECS and RunPod targets are reachable so Grafana does not need
to infer service health from a mix of static scrape targets and dynamic URLs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping

import httpx

from shared.metrics import LIBRARYAI_SERVICE_UP

logger = logging.getLogger(__name__)

_RUNPOD_PORTS = {
    "iep0": 8006,
    "iep1a": 8001,
    "iep1b": 8002,
}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("service_status: invalid %s=%r; using %.1f", name, raw, default)
        return default


def _static_targets() -> dict[str, str]:
    return {
        "eep": "http://localhost:8000/health",
        "eep-worker": "http://eep-worker:9100/health",
        "eep-recovery": "http://eep-recovery:9101/health",
        "shadow-worker": "http://shadow-worker:9102/health",
        "retraining-worker": "http://retraining-worker:9104/health",
        "iep1d": "http://iep1d:8003/health",
        "iep1e": "http://iep1e:8007/health",
        "iep2a": "http://iep2a-v2:8004/health",
        "iep2b": "http://iep2b:8005/health",
    }


def _runpod_pod_ids_from_env() -> dict[str, str]:
    ids = {
        "iep0": os.environ.get("RUNPOD_IEP0_POD_ID", "").strip(),
        "iep1a": os.environ.get("RUNPOD_IEP1A_POD_ID", "").strip(),
        "iep1b": os.environ.get("RUNPOD_IEP1B_POD_ID", "").strip(),
    }
    return {service: pod_id for service, pod_id in ids.items() if pod_id}


def _read_runpod_pod_ids_from_s3() -> dict[str, str]:
    bucket = os.environ.get("S3_BUCKET_NAME", "libraryai2")
    key = os.environ.get("RUNPOD_PODS_STATE_KEY", "ops/runpod-pods.json")
    region = os.environ.get("AWS_REGION") or os.environ.get("S3_REGION") or "eu-central-1"

    try:
        import boto3  # noqa: PLC0415

        response = boto3.client("s3", region_name=region).get_object(Bucket=bucket, Key=key)
        data = json.loads(response["Body"].read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("service_status: no RunPod pod state available: %s", exc)
        return {}

    if not isinstance(data, Mapping):
        return {}
    return {
        service: str(data.get(service, "")).strip()
        for service in _RUNPOD_PORTS
        if str(data.get(service, "")).strip()
    }


def _runpod_targets() -> dict[str, str]:
    pod_ids = _read_runpod_pod_ids_from_s3() or _runpod_pod_ids_from_env()
    return {
        service: f"https://{pod_id}-{_RUNPOD_PORTS[service]}.proxy.runpod.net/health"
        for service, pod_id in pod_ids.items()
        if service in _RUNPOD_PORTS
    }


async def _probe(client: httpx.AsyncClient, service: str, url: str) -> None:
    try:
        response = await client.get(url)
        LIBRARYAI_SERVICE_UP.labels(service=service).set(1 if response.is_success else 0)
    except httpx.RequestError:
        LIBRARYAI_SERVICE_UP.labels(service=service).set(0)


async def run_service_status_loop() -> None:
    interval_seconds = max(5.0, _env_float("SERVICE_STATUS_INTERVAL_SECONDS", 15.0))
    timeout_seconds = max(1.0, _env_float("SERVICE_STATUS_TIMEOUT_SECONDS", 5.0))
    all_services = set(_static_targets()) | set(_RUNPOD_PORTS)
    for service in sorted(all_services):
        LIBRARYAI_SERVICE_UP.labels(service=service).set(0)

    logger.info("service_status: status loop started services=%s", sorted(all_services))
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        while True:
            targets = _static_targets()
            targets.update(_runpod_targets())
            missing_runpod = set(_RUNPOD_PORTS) - set(targets)
            for service in missing_runpod:
                LIBRARYAI_SERVICE_UP.labels(service=service).set(0)
            await asyncio.gather(*(_probe(client, service, url) for service, url in targets.items()))
            await asyncio.sleep(interval_seconds)
