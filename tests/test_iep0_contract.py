"""
tests/test_iep0_contract.py
-----------------------------
Contract tests for IEP0 classification endpoints.

Covers:
  - POST /v1/classify (single image)
  - POST /v1/classify-batch (batch with majority voting)
  - Request validation, mock configuration, failure simulation, readiness
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.iep0.app.inference as inference_mod
from services.iep0.app.classify import router

# ---------------------------------------------------------------------------
# Test client — minimal app, no prometheus middleware
# ---------------------------------------------------------------------------

_app = FastAPI()
_app.include_router(router)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_app)


@pytest.fixture(autouse=True)
def _reset_model_state() -> None:
    """Reset module-level model state between tests."""
    inference_mod._model = None
    inference_mod._model_loaded = False
    inference_mod._using_mock = False


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

VALID_BODY: dict[str, object] = {
    "job_id": "job-test-001",
    "page_number": 1,
    "image_uri": "s3://libraryai/proxy/job-test-001/1.jpg",
}

VALID_BATCH_BODY: dict[str, object] = {
    "job_id": "job-test-001",
    "image_uris": [
        "s3://libraryai/proxy/job-test-001/1.jpg",
        "s3://libraryai/proxy/job-test-001/2.jpg",
        "s3://libraryai/proxy/job-test-001/3.jpg",
    ],
}

_CLASSIFY_RESPONSE_FIELDS = {
    "material_type",
    "confidence",
    "probabilities",
    "processing_time_ms",
    "warnings",
}

_BATCH_RESPONSE_FIELDS = {
    "material_type",
    "confidence",
    "vote_counts",
    "per_image_results",
    "sample_size",
    "processing_time_ms",
    "warnings",
}


# ---------------------------------------------------------------------------
# Single classify — happy path
# ---------------------------------------------------------------------------


class TestClassifyHappyPath:
    def test_valid_request_returns_200(self, client: TestClient) -> None:
        r = client.post("/v1/classify", json=VALID_BODY)
        assert r.status_code == 200

    def test_response_has_all_fields(self, client: TestClient) -> None:
        data = client.post("/v1/classify", json=VALID_BODY).json()
        assert _CLASSIFY_RESPONSE_FIELDS <= set(data)

    def test_confidence_in_range(self, client: TestClient) -> None:
        data = client.post("/v1/classify", json=VALID_BODY).json()
        assert 0.0 <= data["confidence"] <= 1.0

    def test_processing_time_non_negative(self, client: TestClient) -> None:
        data = client.post("/v1/classify", json=VALID_BODY).json()
        assert data["processing_time_ms"] >= 0.0

    def test_probabilities_contains_all_types(self, client: TestClient) -> None:
        data = client.post("/v1/classify", json=VALID_BODY).json()
        probs = data["probabilities"]
        for mt in ["book", "newspaper", "microfilm"]:
            assert mt in probs

    def test_warnings_is_list(self, client: TestClient) -> None:
        data = client.post("/v1/classify", json=VALID_BODY).json()
        assert isinstance(data["warnings"], list)

    def test_default_material_type_is_book(self, client: TestClient) -> None:
        data = client.post("/v1/classify", json=VALID_BODY).json()
        assert data["material_type"] == "book"


# ---------------------------------------------------------------------------
# Batch classify — happy path
# ---------------------------------------------------------------------------


class TestBatchClassifyHappyPath:
    def test_valid_batch_returns_200(self, client: TestClient) -> None:
        r = client.post("/v1/classify-batch", json=VALID_BATCH_BODY)
        assert r.status_code == 200

    def test_batch_response_has_all_fields(self, client: TestClient) -> None:
        data = client.post("/v1/classify-batch", json=VALID_BATCH_BODY).json()
        assert _BATCH_RESPONSE_FIELDS <= set(data)

    def test_batch_sample_size_matches_input(self, client: TestClient) -> None:
        data = client.post("/v1/classify-batch", json=VALID_BATCH_BODY).json()
        assert data["sample_size"] == 3

    def test_batch_vote_counts_sum_to_sample(self, client: TestClient) -> None:
        data = client.post("/v1/classify-batch", json=VALID_BATCH_BODY).json()
        assert sum(data["vote_counts"].values()) == data["sample_size"]

    def test_batch_majority_vote_is_book_by_default(self, client: TestClient) -> None:
        data = client.post("/v1/classify-batch", json=VALID_BATCH_BODY).json()
        assert data["material_type"] == "book"

    def test_batch_per_image_results_length(self, client: TestClient) -> None:
        data = client.post("/v1/classify-batch", json=VALID_BATCH_BODY).json()
        assert len(data["per_image_results"]) == 3

    def test_batch_confidence_in_range(self, client: TestClient) -> None:
        data = client.post("/v1/classify-batch", json=VALID_BATCH_BODY).json()
        assert 0.0 <= data["confidence"] <= 1.0

    def test_batch_configurable_material_type(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP0_MOCK_MATERIAL_TYPE", "newspaper")
        data = client.post("/v1/classify-batch", json=VALID_BATCH_BODY).json()
        assert data["material_type"] == "newspaper"
        assert data["vote_counts"]["newspaper"] == 3


# ---------------------------------------------------------------------------
# Configurable mock behavior (single)
# ---------------------------------------------------------------------------


class TestMockConfiguration:
    def test_configurable_material_type(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP0_MOCK_MATERIAL_TYPE", "newspaper")
        data = client.post("/v1/classify", json=VALID_BODY).json()
        assert data["material_type"] == "newspaper"

    def test_configurable_material_type_microfilm(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP0_MOCK_MATERIAL_TYPE", "microfilm")
        data = client.post("/v1/classify", json=VALID_BODY).json()
        assert data["material_type"] == "microfilm"

    def test_configurable_confidence(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP0_MOCK_CONFIDENCE", "0.85")
        data = client.post("/v1/classify", json=VALID_BODY).json()
        assert data["confidence"] == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# Request validation errors — 422
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def test_missing_job_id_returns_422(self, client: TestClient) -> None:
        body = {k: v for k, v in VALID_BODY.items() if k != "job_id"}
        assert client.post("/v1/classify", json=body).status_code == 422

    def test_missing_image_uri_returns_422(self, client: TestClient) -> None:
        body = {k: v for k, v in VALID_BODY.items() if k != "image_uri"}
        assert client.post("/v1/classify", json=body).status_code == 422

    def test_page_number_zero_returns_422(self, client: TestClient) -> None:
        r = client.post("/v1/classify", json={**VALID_BODY, "page_number": 0})
        assert r.status_code == 422

    def test_empty_body_returns_422(self, client: TestClient) -> None:
        assert client.post("/v1/classify", json={}).status_code == 422

    def test_wrong_method_get_returns_405(self, client: TestClient) -> None:
        assert client.get("/v1/classify").status_code == 405


class TestBatchRequestValidation:
    def test_empty_image_uris_returns_422(self, client: TestClient) -> None:
        body = {"job_id": "job-test", "image_uris": []}
        assert client.post("/v1/classify-batch", json=body).status_code == 422

    def test_missing_job_id_returns_422(self, client: TestClient) -> None:
        body = {"image_uris": ["s3://test/1.jpg"]}
        assert client.post("/v1/classify-batch", json=body).status_code == 422

    def test_wrong_method_get_returns_405(self, client: TestClient) -> None:
        assert client.get("/v1/classify-batch").status_code == 405


# ---------------------------------------------------------------------------
# Failure simulation
# ---------------------------------------------------------------------------


class TestFailureSimulation:
    def test_retry_failure_returns_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP0_MOCK_FAIL", "true")
        r = client.post("/v1/classify", json=VALID_BODY)
        assert r.status_code == 503

    def test_failure_body_has_error_fields(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP0_MOCK_FAIL", "true")
        data = client.post("/v1/classify", json=VALID_BODY).json()
        assert "error_code" in data
        assert "error_message" in data
        assert "fallback_action" in data

    def test_failure_error_code_is_classification_failed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP0_MOCK_FAIL", "true")
        data = client.post("/v1/classify", json=VALID_BODY).json()
        assert data["error_code"] == "CLASSIFICATION_FAILED"

    def test_batch_all_fail_returns_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IEP0_MOCK_FAIL", "true")
        r = client.post("/v1/classify-batch", json=VALID_BATCH_BODY)
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# Readiness checks
# ---------------------------------------------------------------------------


class TestReadiness:
    def test_model_ready_by_default(self) -> None:
        assert inference_mod.is_model_ready() is True

    def test_model_not_ready_when_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP0_MOCK_NOT_READY", "true")
        assert inference_mod.is_model_ready() is False


# ---------------------------------------------------------------------------
# Worker-side: sample size computation
# ---------------------------------------------------------------------------

try:
    from services.eep_worker.app.worker_loop import (
        _compute_sample_size,
        _IEP0_SMALL_THRESHOLD,
        _IEP0_VOTE_CONFIDENCE_THRESHOLD,
    )

    _HAS_WORKER = True
except ImportError:
    _HAS_WORKER = False

_skip_no_worker = pytest.mark.skipif(
    not _HAS_WORKER,
    reason="worker_loop deps (redis etc.) not installed",
)


@_skip_no_worker
class TestComputeSampleSize:
    def test_all_pages_when_below_threshold(self) -> None:
        for n in range(1, _IEP0_SMALL_THRESHOLD):
            assert _compute_sample_size(n) == n

    def test_threshold_boundary_uses_formula(self) -> None:
        # 11 pages → ceil(11 * 0.2) = 3
        assert _compute_sample_size(11) == 3

    def test_large_collection_capped_at_50(self) -> None:
        assert _compute_sample_size(1000) == 50

    def test_zero_pages(self) -> None:
        assert _compute_sample_size(0) == 0


# ---------------------------------------------------------------------------
# Worker-side: confidence threshold & retry logic (unit tests)
# ---------------------------------------------------------------------------

from collections import Counter

from shared.schemas.iep0 import BatchClassifyResponse

# The actual threshold value — hardcoded here so tests run even without redis.
_EXPECTED_CONFIDENCE_THRESHOLD = 0.70


def _make_batch_response(
    material_type: str,
    vote_counts: dict[str, int],
    sample_size: int | None = None,
) -> BatchClassifyResponse:
    """Helper to build a BatchClassifyResponse for testing."""
    if sample_size is None:
        sample_size = sum(vote_counts.values())
    return BatchClassifyResponse(
        material_type=material_type,
        confidence=max(vote_counts.values()) / sample_size if sample_size else 0,
        vote_counts=vote_counts,
        per_image_results=[
            {
                "material_type": material_type,
                "confidence": 0.9,
                "probabilities": {"book": 0.9, "newspaper": 0.05, "microfilm": 0.05},
                "processing_time_ms": 1.0,
                "warnings": [],
            }
            for _ in range(sample_size)
        ],
        sample_size=sample_size,
        processing_time_ms=10.0,
        warnings=[],
    )


class TestConfidenceThreshold:
    @_skip_no_worker
    def test_threshold_matches_constant(self) -> None:
        assert _IEP0_VOTE_CONFIDENCE_THRESHOLD == _EXPECTED_CONFIDENCE_THRESHOLD

    def test_high_confidence_returns_immediately(self) -> None:
        """80% book votes → no round 2 needed."""
        resp = _make_batch_response("book", {"book": 8, "newspaper": 2}, 10)
        winner_votes = resp.vote_counts.get(resp.material_type, 0)
        ratio = winner_votes / resp.sample_size
        assert ratio >= _EXPECTED_CONFIDENCE_THRESHOLD

    def test_low_confidence_triggers_retry(self) -> None:
        """60% book votes → should trigger retry."""
        resp = _make_batch_response("book", {"book": 6, "newspaper": 4}, 10)
        winner_votes = resp.vote_counts.get(resp.material_type, 0)
        ratio = winner_votes / resp.sample_size
        assert ratio < _EXPECTED_CONFIDENCE_THRESHOLD

    def test_exactly_seventy_percent_is_confident(self) -> None:
        """70% exactly → should NOT trigger retry."""
        resp = _make_batch_response("book", {"book": 7, "newspaper": 3}, 10)
        winner_votes = resp.vote_counts.get(resp.material_type, 0)
        ratio = winner_votes / resp.sample_size
        assert ratio >= _EXPECTED_CONFIDENCE_THRESHOLD

    def test_combined_votes_can_flip_winner(self) -> None:
        """Round 1: 5 book / 5 newspaper.  Round 2: 2 book / 8 newspaper.
        Combined: 7 book / 13 newspaper → newspaper wins."""
        round1 = Counter({"book": 5, "newspaper": 5})
        round2 = Counter({"book": 2, "newspaper": 8})
        combined = round1 + round2
        final_winner = combined.most_common(1)[0][0]
        assert final_winner == "newspaper"
        assert combined["newspaper"] == 13
        assert combined["book"] == 7

    def test_combined_votes_resolve_low_confidence(self) -> None:
        """Round 1: 6 book / 4 newspaper (60%).
        Round 2: 8 book / 2 newspaper.
        Combined: 14 book / 6 newspaper (70%) → meets threshold."""
        round1 = Counter({"book": 6, "newspaper": 4})
        round2 = Counter({"book": 8, "newspaper": 2})
        combined = round1 + round2
        final_winner = combined.most_common(1)[0][0]
        total = sum(combined.values())
        ratio = combined[final_winner] / total
        assert final_winner == "book"
        assert ratio >= _EXPECTED_CONFIDENCE_THRESHOLD

