"""
tests/test_iep1e_service.py
-----------------------------
Contract and behaviour tests for the IEP1E service endpoint.

Runs entirely with IEP1E_MOCK_MODE=true — no PaddleOCR is loaded.
Tests cover:
  - POST /v1/semantic-norm returns valid SemanticNormResponse in mock mode
  - single-page request → 1 page result, reading_direction="unresolved"
  - two-page request → 2 page results, ordered_page_uris has 2 entries
  - mismatched page_uris / x_centers → 422
  - empty page_uris → 422
  - GET /health always returns 200
  - GET /ready returns 200 in mock mode (model always "ready")
  - GET /metrics returns 200
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def iep1e_client():
    """
    TestClient for IEP1E app with mock mode enabled and model state reset.
    """
    os.environ["IEP1E_MOCK_MODE"] = "true"

    # Reset singleton so each test module gets a clean state
    import services.iep1e.app.model as model_mod
    model_mod.reset_for_testing()

    from services.iep1e.app.main import app

    with TestClient(app) as client:
        yield client


# ── Health / readiness ────────────────────────────────────────────────────────


class TestHealthReady:
    def test_health_always_200(self, iep1e_client):
        r = iep1e_client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_ready_200_in_mock_mode(self, iep1e_client):
        r = iep1e_client.get("/ready")
        assert r.status_code == 200

    def test_metrics_200(self, iep1e_client):
        r = iep1e_client.get("/metrics")
        assert r.status_code == 200


# ── Single-page request ───────────────────────────────────────────────────────


class TestSinglePage:
    def test_returns_valid_response(self, iep1e_client):
        payload = {
            "job_id": "job-001",
            "page_number": 1,
            "page_uris": ["file://jobs/job-001/output/1.tiff"],
            "x_centers": [300.0],
            "sub_page_indices": [0],
            "material_type": "book",
        }
        r = iep1e_client.post("/v1/semantic-norm", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert len(data["pages"]) == 1
        assert data["reading_direction"] == "unresolved"
        assert len(data["ordered_page_uris"]) == 1
        assert data["fallback_used"] is True

    def test_page_uri_passthrough_in_mock(self, iep1e_client):
        uri = "file://jobs/job-001/output/1.tiff"
        payload = {
            "job_id": "job-001",
            "page_number": 1,
            "page_uris": [uri],
            "x_centers": [200.0],
            "sub_page_indices": [0],
            "material_type": "book",
        }
        r = iep1e_client.post("/v1/semantic-norm", json=payload)
        assert r.status_code == 200
        data = r.json()
        # In mock mode, oriented_uri equals original_uri (no rotation)
        assert data["pages"][0]["oriented_uri"] == uri
        assert data["pages"][0]["original_uri"] == uri
        assert data["ordered_page_uris"][0] == uri

    def test_orientation_not_confident_in_mock(self, iep1e_client):
        payload = {
            "job_id": "job-002",
            "page_number": 3,
            "page_uris": ["file://jobs/job-002/output/3.tiff"],
            "x_centers": [150.0],
            "sub_page_indices": [0],
            "material_type": "newspaper",
        }
        r = iep1e_client.post("/v1/semantic-norm", json=payload)
        assert r.status_code == 200
        orientation = r.json()["pages"][0]["orientation"]
        assert orientation["best_rotation_deg"] == 0
        assert orientation["orientation_confident"] is False


# ── Two-page (spread) request ─────────────────────────────────────────────────


class TestTwoPageSpread:
    def test_returns_two_page_results(self, iep1e_client):
        payload = {
            "job_id": "job-003",
            "page_number": 5,
            "page_uris": [
                "file://jobs/job-003/output/5_0.tiff",
                "file://jobs/job-003/output/5_1.tiff",
            ],
            "x_centers": [200.0, 600.0],
            "sub_page_indices": [0, 1],
            "material_type": "book",
        }
        r = iep1e_client.post("/v1/semantic-norm", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert len(data["pages"]) == 2
        assert len(data["ordered_page_uris"]) == 2

    def test_ordered_page_uris_length_matches_pages(self, iep1e_client):
        payload = {
            "job_id": "job-004",
            "page_number": 2,
            "page_uris": [
                "file://jobs/job-004/output/2_0.tiff",
                "file://jobs/job-004/output/2_1.tiff",
            ],
            "x_centers": [100.0, 800.0],
            "sub_page_indices": [0, 1],
            "material_type": "book",
        }
        r = iep1e_client.post("/v1/semantic-norm", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert len(data["ordered_page_uris"]) == len(data["pages"])


# ── Validation errors ─────────────────────────────────────────────────────────


class TestValidationErrors:
    def test_empty_page_uris_rejected(self, iep1e_client):
        payload = {
            "job_id": "job-err",
            "page_number": 1,
            "page_uris": [],
            "x_centers": [],
            "sub_page_indices": [],
            "material_type": "book",
        }
        r = iep1e_client.post("/v1/semantic-norm", json=payload)
        assert r.status_code == 422

    def test_mismatched_uris_and_x_centers_rejected(self, iep1e_client):
        payload = {
            "job_id": "job-err",
            "page_number": 1,
            "page_uris": ["file://a.tiff", "file://b.tiff"],
            "x_centers": [100.0],           # length mismatch
            "sub_page_indices": [0, 1],
            "material_type": "book",
        }
        r = iep1e_client.post("/v1/semantic-norm", json=payload)
        assert r.status_code == 422

    def test_missing_required_field_rejected(self, iep1e_client):
        payload = {
            "page_number": 1,
            "page_uris": ["file://a.tiff"],
            "x_centers": [100.0],
            "sub_page_indices": [0],
            "material_type": "book",
            # missing job_id
        }
        r = iep1e_client.post("/v1/semantic-norm", json=payload)
        assert r.status_code == 422


# ── Response schema completeness ──────────────────────────────────────────────


class TestResponseSchema:
    def test_all_expected_fields_present(self, iep1e_client):
        payload = {
            "job_id": "job-schema",
            "page_number": 1,
            "page_uris": ["file://jobs/job-schema/output/1.tiff"],
            "x_centers": [300.0],
            "sub_page_indices": [0],
            "material_type": "book",
        }
        r = iep1e_client.post("/v1/semantic-norm", json=payload)
        assert r.status_code == 200
        data = r.json()

        top_level = {"pages", "reading_direction", "ordered_page_uris",
                     "fallback_used", "processing_time_ms", "warnings"}
        assert top_level.issubset(data.keys())

        page = data["pages"][0]
        page_fields = {"original_uri", "oriented_uri", "sub_page_index", "orientation"}
        assert page_fields.issubset(page.keys())

        orientation = page["orientation"]
        orient_fields = {
            "best_rotation_deg", "orientation_confident",
            "score_ratio", "score_diff", "script_evidence",
        }
        assert orient_fields.issubset(orientation.keys())

        ev = orientation["script_evidence"]
        ev_fields = {"arabic_ratio", "latin_ratio", "garbage_ratio",
                     "n_boxes", "n_chars", "mean_conf"}
        assert ev_fields.issubset(ev.keys())

    def test_processing_time_ms_non_negative(self, iep1e_client):
        payload = {
            "job_id": "job-time",
            "page_number": 1,
            "page_uris": ["file://jobs/job-time/output/1.tiff"],
            "x_centers": [300.0],
            "sub_page_indices": [0],
            "material_type": "book",
        }
        r = iep1e_client.post("/v1/semantic-norm", json=payload)
        assert r.status_code == 200
        assert r.json()["processing_time_ms"] >= 0.0
