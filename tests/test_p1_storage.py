"""
tests/test_p1_storage.py
------------------------
Packet 1.4 validator tests for shared.io.storage:
  - LocalFileBackend: get_bytes / put_bytes round-trip, path creation, error cases
  - S3Backend: get_bytes / put_bytes round-trip, missing object, URI validation
  - get_backend: selects correct backend from URI scheme, rejects unknown schemes

Definition of done:
  - backend selection by URI scheme works
  - local and S3-compatible interfaces exist
"""

import importlib
import os
import sys
from builtins import __import__ as builtin_import
from collections.abc import Iterator, Mapping

import boto3
import pytest
from moto import mock_aws

from shared.io.storage import LocalFileBackend, S3Backend, StorageBackend, get_backend

# ── Helpers ────────────────────────────────────────────────────────────────────

_BUCKET = "test-bucket"
_REGION = "us-east-1"


# ── LocalFileBackend ───────────────────────────────────────────────────────────


class TestLocalFileBackend:
    def test_roundtrip(self, tmp_path: pytest.TempdirFactory) -> None:
        uri = f"file://{tmp_path}/test.tiff"
        b = LocalFileBackend()
        b.put_bytes(uri, b"hello")
        assert b.get_bytes(uri) == b"hello"

    def test_put_creates_parent_dirs(self, tmp_path: pytest.TempdirFactory) -> None:
        uri = f"file://{tmp_path}/a/b/c/artifact.tiff"
        b = LocalFileBackend()
        b.put_bytes(uri, b"data")
        assert b.get_bytes(uri) == b"data"

    def test_overwrite(self, tmp_path: pytest.TempdirFactory) -> None:
        uri = f"file://{tmp_path}/obj.bin"
        b = LocalFileBackend()
        b.put_bytes(uri, b"first")
        b.put_bytes(uri, b"second")
        assert b.get_bytes(uri) == b"second"

    def test_empty_bytes(self, tmp_path: pytest.TempdirFactory) -> None:
        uri = f"file://{tmp_path}/empty.bin"
        b = LocalFileBackend()
        b.put_bytes(uri, b"")
        assert b.get_bytes(uri) == b""

    def test_binary_content(self, tmp_path: pytest.TempdirFactory) -> None:
        payload = bytes(range(256))
        uri = f"file://{tmp_path}/bin.bin"
        b = LocalFileBackend()
        b.put_bytes(uri, payload)
        assert b.get_bytes(uri) == payload

    def test_get_missing_file_raises(self, tmp_path: pytest.TempdirFactory) -> None:
        uri = f"file://{tmp_path}/no_such_file.tiff"
        b = LocalFileBackend()
        with pytest.raises(FileNotFoundError):
            b.get_bytes(uri)

    def test_wrong_scheme_raises(self) -> None:
        b = LocalFileBackend()
        with pytest.raises(ValueError, match="file://"):
            b.put_bytes("s3://bucket/key", b"data")

    def test_wrong_scheme_get_raises(self) -> None:
        b = LocalFileBackend()
        with pytest.raises(ValueError, match="file://"):
            b.get_bytes("s3://bucket/key")


# ── S3Backend ─────────────────────────────────────────────────────────────────


@pytest.fixture
def s3_backend() -> Iterator[S3Backend]:
    """Provide an S3Backend wired to a fresh moto-mocked bucket."""
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", _REGION)
    with mock_aws():
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        yield S3Backend()


class TestS3Backend:
    def test_roundtrip(self, s3_backend: S3Backend) -> None:
        uri = f"s3://{_BUCKET}/jobs/j1/input/1.tiff"
        s3_backend.put_bytes(uri, b"tiff data")
        assert s3_backend.get_bytes(uri) == b"tiff data"

    def test_nested_key(self, s3_backend: S3Backend) -> None:
        uri = f"s3://{_BUCKET}/jobs/j2/preprocessed/5.tiff"
        s3_backend.put_bytes(uri, b"preprocessed")
        assert s3_backend.get_bytes(uri) == b"preprocessed"

    def test_overwrite(self, s3_backend: S3Backend) -> None:
        uri = f"s3://{_BUCKET}/obj.bin"
        s3_backend.put_bytes(uri, b"v1")
        s3_backend.put_bytes(uri, b"v2")
        assert s3_backend.get_bytes(uri) == b"v2"

    def test_empty_payload(self, s3_backend: S3Backend) -> None:
        uri = f"s3://{_BUCKET}/empty.bin"
        s3_backend.put_bytes(uri, b"")
        assert s3_backend.get_bytes(uri) == b""

    def test_binary_content(self, s3_backend: S3Backend) -> None:
        payload = bytes(range(256))
        uri = f"s3://{_BUCKET}/binary.bin"
        s3_backend.put_bytes(uri, payload)
        assert s3_backend.get_bytes(uri) == payload

    def test_get_missing_key_raises(self, s3_backend: S3Backend) -> None:
        uri = f"s3://{_BUCKET}/no/such/key.tiff"
        with pytest.raises(Exception):  # botocore.exceptions.ClientError
            s3_backend.get_bytes(uri)

    def test_wrong_scheme_raises(self, s3_backend: S3Backend) -> None:
        with pytest.raises(ValueError, match="s3://"):
            s3_backend.put_bytes("file:///some/path", b"data")

    def test_missing_bucket_raises(self) -> None:
        with pytest.raises(ValueError, match="Missing bucket"):
            S3Backend._parse_uri("s3:///just-a-key")

    def test_missing_key_raises(self) -> None:
        with pytest.raises(ValueError, match="Missing object key"):
            S3Backend._parse_uri("s3://bucket-only")


# ── S3Backend._parse_uri (static; no mock needed) ─────────────────────────────


class TestS3BackendParseUri:
    def test_simple(self) -> None:
        bucket, key = S3Backend._parse_uri("s3://my-bucket/jobs/j1/1.tiff")
        assert bucket == "my-bucket"
        assert key == "jobs/j1/1.tiff"

    def test_deep_path(self) -> None:
        bucket, key = S3Backend._parse_uri("s3://b/a/b/c/d.json")
        assert bucket == "b"
        assert key == "a/b/c/d.json"

    def test_wrong_scheme(self) -> None:
        with pytest.raises(ValueError, match="s3://"):
            S3Backend._parse_uri("file:///path")


# ── get_backend ────────────────────────────────────────────────────────────────


class TestGetBackend:
    def test_file_scheme_returns_local(self) -> None:
        backend = get_backend("file:///some/path/artifact.tiff")
        assert isinstance(backend, LocalFileBackend)

    def test_s3_scheme_returns_s3(self, s3_backend: S3Backend) -> None:
        # s3_backend fixture provides a fully mocked S3 context; just test
        # that get_backend with an s3:// URI returns an S3Backend instance.
        backend = get_backend("s3://test-bucket/key")
        assert isinstance(backend, S3Backend)

    def test_unsupported_scheme_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported URI scheme"):
            get_backend("gs://bucket/key")

    def test_unsupported_scheme_http_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported URI scheme"):
            get_backend("http://example.com/file")

    def test_unsupported_scheme_ftp_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported URI scheme"):
            get_backend("ftp://host/path")

    def test_error_message_names_scheme(self) -> None:
        with pytest.raises(ValueError, match="gs"):
            get_backend("gs://bucket/key")

    def test_file_backend_does_not_require_boto3_installed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delitem(sys.modules, "shared.io.storage", raising=False)
        monkeypatch.delitem(sys.modules, "boto3", raising=False)

        def guarded_import(
            name: str,
            globals: Mapping[str, object] | None = None,
            locals: Mapping[str, object] | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> object:
            if name == "boto3":
                raise ImportError("No module named 'boto3'")
            return builtin_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr("builtins.__import__", guarded_import)

        storage = importlib.import_module("shared.io.storage")

        backend = storage.get_backend("file:///some/path/artifact.tiff")
        assert isinstance(backend, storage.LocalFileBackend)


# ── StorageBackend Protocol compliance ────────────────────────────────────────


class TestProtocolCompliance:
    """Verify both backends structurally satisfy the StorageBackend Protocol."""

    def test_local_backend_is_storage_backend(self) -> None:
        b: StorageBackend = LocalFileBackend()
        assert hasattr(b, "get_bytes")
        assert hasattr(b, "put_bytes")

    def test_s3_backend_is_storage_backend(self, s3_backend: S3Backend) -> None:
        b: StorageBackend = s3_backend
        assert hasattr(b, "get_bytes")
        assert hasattr(b, "put_bytes")
