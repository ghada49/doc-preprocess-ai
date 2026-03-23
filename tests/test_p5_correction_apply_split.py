"""
tests/test_p5_correction_apply_split.py
-----------------------------------------
Packet 5.3 — Split correction apply path tests.

Covers:
  POST /v1/jobs/{job_id}/pages/{page_number}/correction  (split_x non-null)

  - split_x routes to split correction path, returns 200
  - Two child pages created (sub_page_index 0 and 1)
  - Corrected artifact written to child-specific URI for each child
  - Child lineage rows created with correct metadata
    (split_source=True, parent_page_id, human_correction_fields with split_x)
  - Children transition: pending_human_correction → ptiff_qa_pending
  - Parent stays in pending_human_correction (manual mode; not worker-terminal)
  - auto_continue + preprocess: children → accepted; parent → split
  - auto_continue + layout: children → layout_detection; parent stays
  - Idempotency: existing children are reused, not duplicated
  - Missing parent lineage → 500 (data-integrity failure)
  - Missing source artifact URI → 500 (data-integrity failure)

Session is mocked; no live database required.
HTTP endpoints tested via FastAPI TestClient with dependency override.
advance_page_state and get_backend are patched for isolation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from services.eep.app.db.session import get_session
from services.eep.app.main import app

# ── Factories ──────────────────────────────────────────────────────────────────


def _make_job(
    job_id: str = "job-001",
    pipeline_mode: str = "layout",
    ptiff_qa_mode: str = "manual",
) -> MagicMock:
    job = MagicMock()
    job.job_id = job_id
    job.pipeline_mode = pipeline_mode
    job.ptiff_qa_mode = ptiff_qa_mode
    return job


def _make_page(
    page_id: str = "parent-p1",
    job_id: str = "job-001",
    page_number: int = 3,
    sub_page_index: int | None = None,
    status: str = "pending_human_correction",
    ptiff_qa_approved: bool = False,
    output_image_uri: str | None = "s3://bucket/jobs/job-001/3.tiff",
    input_image_uri: str = "s3://bucket/raw/3.tiff",
) -> MagicMock:
    page = MagicMock()
    page.page_id = page_id
    page.job_id = job_id
    page.page_number = page_number
    page.sub_page_index = sub_page_index
    page.status = status
    page.ptiff_qa_approved = ptiff_qa_approved
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
    lineage.human_correction_fields = None
    lineage.reviewer_notes = None
    return lineage


def _make_session(
    job: Any = None,
    first_results: list[Any] | None = None,
) -> MagicMock:
    """
    Build a mock SQLAlchemy session.

    Query order in apply_correction (split path):
      query #0  (.first()) — parent JobPage
      query #1  (.first()) — parent PageLineage
      query #2  (.first()) — child JobPage  sub_page_index=0
      query #3  (.first()) — child PageLineage sub_page_index=0
      query #4  (.first()) — child JobPage  sub_page_index=1
      query #5  (.first()) — child PageLineage sub_page_index=1
    """
    session = MagicMock()
    session.get.return_value = job

    first_queue: list[Any] = list(first_results or [])

    def query_se(*args: Any, **kwargs: Any) -> MagicMock:
        chain = MagicMock()
        chain.filter.return_value = chain

        def first_se() -> Any:
            return first_queue.pop(0) if first_queue else None

        chain.first.side_effect = first_se
        return chain

    session.query.side_effect = query_se
    return session


# ── Helpers ────────────────────────────────────────────────────────────────────


_SPLIT_BODY = {"crop_box": [10, 20, 900, 700], "deskew_angle": 0.5, "split_x": 450}


def _make_fresh_split_session(job: Any, parent_page: Any, parent_lineage: Any) -> MagicMock:
    """Session where both children are new (None → creates)."""
    return _make_session(
        job=job,
        first_results=[parent_page, parent_lineage, None, None, None, None],
    )


# ── Test class ────────────────────────────────────────────────────────────────


class TestSplitCorrectionEndpoint:
    def setup_method(self) -> None:
        self.client = TestClient(app)
        self.mock_backend = MagicMock()
        self.mock_backend.get_bytes.return_value = b"artifact"
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

    def test_split_returns_200_ok(self) -> None:
        """Split correction with valid split_x returns 200 with status='ok'."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_split_creates_two_child_pages(self) -> None:
        """db.add is called twice — once for each child JobPage."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200
        # db.add called for each child page and each child lineage = 4 calls total
        assert session.add.call_count == 4

    def test_split_artifacts_written_for_each_child(self) -> None:
        """get_bytes and put_bytes are called once per child (two children)."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page(output_image_uri="s3://bucket/jobs/job-001/3.tiff")
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200
        # Two reads from the same source URI
        assert self.mock_backend.get_bytes.call_count == 2
        self.mock_backend.get_bytes.assert_called_with("s3://bucket/jobs/job-001/3.tiff")
        # Two writes to child-specific URIs
        assert self.mock_backend.put_bytes.call_count == 2
        put_calls = [c.args[0] for c in self.mock_backend.put_bytes.call_args_list]
        assert "s3://bucket/jobs/job-001/corrected/3_0.tiff" in put_calls
        assert "s3://bucket/jobs/job-001/corrected/3_1.tiff" in put_calls

    def test_split_children_transition_to_ptiff_qa_pending(self) -> None:
        """advance_page_state called for each child: pending_human_correction → ptiff_qa_pending."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        with patch(
            "services.eep.app.correction.apply.advance_page_state", return_value=True
        ) as mock_adv:
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200
        # At least the two child transitions
        from_to_pairs = [
            (c.kwargs.get("from_state") or c.args[2], c.kwargs.get("to_state") or c.args[3])
            for c in mock_adv.call_args_list
        ]
        child_transitions = [
            p for p in from_to_pairs if p == ("pending_human_correction", "ptiff_qa_pending")
        ]
        assert len(child_transitions) == 2

    def test_split_manual_mode_parent_stays_in_pending(self) -> None:
        """In manual mode both children are in ptiff_qa_pending (not worker-terminal)
        so the parent does NOT transition to 'split'."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        with patch(
            "services.eep.app.correction.apply.advance_page_state", return_value=True
        ) as mock_adv:
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200
        # No parent → split transition
        from_to_pairs = [
            (c.kwargs.get("from_state") or c.args[2], c.kwargs.get("to_state") or c.args[3])
            for c in mock_adv.call_args_list
        ]
        parent_split = [p for p in from_to_pairs if p[1] == "split"]
        assert parent_split == []

    # ── auto_continue paths ───────────────────────────────────────────────────

    def test_auto_continue_preprocess_children_accepted_parent_split(self) -> None:
        """
        auto_continue + preprocess:
        children → accepted (worker-terminal) → parent → split
        """
        job = _make_job(ptiff_qa_mode="auto_continue", pipeline_mode="preprocess")
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        with patch(
            "services.eep.app.correction.apply.advance_page_state", return_value=True
        ) as mock_adv:
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200

        from_to_pairs = [
            (c.kwargs.get("from_state") or c.args[2], c.kwargs.get("to_state") or c.args[3])
            for c in mock_adv.call_args_list
        ]
        # Two children: pending_human_correction → ptiff_qa_pending
        assert from_to_pairs.count(("pending_human_correction", "ptiff_qa_pending")) == 2
        # Two children: ptiff_qa_pending → accepted
        assert from_to_pairs.count(("ptiff_qa_pending", "accepted")) == 2
        # Parent: pending_human_correction → split
        assert ("pending_human_correction", "split") in from_to_pairs

    def test_auto_continue_layout_children_to_layout_detection_parent_stays(self) -> None:
        """
        auto_continue + layout:
        children → layout_detection (NOT worker-terminal) → parent stays in
        pending_human_correction.
        """
        job = _make_job(ptiff_qa_mode="auto_continue", pipeline_mode="layout")
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        with patch(
            "services.eep.app.correction.apply.advance_page_state", return_value=True
        ) as mock_adv:
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200

        from_to_pairs = [
            (c.kwargs.get("from_state") or c.args[2], c.kwargs.get("to_state") or c.args[3])
            for c in mock_adv.call_args_list
        ]
        # Two children: ptiff_qa_pending → layout_detection
        assert from_to_pairs.count(("ptiff_qa_pending", "layout_detection")) == 2
        # Parent must NOT transition to split
        parent_split = [p for p in from_to_pairs if p[1] == "split"]
        assert parent_split == []

    # ── Lineage fields ────────────────────────────────────────────────────────

    def test_child_lineage_has_correct_metadata(self) -> None:
        """Child lineage created with split_source=True, parent_page_id, correction fields."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page(page_id="parent-id-001")
        parent_lineage = _make_lineage(
            correlation_id="corr-xyz",
            otiff_uri="s3://bucket/otiff/3.tiff",
            material_type="newspaper",
            policy_version="v2.0",
        )
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        added_objects: list[Any] = []
        session.add.side_effect = added_objects.append

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200

        # Find added PageLineage objects (not JobPage)
        from services.eep.app.db.models import PageLineage

        added_lineages = [o for o in added_objects if isinstance(o, PageLineage)]
        assert len(added_lineages) == 2

        for lin in added_lineages:
            assert lin.split_source is True
            assert lin.parent_page_id == "parent-id-001"
            assert lin.human_corrected is True
            assert lin.human_correction_fields["split_x"] == 450
            assert lin.human_correction_fields["crop_box"] == [10, 20, 900, 700]
            assert lin.material_type == "newspaper"
            assert lin.policy_version == "v2.0"
            assert lin.otiff_uri == "s3://bucket/otiff/3.tiff"
            assert lin.correlation_id == "corr-xyz"

    def test_child_lineage_output_uris_differ_by_sub_index(self) -> None:
        """Each child lineage row has its own distinct corrected artifact URI."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page(output_image_uri="s3://bucket/jobs/job-001/3.tiff")
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        added_objects: list[Any] = []
        session.add.side_effect = added_objects.append

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200
        from services.eep.app.db.models import PageLineage

        added_lineages = [o for o in added_objects if isinstance(o, PageLineage)]
        uris = {lin.output_image_uri for lin in added_lineages}
        assert uris == {
            "s3://bucket/jobs/job-001/corrected/3_0.tiff",
            "s3://bucket/jobs/job-001/corrected/3_1.tiff",
        }

    def test_notes_stored_in_child_lineage(self) -> None:
        """reviewer_notes propagated to both child lineage rows."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        added_objects: list[Any] = []
        session.add.side_effect = added_objects.append

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post(
                "/v1/jobs/job-001/pages/3/correction",
                json={**_SPLIT_BODY, "notes": "looks like two pages"},
            )

        assert r.status_code == 200
        from services.eep.app.db.models import PageLineage

        added_lineages = [o for o in added_objects if isinstance(o, PageLineage)]
        for lin in added_lineages:
            assert lin.reviewer_notes == "looks like two pages"

    # ── Idempotency ───────────────────────────────────────────────────────────

    def test_idempotency_existing_children_reused(self) -> None:
        """When children already exist, no new db.add calls are made for them."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page()
        parent_lineage = _make_lineage()
        # Existing children already in ptiff_qa_pending
        left_child = _make_page(page_id="child-0", sub_page_index=0, status="ptiff_qa_pending")
        left_lin = _make_lineage(lineage_id="lin-0", sub_page_index=0)
        right_child = _make_page(page_id="child-1", sub_page_index=1, status="ptiff_qa_pending")
        right_lin = _make_lineage(lineage_id="lin-1", sub_page_index=1)

        session = _make_session(
            job=job,
            first_results=[parent, parent_lineage, left_child, left_lin, right_child, right_lin],
        )
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200
        # No new rows created
        session.add.assert_not_called()

    # ── Error cases ───────────────────────────────────────────────────────────

    def test_split_missing_parent_lineage_returns_500(self) -> None:
        """Missing parent lineage row → data-integrity failure → 500."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page()

        # second .first() → parent lineage = None
        session = _make_session(job=job, first_results=[parent, None])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 500
        assert "data-integrity failure" in r.json()["detail"].lower()
        assert "lineage" in r.json()["detail"].lower()

    def test_split_missing_source_uri_returns_500(self) -> None:
        """Parent page with no output_image_uri → data-integrity failure → 500."""
        job = _make_job(ptiff_qa_mode="manual")
        parent = _make_page(output_image_uri=None)
        parent_lineage = _make_lineage(output_image_uri=None)

        session = _make_session(job=job, first_results=[parent, parent_lineage])
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 500
        assert "data-integrity failure" in r.json()["detail"].lower()
        assert "source artifact uri" in r.json()["detail"].lower()

    def test_split_404_when_parent_page_not_found(self) -> None:
        """Parent page not found → 404."""
        job = _make_job()
        session = _make_session(job=job, first_results=[None])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)
        assert r.status_code == 404

    def test_split_409_when_parent_not_in_pending_correction(self) -> None:
        """Parent page in wrong state → 409."""
        job = _make_job()
        parent = _make_page(status="ptiff_qa_pending")
        session = _make_session(job=job, first_results=[parent])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)
        assert r.status_code == 409

    # ── Enqueue behaviour ─────────────────────────────────────────────────────

    def test_auto_continue_layout_enqueues_both_children(self) -> None:
        """auto_continue + layout: both children released to layout_detection are enqueued."""
        job = _make_job(ptiff_qa_mode="auto_continue", pipeline_mode="layout")
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            with patch("services.eep.app.correction.apply.enqueue_page_task") as mock_enqueue:
                r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200
        # Both children should be enqueued for layout detection
        assert mock_enqueue.call_count == 2
        # Each call receives a Redis client and a PageTask
        from shared.schemas.queue import PageTask

        for enqueue_call in mock_enqueue.call_args_list:
            task = enqueue_call.args[1]
            assert isinstance(task, PageTask)
            assert task.job_id == "job-001"
            assert task.page_number == 3
            assert task.retry_count == 0

    def test_auto_continue_preprocess_does_not_enqueue(self) -> None:
        """auto_continue + preprocess: children go to accepted (terminal) — no enqueue."""
        job = _make_job(ptiff_qa_mode="auto_continue", pipeline_mode="preprocess")
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            with patch("services.eep.app.correction.apply.enqueue_page_task") as mock_enqueue:
                r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200
        mock_enqueue.assert_not_called()

    def test_manual_mode_does_not_enqueue(self) -> None:
        """Manual mode: children stay in ptiff_qa_pending — no enqueue."""
        job = _make_job(ptiff_qa_mode="manual", pipeline_mode="layout")
        parent = _make_page()
        parent_lineage = _make_lineage()
        session = _make_fresh_split_session(job, parent, parent_lineage)
        self._inject(session)

        with patch("services.eep.app.correction.apply.advance_page_state", return_value=True):
            with patch("services.eep.app.correction.apply.enqueue_page_task") as mock_enqueue:
                r = self.client.post("/v1/jobs/job-001/pages/3/correction", json=_SPLIT_BODY)

        assert r.status_code == 200
        mock_enqueue.assert_not_called()
