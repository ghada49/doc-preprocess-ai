"""
tests/test_p5_ptiff_qa.py
--------------------------
Packet 5.0a — PTIFF QA workflow tests.

Covers:
  - _is_gate_satisfied: all gate condition combinations
  - _check_and_release_ptiff_qa: gate release logic, idempotency, pipeline modes
  - GET  /v1/jobs/{job_id}/ptiff-qa: status response fields and counts
  - POST …/ptiff-qa/approve: single-page approval, no state change, gate release
  - POST …/ptiff-qa/approve-all: bulk approval, idempotency, gate release
  - POST …/ptiff-qa/edit: state transition, approval flag cleared
  - 404 responses when job not found
  - 409 responses when page not in ptiff_qa_pending
  - Pages in pending_human_correction block gate release
  - Gate release targets: preprocess → accepted, layout → layout_detection

Session is mocked; no live database required.
HTTP endpoints are tested via FastAPI TestClient with dependency override.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from services.eep.app.correction.ptiff_qa import _check_and_release_ptiff_qa, _is_gate_satisfied
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
    page_id: str = "p1",
    job_id: str = "job-001",
    page_number: int = 1,
    sub_page_index: int | None = None,
    status: str = "ptiff_qa_pending",
    ptiff_qa_approved: bool = False,
) -> MagicMock:
    page = MagicMock()
    page.page_id = page_id
    page.job_id = job_id
    page.page_number = page_number
    page.sub_page_index = sub_page_index
    page.status = status
    page.ptiff_qa_approved = ptiff_qa_approved
    return page


def _make_session(
    job: Any = None,
    first_query_pages: list[Any] | None = None,
    second_query_pages: list[Any] | None = None,
) -> MagicMock:
    """
    Build a mock SQLAlchemy session.

    db.get(Job, job_id) returns `job`.
    First db.query(...).filter(...).all() returns first_query_pages.
    Second db.query(...).filter(...).all() returns second_query_pages.
    db.query(...).filter(...).update(...) returns 1 (CAS success).
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
        if idx < len(pages_by_call):
            chain.all.return_value = pages_by_call[idx]
        else:
            chain.all.return_value = []
        chain.update.return_value = 1
        return chain

    session.query.side_effect = query_se
    return session


# ── Unit tests: _is_gate_satisfied ─────────────────────────────────────────────


class TestIsGateSatisfied:
    def test_empty_pages_satisfied(self) -> None:
        assert _is_gate_satisfied([]) is True

    def test_all_approved_no_correction_satisfied(self) -> None:
        p1 = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=True)
        p2 = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=True)
        assert _is_gate_satisfied([p1, p2]) is True

    def test_unapproved_page_blocks_gate(self) -> None:
        p1 = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=True)
        p2 = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=False)
        assert _is_gate_satisfied([p1, p2]) is False

    def test_pending_human_correction_blocks_gate(self) -> None:
        p1 = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=True)
        p2 = _make_page(status="pending_human_correction", ptiff_qa_approved=False)
        assert _is_gate_satisfied([p1, p2]) is False

    def test_non_qa_pages_do_not_block_gate(self) -> None:
        # accepted/review/failed pages don't block the gate
        p1 = _make_page(status="accepted", ptiff_qa_approved=False)
        p2 = _make_page(status="review", ptiff_qa_approved=False)
        p3 = _make_page(status="layout_detection", ptiff_qa_approved=False)
        assert _is_gate_satisfied([p1, p2, p3]) is True

    def test_mixed_all_qa_approved_no_correction_satisfied(self) -> None:
        p1 = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=True)
        p2 = _make_page(status="accepted", ptiff_qa_approved=False)
        assert _is_gate_satisfied([p1, p2]) is True


# ── Unit tests: _check_and_release_ptiff_qa ────────────────────────────────────


class TestCheckAndReleasePtiffQa:
    def test_releases_to_layout_detection_in_layout_mode(self) -> None:
        job = _make_job(pipeline_mode="layout")
        page = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=True)
        session = MagicMock()

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            result = _check_and_release_ptiff_qa(session, job, [page])

        assert result is True
        mock_advance.assert_called_once_with(
            session,
            page.page_id,
            from_state="ptiff_qa_pending",
            to_state="layout_detection",
        )

    def test_releases_to_accepted_in_preprocess_mode(self) -> None:
        job = _make_job(pipeline_mode="preprocess")
        page = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=True)
        session = MagicMock()

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            result = _check_and_release_ptiff_qa(session, job, [page])

        assert result is True
        mock_advance.assert_called_once_with(
            session,
            page.page_id,
            from_state="ptiff_qa_pending",
            to_state="accepted",
        )

    def test_no_release_when_unapproved_page(self) -> None:
        job = _make_job(pipeline_mode="layout")
        p1 = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=True)
        p2 = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=False)
        session = MagicMock()

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state") as mock_advance:
            result = _check_and_release_ptiff_qa(session, job, [p1, p2])

        assert result is False
        mock_advance.assert_not_called()

    def test_no_release_when_page_in_correction(self) -> None:
        job = _make_job(pipeline_mode="layout")
        p1 = _make_page(status="ptiff_qa_pending", ptiff_qa_approved=True)
        p2 = _make_page(status="pending_human_correction")
        session = MagicMock()

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state") as mock_advance:
            result = _check_and_release_ptiff_qa(session, job, [p1, p2])

        assert result is False
        mock_advance.assert_not_called()

    def test_idempotent_when_no_qa_pages(self) -> None:
        """When no ptiff_qa_pending pages exist, gate is not released."""
        job = _make_job(pipeline_mode="layout")
        page = _make_page(status="accepted")  # already released
        session = MagicMock()

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state") as mock_advance:
            result = _check_and_release_ptiff_qa(session, job, [page])

        assert result is False
        mock_advance.assert_not_called()

    def test_releases_multiple_pages(self) -> None:
        job = _make_job(pipeline_mode="layout")
        p1 = _make_page(page_id="p1", status="ptiff_qa_pending", ptiff_qa_approved=True)
        p2 = _make_page(page_id="p2", status="ptiff_qa_pending", ptiff_qa_approved=True)
        session = MagicMock()

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            result = _check_and_release_ptiff_qa(session, job, [p1, p2])

        assert result is True
        assert mock_advance.call_count == 2


# ── HTTP endpoint tests ────────────────────────────────────────────────────────
#
# TestClient with dependency override for get_session.
# Each test class manages the override via setup_method / teardown_method.


class TestGetPtiffQaStatus:
    def setup_method(self) -> None:
        self.client = TestClient(app)

    def teardown_method(self) -> None:
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    def test_returns_200_with_correct_counts(self) -> None:
        job = _make_job(pipeline_mode="layout", ptiff_qa_mode="manual")
        p1 = _make_page(
            page_id="p1", page_number=1, status="ptiff_qa_pending", ptiff_qa_approved=False
        )
        p2 = _make_page(
            page_id="p2", page_number=2, status="ptiff_qa_pending", ptiff_qa_approved=True
        )
        p3 = _make_page(page_id="p3", page_number=3, status="pending_human_correction")

        session = _make_session(job=job, first_query_pages=[p1, p2, p3])
        self._inject(session)

        r = self.client.get("/v1/jobs/job-001/ptiff-qa")
        assert r.status_code == 200
        data = r.json()
        assert data["job_id"] == "job-001"
        assert data["ptiff_qa_mode"] == "manual"
        assert data["total_pages"] == 3
        assert data["pages_pending"] == 1
        assert data["pages_approved"] == 1
        assert data["pages_in_correction"] == 1
        assert data["is_gate_ready"] is False
        assert len(data["pages"]) == 3

    def test_is_gate_ready_when_all_approved_no_correction(self) -> None:
        job = _make_job()
        p1 = _make_page(page_id="p1", status="ptiff_qa_pending", ptiff_qa_approved=True)
        p2 = _make_page(page_id="p2", status="ptiff_qa_pending", ptiff_qa_approved=True)

        session = _make_session(job=job, first_query_pages=[p1, p2])
        self._inject(session)

        r = self.client.get("/v1/jobs/job-001/ptiff-qa")
        assert r.status_code == 200
        data = r.json()
        assert data["pages_pending"] == 0
        assert data["pages_in_correction"] == 0
        assert data["is_gate_ready"] is True

    def test_page_entry_approval_status_field(self) -> None:
        job = _make_job()
        p_approved = _make_page(page_id="pa", page_number=1, ptiff_qa_approved=True)
        p_pending = _make_page(page_id="pp", page_number=2, ptiff_qa_approved=False)

        session = _make_session(job=job, first_query_pages=[p_approved, p_pending])
        self._inject(session)

        r = self.client.get("/v1/jobs/job-001/ptiff-qa")
        assert r.status_code == 200
        pages_by_num = {p["page_number"]: p for p in r.json()["pages"]}
        assert pages_by_num[1]["approval_status"] == "approved"
        assert pages_by_num[2]["approval_status"] == "pending"

    def test_needs_correction_flag(self) -> None:
        job = _make_job()
        p = _make_page(page_id="p1", page_number=1, status="pending_human_correction")

        session = _make_session(job=job, first_query_pages=[p])
        self._inject(session)

        r = self.client.get("/v1/jobs/job-001/ptiff-qa")
        assert r.status_code == 200
        assert r.json()["pages"][0]["needs_correction"] is True

    def test_404_when_job_not_found(self) -> None:
        session = _make_session(job=None)
        self._inject(session)

        r = self.client.get("/v1/jobs/missing-job/ptiff-qa")
        assert r.status_code == 404


class TestApprovePageEndpoint:
    def setup_method(self) -> None:
        self.client = TestClient(app)

    def teardown_method(self) -> None:
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    def test_approve_single_page_no_state_change(self) -> None:
        """Approving one page of two does NOT change page state and does not release gate."""
        job = _make_job(pipeline_mode="layout")
        p1 = _make_page(
            page_id="p1", page_number=1, status="ptiff_qa_pending", ptiff_qa_approved=False
        )
        p2 = _make_page(
            page_id="p2", page_number=2, status="ptiff_qa_pending", ptiff_qa_approved=False
        )

        # After flush: first query returns p1 for page_number=1;
        # second query (_leaf_pages) returns both pages — p1 now approved, p2 not.
        session = _make_session(
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
        # State must NOT change (approval only)
        assert p1.status == "ptiff_qa_pending"
        assert p1.ptiff_qa_approved is True

    def test_approve_last_page_triggers_gate_release_layout(self) -> None:
        """Approving the last unapproved page releases the gate → layout_detection."""
        job = _make_job(pipeline_mode="layout")
        p1 = _make_page(page_id="p1", page_number=1, ptiff_qa_approved=False)
        # After flush, both pages are approved (p2 was already approved)
        p2 = _make_page(page_id="p2", page_number=2, ptiff_qa_approved=True)

        session = _make_session(
            job=job,
            first_query_pages=[p1],
            second_query_pages=[p1, p2],
        )
        self._inject(session)

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/approve")

        assert r.status_code == 200
        data = r.json()
        assert data["gate_released"] is True
        # advance_page_state called for both pages (p1 and p2 are ptiff_qa_pending)
        assert mock_advance.call_count == 2
        for c in mock_advance.call_args_list:
            assert c.kwargs["to_state"] == "layout_detection" or c[0][3] == "layout_detection"

    def test_404_when_job_not_found(self) -> None:
        session = _make_session(job=None)
        self._inject(session)

        r = self.client.post("/v1/jobs/missing/pages/1/ptiff-qa/approve")
        assert r.status_code == 404

    def test_409_when_page_not_in_ptiff_qa_pending(self) -> None:
        """Page in 'accepted' state cannot be approved via PTIFF QA endpoint."""
        job = _make_job()
        # Query returns empty (no ptiff_qa_pending pages for page_number=1)
        session = _make_session(job=job, first_query_pages=[])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/approve")
        assert r.status_code == 409
        assert "ptiff_qa_pending" in r.json()["detail"]


class TestApproveAllEndpoint:
    def setup_method(self) -> None:
        self.client = TestClient(app)

    def teardown_method(self) -> None:
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    def test_approve_all_triggers_gate_release(self) -> None:
        """Approving all pages releases the gate."""
        job = _make_job(pipeline_mode="layout")
        p1 = _make_page(page_id="p1", page_number=1, ptiff_qa_approved=False)
        p2 = _make_page(page_id="p2", page_number=2, ptiff_qa_approved=False)

        # First query (approve-all): all qa_pending pages for job
        # Second query (_leaf_pages): same pages, now both approved
        session = _make_session(
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

    def test_approve_all_gate_release_preprocess_mode(self) -> None:
        """Gate release in preprocess mode transitions pages to accepted."""
        job = _make_job(pipeline_mode="preprocess")
        p1 = _make_page(page_id="p1", ptiff_qa_approved=False)

        session = _make_session(
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
        # Must transition to 'accepted', not 'layout_detection'
        call_args = mock_advance.call_args
        assert call_args[1].get("to_state") == "accepted" or call_args[0][3] == "accepted"

    def test_mixed_approve_edit_gate_not_released(self) -> None:
        """If one page is in correction, gate is not released after approve-all."""
        job = _make_job(pipeline_mode="layout")
        p1 = _make_page(page_id="p1", page_number=1, ptiff_qa_approved=False)
        p2 = _make_page(page_id="p2", page_number=2, status="pending_human_correction")

        # First query: only ptiff_qa_pending pages
        # Second query: all leaf pages including correction page
        session = _make_session(
            job=job,
            first_query_pages=[p1],
            second_query_pages=[p1, p2],
        )
        self._inject(session)

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state") as mock_advance:
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        data = r.json()
        assert data["approved_count"] == 1
        assert data["gate_released"] is False
        mock_advance.assert_not_called()

    def test_approve_all_idempotent_when_no_qa_pages(self) -> None:
        """When no ptiff_qa_pending pages exist, approve-all is a no-op."""
        job = _make_job(pipeline_mode="layout")
        p_accepted = _make_page(status="accepted")  # already released

        session = _make_session(
            job=job,
            first_query_pages=[],  # no ptiff_qa_pending pages
            second_query_pages=[p_accepted],
        )
        self._inject(session)

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state") as mock_advance:
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        data = r.json()
        assert data["approved_count"] == 0
        assert data["gate_released"] is False
        mock_advance.assert_not_called()

    def test_approve_all_skips_already_approved(self) -> None:
        """Already-approved pages are not double-counted in approved_count."""
        job = _make_job(pipeline_mode="layout")
        p_approved = _make_page(page_id="p1", ptiff_qa_approved=True)
        p_unapproved = _make_page(page_id="p2", ptiff_qa_approved=False)

        session = _make_session(
            job=job,
            first_query_pages=[p_approved, p_unapproved],
            second_query_pages=[p_approved, p_unapproved],
        )
        self._inject(session)

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ):
            r = self.client.post("/v1/jobs/job-001/ptiff-qa/approve-all")

        assert r.status_code == 200
        # Only 1 newly approved (p_unapproved); p_approved was already True
        assert r.json()["approved_count"] == 1

    def test_404_when_job_not_found(self) -> None:
        session = _make_session(job=None)
        self._inject(session)

        r = self.client.post("/v1/jobs/missing/ptiff-qa/approve-all")
        assert r.status_code == 404


class TestEditPageEndpoint:
    def setup_method(self) -> None:
        self.client = TestClient(app)

    def teardown_method(self) -> None:
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    def test_edit_transitions_page_to_pending_human_correction(self) -> None:
        """Edit sends the page to pending_human_correction via state machine."""
        job = _make_job()
        page = _make_page(page_id="p1", page_number=1, ptiff_qa_approved=False)

        session = _make_session(job=job, first_query_pages=[page])
        self._inject(session)

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/edit")

        assert r.status_code == 200
        data = r.json()
        assert data["page_number"] == 1
        assert data["new_state"] == "pending_human_correction"

        mock_advance.assert_called_once_with(
            session,
            page.page_id,
            from_state="ptiff_qa_pending",
            to_state="pending_human_correction",
        )

    def test_edit_clears_approval_flag(self) -> None:
        """Edit clears ptiff_qa_approved so page must be re-approved after correction."""
        job = _make_job()
        page = _make_page(page_id="p1", page_number=1, ptiff_qa_approved=True)

        session = _make_session(job=job, first_query_pages=[page])
        self._inject(session)

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state", return_value=True):
            self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/edit")

        assert page.ptiff_qa_approved is False

    def test_404_when_job_not_found(self) -> None:
        session = _make_session(job=None)
        self._inject(session)

        r = self.client.post("/v1/jobs/missing/pages/1/ptiff-qa/edit")
        assert r.status_code == 404

    def test_409_when_page_not_in_ptiff_qa_pending(self) -> None:
        """Cannot edit a page that is not in ptiff_qa_pending."""
        job = _make_job()
        # Query returns empty — no ptiff_qa_pending pages for this page_number
        session = _make_session(job=job, first_query_pages=[])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/ptiff-qa/edit")
        assert r.status_code == 409
        assert "ptiff_qa_pending" in r.json()["detail"]

    def test_pages_in_correction_block_gate_release_after_approve(self) -> None:
        """
        After an edit, the page is in correction.
        Subsequent approve on another page must NOT release the gate.
        """
        job = _make_job(pipeline_mode="layout")
        p1 = _make_page(page_id="p1", page_number=1, ptiff_qa_approved=False)
        p2 = _make_page(page_id="p2", page_number=2, status="pending_human_correction")

        # Approve p1 — gate should NOT release because p2 is in correction
        session = _make_session(
            job=job,
            first_query_pages=[p1],  # pages in ptiff_qa_pending for page 1
            second_query_pages=[p1, p2],  # all leaf pages (p2 in correction)
        )
        app.dependency_overrides[get_session] = lambda: session
        client = TestClient(app)

        with patch("services.eep.app.correction.ptiff_qa.advance_page_state") as mock_advance:
            r = client.post("/v1/jobs/job-001/pages/1/ptiff-qa/approve")

        assert r.status_code == 200
        assert r.json()["gate_released"] is False
        mock_advance.assert_not_called()

        app.dependency_overrides.clear()
