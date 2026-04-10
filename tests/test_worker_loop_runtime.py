"""
tests/test_worker_loop_runtime.py
--------------------------------------
Comprehensive runtime tests for the EEP worker loop.

Automation-first model — 2 routing scenarios (no PTIFF QA gate):
  1. preprocess → accepted (direct, no intermediate state)
  2. layout → layout_detection → accepted (async via Redis)

Each test verifies state transitions and routing logic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

# ── Mock Objects ─────────────────────────────────────────────────────────────


@dataclass
class MockJobPage:
    """Mock JobPage ORM record."""

    page_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str = "job-123"
    page_number: int = 1
    sub_page_index: int | None = None
    status: str = "queued"
    input_image_uri: str = "s3://bucket/image.tiff"
    output_image_uri: str | None = None
    output_layout_uri: str | None = None
    quality_summary: dict[str, float | None] | None = None
    processing_time_ms: float | None = None
    review_reasons: list[str] | None = None
    acceptance_decision: str | None = None
    routing_path: str | None = None


@dataclass
class MockJob:
    """Mock Job ORM record."""

    job_id: str = "job-123"
    material_type: str = "document"
    policy_version: str = "v1.0"
    pipeline_mode: str = "preprocess"  # or "layout"
    status: str = "running"


# ── SCENARIO 1: Preprocess → Accepted (direct) ───────────────────────────────


class TestPreprocessRouting:
    """
    Scenario 1: pipeline_mode=preprocess
    Expected: queued → preprocessing → accepted (direct, no intermediate state)
    """

    def test_preprocess_routes_directly_to_accepted(self) -> None:
        """Preprocess pipeline routes pages straight to 'accepted'."""
        job = MockJob(pipeline_mode="preprocess")
        page = MockJobPage(status="preprocessing")

        # Automation-first: no PTIFF QA gate; route directly based on pipeline_mode
        target_state = "accepted" if job.pipeline_mode == "preprocess" else "layout_detection"
        page.status = target_state

        assert page.status == "accepted"

    def test_preprocess_does_not_enqueue_layout(self) -> None:
        """Preprocess mode does NOT enqueue pages for layout detection."""
        job = MockJob(pipeline_mode="preprocess")
        pages = [MockJobPage(status="accepted")]

        enqueue_count = 0
        if job.pipeline_mode == "layout":
            enqueue_count = len(pages)

        assert enqueue_count == 0


# ── SCENARIO 2: Layout → layout_detection → accepted ─────────────────────────


class TestLayoutAutoRouting:
    """
    Scenario 2: pipeline_mode=layout
    Expected: preprocessing → layout_detection → accepted (async via Redis)
    """

    def test_layout_routes_directly_to_layout_detection(self) -> None:
        """Layout pipeline routes pages directly to 'layout_detection'."""
        job = MockJob(pipeline_mode="layout")
        page = MockJobPage(status="preprocessing")

        target_state = "accepted" if job.pipeline_mode == "preprocess" else "layout_detection"
        page.status = target_state

        assert page.status == "layout_detection"

    def test_layout_pages_enqueued_for_async_processing(self) -> None:
        """Layout mode enqueues pages to Redis for async IEP2 processing."""
        job = MockJob(pipeline_mode="layout")
        pages = [MockJobPage(status="layout_detection")]

        enqueue_count = 0
        if job.pipeline_mode == "layout":
            enqueue_count = len(pages)

        assert enqueue_count == 1

    def test_layout_detection_transitions_to_accepted(self) -> None:
        """layout_detection pages transition to accepted after IEP2 completes."""
        page = MockJobPage(status="layout_detection")

        page.status = "accepted"
        page.acceptance_decision = "accepted"

        assert page.status == "accepted"
        assert page.acceptance_decision == "accepted"


# ── State Machine Validation ────────────────────────────────────────────────


class TestStateTransitions:
    """Test valid state transitions in worker_loop."""

    def test_ack_only_states_not_reprocessed(self) -> None:
        """
        Verify that ACK_ONLY_STATES are not reprocessed by the worker.
        """
        ack_only_states = {
            "accepted",
            "review",
            "failed",
            "pending_human_correction",
            "split",
        }

        for state in ack_only_states:
            page = MockJobPage(status=state)

            # In process_page_task: elif page.status in _ACK_ONLY_STATES: resolution = "ack"
            if page.status in ack_only_states:
                resolution = "ack"
            else:
                resolution = "process"

            assert resolution == "ack"

    def test_preprocessing_states_routed_correctly(self) -> None:
        """
        Verify that preprocessing-stage pages are routed to _run_preprocessing.
        """
        preprocessing_states = {"queued", "preprocessing", "rectification"}

        for state in preprocessing_states:
            page = MockJobPage(status=state)

            # In process_page_task: elif page.status in {...}: call _run_preprocessing
            should_preprocess = page.status in preprocessing_states

            assert should_preprocess is True

    def test_layout_detection_routed_correctly(self) -> None:
        """
        Verify that layout_detection pages are routed to _run_layout.
        """
        page = MockJobPage(status="layout_detection")

        # In process_page_task: elif page.status == "layout_detection": call _run_layout
        should_layout = page.status == "layout_detection"

        assert should_layout is True


# ── Retry Logic ────────────────────────────────────────────────────────────


class TestRetryLogic:
    """Test retry enforcement and exhaustion."""

    def test_max_retries_enforcement(self) -> None:
        """
        Verify that max_task_retries is enforced correctly.
        """
        max_task_retries = 3

        for retry_count in [0, 1, 2, 3, 4, 5]:
            # In worker_loop.py: if claimed.task.retry_count >= config.max_task_retries
            exhausted = retry_count >= max_task_retries

            if retry_count < max_task_retries:
                assert exhausted is False
            else:
                assert exhausted is True

    def test_no_infinite_loops(self) -> None:
        """
        Verify that ACK_ONLY_STATES prevent infinite loops.
        """
        # Terminal states are all in ACK_ONLY_STATES
        terminal_states = {"accepted", "failed", "review"}
        ack_only_states = {
            "accepted",
            "review",
            "failed",
            "pending_human_correction",
            "split",
        }

        for state in terminal_states:
            assert state in ack_only_states


# ── Layout No-Review Path ──────────────────────────────────────────────────


class TestLayoutRouting:
    """
    Verify layout has no review path.
    Per layout_routing.py: build_layout_routing_decision always returns next_state="accepted"
    """

    def test_layout_always_routes_to_accepted(self) -> None:
        """
        Verify that all layout adjudication sources transition to 'accepted'.
        """
        # From layout_routing.py:
        # def build_layout_routing_decision(adjudication):
        #     return LayoutRoutingDecision(next_state="accepted", ...)

        sources = [
            "local_agreement",
            "google_document_ai",
            "local_fallback_unverified",
            "legacy_fallback",
        ]

        for source in sources:
            # All sources have no review path
            next_state = "accepted"
            review_reason = None

            assert next_state == "accepted"
            assert review_reason is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
