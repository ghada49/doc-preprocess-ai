"""
tests/test_p1_schemas_eep.py
----------------------------
Packet 1.3 validator tests for shared.schemas.eep:
  - TERMINAL_PAGE_STATES (exported constant)
  - PageState (all valid states)
  - PageInput
  - JobCreateRequest  (pages bounds, pipeline_mode)
  - JobCreateResponse
  - QualitySummary
  - PageStatus
  - JobStatusSummary
  - JobStatusResponse

Definition of done:
  - TERMINAL_PAGE_STATES exported correctly
  - pending_human_correction is in TERMINAL_PAGE_STATES (worker-terminal)
  - job-related schemas match spec
  - automation-first: preprocessing routes directly to layout_detection/accepted
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from shared.schemas.eep import (
    TERMINAL_PAGE_STATES,
    JobCreateRequest,
    JobCreateResponse,
    JobStatusResponse,
    JobStatusSummary,
    PageInput,
    PageStatus,
    QualitySummary,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 3, 19, 12, 0, 0, tzinfo=timezone.utc)


def _page_input(n: int = 1) -> PageInput:
    return PageInput(page_number=n, input_uri=f"s3://bucket/jobs/j1/input/{n}.tiff")


def _create_request(n_pages: int = 2, **kwargs) -> JobCreateRequest:  # type: ignore[no-untyped-def]
    return JobCreateRequest(
        collection_id="aub_aco003575",
        material_type="book",
        pages=[_page_input(i) for i in range(1, n_pages + 1)],
        policy_version="v1.0",
        **kwargs,
    )


def _job_summary(**kwargs) -> JobStatusSummary:  # type: ignore[no-untyped-def]
    defaults = dict(
        job_id="j1",
        collection_id="aub_aco003575",
        material_type="book",
        pipeline_mode="layout",
        policy_version="v1.0",
        shadow_mode=False,
        status="queued",
        page_count=2,
        accepted_count=0,
        review_count=0,
        failed_count=0,
        pending_human_correction_count=0,
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(kwargs)
    return JobStatusSummary(**defaults)  # type: ignore[arg-type]


# ── TERMINAL_PAGE_STATES ───────────────────────────────────────────────────────


class TestTerminalPageStates:
    def test_is_frozenset(self) -> None:
        assert isinstance(TERMINAL_PAGE_STATES, frozenset)

    def test_contains_accepted(self) -> None:
        assert "accepted" in TERMINAL_PAGE_STATES

    def test_pending_human_correction_not_terminal(self) -> None:
        # pending_human_correction is a worker-stop state (automated worker halts)
        # but NOT a job-accounting terminal state — the page is still open for
        # human action and will re-enter the pipeline after correction.
        assert "pending_human_correction" not in TERMINAL_PAGE_STATES

    def test_contains_review(self) -> None:
        assert "review" in TERMINAL_PAGE_STATES

    def test_contains_failed(self) -> None:
        assert "failed" in TERMINAL_PAGE_STATES

    def test_contains_split(self) -> None:
        assert "split" in TERMINAL_PAGE_STATES

    def test_exact_membership(self) -> None:
        # TERMINAL_PAGE_STATES covers leaf-final job-accounting outcomes only.
        # pending_human_correction and ptiff_qa_pending are worker-stop states but
        # NOT job-terminal — pages in those states are still open for action.
        expected = frozenset({"accepted", "review", "failed", "split"})
        assert TERMINAL_PAGE_STATES == expected

    def test_queued_not_terminal(self) -> None:
        assert "queued" not in TERMINAL_PAGE_STATES

    def test_preprocessing_not_terminal(self) -> None:
        assert "preprocessing" not in TERMINAL_PAGE_STATES

    def test_layout_detection_not_terminal(self) -> None:
        assert "layout_detection" not in TERMINAL_PAGE_STATES


# ── PageState ──────────────────────────────────────────────────────────────────


class TestPageState:
    ALL_PAGE_STATES = [
        "queued",
        "preprocessing",
        "rectification",
        "ptiff_qa_pending",
        "layout_detection",
        "semantic_norm",
        "pending_human_correction",
        "accepted",
        "review",
        "failed",
        "split",
    ]

    def test_eleven_states_total(self) -> None:
        assert len(self.ALL_PAGE_STATES) == 11

    def test_page_status_accepts_all_states(self) -> None:
        for state in self.ALL_PAGE_STATES:
            ps = PageStatus.model_validate({"page_number": 1, "status": state})
            assert ps.status == state

    def test_invalid_state_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PageStatus.model_validate({"page_number": 1, "status": "processing_done"})


# ── PageInput ──────────────────────────────────────────────────────────────────


class TestPageInput:
    def test_valid(self) -> None:
        p = _page_input(5)
        assert p.page_number == 5
        assert p.reference_ptiff_uri is None

    def test_with_reference_ptiff(self) -> None:
        p = PageInput(
            page_number=1,
            input_uri="s3://bucket/input/1.tiff",
            reference_ptiff_uri="s3://bucket/reference/1.tiff",
        )
        assert p.reference_ptiff_uri is not None

    def test_page_number_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PageInput(page_number=0, input_uri="s3://x")

    def test_page_number_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PageInput(page_number=-1, input_uri="s3://x")


# ── JobCreateRequest ───────────────────────────────────────────────────────────


class TestJobCreateRequest:
    def test_valid_defaults(self) -> None:
        r = _create_request()
        assert r.pipeline_mode == "layout"
        assert r.shadow_mode is False

    def test_both_pipeline_modes_valid(self) -> None:
        for mode in ["preprocess", "layout"]:
            r = JobCreateRequest.model_validate(
                {
                    "collection_id": "c1",
                    "material_type": "book",
                    "pages": [{"page_number": 1, "input_uri": "s3://x"}],
                    "pipeline_mode": mode,
                    "policy_version": "v1.0",
                    "shadow_mode": False,
                }
            )
            assert r.pipeline_mode == mode

    def test_all_material_types_valid(self) -> None:
        for mt in ["book", "newspaper", "archival_document", "microfilm"]:
            r = JobCreateRequest.model_validate(
                {
                    "collection_id": "c1",
                    "material_type": mt,
                    "pages": [{"page_number": 1, "input_uri": "s3://x"}],
                    "pipeline_mode": "layout",
                    "policy_version": "v1.0",
                    "shadow_mode": False,
                }
            )
            assert r.material_type == mt

    def test_zero_pages_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JobCreateRequest(
                collection_id="c1",
                material_type="book",
                pages=[],
                policy_version="v1.0",
            )

    def test_1001_pages_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JobCreateRequest(
                collection_id="c1",
                material_type="book",
                pages=[_page_input(i) for i in range(1, 1002)],
                policy_version="v1.0",
            )

    def test_exactly_1000_pages_valid(self) -> None:
        r = _create_request(n_pages=1000)
        assert len(r.pages) == 1000

    def test_single_page_valid(self) -> None:
        r = _create_request(n_pages=1)
        assert len(r.pages) == 1

    def test_invalid_pipeline_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JobCreateRequest.model_validate(
                {
                    "collection_id": "c1",
                    "material_type": "book",
                    "pages": [{"page_number": 1, "input_uri": "s3://x"}],
                    "pipeline_mode": "full",
                    "policy_version": "v1.0",
                    "shadow_mode": False,
                }
            )

    def test_invalid_material_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JobCreateRequest.model_validate(
                {
                    "collection_id": "c1",
                    "material_type": "scroll",
                    "pages": [{"page_number": 1, "input_uri": "s3://x"}],
                    "pipeline_mode": "layout",
                    "policy_version": "v1.0",
                    "shadow_mode": False,
                }
            )


# ── JobCreateResponse ──────────────────────────────────────────────────────────


class TestJobCreateResponse:
    def test_valid(self) -> None:
        r = JobCreateResponse(
            job_id="abc-123",
            status="queued",
            page_count=10,
            created_at=_NOW,
        )
        assert r.status == "queued"
        assert r.page_count == 10

    def test_status_must_be_queued(self) -> None:
        with pytest.raises(ValidationError):
            JobCreateResponse.model_validate(
                {"job_id": "abc", "status": "running", "page_count": 1, "created_at": _NOW}
            )


# ── QualitySummary ─────────────────────────────────────────────────────────────


class TestQualitySummary:
    def test_all_none_valid(self) -> None:
        q = QualitySummary()
        assert q.blur_score is None
        assert q.border_score is None
        assert q.skew_residual is None
        assert q.foreground_coverage is None

    def test_all_populated(self) -> None:
        q = QualitySummary(
            blur_score=0.85,
            border_score=0.9,
            skew_residual=0.05,
            foreground_coverage=0.95,
        )
        assert q.blur_score == 0.85

    def test_blur_score_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            QualitySummary(blur_score=1.1)

    def test_border_score_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            QualitySummary(border_score=-0.01)

    def test_skew_residual_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            QualitySummary(skew_residual=-0.01)

    def test_foreground_coverage_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            QualitySummary(foreground_coverage=1.01)


# ── PageStatus ─────────────────────────────────────────────────────────────────


class TestPageStatus:
    def test_valid_minimal(self) -> None:
        ps = PageStatus(page_number=1, status="queued")
        assert ps.status == "queued"
        assert ps.sub_page_index is None
        assert ps.acceptance_decision is None

    def test_valid_with_all_fields(self) -> None:
        ps = PageStatus(
            page_number=2,
            sub_page_index=0,
            status="accepted",
            routing_path="preprocessing_only",
            output_image_uri="s3://bucket/output/2.tiff",
            output_layout_uri=None,
            quality_summary=QualitySummary(blur_score=0.8),
            review_reasons=None,
            acceptance_decision="accepted",
            processing_time_ms=450.0,
        )
        assert ps.sub_page_index == 0
        assert ps.acceptance_decision == "accepted"

    def test_pending_human_correction_valid_status(self) -> None:
        ps = PageStatus(page_number=3, status="pending_human_correction")
        assert ps.status == "pending_human_correction"

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PageStatus.model_validate({"page_number": 1, "status": "unknown_state"})

    def test_invalid_acceptance_decision_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PageStatus.model_validate(
                {"page_number": 1, "status": "accepted", "acceptance_decision": "pending"}
            )


# ── JobStatusSummary ───────────────────────────────────────────────────────────


class TestJobStatusSummary:
    def test_valid(self) -> None:
        s = _job_summary()
        assert s.job_id == "j1"
        assert s.completed_at is None

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JobStatusSummary.model_validate(
                {
                    **_job_summary().model_dump(),
                    "status": "partial",
                }
            )

    def test_with_completed_at(self) -> None:
        s = _job_summary(status="done", completed_at=_NOW)
        assert s.completed_at == _NOW

    def test_with_created_by(self) -> None:
        s = _job_summary(created_by="user_42")
        assert s.created_by == "user_42"


# ── JobStatusResponse ──────────────────────────────────────────────────────────


class TestJobStatusResponse:
    def test_valid_no_pages(self) -> None:
        r = JobStatusResponse(summary=_job_summary(), pages=[])
        assert r.summary.job_id == "j1"
        assert r.pages == []

    def test_valid_with_pages(self) -> None:
        pages = [
            PageStatus(page_number=1, status="accepted", acceptance_decision="accepted"),
            PageStatus(page_number=2, status="pending_human_correction"),
        ]
        r = JobStatusResponse(summary=_job_summary(page_count=2), pages=pages)
        assert len(r.pages) == 2
        assert r.pages[1].status == "pending_human_correction"

    def test_page_with_pending_human_correction_is_valid(self) -> None:
        # Key invariant: pending_human_correction pages appear in responses (worker-terminal)
        r = JobStatusResponse(
            summary=_job_summary(status="running"),
            pages=[PageStatus(page_number=1, status="pending_human_correction")],
        )
        assert r.pages[0].status == "pending_human_correction"
        # pending_human_correction is a worker-stop state (automated processing halts)
        # but NOT a job-accounting terminal state — the page remains open for human action.
        assert "pending_human_correction" not in TERMINAL_PAGE_STATES

    def test_roundtrip_serialization(self) -> None:
        original = JobStatusResponse(
            summary=_job_summary(status="done", completed_at=_NOW),
            pages=[PageStatus(page_number=1, status="accepted", acceptance_decision="accepted")],
        )
        dumped = original.model_dump()
        restored = JobStatusResponse(**dumped)
        assert restored.summary.status == "done"
        assert restored.pages[0].acceptance_decision == "accepted"
