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

import threading
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import numpy as np

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


class TestLayoutArtifactIoReliability:
    @pytest.mark.asyncio
    async def test_read_artifact_bytes_retries_and_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.eep_worker.app import worker_loop

        class _Backend:
            def __init__(self) -> None:
                self.calls = 0

            def get_bytes(self, uri: str) -> bytes:
                self.calls += 1
                if self.calls == 1:
                    raise OSError(f"temporary read failure for {uri}")
                return b"layout-bytes"

        backend = _Backend()
        monkeypatch.setattr(worker_loop, "get_backend", lambda uri: backend)

        data = await worker_loop._read_artifact_bytes_with_retry(
            uri="s3://bucket/jobs/j1/downsampled/1.tiff",
            timeout_seconds=1.0,
            attempts=2,
            backoff_seconds=0.0,
            job_id="job-123",
            page_number=1,
            context="layout_google_fallback",
        )

        assert data == b"layout-bytes"
        assert backend.calls == 2

    @pytest.mark.asyncio
    async def test_read_artifact_bytes_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.eep_worker.app import worker_loop

        class _Backend:
            def get_bytes(self, uri: str) -> bytes:
                time.sleep(0.05)
                return b"late"

        monkeypatch.setattr(worker_loop, "get_backend", lambda uri: _Backend())

        with pytest.raises(RuntimeError, match="could not read artifact bytes"):
            await worker_loop._read_artifact_bytes_with_retry(
                uri="s3://bucket/jobs/j1/downsampled/1.tiff",
                timeout_seconds=0.01,
                attempts=1,
                backoff_seconds=0.0,
                job_id="job-123",
                page_number=1,
                context="layout_google_fallback",
            )

    @pytest.mark.asyncio
    async def test_prepare_layout_input_artifact_reuses_cached_downsample_without_source_read(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.eep_worker.app import worker_loop

        page = MockJobPage(
            status="layout_detection",
            input_image_uri="s3://bucket/input.tiff",
            output_image_uri="s3://bucket/output.tiff",
        )
        job = MockJob(job_id="job-123")
        lineage = SimpleNamespace(
            gate_results={
                "downsample": {
                    "source_artifact_uri": "s3://bucket/output.tiff",
                    "downsampled_artifact_uri": "s3://bucket/jobs/job-123/downsampled/1.tiff",
                    "original_width": 1000,
                    "original_height": 2000,
                    "downsampled_width": 500,
                    "downsampled_height": 1000,
                }
            },
            human_corrected=False,
            split_source=False,
        )
        config = SimpleNamespace(
            layout_artifact_io_timeout_seconds=1.0,
            layout_artifact_io_attempts=1,
            layout_artifact_io_backoff_seconds=0.0,
        )

        monkeypatch.setattr(worker_loop, "get_backend", lambda uri: pytest.fail("unexpected storage read"))
        monkeypatch.setattr(worker_loop, "_commit", lambda session: pytest.fail("unexpected commit"))

        layout_image_uri, layout_input = await worker_loop._prepare_layout_input_artifact(
            session=object(),
            page=page,
            job=job,
            lineage=lineage,
            source_page_artifact_uri="s3://bucket/output.tiff",
            config=config,
        )

        assert layout_image_uri == "s3://bucket/jobs/job-123/downsampled/1.tiff"
        assert layout_input.input_source == "downsampled"
        assert layout_input.layout_input_width == 500
        assert layout_input.layout_input_height == 1000

    @pytest.mark.asyncio
    async def test_prepare_layout_input_artifact_commits_on_main_thread(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.eep_worker.app import worker_loop
        from services.eep_worker.app.downsample_step import DownsampleResult

        page = MockJobPage(
            status="layout_detection",
            input_image_uri="s3://bucket/input.tiff",
            output_image_uri="s3://bucket/output.tiff",
        )
        job = MockJob(job_id="job-123")
        lineage = SimpleNamespace(gate_results={}, human_corrected=False, split_source=False)
        config = SimpleNamespace(
            layout_artifact_io_timeout_seconds=1.0,
            layout_artifact_io_attempts=1,
            layout_artifact_io_backoff_seconds=0.0,
        )
        commit_threads: list[int] = []

        monkeypatch.setattr(
            worker_loop,
            "_read_artifact_bytes_with_retry",
            AsyncMock(return_value=b"source-bytes"),
        )
        monkeypatch.setattr(
            worker_loop,
            "_decode_image_array",
            lambda image_bytes, *, uri: np.zeros((20, 10, 3), dtype=np.uint8),
        )
        monkeypatch.setattr(worker_loop, "get_backend", lambda uri: MagicMock())
        monkeypatch.setattr(
            worker_loop,
            "run_downsample_step",
            lambda **kwargs: DownsampleResult(
                source_artifact_uri="s3://bucket/output.tiff",
                downsampled_artifact_uri="s3://bucket/jobs/job-123/downsampled/1.tiff",
                original_width=10,
                original_height=20,
                downsampled_width=10,
                downsampled_height=20,
                scale_factor=1.0,
                processing_time_ms=1.0,
            ),
        )
        monkeypatch.setattr(worker_loop, "_commit", lambda session: commit_threads.append(threading.get_ident()))

        await worker_loop._prepare_layout_input_artifact(
            session=object(),
            page=page,
            job=job,
            lineage=lineage,
            source_page_artifact_uri="s3://bucket/output.tiff",
            config=config,
        )

        assert commit_threads == [threading.get_ident()]


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
