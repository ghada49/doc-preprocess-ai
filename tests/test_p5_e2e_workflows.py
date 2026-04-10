"""
tests/test_p5_e2e_workflows.py
-----------------------------------------------
Packet 5.5 — Correction and PTIFF QA workflow tests.

Covers scenarios and gaps not addressed by the per-module unit tests
(test_p5_correction_apply.py, test_p5_correction_reject.py,
test_p5_ptiff_qa.py, etc.).

Idempotency and terminal-state guards:
  - Double correction-reject returns 409 (page already in 'review')
  - Applying correction after rejection returns 409 (leaf-final guard)
  - Approving a page no longer in ptiff_qa_pending returns 409

Auto-continue mode with manual QA endpoints (no interference):
  - approve-page works on an auto_continue job (gate not released when one page still pending)
  - approve-page releases the gate to layout_detection on an auto_continue layout job
  - approve-all releases the gate to layout_detection on an auto_continue layout job
  - approve-all releases the gate to accepted on an auto_continue preprocess job
  - edit works on an auto_continue job (transitions to pending_human_correction)
  - Gate is blocked by pending correction even in auto_continue mode

Phase definition of done verification:
  - Corrected page re-enters at ptiff_qa_pending stage
  - Rejection is terminal (review blocks further corrections and rejections)
  - PTIFF QA gate works before downstream stages in both pipeline modes
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from services.eep.app.db.session import get_session
from services.eep.app.main import app
from services.eep.app.redis_client import get_redis

pytestmark = [
    pytest.mark.skip(reason="ptiff_qa workflow removed in automation-first refactor"),
    pytest.mark.usefixtures("_bypass_require_user"),
]

# ── Shared factories ───────────────────────────────────────────────────────────


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
    page_id: str = "p1",
    job_id: str = "job-001",
    page_number: int = 1,
    sub_page_index: int | None = None,
    status: str = "pending_human_correction",
    ptiff_qa_approved: bool = False,
) -> MagicMock:
    page = MagicMock()
    page.page_id = page_id
    page.job_id = job_id
    page.page_number = page_number
    page.sub_page_index = sub_page_index
    page.status = status
    page.ptiff_qa_approved = ptiff_qa_approved
    page.review_reasons = None
    return page


def _make_lineage(
    job_id: str = "job-001",
    page_number: int = 1,
) -> MagicMock:
    lineage = MagicMock()
    lineage.job_id = job_id
    lineage.page_number = page_number
    lineage.human_corrected = False
    lineage.reviewer_notes = None
    return lineage


def _make_session_with_first(
    job: Any = None,
    first_results: list[Any] | None = None,
) -> MagicMock:
    """
    Session mock where db.get returns job and .filter().first() calls
    draw sequentially from first_results.  Used by correction apply and
    correction reject endpoints.
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


def _make_session_with_all(
    job: Any = None,
    first_query_pages: list[Any] | None = None,
    second_query_pages: list[Any] | None = None,
) -> MagicMock:
    """
    Session mock for PTIFF QA endpoints where queries use .all().
    First call to .all() returns first_query_pages; second returns second_query_pages.
    """
    session = MagicMock()
    session.get.return_value = job

    call_count: list[int] = [0]
    pages_by_call = [first_query_pages or [], second_query_pages or []]

    def query_se(*args: Any, **kwargs: Any) -> MagicMock:
        chain = MagicMock()
        chain.filter.return_value = chain
        chain.order_by.return_value = chain
        idx = call_count[0]
        call_count[0] += 1
        chain.all.return_value = pages_by_call[idx] if idx < len(pages_by_call) else []
        chain.update.return_value = 1
        return chain

    session.query.side_effect = query_se
    return session


# ── TestIdempotencyAndTerminalState ────────────────────────────────────────────


class TestIdempotencyAndTerminalState:
    """
    Verify that terminal states (review) and already-transitioned states
    correctly reject further corrections, rejections, and approvals.
    """

    def setup_method(self) -> None:
        self.client = TestClient(app)
        # Mock Redis so approve endpoints don't fail on connection.
        self.mock_redis = MagicMock()
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

    def teardown_method(self) -> None:
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    # ── Rejection idempotency ─────────────────────────────────────────────────

    def test_double_reject_returns_409(self) -> None:
        """
        A page already in 'review' (terminal) cannot be rejected again.

        After the first correction-reject the page is in 'review'.  A repeat
        call must return 409 because 'review' is not 'pending_human_correction'.
        """
        job = _make_job()
        page_already_rejected = _make_page(status="review")

        session = _make_session_with_first(job=job, first_results=[page_already_rejected])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")
        assert r.status_code == 409
        assert "pending_human_correction" in r.json()["detail"]

    def test_apply_correction_after_reject_returns_409(self) -> None:
        """
        Applying a correction to a 'review' page must return 409.

        Once rejected, the page is in the leaf-final 'review' state.  No
        further corrections are possible.
        """
        job = _make_job()
        page_in_review = _make_page(status="review")

        session = _make_session_with_first(job=job, first_results=[page_in_review])
        self._inject(session)

        r = self.client.post(
            "/v1/jobs/job-001/pages/1/correction",
            json={"crop_box": [0, 0, 100, 200], "deskew_angle": 0.0},
        )
        assert r.status_code == 409

    # ── Approval idempotency ──────────────────────────────────────────────────

    def test_approve_page_after_gate_release_returns_409(self) -> None:
        """
        A page that has left ptiff_qa_pending (already released by the gate)
        cannot be approved again — the endpoint returns 409.

        The approve endpoint queries for pages with status == 'ptiff_qa_pending'.
        When none are found, it raises 409.
        """
        job = _make_job()
        # No ptiff_qa_pending pages for this page_number (already in layout_detection)
        session = _make_session_with_all(job=job, first_query_pages=[])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/approve")
        assert r.status_code == 409
        assert "ptiff_qa_pending" in r.json()["detail"]

    def test_approve_all_after_gate_release_is_noop(self) -> None:
        """
        approve-all when no ptiff_qa_pending pages remain is idempotent:
        approved_count=0 and gate_released=False.
        """
        job = _make_job()
        # No ptiff_qa_pending pages (all already released or in terminal states)
        session = _make_session_with_all(
            job=job,
            first_query_pages=[],  # no ptiff_qa_pending pages
            second_query_pages=[_make_page(status="layout_detection")],
        )
        self._inject(session)

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state") as mock_advance:
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        data = r.json()
        assert data["approved_count"] == 0
        assert data["gate_released"] is False
        mock_advance.assert_not_called()


# ── TestAutoContineNotDisruptedByManualQA ──────────────────────────────────────


class TestAutoContineNotDisruptedByManualQA:
    """
    Verify that setting ptiff_qa_mode='auto_continue' on a job does NOT
    disable or interfere with the manual PTIFF QA endpoints.

    Spec requirement: auto-continue mode fires the gate check automatically
    after correction apply.  Manual approve/edit endpoints must still work
    normally (they do not inspect ptiff_qa_mode).
    """

    def setup_method(self) -> None:
        self.client = TestClient(app)
        self.mock_redis = MagicMock()
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

    def teardown_method(self) -> None:
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    # ── approve-page ──────────────────────────────────────────────────────────

    def test_approve_page_works_on_auto_continue_job_no_gate_release(self) -> None:
        """approve-page on auto_continue job; gate NOT released (two pages, one pending)."""
        job = _make_job(ptiff_qa_mode="auto_continue", pipeline_mode="layout")
        p1 = _make_page(
            page_id="p1", page_number=1, status="ptiff_qa_pending", ptiff_qa_approved=False
        )
        p2 = _make_page(
            page_id="p2", page_number=2, status="ptiff_qa_pending", ptiff_qa_approved=False
        )

        # After approving p1, p2 is still unapproved → gate not released.
        session = _make_session_with_all(
            job=job,
            first_query_pages=[p1],
            second_query_pages=[p1, p2],
        )
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/approve")
        assert r.status_code == 200
        data = r.json()
        assert data["approved"] is True
        assert data["gate_released"] is False

    def test_approve_page_releases_gate_on_auto_continue_layout_job(self) -> None:
        """
        approve-page releases the gate to layout_detection on an auto_continue
        layout job when all conditions are met.
        """
        job = _make_job(ptiff_qa_mode="auto_continue", pipeline_mode="layout")
        p1 = _make_page(
            page_id="p1", page_number=1, status="ptiff_qa_pending", ptiff_qa_approved=False
        )

        session = _make_session_with_all(
            job=job,
            first_query_pages=[p1],  # approve this page
            second_query_pages=[p1],  # leaf_pages: only p1, now approved
        )
        self._inject(session)

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/approve")

        assert r.status_code == 200
        assert r.json()["gate_released"] is True
        mock_advance.assert_called_once()
        call_kwargs = mock_advance.call_args
        to_state = call_kwargs[1].get("to_state") or call_kwargs[0][3]
        assert to_state == "layout_detection"

    # ── approve-all ───────────────────────────────────────────────────────────

    def test_approve_all_releases_gate_on_auto_continue_layout_job(self) -> None:
        """approve-all on auto_continue + layout mode releases gate to layout_detection."""
        job = _make_job(ptiff_qa_mode="auto_continue", pipeline_mode="layout")
        p1 = _make_page(
            page_id="p1", page_number=1, status="ptiff_qa_pending", ptiff_qa_approved=False
        )
        p2 = _make_page(
            page_id="p2", page_number=2, status="ptiff_qa_pending", ptiff_qa_approved=False
        )

        session = _make_session_with_all(
            job=job,
            first_query_pages=[p1, p2],
            second_query_pages=[p1, p2],
        )
        self._inject(session)

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        data = r.json()
        assert data["approved_count"] == 2
        assert data["gate_released"] is True
        assert mock_advance.call_count == 2
        for call in mock_advance.call_args_list:
            to_state = call[1].get("to_state") or call[0][3]
            assert to_state == "layout_detection"

    def test_approve_all_releases_gate_on_auto_continue_preprocess_job(self) -> None:
        """approve-all on auto_continue + preprocess mode releases gate to accepted."""
        job = _make_job(ptiff_qa_mode="auto_continue", pipeline_mode="preprocess")
        p1 = _make_page(page_id="p1", status="ptiff_qa_pending", ptiff_qa_approved=False)

        session = _make_session_with_all(
            job=job,
            first_query_pages=[p1],
            second_query_pages=[p1],
        )
        self._inject(session)

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        assert r.json()["gate_released"] is True
        call_kwargs = mock_advance.call_args
        to_state = call_kwargs[1].get("to_state") or call_kwargs[0][3]
        assert to_state == "accepted"

    def test_approve_all_gate_blocked_by_correction_in_auto_continue_mode(self) -> None:
        """
        Gate is blocked by a pending correction even when ptiff_qa_mode='auto_continue'.

        Auto-continue mode does not bypass the gate condition: pending corrections
        still prevent release regardless of how the gate check was triggered.
        """
        job = _make_job(ptiff_qa_mode="auto_continue", pipeline_mode="layout")
        p1 = _make_page(
            page_id="p1", page_number=1, status="ptiff_qa_pending", ptiff_qa_approved=False
        )
        p2 = _make_page(page_id="p2", page_number=2, status="pending_human_correction")

        session = _make_session_with_all(
            job=job,
            first_query_pages=[p1],  # only p1 is ptiff_qa_pending
            second_query_pages=[p1, p2],  # leaf_pages includes correction page
        )
        self._inject(session)

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state") as mock_advance:
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        data = r.json()
        assert data["approved_count"] == 1
        assert data["gate_released"] is False
        mock_advance.assert_not_called()

    # ── edit ──────────────────────────────────────────────────────────────────

    def test_edit_works_on_auto_continue_job(self) -> None:
        """
        edit-page transitions ptiff_qa_pending → pending_human_correction on an
        auto_continue job.  ptiff_qa_mode has no effect on the edit endpoint.
        """
        job = _make_job(ptiff_qa_mode="auto_continue", pipeline_mode="layout")
        page = _make_page(
            page_id="p1", page_number=1, status="ptiff_qa_pending", ptiff_qa_approved=True
        )

        session = _make_session_with_all(job=job, first_query_pages=[page])
        self._inject(session)

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/edit")

        assert r.status_code == 200
        data = r.json()
        assert data["new_state"] == "pending_human_correction"
        mock_advance.assert_called_once_with(
            session,
            page.page_id,
            from_state="ptiff_qa_pending",
            to_state="pending_human_correction",
        )
        # Approval flag must be cleared so page must be re-approved after correction.
        assert page.ptiff_qa_approved is False

    def test_edit_returns_409_when_page_not_in_ptiff_qa_pending_auto_continue(self) -> None:
        """edit on a non-ptiff_qa_pending page returns 409 in auto_continue mode."""
        job = _make_job(ptiff_qa_mode="auto_continue")
        # Query returns empty: no ptiff_qa_pending pages for this page_number
        session = _make_session_with_all(job=job, first_query_pages=[])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/edit")
        assert r.status_code == 409


# ── TestPhaseDefinitionOfDone ──────────────────────────────────────────────────


class TestPhaseDefinitionOfDone:
    """
    Explicit DoD verification tests aligned with the Phase 5 definition of done
    from the implementation roadmap.
    """

    def setup_method(self) -> None:
        self.client = TestClient(app)
        self.mock_redis = MagicMock()
        app.dependency_overrides[get_redis] = lambda: self.mock_redis
        # Storage backend mock for correction apply.
        self.mock_backend = MagicMock()
        self.mock_backend.get_bytes.return_value = b"artifact"
        self._storage_patcher = patch(
            "services.eep.app.correction.apply.get_backend",
            return_value=self.mock_backend,
        )
        self._storage_patcher.start()

    def teardown_method(self) -> None:
        self._storage_patcher.stop()
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    def test_corrected_page_re_enters_at_ptiff_qa_pending(self) -> None:
        """
        DoD: corrected pages re-enter at the correct stage (ptiff_qa_pending).

        Submitting a correction transitions the page from pending_human_correction
        to ptiff_qa_pending, as required before the PTIFF QA gate.
        """
        job = _make_job(ptiff_qa_mode="manual")
        page = _make_page(status="pending_human_correction")
        page.output_image_uri = "s3://b/p.tiff"
        lineage = _make_lineage()
        lineage.output_image_uri = "s3://b/p.tiff"

        session = _make_session_with_first(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch(
            "services.eep.app.correction.apply.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post(
                "/v1/jobs/job-001/pages/1/correction",
                json={"crop_box": [0, 0, 100, 200], "deskew_angle": 0.0},
            )

        assert r.status_code == 200
        mock_advance.assert_called_once_with(
            session,
            page.page_id,
            from_state="pending_human_correction",
            to_state="ptiff_qa_pending",
        )

    def test_rejection_is_terminal_cannot_correct_from_review(self) -> None:
        """
        DoD: rejection is terminal.

        After rejection, the page is in 'review' (leaf-final).  Attempting to
        apply a correction returns 409 — no further corrections are possible.
        """
        job = _make_job()
        page_in_review = _make_page(status="review")

        session = _make_session_with_first(job=job, first_results=[page_in_review])
        self._inject(session)

        r = self.client.post(
            "/v1/jobs/job-001/pages/1/correction",
            json={"crop_box": [0, 0, 100, 200], "deskew_angle": 0.0},
        )
        assert r.status_code == 409

    def test_rejection_is_terminal_cannot_reject_from_review(self) -> None:
        """
        DoD: rejection is terminal.

        After rejection, the page is in 'review'.  Attempting to reject again
        returns 409 — the state machine prevents double-rejection.
        """
        job = _make_job()
        page_in_review = _make_page(status="review")

        session = _make_session_with_first(job=job, first_results=[page_in_review])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")
        assert r.status_code == 409

    def test_ptiff_qa_gate_releases_to_layout_detection_for_layout_jobs(self) -> None:
        """
        DoD: job-level PTIFF QA gate works before downstream stages.

        In layout pipeline mode, gate release transitions pages to
        layout_detection — the next downstream processing stage.
        """
        job = _make_job(ptiff_qa_mode="manual", pipeline_mode="layout")
        page = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=False)

        session = _make_session_with_all(
            job=job,
            first_query_pages=[page],
            second_query_pages=[page],
        )
        self._inject(session)

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        assert r.json()["gate_released"] is True
        call_kwargs = mock_advance.call_args
        to_state = call_kwargs[1].get("to_state") or call_kwargs[0][3]
        assert to_state == "layout_detection"

    def test_ptiff_qa_gate_releases_to_accepted_for_preprocess_jobs(self) -> None:
        """
        DoD: job-level PTIFF QA gate works before downstream stages.

        In preprocess pipeline mode, gate release transitions pages to 'accepted'
        — the terminal acceptance state for preprocess-only jobs.
        """
        job = _make_job(ptiff_qa_mode="manual", pipeline_mode="preprocess")
        page = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=False)

        session = _make_session_with_all(
            job=job,
            first_query_pages=[page],
            second_query_pages=[page],
        )
        self._inject(session)

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        assert r.json()["gate_released"] is True
        call_kwargs = mock_advance.call_args
        to_state = call_kwargs[1].get("to_state") or call_kwargs[0][3]
        assert to_state == "accepted"

    def test_ptiff_qa_gate_requires_no_pending_corrections(self) -> None:
        """
        DoD: PTIFF QA gate must not release when any page is in pending_human_correction.

        The approve-all endpoint approves all ptiff_qa_pending pages but still
        blocks gate release when a correction is outstanding on another page.
        """
        job = _make_job(ptiff_qa_mode="manual", pipeline_mode="layout")
        p_qa = _make_page(
            page_id="p1", page_number=1, status="ptiff_qa_pending", ptiff_qa_approved=False
        )
        p_correction = _make_page(page_id="p2", page_number=2, status="pending_human_correction")

        session = _make_session_with_all(
            job=job,
            first_query_pages=[p_qa],
            second_query_pages=[p_qa, p_correction],
        )
        self._inject(session)

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state") as mock_advance:
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        assert r.json()["gate_released"] is False
        mock_advance.assert_not_called()

    def test_approve_all_no_deadlock_when_split_parent_pending(self) -> None:
        """
        Regression test — Packet 5.3 deadlock fix.

        In manual mode with a split correction the split parent remains in
        pending_human_correction while its children are in ptiff_qa_pending.
        Before the fix, _is_gate_satisfied blocked on the parent, so
        approve_all always returned gate_released=False (infinite deadlock).

        After the fix the gate recognises the parent as a split parent
        (its page_number appears among children with sub_page_index IS NOT
        None) and skips it. approve_all must therefore:
          - approve both children
          - release the gate (gate_released=True)
          - transition children to layout_detection
        """
        job = _make_job(ptiff_qa_mode="manual", pipeline_mode="layout")
        parent = _make_page(
            page_id="parent",
            page_number=3,
            sub_page_index=None,
            status="pending_human_correction",
        )
        child_0 = _make_page(
            page_id="c0",
            page_number=3,
            sub_page_index=0,
            status="ptiff_qa_pending",
            ptiff_qa_approved=False,
        )
        child_1 = _make_page(
            page_id="c1",
            page_number=3,
            sub_page_index=1,
            status="ptiff_qa_pending",
            ptiff_qa_approved=False,
        )

        # approve_all query 1: ptiff_qa_pending pages (children only — parent
        #   is not ptiff_qa_pending so it is not returned by the filter).
        # approve_all query 2: _leaf_pages (parent + both children).
        session = _make_session_with_all(
            job=job,
            first_query_pages=[child_0, child_1],
            second_query_pages=[parent, child_0, child_1],
        )
        self._inject(session)

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        data = r.json()
        # Both children must have been approved and the gate released.
        assert data["approved_count"] == 2
        assert data["gate_released"] is True
        # advance_page_state called for each child: ptiff_qa_pending → layout_detection.
        assert mock_advance.call_count == 2
        for call in mock_advance.call_args_list:
            to_state = call[1].get("to_state") or call[0][3]
            assert to_state == "layout_detection"

    def test_ptiff_qa_edit_clears_approval_flag_for_re_review(self) -> None:
        """
        DoD: edit routes through correction and returns to ptiff_qa_pending.

        The edit endpoint clears the approval flag so the page must be
        explicitly re-approved after the human correction is submitted.
        """
        job = _make_job()
        page = _make_page(
            page_id="p1", page_number=1, status="ptiff_qa_pending", ptiff_qa_approved=True
        )

        session = _make_session_with_all(job=job, first_query_pages=[page])
        self._inject(session)

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/edit")

        assert r.status_code == 200
        assert r.json()["new_state"] == "pending_human_correction"
        # Approval flag cleared: page must be re-approved after correction
        assert page.ptiff_qa_approved is False
