"""
tests/test_p1_uploads.py
------------------------
Packet 1.7b contract tests for POST /v1/uploads/jobs/presign.

Tests cover:
  - HTTP 200 with correct JSON schema on success
  - object_uri follows s3://{bucket}/uploads/{uuid}.tiff staging convention
  - upload_url is an HTTP(S) URL string
  - expires_in matches _EXPIRES_IN module constant (default 3600)
  - Each call produces a unique UUID (object_uri and upload_url differ per call)
  - HTTP 503 when S3 / boto3 raises any exception

moto[s3] (mock_aws) is used to provide fake AWS credentials so that
boto3.client() and generate_presigned_url() succeed without real AWS access.
S3_ENDPOINT_URL is cleared in the autouse fixture so moto can intercept calls.

No real AWS credentials or running S3 / MinIO service are required.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

import services.eep.app.uploads as uploads_mod
from services.eep.app.uploads import router

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Matches s3://<bucket>/uploads/<uuid>.tiff
_OBJECT_URI_RE = re.compile(
    r"^s3://[^/]+/uploads/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.tiff$"
)
# Matches any http or https URL
_URL_RE = re.compile(r"^https?://")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Ensure a clean S3 environment for every test:
      - Clear S3_ENDPOINT_URL so moto can intercept boto3 calls
        (a custom endpoint like http://localhost:9000 bypasses moto).
      - Provide fake credentials so boto3 does not raise NoCredentialsError.
    """
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("S3_ACCESS_KEY", "testing")
    monkeypatch.setenv("S3_SECRET_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture()
def presign_ok(client: TestClient) -> dict:  # type: ignore[type-arg]
    """Single successful presign response (JSON body as dict)."""
    with mock_aws():
        r = client.post("/v1/uploads/jobs/presign")
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# HTTP status
# ---------------------------------------------------------------------------


class TestPresignStatus:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with mock_aws():
            r = client.post("/v1/uploads/jobs/presign")
        assert r.status_code == 200

    def test_method_get_not_allowed(self, client: TestClient) -> None:
        r = client.get("/v1/uploads/jobs/presign")
        assert r.status_code == 405


# ---------------------------------------------------------------------------
# Response schema — field presence
# ---------------------------------------------------------------------------


class TestPresignResponseFields:
    def test_upload_url_present(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert "upload_url" in presign_ok

    def test_object_uri_present(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert "object_uri" in presign_ok

    def test_expires_in_present(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert "expires_in" in presign_ok

    def test_no_extra_top_level_keys(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert set(presign_ok.keys()) == {"upload_url", "object_uri", "expires_in"}


# ---------------------------------------------------------------------------
# upload_url constraints
# ---------------------------------------------------------------------------


class TestUploadUrl:
    def test_is_string(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert isinstance(presign_ok["upload_url"], str)

    def test_is_http_url(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert _URL_RE.match(
            presign_ok["upload_url"]
        ), f"upload_url is not an HTTP(S) URL: {presign_ok['upload_url']!r}"

    def test_is_non_empty(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert presign_ok["upload_url"]


# ---------------------------------------------------------------------------
# object_uri constraints
# ---------------------------------------------------------------------------


class TestObjectUri:
    def test_is_string(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert isinstance(presign_ok["object_uri"], str)

    def test_matches_staging_pattern(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert _OBJECT_URI_RE.match(
            presign_ok["object_uri"]
        ), f"object_uri does not match expected pattern: {presign_ok['object_uri']!r}"

    def test_uses_s3_scheme(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert presign_ok["object_uri"].startswith("s3://")

    def test_uses_default_bucket(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert presign_ok["object_uri"].startswith("s3://libraryai/")

    def test_staging_path_prefix(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        # After s3://<bucket>/ the path must start with uploads/
        uri = presign_ok["object_uri"]
        path = uri.split("//", 1)[1].split("/", 1)[1]  # strip scheme + bucket
        assert path.startswith("uploads/")

    def test_ends_with_tiff(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert presign_ok["object_uri"].endswith(".tiff")

    def test_uuid_is_valid_format(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        uri = presign_ok["object_uri"]
        uuid_part = uri.split("/uploads/")[1].removesuffix(".tiff")
        uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        assert uuid_re.match(uuid_part), f"UUID part is malformed: {uuid_part!r}"


# ---------------------------------------------------------------------------
# expires_in constraints
# ---------------------------------------------------------------------------


class TestExpiresIn:
    def test_is_int(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert isinstance(presign_ok["expires_in"], int)

    def test_default_is_3600(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert presign_ok["expires_in"] == 3600

    def test_is_positive(self, presign_ok: dict) -> None:  # type: ignore[type-arg]
        assert presign_ok["expires_in"] > 0

    def test_custom_expires_in_reflected(self, client: TestClient) -> None:
        with mock_aws():
            with patch.object(uploads_mod, "_EXPIRES_IN", 7200):
                r = client.post("/v1/uploads/jobs/presign")
        assert r.status_code == 200
        assert r.json()["expires_in"] == 7200


# ---------------------------------------------------------------------------
# Uniqueness per call
# ---------------------------------------------------------------------------


class TestUniquenessPerCall:
    def test_object_uris_are_unique(self, client: TestClient) -> None:
        with mock_aws():
            r1 = client.post("/v1/uploads/jobs/presign")
            r2 = client.post("/v1/uploads/jobs/presign")
        assert r1.json()["object_uri"] != r2.json()["object_uri"]

    def test_upload_urls_are_unique(self, client: TestClient) -> None:
        with mock_aws():
            r1 = client.post("/v1/uploads/jobs/presign")
            r2 = client.post("/v1/uploads/jobs/presign")
        assert r1.json()["upload_url"] != r2.json()["upload_url"]


# ---------------------------------------------------------------------------
# Custom bucket
# ---------------------------------------------------------------------------


class TestCustomBucket:
    def test_custom_bucket_reflected_in_object_uri(self, client: TestClient) -> None:
        with mock_aws():
            with patch.object(uploads_mod, "_BUCKET", "custombucket"):
                r = client.post("/v1/uploads/jobs/presign")
        assert r.status_code == 200
        assert r.json()["object_uri"].startswith("s3://custombucket/")


# ---------------------------------------------------------------------------
# S3 failure → 503
# ---------------------------------------------------------------------------


class TestPresignFailure:
    def test_s3_client_exception_returns_503(self, client: TestClient) -> None:
        with patch.object(uploads_mod, "_s3_client", side_effect=Exception("S3 down")):
            r = client.post("/v1/uploads/jobs/presign")
        assert r.status_code == 503

    def test_503_response_has_detail(self, client: TestClient) -> None:
        with patch.object(uploads_mod, "_s3_client", side_effect=Exception("S3 down")):
            r = client.post("/v1/uploads/jobs/presign")
        assert "detail" in r.json()

    def test_generate_presigned_url_exception_returns_503(self, client: TestClient) -> None:
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.side_effect = Exception("connection refused")
        with patch.object(uploads_mod, "_s3_client", return_value=mock_s3):
            r = client.post("/v1/uploads/jobs/presign")
        assert r.status_code == 503

    def test_503_detail_is_string(self, client: TestClient) -> None:
        with patch.object(uploads_mod, "_s3_client", side_effect=Exception("S3 down")):
            r = client.post("/v1/uploads/jobs/presign")
        assert isinstance(r.json()["detail"], str)
        assert len(r.json()["detail"]) > 0
