"""
tests/test_p5_correction_apply.py
-----------------------------------
Packet 5.2 — Single-page correction apply path tests.

Covers:
  POST /v1/jobs/{job_id}/pages/{page_number}/correction

  - Apply correction (any mode) → state becomes semantic_norm, enqueued for iep1e
  - Worker runs iep1e then routes to layout_detection (layout) or accepted (preprocess)
  - Reject invalid state (not pending_human_correction) → 409
  - Reject missing page → 404
  - Reject missing job → 404
  - Correction fields persisted to lineage row
  - Idempotency: repeat call after first succeeds returns 409 (state guard)
  - crop_box validation: wrong length → 422
  - crop_box validation: x_min >= x_max → 422
  - crop_box validation: negative values → 422
  - Derived corrected URI written to lineage.output_image_uri
  - notes stored in lineage.reviewer_notes
  - Non-null split_x routes to split correction path (Packet 5.3), not tested here
  - Corrected artifact written through storage backend
  - Missing lineage row returns 500
  - Missing source artifact URI returns 500

Session is mocked; no live database required.
HTTP endpoints are tested via FastAPI TestClient with dependency override.
advance_page_state is patched for isolation.
get_backend is patched at class level to prevent real storage I/O.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from services.eep.app.correction.apply import CorrectionApplyRequest, _derive_corrected_uri
from services.eep.app.db.session import get_session
from services.eep.app.main import app
from shared.normalization.perspective import four_point_transform

pytestmark = pytest.mark.usefixtures("_bypass_require_user")

# ── Factories ──────────────────────────────────────────────────────────────────


def _make_job(
    job_id: str = "job-001",
    pipeline_mode: str = "layout",
) -> MagicMock:
    job = MagicMock()
    job.job_id = job_id
    job.pipeline_mode = pipeline_mode
    return job


def _make_page(
    page_id: str = "p1",
    job_id: str = "job-001",
    page_number: int = 1,
    sub_page_index: int | None = None,
    status: str = "pending_human_correction",
    output_image_uri: str | None = "s3://bucket/norm.tiff",
    input_image_uri: str | None = "s3://bucket/raw.tiff",
) -> MagicMock:
    page = MagicMock()
    page.page_id = page_id
    page.job_id = job_id
    page.page_number = page_number
    page.sub_page_index = sub_page_index
    page.status = status
    page.output_image_uri = output_image_uri
    page.input_image_uri = input_image_uri
    return page


def _make_lineage(
    lineage_id: str = "lin-001",
    job_id: str = "job-001",
    page_number: int = 1,
    sub_page_index: int | None = None,
    output_image_uri: str | None = "s3://bucket/norm.tiff",
    otiff_uri: str | None = "s3://bucket/raw.tiff",
    human_correction_fields: dict[str, Any] | None = None,
) -> MagicMock:
    lineage = MagicMock()
    lineage.lineage_id = lineage_id
    lineage.job_id = job_id
    lineage.page_number = page_number
    lineage.sub_page_index = sub_page_index
    lineage.output_image_uri = output_image_uri
    lineage.otiff_uri = otiff_uri
    lineage.iep1d_used = False
    lineage.human_corrected = False
    lineage.human_correction_timestamp = None
    lineage.human_correction_fields = human_correction_fields
    lineage.reviewer_notes = None
    return lineage


def _make_session(
    job: Any = None,
    first_results: list[Any] | None = None,
    all_results: list[Any] | None = None,
) -> MagicMock:
    """
    Build a mock SQLAlchemy session for the apply endpoint.

    Query order in apply_correction:
      query #0 (.first()) — fetch JobPage by (job_id, page_number, sub_page_index=NULL)
      query #1 (.first()) — fetch PageLineage by (job_id, page_number, sub_page_index=NULL)
      query #2 (.all())   — _leaf_pages for auto_continue gate release

    Args:
        job:          Value returned by db.get(Job, job_id).
        first_results: Sequential return values for .first() calls (indexed 0, 1, …).
        all_results:   Sequential return values for .all() calls (indexed 0, 1, …).
    """
    session = MagicMock()
    session.get.return_value = job

    first_queue: list[Any] = list(first_results or [])
    all_queue: list[Any] = list(all_results or [])

    def query_se(*args: Any, **kwargs: Any) -> MagicMock:
        chain = MagicMock()
        chain.filter.return_value = chain

        def first_se() -> Any:
            return first_queue.pop(0) if first_queue else None

        def all_se() -> Any:
            return all_queue.pop(0) if all_queue else []

        chain.first.side_effect = first_se
        chain.all.side_effect = all_se
        return chain

    session.query.side_effect = query_se
    return session


# ── Unit tests: helpers ─────────────────────────────────────────────────────────


class TestDeriveCorrectUri:
    def test_inserts_corrected_before_extension(self) -> None:
        assert _derive_corrected_uri("s3://bucket/norm.tiff") == "s3://bucket/norm_corrected.tiff"

    def test_appends_corrected_when_no_extension(self) -> None:
        assert _derive_corrected_uri("s3://bucket/page") == "s3://bucket/page_corrected"

    def test_none_returns_none(self) -> None:
        assert _derive_corrected_uri(None) is None

    def test_multi_dot_path_uses_last_extension(self) -> None:
        result = _derive_corrected_uri("s3://bucket/v1.2/page.tiff")
        assert result == "s3://bucket/v1.2/page_corrected.tiff"


# ── Unit tests: CorrectionApplyRequest validation ──────────────────────────────


class TestCorrectionApplyRequestValidation:
    def test_valid_request(self) -> None:
        req = CorrectionApplyRequest(crop_box=[10, 20, 500, 700], deskew_angle=0.5)
        assert req.crop_box == [10, 20, 500, 700]
        assert req.deskew_angle == pytest.approx(0.5)
        assert req.split_x is None
        assert req.notes is None

    def test_crop_box_wrong_length_raises(self) -> None:
        with pytest.raises(Exception):
            CorrectionApplyRequest(crop_box=[10, 20, 500], deskew_angle=0.0)

    def test_crop_box_negative_values_raises(self) -> None:
        with pytest.raises(Exception):
            CorrectionApplyRequest(crop_box=[-1, 20, 500, 700], deskew_angle=0.0)

    def test_crop_box_x_min_ge_x_max_raises(self) -> None:
        with pytest.raises(Exception):
            CorrectionApplyRequest(crop_box=[500, 20, 500, 700], deskew_angle=0.0)

    def test_crop_box_y_min_ge_y_max_raises(self) -> None:
        with pytest.raises(Exception):
            CorrectionApplyRequest(crop_box=[10, 700, 500, 700], deskew_angle=0.0)

    def test_optional_fields_accepted(self) -> None:
        req = CorrectionApplyRequest(
            crop_box=[0, 0, 100, 200],
            deskew_angle=-1.5,
            page_structure="spread",
            split_x=300,
            source_artifact_uri="s3://bucket/raw.tiff",
            notes="looks good",
        )
        assert req.page_structure == "spread"
        assert req.split_x == 300
        assert req.source_artifact_uri == "s3://bucket/raw.tiff"
        assert req.notes == "looks good"

    def test_null_deskew_angle_accepted(self) -> None:
        req = CorrectionApplyRequest(crop_box=[0, 0, 100, 200], deskew_angle=None)
        assert req.deskew_angle is None

    def test_null_crop_box_accepted(self) -> None:
        req = CorrectionApplyRequest(crop_box=None, deskew_angle=None, page_structure="single")
        assert req.crop_box is None
        assert req.page_structure == "single"

    def test_quad_selection_requires_four_points(self) -> None:
        with pytest.raises(Exception):
            CorrectionApplyRequest(
                selection_mode="quad",
                quad_points=[(0, 0), (100, 0), (100, 200)],
            )

    def test_quad_points_infer_quad_selection_mode(self) -> None:
        req = CorrectionApplyRequest(
            quad_points=[(10, 20), (110, 20), (120, 220), (0, 220)],
        )
        assert req.selection_mode == "quad"
        assert req.quad_points == [(10.0, 20.0), (110.0, 20.0), (120.0, 220.0), (0.0, 220.0)]


# ── HTTP endpoint tests ────────────────────────────────────────────────────────


_DEFAULT_BODY = {"crop_box": [10, 20, 500, 700], "deskew_angle": 0.5}


class TestApplyCorrectionEndpoint:
    def setup_method(self) -> None:
        self.client = TestClient(app)
        # Prevent real storage I/O in all endpoint tests.
        image = np.full((32, 32, 3), 255, dtype=np.uint8)
        ok, encoded = cv2.imencode(".tiff", image)
        assert ok
        self.mock_backend = MagicMock()
        self.mock_backend.get_bytes.return_value = encoded.tobytes()
        self._storage_patcher = patch(
            "services.eep.app.correction.apply.get_backend",
            return_value=self.mock_backend,
        )
        self._storage_patcher.start()
        # Prevent real Redis connections for all endpoint tests.
        from services.eep.app.redis_client import get_redis

        self.mock_redis = MagicMock()
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

    def teardown_method(self) -> None:
        self._storage_patcher.stop()
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    # ── Core path ─────────────────────────────────────────────────────────────

    def test_apply_correction_returns_ok(self) -> None:
        """Successful correction returns 200 with status='ok'."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)

        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_apply_correction_transitions_to_semantic_norm_in_layout_mode(self) -> None:
        """advance_page_state called with to_state=semantic_norm for layout pipeline."""
        job = _make_job(pipeline_mode="layout")
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch(
            "services.eep.app.correction.apply.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)

        assert r.status_code == 200
        mock_advance.assert_called_once_with(
            session,
            page.page_id,
            from_state="pending_human_correction",
            to_state="semantic_norm",
        )

    def test_apply_correction_transitions_to_semantic_norm_in_preprocess_mode(self) -> None:
        """advance_page_state called with to_state=semantic_norm for preprocess pipeline too."""
        job = _make_job(pipeline_mode="preprocess")
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch(
            "services.eep.app.correction.apply.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)

        assert r.status_code == 200
        mock_advance.assert_called_once_with(
            session,
            page.page_id,
            from_state="pending_human_correction",
            to_state="semantic_norm",
        )

    def test_child_correction_targets_requested_sub_page_only(self) -> None:
        job = _make_job(pipeline_mode="layout")
        parent_image = np.arange(32 * 32 * 3, dtype=np.uint8).reshape(32, 32, 3)
        ok, encoded = cv2.imencode(".tiff", parent_image)
        assert ok
        self.mock_backend.get_bytes.return_value = encoded.tobytes()
        child = _make_page(
            page_id="child-p1",
            sub_page_index=1,
            status="pending_human_correction",
            output_image_uri=None,
        )
        child_lineage = _make_lineage(
            lineage_id="lin-child-1",
            sub_page_index=1,
            output_image_uri=None,
            otiff_uri="s3://bucket/raw-parent.tiff",
            human_correction_fields={"source_artifact_uri": "s3://bucket/norm-parent.tiff"},
        )

        session = _make_session(job=job, first_results=[child, child_lineage])
        self._inject(session)

        with patch(
            "services.eep.app.correction.apply.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post(
                "/v1/jobs/job-001/pages/1/correction?sub_page_index=1",
                json={
                    "selection_mode": "quad",
                    "quad_points": [[8, 6], [20, 4], [22, 22], [6, 24]],
                    "deskew_angle": None,
                },
            )

        assert r.status_code == 200
        mock_advance.assert_called_once_with(
            session,
            child.page_id,
            from_state="pending_human_correction",
            to_state="semantic_norm",
        )
        self.mock_backend.get_bytes.assert_called_with("s3://bucket/norm-parent.tiff")
        assert child.output_image_uri == "s3://bucket/jobs/job-001/corrected/1_1.tiff"
        assert child_lineage.output_image_uri == "s3://bucket/jobs/job-001/corrected/1_1.tiff"
        written_uri, written_bytes = self.mock_backend.put_bytes.call_args.args
        assert written_uri == "s3://bucket/jobs/job-001/corrected/1_1.tiff"
        decoded = cv2.imdecode(np.frombuffer(written_bytes, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        assert decoded is not None
        expected, _, _ = four_point_transform(
            parent_image,
            [(8.0, 6.0), (20.0, 4.0), (22.0, 22.0), (6.0, 24.0)],
        )
        assert np.array_equal(decoded, expected)
        assert child_lineage.human_correction_fields["selection_mode"] == "quad"
        assert child_lineage.human_correction_fields["quad_points"] == [
            [8, 6],
            [20, 4],
            [22, 22],
            [6, 24],
        ]

    def test_apply_correction_uses_requested_source_artifact_uri(self) -> None:
        job = _make_job(pipeline_mode="layout")
        page = _make_page(
            status="pending_human_correction",
            output_image_uri="s3://bucket/norm.tiff",
            input_image_uri="s3://bucket/input.tiff",
        )
        lineage = _make_lineage(
            output_image_uri="s3://bucket/norm.tiff",
            otiff_uri="s3://bucket/original.tiff",
        )
        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post(
                "/v1/jobs/job-001/pages/1/correction",
                json={**_DEFAULT_BODY, "source_artifact_uri": "s3://bucket/original.tiff"},
            )

        assert r.status_code == 200
        self.mock_backend.get_bytes.assert_called_with("s3://bucket/original.tiff")
        assert page.output_image_uri == "s3://bucket/original_corrected.tiff"
        assert lineage.output_image_uri == "s3://bucket/original_corrected.tiff"
        assert lineage.human_correction_fields["source_artifact_uri"] == "s3://bucket/original.tiff"

    def test_invalid_requested_source_artifact_uri_returns_422(self) -> None:
        job = _make_job(pipeline_mode="layout")
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()
        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        r = self.client.post(
            "/v1/jobs/job-001/pages/1/correction",
            json={**_DEFAULT_BODY, "source_artifact_uri": "s3://bucket/not-allowed.tiff"},
        )

        assert r.status_code == 422
        assert "source_artifact_uri" in r.json()["detail"]

    # ── Error cases ───────────────────────────────────────────────────────────

    def test_404_when_job_not_found(self) -> None:
        session = _make_session(job=None)
        self._inject(session)

        r = self.client.post("/v1/jobs/missing-job/pages/1/correction", json=_DEFAULT_BODY)
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_404_when_page_not_found(self) -> None:
        """page query returns None → 404."""
        job = _make_job()
        session = _make_session(job=job, first_results=[None])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/99/correction", json=_DEFAULT_BODY)
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_409_when_page_not_in_pending_human_correction(self) -> None:
        """Page in wrong state → 409 with state name in detail."""
        job = _make_job()
        page = _make_page(status="layout_detection")

        session = _make_session(job=job, first_results=[page])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)
        assert r.status_code == 409
        assert "pending_human_correction" in r.json()["detail"]

    def test_409_for_accepted_page(self) -> None:
        """Page in 'accepted' state cannot have a correction applied."""
        job = _make_job()
        page = _make_page(status="accepted")

        session = _make_session(job=job, first_results=[page])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)
        assert r.status_code == 409

    def test_422_crop_box_wrong_length(self) -> None:
        """crop_box with fewer than 4 values → 422."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        session = _make_session(job=job, first_results=[page])
        self._inject(session)

        r = self.client.post(
            "/v1/jobs/job-001/pages/1/correction",
            json={"crop_box": [10, 20, 500], "deskew_angle": 0.0},
        )
        assert r.status_code == 422

    def test_422_crop_box_negative_value(self) -> None:
        """crop_box with a negative coordinate → 422."""
        job = _make_job()
        session = _make_session(job=job)
        self._inject(session)

        r = self.client.post(
            "/v1/jobs/job-001/pages/1/correction",
            json={"crop_box": [-1, 20, 500, 700], "deskew_angle": 0.0},
        )
        assert r.status_code == 422

    def test_missing_lineage_returns_error(self) -> None:
        """When no lineage row exists, the endpoint returns HTTP 500."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")

        # second .first() returns None (no lineage row)
        session = _make_session(job=job, first_results=[page, None])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)

        assert r.status_code == 500
        assert "data-integrity failure" in r.json()["detail"].lower()
        assert "lineage" in r.json()["detail"].lower()

    def test_missing_source_uri_returns_error(self) -> None:
        """Page with no output_image_uri cannot have artifact copied; returns 500."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            output_image_uri=None,
            input_image_uri=None,
        )
        lineage = _make_lineage(output_image_uri=None, otiff_uri=None)

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)

        assert r.status_code == 500
        assert "data-integrity failure" in r.json()["detail"].lower()
        assert "source artifact uri" in r.json()["detail"].lower()

    # ── Storage backend ───────────────────────────────────────────────────────

    def test_corrected_artifact_written_to_storage(self) -> None:
        """Source artifact is read and corrected artifact is written via storage backend."""
        job = _make_job()
        page = _make_page(status="pending_human_correction", output_image_uri="s3://b/p.tiff")
        lineage = _make_lineage(output_image_uri="s3://b/p.tiff")

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)

        assert r.status_code == 200
        self.mock_backend.get_bytes.assert_called_once_with("s3://b/p.tiff")
        self.mock_backend.put_bytes.assert_called_once()
        put_uri, put_bytes = self.mock_backend.put_bytes.call_args.args
        assert put_uri == "s3://b/p_corrected.tiff"
        assert isinstance(put_bytes, bytes)
        assert put_bytes

    # ── Persistence checks ────────────────────────────────────────────────────

    def test_correction_fields_persisted_to_lineage(self) -> None:
        """crop_box and deskew_angle written to lineage.human_correction_fields."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post(
                "/v1/jobs/job-001/pages/1/correction",
                json={"crop_box": [5, 10, 400, 600], "deskew_angle": 1.5},
            )

        assert r.status_code == 200
        assert lineage.human_corrected is True
        assert lineage.human_correction_fields["crop_box"] == [5, 10, 400, 600]
        assert lineage.human_correction_fields["deskew_angle"] == pytest.approx(1.5)
        assert lineage.human_correction_timestamp is not None

    def test_null_deskew_angle_persisted_without_error(self) -> None:
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post(
                "/v1/jobs/job-001/pages/1/correction",
                json={"crop_box": [5, 10, 400, 600], "deskew_angle": None},
            )

        assert r.status_code == 200
        assert lineage.human_correction_fields["deskew_angle"] is None

    def test_notes_stored_in_reviewer_notes(self) -> None:
        """notes field written to lineage.reviewer_notes."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post(
                "/v1/jobs/job-001/pages/1/correction",
                json={**_DEFAULT_BODY, "notes": "page was skewed"},
            )

        assert r.status_code == 200
        assert lineage.reviewer_notes == "page was skewed"

    def test_corrected_uri_written_to_lineage(self) -> None:
        """Derived corrected URI is stored in lineage.output_image_uri (authoritative)."""
        job = _make_job()
        page = _make_page(status="pending_human_correction", output_image_uri="s3://b/p.tiff")
        lineage = _make_lineage(output_image_uri="s3://b/p.tiff")

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)

        assert r.status_code == 200
        assert lineage.output_image_uri == "s3://b/p_corrected.tiff"

    def test_corrected_uri_also_written_to_page_record(self) -> None:
        """Corrected URI is mirrored to job_pages.output_image_uri for fast lookups."""
        job = _make_job()
        page = _make_page(status="pending_human_correction", output_image_uri="s3://b/p.tiff")
        lineage = _make_lineage(output_image_uri="s3://b/p.tiff")

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)

        assert r.status_code == 200
        assert page.output_image_uri == "s3://b/p_corrected.tiff"

    # ── Idempotency ───────────────────────────────────────────────────────────

    def test_idempotency_second_call_returns_409(self) -> None:
        """
        After a correction is applied the page moves to layout_detection (layout mode).
        A repeat call finds the page in the wrong state and returns 409,
        preserving the state machine invariant.
        """
        job = _make_job()
        # Simulate the page already having been transitioned by the first call.
        page = _make_page(status="layout_detection")

        session = _make_session(job=job, first_results=[page])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/correction", json=_DEFAULT_BODY)
        assert r.status_code == 409
        # State must be unchanged — not further transitioned or corrupted.
        assert page.status == "layout_detection"
