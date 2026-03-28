"""
tests/test_p5_correction_reject.py
------------------------------------
Packet 5.4 — Correction reject path tests.

Covers:
  POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject

  - Successful reject returns 200 with page_number and new_state="review"
  - State transition: pending_human_correction → review (advance_page_state called correctly)
  - review_reasons set to ["human_correction_rejected"] on page row
  - human_corrected set to False on lineage row
  - Optional notes stored in lineage.reviewer_notes
  - Notes not written when body omits them (no-body and empty-body cases)
  - CAS miss (advance_page_state returns False) logs warning but still returns 200
  - 404 — job not found
  - 404 — page not found
  - 409 — page not in pending_human_correction state
  - 500 — lineage row missing (data-integrity failure)

Session is mocked; no live database required.
HTTP endpoints are tested via FastAPI TestClient with dependency override.
advance_page_state is patched for isolation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from services.eep.app.db.session import get_session
from services.eep.app.main import app

pytestmark = pytest.mark.usefixtures("_bypass_require_user")

# ── Factories ──────────────────────────────────────────────────────────────────


def _make_job(job_id: str = "job-001") -> MagicMock:
    job = MagicMock()
    job.job_id = job_id
    return job


def _make_page(
    page_id: str = "p1",
    job_id: str = "job-001",
    page_number: int = 1,
    sub_page_index: int | None = None,
    status: str = "pending_human_correction",
) -> MagicMock:
    page = MagicMock()
    page.page_id = page_id
    page.job_id = job_id
    page.page_number = page_number
    page.sub_page_index = sub_page_index
    page.status = status
    page.review_reasons = None
    return page


def _make_lineage(
    lineage_id: str = "lin-001",
    job_id: str = "job-001",
    page_number: int = 1,
) -> MagicMock:
    lineage = MagicMock()
    lineage.lineage_id = lineage_id
    lineage.job_id = job_id
    lineage.page_number = page_number
    lineage.human_corrected = True  # starts as True; reject must set it to False
    lineage.reviewer_notes = None
    return lineage


def _make_session(
    job: Any = None,
    first_results: list[Any] | None = None,
) -> MagicMock:
    """
    Build a mock SQLAlchemy session for the reject endpoint.

    Query order in reject_correction:
      db.get(Job, job_id)                   — configured via session.get
      query #0 (.first()) — fetch JobPage   by (job_id, page_number, sub_page_index=NULL)
      query #1 (.first()) — fetch PageLineage by (job_id, page_number, sub_page_index=NULL)
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


# ── HTTP endpoint tests ────────────────────────────────────────────────────────


class TestRejectCorrectionEndpoint:
    def setup_method(self) -> None:
        self.client = TestClient(app)

    def teardown_method(self) -> None:
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    # ── Success path ──────────────────────────────────────────────────────────

    def test_reject_returns_200(self) -> None:
        """Successful reject returns HTTP 200."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.reject.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")

        assert r.status_code == 200

    def test_reject_response_body(self) -> None:
        """Response contains page_number and new_state='review'."""
        job = _make_job()
        page = _make_page(page_number=3, status="pending_human_correction")
        lineage = _make_lineage(page_number=3)

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.reject.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/3/correction-reject")

        assert r.status_code == 200
        body = r.json()
        assert body["page_number"] == 3
        assert body["new_state"] == "review"

    # ── State transition ──────────────────────────────────────────────────────

    def test_advance_page_state_called_correctly(self) -> None:
        """advance_page_state is called with correct from/to states."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch(
            "services.eep.app.correction.reject.advance_page_state",
            return_value=True,
        ) as mock_advance:
            r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")

        assert r.status_code == 200
        mock_advance.assert_called_once_with(
            session,
            page.page_id,
            from_state="pending_human_correction",
            to_state="review",
        )

    def test_cas_miss_still_returns_200(self) -> None:
        """advance_page_state returning False (CAS miss) does not fail the request."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.reject.advance_page_state", return_value=False):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")

        assert r.status_code == 200

    # ── Persistence checks ────────────────────────────────────────────────────

    def test_review_reasons_set_on_page(self) -> None:
        """page.review_reasons is set to ['human_correction_rejected']."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.reject.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")

        assert r.status_code == 200
        assert page.review_reasons == ["human_correction_rejected"]

    def test_human_corrected_set_to_false(self) -> None:
        """lineage.human_corrected is always set to False on reject."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()
        lineage.human_corrected = True  # starts True; must become False

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.reject.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")

        assert r.status_code == 200
        assert lineage.human_corrected is False

    def test_notes_stored_when_provided(self) -> None:
        """Reviewer notes from request body are stored in lineage.reviewer_notes."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.reject.advance_page_state", return_value=True):
            r = self.client.post(
                "/v1/jobs/job-001/pages/1/correction-reject",
                json={"notes": "image quality too poor to correct"},
            )

        assert r.status_code == 200
        assert lineage.reviewer_notes == "image quality too poor to correct"

    def test_notes_not_written_when_absent(self) -> None:
        """When notes is absent, lineage.reviewer_notes is not overwritten."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()
        lineage.reviewer_notes = "pre-existing note"

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.reject.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")

        assert r.status_code == 200
        # notes was None in request → reviewer_notes must not be touched
        assert lineage.reviewer_notes == "pre-existing note"

    def test_empty_body_accepted(self) -> None:
        """Request with no body (empty JSON object) is accepted."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.reject.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject", json={})

        assert r.status_code == 200

    def test_db_commit_called(self) -> None:
        """db.commit() is called to persist changes."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")
        lineage = _make_lineage()

        session = _make_session(job=job, first_results=[page, lineage])
        self._inject(session)

        with patch("services.eep.app.correction.reject.advance_page_state", return_value=True):
            r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")

        assert r.status_code == 200
        session.commit.assert_called_once()

    # ── Error cases ───────────────────────────────────────────────────────────

    def test_404_when_job_not_found(self) -> None:
        """Job not found → 404 with 'not found' in detail."""
        session = _make_session(job=None)
        self._inject(session)

        r = self.client.post("/v1/jobs/missing-job/pages/1/correction-reject")
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_404_when_page_not_found(self) -> None:
        """Page query returns None → 404."""
        job = _make_job()
        session = _make_session(job=job, first_results=[None])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/99/correction-reject")
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_409_when_page_not_in_pending_human_correction(self) -> None:
        """Page in wrong state → 409 with state name in detail."""
        job = _make_job()
        page = _make_page(status="ptiff_qa_pending")

        session = _make_session(job=job, first_results=[page])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")
        assert r.status_code == 409
        assert "pending_human_correction" in r.json()["detail"]

    @pytest.mark.parametrize(
        "state",
        ["accepted", "review", "failed", "layout_detection", "ptiff_qa_pending"],
    )
    def test_409_for_various_non_pending_states(self, state: str) -> None:
        """Any state other than pending_human_correction returns 409."""
        job = _make_job()
        page = _make_page(status=state)

        session = _make_session(job=job, first_results=[page])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")
        assert r.status_code == 409

    def test_500_when_lineage_missing(self) -> None:
        """Missing lineage row → 500 with 'data-integrity' in detail."""
        job = _make_job()
        page = _make_page(status="pending_human_correction")

        session = _make_session(job=job, first_results=[page, None])
        self._inject(session)

        r = self.client.post("/v1/jobs/job-001/pages/1/correction-reject")
        assert r.status_code == 500
        detail = r.json()["detail"].lower()
        assert "data-integrity" in detail or "lineage" in detail
