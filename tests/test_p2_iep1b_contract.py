"""
tests/test_p2_iep1b_contract.py
--------------------------------
Packet 2.3 contract tests for IEP1B POST /v1/geometry.

IEP1B uses YOLOv8-pose keypoint regression and exposes the identical
GeometryRequest/GeometryResponse contract as IEP1A.  These tests mirror the
IEP1A contract tests (Packet 2.1) with IEP1B-specific env var names.

Covers:
  - POST /v1/geometry valid single-page → 200, full GeometryResponse schema
  - POST /v1/geometry valid two-page spread → split semantics enforced
  - Required response fields present (all GeometryResponse fields)
  - PageRegion fields present and valid
  - split_required / split_x consistency with page_count
  - pages length matches page_count
  - geometry_confidence in [0, 1]
  - tta_passes >= 1
  - processing_time_ms >= 0
  - Configurable confidence reflected in response
  - Configurable TTA passes reflected
  - All three canonical material_types accepted
  - Missing required request fields → 422 (FastAPI validation)
  - Invalid material_type ("microfilm") → 422
  - page_number = 0 → 422
  - GET /v1/geometry (wrong method) → 405
  - Failure simulation: ESCALATE_REVIEW → 422 with PreprocessError body
  - Failure simulation: RETRY → 503 with PreprocessError body
  - PreprocessError body fields: error_code, error_message, fallback_action
  - Default error code is GEOMETRY_FAILED
  - Custom error code reflected
  - is_model_ready() returns True by default
  - is_model_ready() returns False when IEP1B_MOCK_NOT_READY=true
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.iep1b.app.inference as inference_mod
from services.iep1b.app.geometry import router

# ---------------------------------------------------------------------------
# Test client — minimal app, no prometheus middleware
# ---------------------------------------------------------------------------

_app = FastAPI()
_app.include_router(router)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_app)


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

VALID_BODY: dict[str, object] = {
    "job_id": "job-test-002",
    "page_number": 1,
    "image_uri": "s3://libraryai/proxy/job-test-002/1.jpg",
    "material_type": "book",
}

_GEOMETRY_RESPONSE_FIELDS = {
    "page_count",
    "pages",
    "split_required",
    "split_x",
    "geometry_confidence",
    "tta_structural_agreement_rate",
    "tta_prediction_variance",
    "tta_passes",
    "uncertainty_flags",
    "warnings",
    "processing_time_ms",
}

_PAGE_REGION_FIELDS = {
    "region_id",
    "geometry_type",
    "corners",
    "bbox",
    "confidence",
    "page_area_fraction",
}

_PREPROCESS_ERROR_FIELDS = {"error_code", "error_message", "fallback_action"}


@pytest.fixture(autouse=True)
def _clean_mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all IEP1B_MOCK_* env vars before each test."""
    for key in (
        "IEP1B_MOCK_FAIL",
        "IEP1B_MOCK_FAIL_CODE",
        "IEP1B_MOCK_FAIL_ACTION",
        "IEP1B_MOCK_PAGE_COUNT",
        "IEP1B_MOCK_CONFIDENCE",
        "IEP1B_MOCK_TTA_PASSES",
        "IEP1B_MOCK_TTA_AGREEMENT_RATE",
        "IEP1B_MOCK_TTA_VARIANCE",
        "IEP1B_MOCK_NOT_READY",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Readiness logic (unit — no HTTP layer needed)
# ---------------------------------------------------------------------------


class TestReadinessLogic:
    def test_model_ready_by_default(self) -> None:
        assert inference_mod.is_model_ready() is True

    def test_model_not_ready_when_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP1B_MOCK_NOT_READY", "true")
        assert inference_mod.is_model_ready() is False

    def test_model_ready_when_env_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP1B_MOCK_NOT_READY", "false")
        assert inference_mod.is_model_ready() is True


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def test_missing_job_id_returns_422(self, client: TestClient) -> None:
        body = {k: v for k, v in VALID_BODY.items() if k != "job_id"}
        assert client.post("/v1/geometry", json=body).status_code == 422

    def test_missing_page_number_returns_422(self, client: TestClient) -> None:
        body = {k: v for k, v in VALID_BODY.items() if k != "page_number"}
        assert client.post("/v1/geometry", json=body).status_code == 422

    def test_missing_image_uri_returns_422(self, client: TestClient) -> None:
        body = {k: v for k, v in VALID_BODY.items() if k != "image_uri"}
        assert client.post("/v1/geometry", json=body).status_code == 422

    def test_missing_material_type_returns_422(self, client: TestClient) -> None:
        body = {k: v for k, v in VALID_BODY.items() if k != "material_type"}
        assert client.post("/v1/geometry", json=body).status_code == 422

    def test_invalid_material_type_microfilm_returns_422(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json={**VALID_BODY, "material_type": "microfilm"})
        assert r.status_code == 422

    def test_invalid_material_type_document_returns_422(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json={**VALID_BODY, "material_type": "document"})
        assert r.status_code == 422

    def test_page_number_zero_returns_422(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json={**VALID_BODY, "page_number": 0})
        assert r.status_code == 422

    def test_empty_body_returns_422(self, client: TestClient) -> None:
        assert client.post("/v1/geometry", json={}).status_code == 422

    def test_wrong_method_get_returns_405(self, client: TestClient) -> None:
        assert client.get("/v1/geometry").status_code == 405


# ---------------------------------------------------------------------------
# Single-page success
# ---------------------------------------------------------------------------


class TestSinglePageSuccess:
    @pytest.fixture()
    def resp(self, client: TestClient) -> dict:  # type: ignore[type-arg]
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200, r.text
        return r.json()  # type: ignore[no-any-return]

    def test_status_200(self, client: TestClient) -> None:
        assert client.post("/v1/geometry", json=VALID_BODY).status_code == 200

    def test_all_response_fields_present(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert _GEOMETRY_RESPONSE_FIELDS.issubset(resp.keys())

    def test_page_count_is_1(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert resp["page_count"] == 1

    def test_split_required_false(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert resp["split_required"] is False

    def test_split_x_is_null(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert resp["split_x"] is None

    def test_pages_length_matches_page_count(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert len(resp["pages"]) == resp["page_count"]

    def test_geometry_confidence_in_range(self, resp: dict) -> None:  # type: ignore[type-arg]
        c = resp["geometry_confidence"]
        assert 0.0 <= c <= 1.0

    def test_tta_structural_agreement_rate_in_range(
        self, resp: dict  # type: ignore[type-arg]
    ) -> None:
        r = resp["tta_structural_agreement_rate"]
        assert 0.0 <= r <= 1.0

    def test_tta_prediction_variance_non_negative(
        self, resp: dict  # type: ignore[type-arg]
    ) -> None:
        assert resp["tta_prediction_variance"] >= 0.0

    def test_tta_passes_at_least_1(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert resp["tta_passes"] >= 1

    def test_processing_time_ms_non_negative(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert resp["processing_time_ms"] >= 0.0

    def test_uncertainty_flags_is_list(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert isinstance(resp["uncertainty_flags"], list)

    def test_warnings_is_list(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert isinstance(resp["warnings"], list)


# ---------------------------------------------------------------------------
# PageRegion fields
# ---------------------------------------------------------------------------


class TestPageRegionFields:
    def test_page_region_has_all_required_fields(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert _PAGE_REGION_FIELDS.issubset(r.json()["pages"][0].keys())

    def test_region_id_is_string(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert isinstance(r.json()["pages"][0]["region_id"], str)

    def test_confidence_in_range(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json=VALID_BODY)
        c = r.json()["pages"][0]["confidence"]
        assert 0.0 <= c <= 1.0

    def test_page_area_fraction_in_range(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json=VALID_BODY)
        f = r.json()["pages"][0]["page_area_fraction"]
        assert 0.0 <= f <= 1.0

    def test_quadrilateral_has_4_corners(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json=VALID_BODY)
        region = r.json()["pages"][0]
        if region["geometry_type"] == "quadrilateral":
            assert len(region["corners"]) == 4


# ---------------------------------------------------------------------------
# Two-page spread (split)
# ---------------------------------------------------------------------------


class TestTwoPageSplit:
    @pytest.fixture()
    def resp(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> dict:  # type: ignore[type-arg]
        monkeypatch.setenv("IEP1B_MOCK_PAGE_COUNT", "2")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200, r.text
        return r.json()  # type: ignore[no-any-return]

    def test_page_count_is_2(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert resp["page_count"] == 2

    def test_split_required_true(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert resp["split_required"] is True

    def test_split_x_is_set(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert resp["split_x"] is not None

    def test_split_x_non_negative(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert resp["split_x"] >= 0

    def test_pages_has_two_entries(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert len(resp["pages"]) == 2

    def test_pages_length_matches_page_count(self, resp: dict) -> None:  # type: ignore[type-arg]
        assert len(resp["pages"]) == resp["page_count"]

    def test_region_ids_are_distinct(self, resp: dict) -> None:  # type: ignore[type-arg]
        ids = [p["region_id"] for p in resp["pages"]]
        assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# Configurable confidence
# ---------------------------------------------------------------------------


class TestConfigurableConfidence:
    def test_low_confidence_reflected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_CONFIDENCE", "0.30")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert r.json()["geometry_confidence"] == pytest.approx(0.30)

    def test_high_confidence_reflected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_CONFIDENCE", "0.99")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert r.json()["geometry_confidence"] == pytest.approx(0.99)

    def test_per_region_confidence_matches_global(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_CONFIDENCE", "0.75")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        for region in r.json()["pages"]:
            assert region["confidence"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Configurable TTA passes
# ---------------------------------------------------------------------------


class TestConfigurableTtaPasses:
    def test_tta_passes_reflected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_TTA_PASSES", "3")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert r.json()["tta_passes"] == 3


# ---------------------------------------------------------------------------
# Material type acceptance
# ---------------------------------------------------------------------------


class TestMaterialTypeAcceptance:
    @pytest.mark.parametrize("mat", ["book", "newspaper", "archival_document"])
    def test_valid_material_types_return_200(self, client: TestClient, mat: str) -> None:
        r = client.post("/v1/geometry", json={**VALID_BODY, "material_type": mat})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Failure simulation — PreprocessError responses
# ---------------------------------------------------------------------------


class TestFailureSimulation:
    def test_failure_returns_non_200(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_FAIL", "true")
        assert client.post("/v1/geometry", json=VALID_BODY).status_code != 200

    def test_escalate_review_returns_422(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_FAIL", "true")
        monkeypatch.setenv("IEP1B_MOCK_FAIL_ACTION", "ESCALATE_REVIEW")
        assert client.post("/v1/geometry", json=VALID_BODY).status_code == 422

    def test_retry_returns_503(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP1B_MOCK_FAIL", "true")
        monkeypatch.setenv("IEP1B_MOCK_FAIL_ACTION", "RETRY")
        assert client.post("/v1/geometry", json=VALID_BODY).status_code == 503

    def test_error_body_has_all_preprocess_error_fields(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_FAIL", "true")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert _PREPROCESS_ERROR_FIELDS.issubset(r.json().keys())

    def test_default_error_code_is_geometry_failed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_FAIL", "true")
        monkeypatch.delenv("IEP1B_MOCK_FAIL_CODE", raising=False)
        assert (
            client.post("/v1/geometry", json=VALID_BODY).json()["error_code"] == "GEOMETRY_FAILED"
        )

    def test_custom_error_code_invalid_image_reflected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_FAIL", "true")
        monkeypatch.setenv("IEP1B_MOCK_FAIL_CODE", "INVALID_IMAGE")
        assert client.post("/v1/geometry", json=VALID_BODY).json()["error_code"] == "INVALID_IMAGE"

    def test_fallback_action_in_error_body(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_FAIL", "true")
        monkeypatch.setenv("IEP1B_MOCK_FAIL_ACTION", "ESCALATE_REVIEW")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.json()["fallback_action"] == "ESCALATE_REVIEW"

    def test_error_message_is_non_empty_string(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_FAIL", "true")
        r = client.post("/v1/geometry", json=VALID_BODY)
        msg = r.json()["error_message"]
        assert isinstance(msg, str) and len(msg) > 0


# ---------------------------------------------------------------------------
# Packet 2.4 — TTA agreement rate and variance
# ---------------------------------------------------------------------------


class TestTTAAgreementRate:
    def test_default_agreement_rate_is_1(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert r.json()["tta_structural_agreement_rate"] == pytest.approx(1.0)

    def test_low_agreement_rate_reflected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_TTA_AGREEMENT_RATE", "0.6")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert r.json()["tta_structural_agreement_rate"] == pytest.approx(0.6)

    def test_high_agreement_rate_reflected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_TTA_AGREEMENT_RATE", "0.9")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert r.json()["tta_structural_agreement_rate"] == pytest.approx(0.9)

    def test_agreement_rate_in_range(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_TTA_AGREEMENT_RATE", "0.75")
        r = client.post("/v1/geometry", json=VALID_BODY)
        val = r.json()["tta_structural_agreement_rate"]
        assert 0.0 <= val <= 1.0


class TestTTAVariance:
    def test_default_variance_is_low(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert r.json()["tta_prediction_variance"] == pytest.approx(0.001)

    def test_high_variance_reflected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_TTA_VARIANCE", "0.5")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert r.json()["tta_prediction_variance"] == pytest.approx(0.5)

    def test_variance_non_negative(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_TTA_VARIANCE", "0.05")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.json()["tta_prediction_variance"] >= 0.0


class TestUncertaintyFlags:
    def test_no_flags_by_default(self, client: TestClient) -> None:
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert r.json()["uncertainty_flags"] == []

    def test_low_agreement_adds_flag(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Below threshold of 0.80
        monkeypatch.setenv("IEP1B_MOCK_TTA_AGREEMENT_RATE", "0.5")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert "low_structural_agreement" in r.json()["uncertainty_flags"]

    def test_agreement_at_threshold_no_flag(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exactly at threshold (0.80) → no flag (strict less-than)
        monkeypatch.setenv("IEP1B_MOCK_TTA_AGREEMENT_RATE", "0.80")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert "low_structural_agreement" not in r.json()["uncertainty_flags"]

    def test_high_variance_adds_flag(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Above threshold of 0.10
        monkeypatch.setenv("IEP1B_MOCK_TTA_VARIANCE", "0.5")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert r.status_code == 200
        assert "high_prediction_variance" in r.json()["uncertainty_flags"]

    def test_variance_at_threshold_no_flag(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exactly at threshold (0.10) → no flag (strict greater-than)
        monkeypatch.setenv("IEP1B_MOCK_TTA_VARIANCE", "0.10")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert "high_prediction_variance" not in r.json()["uncertainty_flags"]

    def test_both_flags_set_together(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_TTA_AGREEMENT_RATE", "0.5")
        monkeypatch.setenv("IEP1B_MOCK_TTA_VARIANCE", "0.5")
        r = client.post("/v1/geometry", json=VALID_BODY)
        flags = r.json()["uncertainty_flags"]
        assert "low_structural_agreement" in flags
        assert "high_prediction_variance" in flags

    def test_high_agreement_no_low_flag(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_TTA_AGREEMENT_RATE", "1.0")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert "low_structural_agreement" not in r.json()["uncertainty_flags"]

    def test_low_variance_no_high_flag(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP1B_MOCK_TTA_VARIANCE", "0.001")
        r = client.post("/v1/geometry", json=VALID_BODY)
        assert "high_prediction_variance" not in r.json()["uncertainty_flags"]
