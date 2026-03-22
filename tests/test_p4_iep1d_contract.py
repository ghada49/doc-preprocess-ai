"""
tests/test_p4_iep1d_contract.py
---------------------------------
Packet 4.8 — IEP1D POST /v1/rectify contract tests.

Tests the actual IEP1D FastAPI router (not mocked) via TestClient.

Covers:
  - POST /v1/rectify valid request → 200, full RectifyResponse schema
  - All required response fields present and correctly typed
  - rectification_confidence in [0, 1]
  - skew_residual_after < skew_residual_before (mock guarantee)
  - border_score_after > border_score_before (mock guarantee)
  - processing_time_ms >= 0
  - warnings is a list
  - rectified_image_uri matches input image_uri (pass-through mock)
  - All three canonical material_types accepted without error
  - page_number = 0 → 422 (FastAPI validation)
  - Missing required field (job_id) → 422
  - Invalid material_type → 422
  - GET /v1/rectify (wrong method) → 405
  - IEP1D_SIMULATE_FAILURE=1 → 500 with error_code in detail
  - IEP1D_MOCK_CONFIDENCE env var reflected in rectification_confidence
  - IEP1D_MOCK_CONFIDENCE out-of-range values are clamped to [0, 1]

Tests import the rectify router into a minimal FastAPI app to avoid pulling
in prometheus_client (which configure_observability requires but is not
installed in the test environment).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.iep1d.app.rectify as rectify_mod
from services.iep1d.app.rectify import router

# ---------------------------------------------------------------------------
# Test client — minimal app, no prometheus middleware
# ---------------------------------------------------------------------------

_app = FastAPI()
_app.include_router(router)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_app)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_VALID_PAYLOAD = {
    "job_id": "job-abc123",
    "page_number": 5,
    "image_uri": "s3://bucket/artifacts/page5.tiff",
    "material_type": "book",
}


# ---------------------------------------------------------------------------
# Happy path — schema correctness
# ---------------------------------------------------------------------------


class TestRectifyHappyPath:
    def test_200_on_valid_request(self, client: TestClient) -> None:
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        assert resp.status_code == 200

    def test_response_is_json(self, client: TestClient) -> None:
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        assert resp.headers["content-type"].startswith("application/json")

    def test_rectified_image_uri_present(self, client: TestClient) -> None:
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        assert "rectified_image_uri" in resp.json()

    def test_rectified_image_uri_is_passthrough(self, client: TestClient) -> None:
        """Mock passes the input image_uri through unchanged."""
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        assert resp.json()["rectified_image_uri"] == _VALID_PAYLOAD["image_uri"]

    def test_rectification_confidence_in_range(self, client: TestClient) -> None:
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        conf = resp.json()["rectification_confidence"]
        assert 0.0 <= conf <= 1.0

    def test_skew_residuals_present_and_non_negative(self, client: TestClient) -> None:
        data = client.post("/v1/rectify", json=_VALID_PAYLOAD).json()
        assert data["skew_residual_before"] >= 0.0
        assert data["skew_residual_after"] >= 0.0

    def test_skew_improves_after_rectification(self, client: TestClient) -> None:
        data = client.post("/v1/rectify", json=_VALID_PAYLOAD).json()
        assert data["skew_residual_after"] < data["skew_residual_before"]

    def test_border_scores_present_and_in_range(self, client: TestClient) -> None:
        data = client.post("/v1/rectify", json=_VALID_PAYLOAD).json()
        assert 0.0 <= data["border_score_before"] <= 1.0
        assert 0.0 <= data["border_score_after"] <= 1.0

    def test_border_improves_after_rectification(self, client: TestClient) -> None:
        data = client.post("/v1/rectify", json=_VALID_PAYLOAD).json()
        assert data["border_score_after"] > data["border_score_before"]

    def test_processing_time_ms_non_negative(self, client: TestClient) -> None:
        data = client.post("/v1/rectify", json=_VALID_PAYLOAD).json()
        assert data["processing_time_ms"] >= 0.0

    def test_warnings_is_list(self, client: TestClient) -> None:
        data = client.post("/v1/rectify", json=_VALID_PAYLOAD).json()
        assert isinstance(data["warnings"], list)

    def test_all_required_fields_present(self, client: TestClient) -> None:
        data = client.post("/v1/rectify", json=_VALID_PAYLOAD).json()
        required = {
            "rectified_image_uri",
            "rectification_confidence",
            "skew_residual_before",
            "skew_residual_after",
            "border_score_before",
            "border_score_after",
            "processing_time_ms",
            "warnings",
        }
        assert required.issubset(data.keys())


# ---------------------------------------------------------------------------
# Material type acceptance
# ---------------------------------------------------------------------------


class TestMaterialTypeAcceptance:
    @pytest.mark.parametrize("material_type", ["book", "newspaper", "archival_document"])
    def test_canonical_material_types_accepted(
        self, client: TestClient, material_type: str
    ) -> None:
        payload = {**_VALID_PAYLOAD, "material_type": material_type}
        resp = client.post("/v1/rectify", json=payload)
        assert resp.status_code == 200

    def test_invalid_material_type_rejected(self, client: TestClient) -> None:
        payload = {**_VALID_PAYLOAD, "material_type": "microfilm"}
        resp = client.post("/v1/rectify", json=payload)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Request validation errors
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def test_page_number_zero_rejected(self, client: TestClient) -> None:
        payload = {**_VALID_PAYLOAD, "page_number": 0}
        resp = client.post("/v1/rectify", json=payload)
        assert resp.status_code == 422

    def test_missing_job_id_rejected(self, client: TestClient) -> None:
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "job_id"}
        resp = client.post("/v1/rectify", json=payload)
        assert resp.status_code == 422

    def test_missing_image_uri_rejected(self, client: TestClient) -> None:
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "image_uri"}
        resp = client.post("/v1/rectify", json=payload)
        assert resp.status_code == 422

    def test_missing_material_type_rejected(self, client: TestClient) -> None:
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "material_type"}
        resp = client.post("/v1/rectify", json=payload)
        assert resp.status_code == 422

    def test_empty_body_rejected(self, client: TestClient) -> None:
        resp = client.post("/v1/rectify", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Wrong HTTP method
# ---------------------------------------------------------------------------


class TestWrongMethod:
    def test_get_returns_405(self, client: TestClient) -> None:
        resp = client.get("/v1/rectify")
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Failure simulation
# ---------------------------------------------------------------------------


class TestFailureSimulation:
    def test_simulate_failure_returns_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1D_SIMULATE_FAILURE", "1")
        # Re-evaluate the env var lookup in the handler.
        monkeypatch.setattr(rectify_mod, "_simulate_failure", lambda: True)
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        assert resp.status_code == 500

    def test_simulate_failure_body_has_error_code(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rectify_mod, "_simulate_failure", lambda: True)
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        assert resp.status_code == 500
        detail = resp.json().get("detail", {})
        assert "error_code" in detail

    def test_no_failure_by_default(self, client: TestClient) -> None:
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Configurable confidence
# ---------------------------------------------------------------------------


class TestConfigurableConfidence:
    def test_mock_confidence_env_var_reflected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1D_MOCK_CONFIDENCE", "0.55")
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        assert resp.status_code == 200
        assert abs(resp.json()["rectification_confidence"] - 0.55) < 1e-6

    def test_confidence_clamped_above_one(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1D_MOCK_CONFIDENCE", "1.5")
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["rectification_confidence"] <= 1.0

    def test_confidence_clamped_below_zero(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1D_MOCK_CONFIDENCE", "-0.3")
        resp = client.post("/v1/rectify", json=_VALID_PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["rectification_confidence"] >= 0.0
