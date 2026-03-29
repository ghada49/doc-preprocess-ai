"""
tests/test_p6_iep2b_contract.py
---------------------------------
Packet 6.3 — IEP2B POST /v1/layout-detect contract tests.

Tests the actual IEP2B FastAPI router (not mocked) via TestClient.

Covers:
  - POST /v1/layout-detect valid request → 200, full LayoutDetectResponse schema
  - All required response fields present and correctly typed
  - detector_type == "doclayout_yolo"
  - region_schema_version == "v1"
  - regions is a non-empty list
  - region IDs are unique within the response
  - region IDs match the pattern ^r\\d+$
  - Only canonical RegionType values appear in regions
  - All five canonical region types represented in the mock response
  - layout_conf_summary populated with mean_conf and low_conf_frac in [0, 1]
  - region_type_histogram populated with non-negative integer values
  - histogram keys are all canonical region types
  - histogram counts sum to len(regions)
  - column_structure is None in Packet 6.3 (postprocessing deferred to 6.4)
  - processing_time_ms >= 0
  - model_version is a non-empty string
  - warnings is a list
  - All three canonical material_types accepted without error
  - Invalid material_type → 422
  - page_number = 0 → 422
  - Missing required fields → 422
  - Empty body → 422
  - GET /v1/layout-detect (wrong method) → 405
  - IEP2B_MOCK_FAIL=true → 500 with error_code in detail
  - IEP2B_MOCK_CONFIDENCE env var reflected in per-region confidence (≤ raw)
  - IEP2B_MOCK_CONFIDENCE out-of-range values clamped to [0, 1]

Tests import the detect router into a minimal FastAPI app to avoid pulling
in prometheus_client (which configure_observability requires but is not
installed in the test environment).
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.iep2b.app.detect import router
from shared.schemas.layout import RegionType

# ---------------------------------------------------------------------------
# Minimal test app — no prometheus middleware
# ---------------------------------------------------------------------------

_app = FastAPI()
_app.include_router(router)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_app)


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_VALID_PAYLOAD: dict[str, object] = {
    "job_id": "job-iep2b-001",
    "page_number": 2,
    "image_uri": "s3://bucket/artifacts/page2.tiff",
    "material_type": "newspaper",
}

_CANONICAL_TYPES: frozenset[str] = frozenset(rt.value for rt in RegionType)
_REGION_ID_PATTERN = re.compile(r"^r\d+$")


# ---------------------------------------------------------------------------
# Happy path — HTTP 200 and response shape
# ---------------------------------------------------------------------------


class TestLayoutDetectHappyPath:
    def test_200_on_valid_request(self, client: TestClient) -> None:
        resp = client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        assert resp.status_code == 200

    def test_response_is_json(self, client: TestClient) -> None:
        resp = client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        assert resp.headers["content-type"].startswith("application/json")

    def test_all_required_fields_present(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        required = {
            "region_schema_version",
            "regions",
            "layout_conf_summary",
            "region_type_histogram",
            "model_version",
            "detector_type",
            "processing_time_ms",
            "warnings",
        }
        assert required.issubset(data.keys())

    def test_region_schema_version_is_v1(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert data["region_schema_version"] == "v1"

    def test_model_version_non_empty(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert isinstance(data["model_version"], str)
        assert len(data["model_version"]) > 0

    def test_processing_time_ms_non_negative(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert data["processing_time_ms"] >= 0.0

    def test_warnings_is_list(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert isinstance(data["warnings"], list)

    def test_column_structure_is_none_in_packet_63(self, client: TestClient) -> None:
        """Postprocessing (including column inference) is deferred to Packet 6.4."""
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert data.get("column_structure") is None


# ---------------------------------------------------------------------------
# detector_type contract — must be exactly "doclayout_yolo"
# ---------------------------------------------------------------------------


class TestDetectorType:
    def test_detector_type_is_doclayout_yolo(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert data["detector_type"] == "doclayout_yolo"

    def test_detector_type_is_not_detectron2(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert data["detector_type"] != "detectron2"

    def test_detector_type_is_string(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert isinstance(data["detector_type"], str)


# ---------------------------------------------------------------------------
# regions contract
# ---------------------------------------------------------------------------


class TestRegions:
    def test_regions_is_list(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert isinstance(data["regions"], list)

    def test_regions_non_empty(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert len(data["regions"]) > 0

    def test_region_ids_are_unique(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        ids = [r["id"] for r in data["regions"]]
        assert len(ids) == len(set(ids)), "Region IDs must be unique within a response"

    def test_region_ids_match_pattern(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        for region in data["regions"]:
            assert _REGION_ID_PATTERN.match(
                region["id"]
            ), f"Region ID {region['id']!r} does not match ^r\\d+$"

    def test_only_canonical_region_types(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        for region in data["regions"]:
            assert (
                region["type"] in _CANONICAL_TYPES
            ), f"Non-canonical region type: {region['type']!r}"

    def test_all_five_canonical_types_represented(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        types_present = {r["type"] for r in data["regions"]}
        assert (
            types_present == _CANONICAL_TYPES
        ), f"Expected all 5 canonical types; got {types_present}"

    def test_region_confidence_in_range(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        for region in data["regions"]:
            assert 0.0 <= region["confidence"] <= 1.0

    def test_region_bbox_fields_present(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        for region in data["regions"]:
            assert {"x_min", "y_min", "x_max", "y_max"}.issubset(region["bbox"].keys())

    def test_region_bbox_valid(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        for region in data["regions"]:
            bbox = region["bbox"]
            assert bbox["x_min"] < bbox["x_max"]
            assert bbox["y_min"] < bbox["y_max"]


# ---------------------------------------------------------------------------
# layout_conf_summary contract
# ---------------------------------------------------------------------------


class TestLayoutConfSummary:
    def test_conf_summary_fields_present(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        summary = data["layout_conf_summary"]
        assert "mean_conf" in summary
        assert "low_conf_frac" in summary

    def test_mean_conf_in_range(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert 0.0 <= data["layout_conf_summary"]["mean_conf"] <= 1.0

    def test_low_conf_frac_in_range(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert 0.0 <= data["layout_conf_summary"]["low_conf_frac"] <= 1.0


# ---------------------------------------------------------------------------
# region_type_histogram contract
# ---------------------------------------------------------------------------


class TestRegionTypeHistogram:
    def test_histogram_is_dict(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert isinstance(data["region_type_histogram"], dict)

    def test_histogram_non_empty(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert len(data["region_type_histogram"]) > 0

    def test_histogram_values_non_negative(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        for key, count in data["region_type_histogram"].items():
            assert count >= 0, f"histogram[{key!r}] = {count} must be >= 0"

    def test_histogram_keys_are_canonical(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        for key in data["region_type_histogram"]:
            assert key in _CANONICAL_TYPES, f"Non-canonical histogram key: {key!r}"

    def test_histogram_consistent_with_regions(self, client: TestClient) -> None:
        data = client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        total = sum(data["region_type_histogram"].values())
        assert total == len(data["regions"])


# ---------------------------------------------------------------------------
# Material type acceptance
# ---------------------------------------------------------------------------


class TestMaterialTypeAcceptance:
    @pytest.mark.parametrize("material_type", ["book", "newspaper", "archival_document"])
    def test_canonical_material_types_accepted(
        self, client: TestClient, material_type: str
    ) -> None:
        payload = {**_VALID_PAYLOAD, "material_type": material_type}
        resp = client.post("/v1/layout-detect", json=payload)
        assert resp.status_code == 200

    def test_invalid_material_type_rejected(self, client: TestClient) -> None:
        payload = {**_VALID_PAYLOAD, "material_type": "microfilm"}
        resp = client.post("/v1/layout-detect", json=payload)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Request validation errors — 422
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def test_page_number_zero_rejected(self, client: TestClient) -> None:
        payload = {**_VALID_PAYLOAD, "page_number": 0}
        resp = client.post("/v1/layout-detect", json=payload)
        assert resp.status_code == 422

    def test_missing_job_id_rejected(self, client: TestClient) -> None:
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "job_id"}
        resp = client.post("/v1/layout-detect", json=payload)
        assert resp.status_code == 422

    def test_missing_image_uri_rejected(self, client: TestClient) -> None:
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "image_uri"}
        resp = client.post("/v1/layout-detect", json=payload)
        assert resp.status_code == 422

    def test_missing_material_type_rejected(self, client: TestClient) -> None:
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "material_type"}
        resp = client.post("/v1/layout-detect", json=payload)
        assert resp.status_code == 422

    def test_missing_page_number_rejected(self, client: TestClient) -> None:
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "page_number"}
        resp = client.post("/v1/layout-detect", json=payload)
        assert resp.status_code == 422

    def test_empty_body_rejected(self, client: TestClient) -> None:
        resp = client.post("/v1/layout-detect", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Wrong HTTP method
# ---------------------------------------------------------------------------


class TestWrongMethod:
    def test_get_returns_405(self, client: TestClient) -> None:
        resp = client.get("/v1/layout-detect")
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Failure simulation
# ---------------------------------------------------------------------------


class TestFailureSimulation:
    def test_mock_fail_returns_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP2B_MOCK_FAIL", "true")
        resp = client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        assert resp.status_code == 500

    def test_mock_fail_body_has_error_code(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP2B_MOCK_FAIL", "true")
        resp = client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        assert resp.status_code == 500
        detail = resp.json().get("detail", {})
        assert "error_code" in detail

    def test_no_failure_by_default(self, client: TestClient) -> None:
        resp = client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Configurable confidence
# ---------------------------------------------------------------------------


class TestConfigurableConfidence:
    def test_mock_confidence_reflected_in_regions(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No postprocessing in Packet 6.3: raw confidence is returned as-is.
        monkeypatch.setenv("IEP2B_MOCK_CONFIDENCE", "0.55")
        resp = client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        assert resp.status_code == 200
        for region in resp.json()["regions"]:
            assert abs(region["confidence"] - 0.55) < 1e-6

    def test_confidence_clamped_above_one(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP2B_MOCK_CONFIDENCE", "1.5")
        resp = client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        assert resp.status_code == 200
        for region in resp.json()["regions"]:
            assert region["confidence"] <= 1.0

    def test_confidence_clamped_below_zero(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP2B_MOCK_CONFIDENCE", "-0.3")
        resp = client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        assert resp.status_code == 200
        for region in resp.json()["regions"]:
            assert region["confidence"] >= 0.0
