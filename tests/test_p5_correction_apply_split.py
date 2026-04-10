"""
tests/test_p5_correction_apply_split.py
-----------------------------------------
Packet 5.3 - Spread correction apply path tests.

Covers:
  POST /v1/jobs/{job_id}/pages/{page_number}/correction

  - page_structure="spread" creates or reuses child sub-pages
  - Child artifacts are true left/right TIFF splits, not parent copies
  - Child lineage rows preserve parent linkage and store spread correction fields
  - Children remain in pending_human_correction for child-specific review
  - Parent closes to split once children exist
  - split_x can be derived from geometry gate when not provided by the client
  - Idempotency: existing children are reused, not duplicated
  - Missing parent lineage -> 500
  - Missing source artifact URI -> 500
  - Parent not found -> 404
  - Parent not pending_human_correction -> 409

Session is mocked; no live database required.
HTTP endpoints tested via FastAPI TestClient with dependency override.
advance_page_state and get_backend are patched for isolation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from services.eep.app.db.session import get_session
from services.eep.app.main import app

pytestmark = pytest.mark.usefixtures("_bypass_require_user")


def _make_job(
    job_id: str = "job-001",
    pipeline_mode: str = "layout",
) -> MagicMock:
    job = MagicMock()
    job.job_id = job_id
    job.pipeline_mode = pipeline_mode
    return job


def _make_page(
    page_id: str = "parent-p1",
    job_id: str = "job-001",
    page_number: int = 3,
    sub_page_index: int | None = None,
    status: str = "pending_human_correction",
    output_image_uri: str | None = "s3://bucket/jobs/job-001/3.tiff",
    input_image_uri: str = "s3://bucket/raw/3.tiff",
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
    lineage_id: str = "lin-parent",
    job_id: str = "job-001",
    page_number: int = 3,
    sub_page_index: int | None = None,
    output_image_uri: str | None = "s3://bucket/jobs/job-001/3.tiff",
    correlation_id: str = "corr-001",
    input_image_uri: str = "s3://bucket/raw/3.tiff",
    input_image_hash: str | None = "abc123",
    otiff_uri: str = "s3://bucket/otiff/3.tiff",
    material_type: str = "book",
    routing_path: str | None = "preprocessing_only",
    policy_version: str = "v1.0",
    parent_page_id: str | None = None,
    split_source: bool = False,
    human_correction_fields: dict[str, Any] | None = None,
) -> MagicMock:
    lineage = MagicMock()
    lineage.lineage_id = lineage_id
    lineage.job_id = job_id
    lineage.page_number = page_number
    lineage.sub_page_index = sub_page_index
    lineage.output_image_uri = output_image_uri
    lineage.correlation_id = correlation_id
    lineage.input_image_uri = input_image_uri
    lineage.input_image_hash = input_image_hash
    lineage.otiff_uri = otiff_uri
    lineage.material_type = material_type
    lineage.routing_path = routing_path
    lineage.policy_version = policy_version
    lineage.parent_page_id = parent_page_id
    lineage.split_source = split_source
    lineage.human_corrected = False
    lineage.human_correction_timestamp = None
    lineage.human_correction_fields = human_correction_fields
    lineage.reviewer_notes = None
    return lineage


def _make_gate(split_x: int | None = 450, selected_model: str | None = "iep1a") -> MagicMock:
    gate = MagicMock()
    gate.job_id = "job-001"
    gate.page_number = 3
    gate.selected_model = selected_model
    gate.iep1a_geometry = (
        {
            "page_count": 2,
            "split_required": True,
            "geometry_confidence": 0.88,
            "split_x": split_x,
            "pages": [],
        }
        if split_x is not None
        else None
    )
    gate.iep1b_geometry = None
    return gate


def _make_session(
    job: Any = None,
    first_results: list[Any] | None = None,
) -> MagicMock:
    """
    Query order in split apply path:
      .get(Job, job_id)                       -> Job
      query #0  (.first())                    -> parent JobPage
      query #1  (.first())                    -> parent PageLineage
      query #2  (.order_by().first())         -> latest geometry gate
      query #3  (.first())                    -> child JobPage sub 0
      query #4  (.first())                    -> child PageLineage sub 0
      query #5  (.first())                    -> child JobPage sub 1
      query #6  (.first())                    -> child PageLineage sub 1
    """
    session = MagicMock()
    session.get.return_value = job

    first_queue: list[Any] = list(first_results or [])

    def query_se(*args: Any, **kwargs: Any) -> MagicMock:
        chain = MagicMock()
        chain.filter.return_value = chain
        chain.order_by.return_value = chain

        def first_se() -> Any:
            return first_queue.pop(0) if first_queue else None

        chain.first.side_effect = first_se
        return chain

    session.query.side_effect = query_se
    return session


_SPREAD_BODY = {"crop_box": None, "deskew_angle": None, "page_structure": "spread"}
_SPREAD_BODY_WITH_SPLIT = {
    "crop_box": [10, 20, 900, 700],
    "deskew_angle": 0.5,
    "page_structure": "spread",
    "split_x": 450,
}


def _make_fresh_split_session(
    job: Any,
    parent_page: Any,
    parent_lineage: Any,
    gate: Any | None = None,
) -> MagicMock:
    return _make_session(
        job=job,
        first_results=[parent_page, parent_lineage, gate, None, None, None, None],
    )


def _make_parent_image(width: int = 900, height: int = 8) -> np.ndarray:
    y = np.arange(height, dtype=np.uint16)[:, None]
    x = np.arange(width, dtype=np.uint16)[None, :]
    image = np.empty((height, width, 3), dtype=np.uint8)
    image[..., 0] = ((x + y) % 256).astype(np.uint8)
    image[..., 1] = ((3 * x + 5 * y) % 256).astype(np.uint8)
    image[..., 2] = ((7 * x + 11 * y) % 256).astype(np.uint8)
    return image


def _encode_tiff(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".tiff", image)
    assert ok
    encoded_bytes: bytes = encoded.tobytes()
    return encoded_bytes


def _decode_tiff(data: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert image is not None
    return image


class TestSplitCorrectionEndpoint:
    def setup_method(self) -> None:
        self.client = TestClient(app)
        self.parent_image = _make_parent_image()
        self.parent_tiff = _encode_tiff(self.parent_image)
        self.mock_backend = MagicMock()
        self.mock_backend.get_bytes.return_value = self.parent_tiff
        self._storage_patcher = patch(
            "services.eep.app.correction.apply.get_backend",
            return_value=self.mock_backend,
        )
        self._storage_patcher.start()

        from services.eep.app.redis_client import get_redis

        self.mock_redis = MagicMock()
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

    def teardown_method(self) -> None:
        self._storage_patcher.stop()
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    def test_split_returns_200_ok(self) -> None:
        job = _make_job()
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage, _make_gate())
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_split_creates_two_child_pages(self) -> None:
        job = _make_job()
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage, _make_gate())
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)

        assert response.status_code == 200
        assert session.add.call_count == 4

    def test_split_artifacts_written_for_each_child(self) -> None:
        job = _make_job()
        parent = _make_page(output_image_uri="s3://bucket/jobs/job-001/3.tiff")
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage, _make_gate())
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)

        assert response.status_code == 200
        self.mock_backend.get_bytes.assert_called_once_with("s3://bucket/jobs/job-001/3.tiff")
        assert self.mock_backend.put_bytes.call_count == 2
        put_calls = [call.args[0] for call in self.mock_backend.put_bytes.call_args_list]
        assert "s3://bucket/jobs/job-001/corrected/3_0.tiff" in put_calls
        assert "s3://bucket/jobs/job-001/corrected/3_1.tiff" in put_calls

    def test_split_artifacts_are_actual_left_and_right_portions(self) -> None:
        job = _make_job()
        parent = _make_page(output_image_uri="s3://bucket/jobs/job-001/3.tiff")
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage, _make_gate())
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)

        assert response.status_code == 200
        written = {
            call.args[0]: _decode_tiff(call.args[1])
            for call in self.mock_backend.put_bytes.call_args_list
        }
        left = written["s3://bucket/jobs/job-001/corrected/3_0.tiff"]
        right = written["s3://bucket/jobs/job-001/corrected/3_1.tiff"]

        assert left.shape[1] == 450
        assert right.shape[1] == 450
        assert left.shape[1] != self.parent_image.shape[1]
        assert right.shape[1] != self.parent_image.shape[1]
        assert np.array_equal(left, self.parent_image[:, :450])
        assert np.array_equal(right, self.parent_image[:, 450:])

    def test_children_remain_pending_human_correction_and_parent_closes_to_split(self) -> None:
        job = _make_job()
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage, _make_gate())
        self._inject(session)
        added_objects: list[Any] = []
        session.add.side_effect = added_objects.append

        with patch(
            "services.eep.app.correction.apply.advance_page_state",
            return_value=True,
        ) as mock_advance:
            response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)

        assert response.status_code == 200
        mock_advance.assert_called_once_with(
            session,
            parent.page_id,
            from_state="pending_human_correction",
            to_state="split",
        )
        from services.eep.app.db.models import JobPage

        child_pages = [obj for obj in added_objects if isinstance(obj, JobPage)]
        assert len(child_pages) == 2
        assert {child.status for child in child_pages} == {"pending_human_correction"}

    def test_child_lineage_has_correct_metadata(self) -> None:
        job = _make_job()
        parent = _make_page(page_id="parent-id-001")
        parent_lineage = _make_lineage(
            correlation_id="corr-xyz",
            otiff_uri="s3://bucket/otiff/3.tiff",
            material_type="newspaper",
            policy_version="v2.0",
        )
        session = _make_fresh_split_session(job, parent, parent_lineage, _make_gate())
        self._inject(session)

        added_objects: list[Any] = []
        session.add.side_effect = added_objects.append

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            response = self.client.post(
                "/v1/jobs/job-001/pages/3/correction",
                json={**_SPREAD_BODY_WITH_SPLIT, "notes": "looks like two pages"},
            )

        assert response.status_code == 200

        from services.eep.app.db.models import PageLineage

        added_lineages = [obj for obj in added_objects if isinstance(obj, PageLineage)]
        assert len(added_lineages) == 2
        assert parent_lineage.human_correction_fields is None

        for lineage in added_lineages:
            assert lineage.split_source is True
            assert lineage.parent_page_id == "parent-id-001"
            assert lineage.human_corrected is True
            assert lineage.human_correction_fields["page_structure"] == "spread"
            assert lineage.human_correction_fields["split_x"] == 450
            assert lineage.human_correction_fields["crop_box"] == [10, 20, 900, 700]
            assert lineage.human_correction_fields["deskew_angle"] == pytest.approx(0.5)
            assert lineage.material_type == "newspaper"
            assert lineage.policy_version == "v2.0"
            assert lineage.otiff_uri == "s3://bucket/otiff/3.tiff"
            assert lineage.correlation_id == "corr-xyz"
            assert lineage.reviewer_notes == "looks like two pages"

    def test_split_x_is_derived_from_geometry_gate_when_not_provided(self) -> None:
        job = _make_job()
        parent = _make_page(output_image_uri="s3://bucket/jobs/job-001/3.tiff")
        parent_lineage = _make_lineage()
        gate = _make_gate(split_x=320)
        session = _make_fresh_split_session(job, parent, parent_lineage, gate)
        self._inject(session)

        added_objects: list[Any] = []
        session.add.side_effect = added_objects.append

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)

        assert response.status_code == 200
        written = {
            call.args[0]: _decode_tiff(call.args[1])
            for call in self.mock_backend.put_bytes.call_args_list
        }
        assert written["s3://bucket/jobs/job-001/corrected/3_0.tiff"].shape[1] == 320
        assert written["s3://bucket/jobs/job-001/corrected/3_1.tiff"].shape[1] == 580

        from services.eep.app.db.models import PageLineage

        added_lineages = [obj for obj in added_objects if isinstance(obj, PageLineage)]
        assert {lineage.human_correction_fields["split_x"] for lineage in added_lineages} == {320}

    def test_idempotency_existing_children_reused(self) -> None:
        job = _make_job()
        parent = _make_page()
        parent_lineage = _make_lineage()
        left_child = _make_page(
            page_id="child-0", sub_page_index=0, status="pending_human_correction"
        )
        left_lineage = _make_lineage(lineage_id="lin-0", sub_page_index=0)
        right_child = _make_page(
            page_id="child-1", sub_page_index=1, status="pending_human_correction"
        )
        right_lineage = _make_lineage(lineage_id="lin-1", sub_page_index=1)
        session = _make_session(
            job=job,
            first_results=[
                parent,
                parent_lineage,
                _make_gate(),
                left_child,
                left_lineage,
                right_child,
                right_lineage,
            ],
        )
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)

        assert response.status_code == 200
        session.add.assert_not_called()
        assert self.mock_backend.put_bytes.call_count == 2

    def test_split_missing_parent_lineage_returns_500(self) -> None:
        job = _make_job()
        parent = _make_page()
        session = _make_session(job=job, first_results=[parent, None])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)

        assert response.status_code == 500
        assert "data-integrity failure" in response.json()["detail"].lower()
        assert "lineage" in response.json()["detail"].lower()

    def test_split_missing_source_uri_returns_500(self) -> None:
        # Both output_image_uri and input_image_uri must be None to get a 500 now;
        # if either is set the code falls back gracefully (see test below).
        job = _make_job()
        parent = _make_page(output_image_uri=None)
        parent.input_image_uri = None  # force truly no source
        parent_lineage = _make_lineage(output_image_uri=None)
        session = _make_session(job=job, first_results=[parent, parent_lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)

        assert response.status_code == 500
        assert "data-integrity failure" in response.json()["detail"].lower()
        assert "source artifact uri" in response.json()["detail"].lower()

    def test_split_falls_back_to_input_uri_when_output_uri_is_none(self) -> None:
        """Split must succeed when output_image_uri is None but input_image_uri is set.

        Reproduces the live bug: page went to pending_human_correction before
        preprocessing completed, so output_image_uri was never populated.
        The original uploaded OTIFF (input_image_uri) must be used as source.
        """
        job = _make_job()
        parent = _make_page(
            output_image_uri=None,
            input_image_uri="s3://bucket/raw/3.tiff",
        )
        parent_lineage = _make_lineage(output_image_uri=None)
        session = _make_fresh_split_session(job, parent, parent_lineage, _make_gate())
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)

        assert response.status_code == 200
        # Source must have been read from input_image_uri
        self.mock_backend.get_bytes.assert_called_once_with("s3://bucket/raw/3.tiff")
        # Both child artifacts must be written
        assert self.mock_backend.put_bytes.call_count == 2

    def test_split_404_when_parent_page_not_found(self) -> None:
        job = _make_job()
        session = _make_session(job=job, first_results=[None])
        self._inject(session)

        response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)
        assert response.status_code == 404

    def test_split_409_when_parent_not_in_pending_correction(self) -> None:
        job = _make_job()
        parent = _make_page(status="layout_detection")
        session = _make_session(job=job, first_results=[parent])
        self._inject(session)

        response = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPREAD_BODY)
        assert response.status_code == 409
