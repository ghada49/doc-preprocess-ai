"""
shared.io.storage
-----------------
Storage backend abstraction for LibraryAI artifact I/O. (spec Section 12.2)

All services must use this module for artifact reads and writes.
URI scheme determines backend selection; callers should use get_backend(uri)
rather than constructing backends directly.

Supported URI schemes:
    file://  — local filesystem (development / CI)
    s3://    — S3-compatible object storage (production and staging)

Exported:
    StorageBackend   — Protocol defining the read/write interface
    LocalFileBackend — file:// URI backend
    S3Backend        — s3:// URI backend (boto3; custom endpoint via env vars)
    get_backend      — returns the appropriate backend for a given URI

S3 env vars (spec Section 12.2):
    S3_ENDPOINT_URL  — custom endpoint URL (e.g. http://localhost:9000 for MinIO)
    S3_ACCESS_KEY    — AWS / MinIO access key ID
    S3_SECRET_KEY    — AWS / MinIO secret access key
    S3_BUCKET_NAME   — default bucket name (informational; bucket is in the URI)
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Protocol
from urllib.parse import urlparse

import boto3

# ── Protocol ───────────────────────────────────────────────────────────────────


class StorageBackend(Protocol):
    """
    Minimal synchronous read/write interface for artifact storage.

    Implementations: LocalFileBackend (file://), S3Backend (s3://).
    """

    def get_bytes(self, uri: str) -> bytes:
        """Download and return the raw bytes at *uri*."""
        ...

    def put_bytes(self, uri: str, data: bytes) -> None:
        """Write *data* to *uri*, creating any intermediate path components."""
        ...


# ── Local file backend ─────────────────────────────────────────────────────────


class LocalFileBackend:
    """
    file:// URI backend for local development.

    Mirrors the S3 path conventions under the local filesystem (spec Section 15.3).

    URI format:
        file:///absolute/path/to/file   — absolute path
        file://relative/path/to/file    — relative path (relative to CWD)
    """

    @staticmethod
    def _to_path(uri: str) -> pathlib.Path:
        if not uri.startswith("file://"):
            raise ValueError(f"LocalFileBackend requires a file:// URI; got: {uri!r}")
        return pathlib.Path(uri[len("file://") :])

    def get_bytes(self, uri: str) -> bytes:
        return self._to_path(uri).read_bytes()

    def put_bytes(self, uri: str, data: bytes) -> None:
        path = self._to_path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


# ── S3 backend ─────────────────────────────────────────────────────────────────


class S3Backend:
    """
    s3:// URI backend using boto3.

    Compatible with AWS S3 and any S3-compatible store (MinIO, LocalStack).

    URI format: s3://bucket/key/path/to/object

    Config env vars (spec Section 12.2):
        S3_ENDPOINT_URL  — custom endpoint (omit for AWS; set for MinIO/LocalStack)
        S3_ACCESS_KEY    — access key ID
        S3_SECRET_KEY    — secret access key
    """

    def __init__(self) -> None:
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY"),
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY"),
        )

    @staticmethod
    def _parse_uri(uri: str) -> tuple[str, str]:
        """Return (bucket, key) from an s3://bucket/key URI."""
        parsed = urlparse(uri)
        if parsed.scheme != "s3":
            raise ValueError(f"S3Backend requires an s3:// URI; got: {uri!r}")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if not bucket:
            raise ValueError(f"Missing bucket in S3 URI: {uri!r}")
        if not key:
            raise ValueError(f"Missing object key in S3 URI: {uri!r}")
        return bucket, key

    def get_bytes(self, uri: str) -> bytes:
        bucket, key = self._parse_uri(uri)
        response: Any = self._client.get_object(Bucket=bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    def put_bytes(self, uri: str, data: bytes) -> None:
        bucket, key = self._parse_uri(uri)
        self._client.put_object(Bucket=bucket, Key=key, Body=data)


# ── Backend selector ───────────────────────────────────────────────────────────


def get_backend(uri: str) -> StorageBackend:
    """
    Return the appropriate StorageBackend for the given URI.

    Scheme  → Backend
    -------   -------
    file://  → LocalFileBackend
    s3://    → S3Backend (reads S3_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY)

    Raises:
        ValueError — if the URI scheme is not supported
    """
    scheme = urlparse(uri).scheme
    if scheme == "file":
        return LocalFileBackend()
    if scheme == "s3":
        return S3Backend()
    raise ValueError(f"Unsupported URI scheme '{scheme}'. Supported schemes: file://, s3://")
