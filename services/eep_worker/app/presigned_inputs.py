"""
Presigned input URL helpers for external inference services.

The worker runs inside AWS and can use its task role to read S3. RunPod pods
run outside the VPC, so they receive short-lived HTTPS GET URLs instead of
raw s3:// URIs or AWS credentials.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Iterable
from urllib.parse import urlparse

from shared.io.storage import S3Backend, rewrite_presigned_url_for_public_endpoint


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def is_runpod_endpoint(endpoint: str) -> bool:
    hostname = (urlparse(endpoint).hostname or "").lower()
    return "runpod" in hostname


def should_presign_for_endpoint(endpoint: str) -> bool:
    mode = os.environ.get("RUNPOD_PRESIGN_INPUTS", "auto").strip().lower()
    if mode in {"1", "true", "yes", "always"}:
        return True
    if mode in {"0", "false", "no", "never"}:
        return False
    return is_runpod_endpoint(endpoint)


@lru_cache(maxsize=1)
def _s3_client():
    import boto3
    from botocore.config import Config as BotoConfig

    access_key = os.environ.get("S3_ACCESS_KEY") or os.environ.get("S3_ACCESS_KEY_ID")
    secret_key = os.environ.get("S3_SECRET_KEY") or os.environ.get("S3_SECRET_ACCESS_KEY")
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION"),
        config=BotoConfig(
            connect_timeout=_env_float("S3_CONNECT_TIMEOUT_SECONDS", 5.0),
            read_timeout=_env_float("S3_READ_TIMEOUT_SECONDS", 60.0),
            retries={
                "max_attempts": _env_int("S3_MAX_RETRIES", 3),
                "mode": "standard",
            },
            max_pool_connections=_env_int("S3_MAX_POOL_CONNECTIONS", 10),
        ),
    )


def presign_s3_get_url(uri: str, *, expires_in: int | None = None) -> str:
    bucket, key = S3Backend._parse_uri(uri)
    ttl = expires_in if expires_in is not None else _env_int("RUNPOD_INPUT_URL_TTL_SECONDS", 3600)
    url = _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl,
    )
    return rewrite_presigned_url_for_public_endpoint(url)


def maybe_presign_input_uri(uri: str, endpoint: str) -> str:
    if not uri.startswith("s3://"):
        return uri
    if not should_presign_for_endpoint(endpoint):
        return uri
    return presign_s3_get_url(uri)


def maybe_presign_input_uris(uris: Iterable[str], endpoint: str) -> list[str]:
    if not should_presign_for_endpoint(endpoint):
        return list(uris)
    return [presign_s3_get_url(uri) if uri.startswith("s3://") else uri for uri in uris]
