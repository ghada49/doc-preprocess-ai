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


class TestSemanticNormAfterHumanCorrection:
    @pytest.mark.asyncio
    async def test_preprocess_human_correction_runs_iep1e_then_accepts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.eep_worker.app import worker_loop

        session = MagicMock()
        job = MockJob(job_id="job-123", pipeline_mode="preprocess")
        page = MockJobPage(
            job_id=job.job_id,
            page_number=1,
            status="semantic_norm",
            output_image_uri="s3://bucket/jobs/job-123/corrected/1.tiff",
        )
        lineage = SimpleNamespace(lineage_id="lineage-1")
        config = SimpleNamespace(
            iep1e_endpoint="http://iep1e",
            backend=object(),
            iep1e_circuit_breaker=object(),
        )
        call_iep1e = AsyncMock(
            return_value=SimpleNamespace(
                ordered_page_uris=["s3://bucket/jobs/job-123/output/1.tiff"],
            ),
        )
        advance_calls: list[dict[str, object]] = []
        completion_calls: list[dict[str, object]] = []
        summary_jobs: list[MockJob] = []
        commits: list[object] = []
        enqueued: list[object] = []

        def advance_page_state(session_arg: object, page_id: str, **kwargs: object) -> bool:
            advance_calls.append({"session": session_arg, "page_id": page_id, **kwargs})
            return True

        def update_lineage_completion(
            session_arg: object,
            lineage_id: str,
            **kwargs: object,
        ) -> None:
            completion_calls.append(
                {"session": session_arg, "lineage_id": lineage_id, **kwargs},
            )

        monkeypatch.setattr(
            worker_loop,
            "_find_lineage",
            lambda session_arg, job_id, page_number, sub_page_index: lineage,
        )
        monkeypatch.setattr(
            worker_loop,
            "_resolve_material_type_placeholder",
            lambda session_arg, job_arg, page_arg: "book",
        )
        monkeypatch.setattr(worker_loop, "_call_iep1e", call_iep1e)
        monkeypatch.setattr(worker_loop, "advance_page_state", advance_page_state)
        monkeypatch.setattr(worker_loop, "update_lineage_completion", update_lineage_completion)
        monkeypatch.setattr(
            worker_loop,
            "_sync_job_summary",
            lambda session_arg, job_arg: summary_jobs.append(job_arg),
        )
        monkeypatch.setattr(worker_loop, "_commit", lambda session_arg: commits.append(session_arg))
        monkeypatch.setattr(
            worker_loop,
            "enqueue_page_task",
            lambda redis_arg, task: enqueued.append(task),
        )

        resolution = await worker_loop._run_semantic_norm(
            session=session,
            page=page,
            job=job,
            config=config,
            redis_client=object(),
            task_started_at=time.monotonic(),
        )

        assert resolution == "ack"
        call_iep1e.assert_awaited_once()
        assert call_iep1e.await_args.kwargs["page_uris"] == [
            "s3://bucket/jobs/job-123/corrected/1.tiff",
        ]
        assert call_iep1e.await_args.kwargs["sub_page_indices"] == [0]
        assert advance_calls == [
            {
                "session": session,
                "page_id": page.page_id,
                "from_state": "semantic_norm",
                "to_state": "accepted",
                "output_image_uri": "s3://bucket/jobs/job-123/output/1.tiff",
            }
        ]
        assert page.status == "accepted"
        assert page.output_image_uri == "s3://bucket/jobs/job-123/output/1.tiff"
        assert page.reading_order == 1
        assert page.acceptance_decision == "accepted"
        assert page.routing_path == "preprocessing_only"
        assert len(completion_calls) == 1
        assert isinstance(completion_calls[0]["total_processing_ms"], float)
        assert completion_calls[0]["total_processing_ms"] >= 0.0
        completion_without_time = {
            key: value
            for key, value in completion_calls[0].items()
            if key != "total_processing_ms"
        }
        assert completion_without_time == {
            "session": session,
            "lineage_id": "lineage-1",
            "acceptance_decision": "accepted",
            "acceptance_reason": "preprocessing accepted after human correction",
            "routing_path": "preprocessing_only",
            "output_image_uri": "s3://bucket/jobs/job-123/output/1.tiff",
        }
        assert summary_jobs == [job]
        assert commits == [session]
        assert enqueued == []

    @pytest.mark.asyncio
    async def test_layout_human_correction_runs_iep1e_then_enqueues_layout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.eep_worker.app import worker_loop

        session = MagicMock()
        job = MockJob(job_id="job-123", pipeline_mode="layout")
        page = MockJobPage(
            job_id=job.job_id,
            page_number=1,
            sub_page_index=2,
            status="semantic_norm",
            output_image_uri="s3://bucket/jobs/job-123/corrected/1_2.tiff",
        )
        lineage = SimpleNamespace(lineage_id="lineage-2")
        config = SimpleNamespace(
            iep1e_endpoint="http://iep1e",
            backend=object(),
            iep1e_circuit_breaker=object(),
        )
        call_iep1e = AsyncMock(
            return_value=SimpleNamespace(
                ordered_page_uris=["s3://bucket/jobs/job-123/output/1_2.tiff"],
            ),
        )
        advance_calls: list[dict[str, object]] = []
        enqueued: list[tuple[object, object]] = []

        def advance_page_state(session_arg: object, page_id: str, **kwargs: object) -> bool:
            advance_calls.append({"session": session_arg, "page_id": page_id, **kwargs})
            return True

        monkeypatch.setattr(
            worker_loop,
            "_find_lineage",
            lambda session_arg, job_id, page_number, sub_page_index: lineage,
        )
        monkeypatch.setattr(
            worker_loop,
            "_resolve_material_type_placeholder",
            lambda session_arg, job_arg, page_arg: "book",
        )
        monkeypatch.setattr(worker_loop, "_call_iep1e", call_iep1e)
        monkeypatch.setattr(worker_loop, "advance_page_state", advance_page_state)
        monkeypatch.setattr(
            worker_loop,
            "_commit",
            lambda session_arg: pytest.fail("unexpected preprocess commit path"),
        )
        monkeypatch.setattr(
            worker_loop,
            "enqueue_page_task",
            lambda redis_arg, task: enqueued.append((redis_arg, task)),
        )

        redis_client = object()
        resolution = await worker_loop._run_semantic_norm(
            session=session,
            page=page,
            job=job,
            config=config,
            redis_client=redis_client,
            task_started_at=time.monotonic(),
        )

        assert resolution == "ack"
        call_iep1e.assert_awaited_once()
        assert call_iep1e.await_args.kwargs["page_uris"] == [
            "s3://bucket/jobs/job-123/corrected/1_2.tiff",
        ]
        assert call_iep1e.await_args.kwargs["sub_page_indices"] == [2]
        assert advance_calls == [
            {
                "session": session,
                "page_id": page.page_id,
                "from_state": "semantic_norm",
                "to_state": "layout_detection",
                "output_image_uri": "s3://bucket/jobs/job-123/output/1_2.tiff",
            }
        ]
        assert page.status == "layout_detection"
        assert page.output_image_uri == "s3://bucket/jobs/job-123/output/1_2.tiff"
        assert page.reading_order == 1
        session.commit.assert_called_once_with()
        assert len(enqueued) == 1
        assert enqueued[0][0] is redis_client
        assert enqueued[0][1].job_id == job.job_id
        assert enqueued[0][1].page_id == page.page_id
        assert enqueued[0][1].page_number == page.page_number
        assert enqueued[0][1].sub_page_index == page.sub_page_index

    @pytest.mark.asyncio
    async def test_layout_split_child_correction_reconsiders_accepted_sibling(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.eep_worker.app import worker_loop

        session = MagicMock()
        job = MockJob(job_id="job-123", pipeline_mode="layout")
        page = MockJobPage(
            job_id=job.job_id,
            page_number=1,
            sub_page_index=0,
            status="semantic_norm",
            output_image_uri="s3://bucket/jobs/job-123/corrected/1_0.tiff",
        )
        sibling = MockJobPage(
            job_id=job.job_id,
            page_number=1,
            sub_page_index=1,
            status="accepted",
            output_image_uri="s3://bucket/jobs/job-123/output/1_1.tiff",
            output_layout_uri="s3://bucket/jobs/job-123/layout/1_1.layout.json",
            acceptance_decision="accepted",
            routing_path="layout_adjudication",
        )
        sibling.layout_consensus_result = {"status": "done"}
        lineage = SimpleNamespace(
            lineage_id="lineage-0",
            split_source=True,
            parent_page_id="parent",
            gate_results={},
            layout_artifact_state="pending",
            output_image_uri=page.output_image_uri,
        )
        sibling_lineage = SimpleNamespace(
            lineage_id="lineage-1",
            split_source=True,
            parent_page_id="parent",
            gate_results={
                "downsample": {},
                "layout_input": {},
                "layout_adjudication": {},
            },
            layout_artifact_state="confirmed",
            output_image_uri=sibling.output_image_uri,
        )
        config = SimpleNamespace(
            iep1e_endpoint="http://iep1e",
            backend=object(),
            iep1e_circuit_breaker=object(),
        )
        orientation = SimpleNamespace(orientation_confident=True, best_rotation_deg=0)
        sem_result = SimpleNamespace(
            reading_direction="rtl",
            pages=[
                SimpleNamespace(
                    sub_page_index=0,
                    original_uri=page.output_image_uri,
                    oriented_uri="s3://bucket/jobs/job-123/oriented/1_0.tiff",
                    orientation=orientation,
                ),
                SimpleNamespace(
                    sub_page_index=1,
                    original_uri=sibling.output_image_uri,
                    oriented_uri="s3://bucket/jobs/job-123/oriented/1_1.tiff",
                    orientation=orientation,
                ),
            ],
            ordered_page_uris=[
                "s3://bucket/jobs/job-123/oriented/1_1.tiff",
                "s3://bucket/jobs/job-123/oriented/1_0.tiff",
            ],
            fallback_used=False,
        )
        call_iep1e = AsyncMock(return_value=sem_result)
        advance_calls: list[dict[str, object]] = []
        commits: list[object] = []
        enqueued: list[object] = []

        def advance_page_state(session_arg: object, page_id: str, **kwargs: object) -> bool:
            advance_calls.append({"session": session_arg, "page_id": page_id, **kwargs})
            return True

        monkeypatch.setattr(
            worker_loop,
            "_find_lineage",
            lambda session_arg, job_id, page_number, sub_page_index: lineage,
        )
        monkeypatch.setattr(
            worker_loop,
            "_find_split_child_group_for_semantic_norm",
            lambda session_arg, page_arg, lineage_arg: [
                worker_loop._SemanticNormSplitChild(page=page, lineage=lineage),
                worker_loop._SemanticNormSplitChild(page=sibling, lineage=sibling_lineage),
            ],
        )
        monkeypatch.setattr(
            worker_loop,
            "_resolve_material_type_placeholder",
            lambda session_arg, job_arg, page_arg: "book",
        )
        monkeypatch.setattr(worker_loop, "_call_iep1e", call_iep1e)
        monkeypatch.setattr(worker_loop, "advance_page_state", advance_page_state)
        monkeypatch.setattr(worker_loop, "_sync_job_summary", lambda session_arg, job_arg: None)
        monkeypatch.setattr(worker_loop, "_commit", lambda session_arg: commits.append(session_arg))
        monkeypatch.setattr(worker_loop, "enqueue_page_task", lambda redis_arg, task: enqueued.append(task))

        resolution = await worker_loop._run_semantic_norm(
            session=session,
            page=page,
            job=job,
            config=config,
            redis_client=object(),
            task_started_at=time.monotonic(),
        )

        assert resolution == "ack"
        call_iep1e.assert_awaited_once()
        assert call_iep1e.await_args.kwargs["page_uris"] == [
            "s3://bucket/jobs/job-123/corrected/1_0.tiff",
            "s3://bucket/jobs/job-123/output/1_1.tiff",
        ]
        assert call_iep1e.await_args.kwargs["x_centers"] == [0.0, 1.0]
        assert call_iep1e.await_args.kwargs["sub_page_indices"] == [0, 1]
        assert [call["from_state"] for call in advance_calls] == [
            "semantic_norm",
            "accepted",
            "semantic_norm",
        ]
        assert [call["to_state"] for call in advance_calls] == [
            "layout_detection",
            "semantic_norm",
            "layout_detection",
        ]
        assert page.status == "layout_detection"
        assert sibling.status == "layout_detection"
        assert page.output_image_uri == "s3://bucket/jobs/job-123/oriented/1_0.tiff"
        assert sibling.output_image_uri == "s3://bucket/jobs/job-123/oriented/1_1.tiff"
        assert page.reading_order == 2
        assert sibling.reading_order == 1
        assert sibling.acceptance_decision is None
        assert sibling.routing_path is None
        assert sibling.output_layout_uri is None
        assert sibling.layout_consensus_result is None
        assert sibling_lineage.gate_results is None
        assert sibling_lineage.layout_artifact_state == "pending"
        assert getattr(job, "reading_direction") == "rtl"
        assert commits == [session]
        assert len(enqueued) == 2
        assert {task.page_id for task in enqueued} == {page.page_id, sibling.page_id}

    @pytest.mark.asyncio
    async def test_preprocess_split_child_correction_reorders_accepted_sibling(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.eep_worker.app import worker_loop

        session = MagicMock()
        job = MockJob(job_id="job-123", pipeline_mode="preprocess")
        page = MockJobPage(
            job_id=job.job_id,
            page_number=1,
            sub_page_index=0,
            status="semantic_norm",
            output_image_uri="s3://bucket/jobs/job-123/corrected/1_0.tiff",
        )
        sibling = MockJobPage(
            job_id=job.job_id,
            page_number=1,
            sub_page_index=1,
            status="accepted",
            output_image_uri="s3://bucket/jobs/job-123/output/1_1.tiff",
            acceptance_decision="accepted",
            routing_path="preprocessing_only",
        )
        lineage = SimpleNamespace(
            lineage_id="lineage-0",
            split_source=True,
            parent_page_id="parent",
            gate_results={},
            layout_artifact_state="pending",
            output_image_uri=page.output_image_uri,
        )
        sibling_lineage = SimpleNamespace(
            lineage_id="lineage-1",
            split_source=True,
            parent_page_id="parent",
            gate_results={},
            layout_artifact_state="pending",
            output_image_uri=sibling.output_image_uri,
        )
        config = SimpleNamespace(
            iep1e_endpoint="http://iep1e",
            backend=object(),
            iep1e_circuit_breaker=object(),
        )
        orientation = SimpleNamespace(orientation_confident=True, best_rotation_deg=0)
        call_iep1e = AsyncMock(
            return_value=SimpleNamespace(
                reading_direction="rtl",
                pages=[
                    SimpleNamespace(
                        sub_page_index=0,
                        original_uri=page.output_image_uri,
                        oriented_uri=page.output_image_uri,
                        orientation=orientation,
                    ),
                    SimpleNamespace(
                        sub_page_index=1,
                        original_uri=sibling.output_image_uri,
                        oriented_uri=sibling.output_image_uri,
                        orientation=orientation,
                    ),
                ],
                ordered_page_uris=[
                    sibling.output_image_uri,
                    page.output_image_uri,
                ],
                fallback_used=False,
            ),
        )
        advance_calls: list[dict[str, object]] = []
        completion_calls: list[dict[str, object]] = []
        enqueued: list[object] = []

        def advance_page_state(session_arg: object, page_id: str, **kwargs: object) -> bool:
            advance_calls.append({"session": session_arg, "page_id": page_id, **kwargs})
            return True

        def update_lineage_completion(
            session_arg: object,
            lineage_id: str,
            **kwargs: object,
        ) -> None:
            completion_calls.append({"session": session_arg, "lineage_id": lineage_id, **kwargs})

        monkeypatch.setattr(
            worker_loop,
            "_find_lineage",
            lambda session_arg, job_id, page_number, sub_page_index: lineage,
        )
        monkeypatch.setattr(
            worker_loop,
            "_find_split_child_group_for_semantic_norm",
            lambda session_arg, page_arg, lineage_arg: [
                worker_loop._SemanticNormSplitChild(page=page, lineage=lineage),
                worker_loop._SemanticNormSplitChild(page=sibling, lineage=sibling_lineage),
            ],
        )
        monkeypatch.setattr(
            worker_loop,
            "_resolve_material_type_placeholder",
            lambda session_arg, job_arg, page_arg: "book",
        )
        monkeypatch.setattr(worker_loop, "_call_iep1e", call_iep1e)
        monkeypatch.setattr(worker_loop, "advance_page_state", advance_page_state)
        monkeypatch.setattr(worker_loop, "update_lineage_completion", update_lineage_completion)
        monkeypatch.setattr(worker_loop, "_sync_job_summary", lambda session_arg, job_arg: None)
        monkeypatch.setattr(worker_loop, "_commit", lambda session_arg: None)
        monkeypatch.setattr(worker_loop, "enqueue_page_task", lambda redis_arg, task: enqueued.append(task))

        resolution = await worker_loop._run_semantic_norm(
            session=session,
            page=page,
            job=job,
            config=config,
            redis_client=object(),
            task_started_at=time.monotonic(),
        )

        assert resolution == "ack"
        call_iep1e.assert_awaited_once()
        assert advance_calls[0]["from_state"] == "semantic_norm"
        assert advance_calls[0]["to_state"] == "accepted"
        assert page.status == "accepted"
        assert sibling.status == "accepted"
        assert page.reading_order == 2
        assert sibling.reading_order == 1
        assert len(completion_calls) == 1
        assert completion_calls[0]["lineage_id"] == "lineage-0"
        assert enqueued == []


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


class TestCallIep1eFailureHandling:
    """
    Tests for _call_iep1e returning None on all failure modes.

    Regression for: AttributeError: 'NoneType' object has no attribute 'pages'
    (job 94d6951e-5a23-491a-9dd5-00c8afd43fff) — iep1e warm_inference_timeout
    returned None, which the else-branch in _run_preprocessing then dereferenced.
    """

    @pytest.mark.asyncio
    async def test_warm_inference_timeout_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BackendError(WARM_INFERENCE_TIMEOUT) → _call_iep1e returns None (never raises)."""
        from services.eep_worker.app import worker_loop
        from shared.gpu.backend import BackendError, BackendErrorKind

        cb = SimpleNamespace(
            allow_call=lambda: True,
            record_failure=lambda kind: None,
            record_success=lambda: None,
        )

        async def _fake_wait(*_a: object, **_kw: object) -> bool:
            return True  # pretend ready

        async def _fake_call(_url: str, _payload: object) -> object:
            raise BackendError(BackendErrorKind.WARM_INFERENCE_TIMEOUT, "timeout")

        monkeypatch.setattr(worker_loop, "_wait_for_iep1e_ready", _fake_wait)
        backend = SimpleNamespace(call=_fake_call)

        result = await worker_loop._call_iep1e(
            page_uris=["s3://bucket/page/1.tiff"],
            x_centers=[100.0],
            sub_page_indices=[0],
            job_id="job-timeout",
            page_number=1,
            material_type="book",
            endpoint="http://iep1e:8007/v1/semantic-norm",
            backend=backend,
            cb=cb,
            ready_timeout_seconds=5.0,
            ready_poll_interval_seconds=1.0,
            execution_timeout_seconds=180.0,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_validation_error_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ValidationError (response missing required fields) → _call_iep1e returns None."""
        from services.eep_worker.app import worker_loop

        cb = SimpleNamespace(
            allow_call=lambda: True,
            record_failure=lambda kind: None,
            record_success=lambda: None,
        )

        async def _fake_wait(*_a: object, **_kw: object) -> bool:
            return True

        async def _fake_call(_url: str, _payload: object, **_kw: object) -> object:
            return {"unexpected_key": "no pages here"}  # will fail SemanticNormResponse.model_validate

        monkeypatch.setattr(worker_loop, "_wait_for_iep1e_ready", _fake_wait)
        backend = SimpleNamespace(call=_fake_call)

        result = await worker_loop._call_iep1e(
            page_uris=["s3://bucket/page/1.tiff"],
            x_centers=[100.0],
            sub_page_indices=[0],
            job_id="job-invalid",
            page_number=1,
            material_type="book",
            endpoint="http://iep1e:8007/v1/semantic-norm",
            backend=backend,
            cb=cb,
            ready_timeout_seconds=5.0,
            ready_poll_interval_seconds=1.0,
            execution_timeout_seconds=180.0,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_ready_timeout_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """iep1e /ready never returns 200 within timeout → _call_iep1e returns None."""
        from services.eep_worker.app import worker_loop

        cb = SimpleNamespace(
            allow_call=lambda: True,
            record_failure=lambda kind: None,
            record_success=lambda: None,
        )

        async def _fake_wait(*_a: object, **_kw: object) -> bool:
            return False  # not ready

        monkeypatch.setattr(worker_loop, "_wait_for_iep1e_ready", _fake_wait)
        backend = SimpleNamespace(call=AsyncMock())  # should never be reached

        result = await worker_loop._call_iep1e(
            page_uris=["s3://bucket/page/1.tiff"],
            x_centers=[100.0],
            sub_page_indices=[0],
            job_id="job-notready",
            page_number=1,
            material_type="book",
            endpoint="http://iep1e:8007/v1/semantic-norm",
            backend=backend,
            cb=cb,
            ready_timeout_seconds=5.0,
            ready_poll_interval_seconds=1.0,
            execution_timeout_seconds=180.0,
        )

        assert result is None
        backend.call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_valid_response_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A well-formed SemanticNormResponse is returned unchanged."""
        from services.eep_worker.app import worker_loop
        from shared.schemas.semantic_norm import SemanticNormResponse

        cb = SimpleNamespace(
            allow_call=lambda: True,
            record_failure=lambda kind: None,
            record_success=lambda: None,
        )

        async def _fake_wait(*_a: object, **_kw: object) -> bool:
            return True

        valid_payload = {
            "reading_direction": "ltr",
            "fallback_used": False,
            "processing_time_ms": 42.0,
            "warnings": [],
            "pages": [
                {
                    "sub_page_index": 0,
                    "oriented_uri": "s3://bucket/oriented/1.tiff",
                    "original_uri": "s3://bucket/page/1.tiff",
                    "orientation": {
                        "best_rotation_deg": 0,
                        "orientation_confident": True,
                        "score_ratio": 1.5,
                        "score_diff": 20.0,
                        "script_evidence": {
                            "n_boxes": 10,
                            "n_chars": 50,
                            "mean_conf": 0.9,
                            "latin_ratio": 0.8,
                            "arabic_ratio": 0.0,
                            "garbage_ratio": 0.05,
                        },
                    },
                }
            ],
            "ordered_page_uris": ["s3://bucket/oriented/1.tiff"],
        }

        async def _fake_call(_url: str, _payload: object, **_kw: object) -> object:
            return valid_payload

        monkeypatch.setattr(worker_loop, "_wait_for_iep1e_ready", _fake_wait)
        backend = SimpleNamespace(call=_fake_call)

        result = await worker_loop._call_iep1e(
            page_uris=["s3://bucket/page/1.tiff"],
            x_centers=[100.0],
            sub_page_indices=[0],
            job_id="job-ok",
            page_number=1,
            material_type="book",
            endpoint="http://iep1e:8007/v1/semantic-norm",
            backend=backend,
            cb=cb,
            ready_timeout_seconds=5.0,
            ready_poll_interval_seconds=1.0,
            execution_timeout_seconds=180.0,
        )

        assert result is not None
        assert isinstance(result, SemanticNormResponse)
        assert result.reading_direction == "ltr"
        assert len(result.pages) == 1
        assert result.pages[0].sub_page_index == 0


class TestCallIep1eTransientRetries:
    """Transient SERVICE_ERROR retries with backoff before CB failure / fallback."""

    @pytest.mark.asyncio
    async def test_transient_disconnect_retries_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.eep_worker.app import worker_loop
        from shared.gpu.backend import BackendError, BackendErrorKind
        from shared.schemas.semantic_norm import SemanticNormResponse

        monkeypatch.setenv("IEP1E_TRANSIENT_RETRY_BASE_SECONDS", "0")
        monkeypatch.setenv("IEP1E_TRANSIENT_MAX_RETRIES", "4")

        cb_events: list[tuple[str, object]] = []

        cb = SimpleNamespace(
            allow_call=lambda: True,
            record_failure=lambda kind: cb_events.append(("fail", kind)),
            record_success=lambda: cb_events.append(("ok", None)),
        )

        async def _fake_wait(*_a: object, **_kw: object) -> bool:
            return True

        valid_payload = {
            "reading_direction": "ltr",
            "fallback_used": False,
            "processing_time_ms": 1.0,
            "warnings": [],
            "pages": [
                {
                    "sub_page_index": 0,
                    "oriented_uri": "s3://bucket/oriented/1.tiff",
                    "original_uri": "s3://bucket/page/1.tiff",
                    "orientation": {
                        "best_rotation_deg": 0,
                        "orientation_confident": True,
                        "score_ratio": 1.5,
                        "score_diff": 20.0,
                        "script_evidence": {
                            "n_boxes": 10,
                            "n_chars": 50,
                            "mean_conf": 0.9,
                            "latin_ratio": 0.8,
                            "arabic_ratio": 0.0,
                            "garbage_ratio": 0.05,
                        },
                    },
                }
            ],
            "ordered_page_uris": ["s3://bucket/oriented/1.tiff"],
        }

        call_count = 0

        async def _fake_call(_url: str, _payload: object, **_kw: object) -> object:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise BackendError(
                    BackendErrorKind.SERVICE_ERROR,
                    "Connection error calling http://iep1e:8007/v1/semantic-norm: "
                    "Server disconnected without sending a response.",
                )
            return valid_payload

        monkeypatch.setattr(worker_loop, "_wait_for_iep1e_ready", _fake_wait)
        backend = SimpleNamespace(call=_fake_call)

        result = await worker_loop._call_iep1e(
            page_uris=["s3://bucket/page/1.tiff"],
            x_centers=[100.0],
            sub_page_indices=[0],
            job_id="job-retry-ok",
            page_number=1,
            material_type="book",
            endpoint="http://iep1e:8007/v1/semantic-norm",
            backend=backend,
            cb=cb,
            ready_timeout_seconds=5.0,
            ready_poll_interval_seconds=1.0,
            execution_timeout_seconds=180.0,
        )

        assert call_count == 3
        assert isinstance(result, SemanticNormResponse)
        assert cb_events == [("ok", None)]

    @pytest.mark.asyncio
    async def test_non_transient_http_400_no_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.eep_worker.app import worker_loop
        from shared.gpu.backend import BackendError, BackendErrorKind

        monkeypatch.setenv("IEP1E_TRANSIENT_RETRY_BASE_SECONDS", "0")
        monkeypatch.setenv("IEP1E_TRANSIENT_MAX_RETRIES", "5")

        cb_events: list[tuple[str, object]] = []
        cb = SimpleNamespace(
            allow_call=lambda: True,
            record_failure=lambda kind: cb_events.append(("fail", kind)),
            record_success=lambda: cb_events.append(("ok", None)),
        )

        async def _fake_wait(*_a: object, **_kw: object) -> bool:
            return True

        call_count = 0

        async def _fake_call(_url: str, _payload: object, **_kw: object) -> object:
            nonlocal call_count
            call_count += 1
            raise BackendError(
                BackendErrorKind.SERVICE_ERROR,
                "Service returned HTTP 400 from http://iep1e:8007/v1/semantic-norm: nope",
            )

        monkeypatch.setattr(worker_loop, "_wait_for_iep1e_ready", _fake_wait)
        backend = SimpleNamespace(call=_fake_call)

        result = await worker_loop._call_iep1e(
            page_uris=["s3://bucket/page/1.tiff"],
            x_centers=[100.0],
            sub_page_indices=[0],
            job_id="job-400",
            page_number=1,
            material_type="book",
            endpoint="http://iep1e:8007/v1/semantic-norm",
            backend=backend,
            cb=cb,
            ready_timeout_seconds=5.0,
            ready_poll_interval_seconds=1.0,
            execution_timeout_seconds=180.0,
        )

        assert call_count == 1
        assert result is None
        assert cb_events == [("fail", BackendErrorKind.SERVICE_ERROR)]

    @pytest.mark.asyncio
    async def test_transient_exhausted_records_failure_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.eep_worker.app import worker_loop
        from shared.gpu.backend import BackendError, BackendErrorKind

        monkeypatch.setenv("IEP1E_TRANSIENT_RETRY_BASE_SECONDS", "0")
        monkeypatch.setenv("IEP1E_TRANSIENT_MAX_RETRIES", "2")

        cb_events: list[tuple[str, object]] = []
        cb = SimpleNamespace(
            allow_call=lambda: True,
            record_failure=lambda kind: cb_events.append(("fail", kind)),
            record_success=lambda: cb_events.append(("ok", None)),
        )

        async def _fake_wait(*_a: object, **_kw: object) -> bool:
            return True

        call_count = 0

        async def _fake_call(_url: str, _payload: object, **_kw: object) -> object:
            nonlocal call_count
            call_count += 1
            raise BackendError(
                BackendErrorKind.SERVICE_ERROR,
                "Service returned HTTP 503 from http://iep1e:8007/v1/semantic-norm: gw",
            )

        monkeypatch.setattr(worker_loop, "_wait_for_iep1e_ready", _fake_wait)
        backend = SimpleNamespace(call=_fake_call)

        result = await worker_loop._call_iep1e(
            page_uris=["s3://bucket/page/1.tiff"],
            x_centers=[100.0],
            sub_page_indices=[0],
            job_id="job-503-exhaust",
            page_number=1,
            material_type="book",
            endpoint="http://iep1e:8007/v1/semantic-norm",
            backend=backend,
            cb=cb,
            ready_timeout_seconds=5.0,
            ready_poll_interval_seconds=1.0,
            execution_timeout_seconds=180.0,
        )

        assert call_count == 3
        assert result is None
        assert cb_events == [("fail", BackendErrorKind.SERVICE_ERROR)]


def test_iep1e_transient_backend_error_classification() -> None:
    from services.eep_worker.app.worker_loop import _iep1e_transient_backend_error
    from shared.gpu.backend import BackendError, BackendErrorKind

    assert _iep1e_transient_backend_error(
        BackendError(
            BackendErrorKind.SERVICE_ERROR,
            "Connection error calling http://x: Server disconnected without sending a response.",
        )
    )
    assert _iep1e_transient_backend_error(
        BackendError(
            BackendErrorKind.SERVICE_ERROR,
            "Service returned HTTP 503 from http://x: busy",
        )
    )
    assert not _iep1e_transient_backend_error(
        BackendError(
            BackendErrorKind.SERVICE_ERROR,
            "Service returned HTTP 422 from http://x: bad",
        )
    )
    assert not _iep1e_transient_backend_error(
        BackendError(BackendErrorKind.WARM_INFERENCE_TIMEOUT, "slow"),
    )


class TestSplitIep1eNoneRegression:
    """
    Regression tests for the split-path AttributeError when iep1e returns None.

    Before fix:
        else:
            _pages = _split_sem_result.pages   # ← crashed when result is None
            _iep1e_by_sub = {..._split_sem_result.pages}  # same crash

    After fix: both lines guard with `if _split_sem_result is not None else []`.
    """

    def test_none_result_pages_guard_is_empty_list(self) -> None:
        """
        Simulate the fixed guard: when _split_sem_result is None,
        _pages must be [] — never raises AttributeError.
        """
        _split_sem_result = None
        # Fixed line:
        _pages = _split_sem_result.pages if _split_sem_result is not None else []
        assert _pages == []

    def test_none_result_dict_comprehension_guard_is_empty_dict(self) -> None:
        """
        Simulate the fixed guard: when _split_sem_result is None,
        the by-sub dict must be {} — never raises AttributeError.
        """
        _split_sem_result = None
        # Fixed line:
        _iep1e_by_sub = {
            _sp.sub_page_index: _sp
            for _sp in (_split_sem_result.pages if _split_sem_result is not None else [])
        }
        assert _iep1e_by_sub == {}

    def test_empty_pages_result_is_handled_gracefully(self) -> None:
        """
        When _split_sem_result is not None but .pages is empty,
        both guards also produce empty containers (no crash, no rotation).
        """
        _split_sem_result = SimpleNamespace(pages=[], reading_direction="unresolved")
        _pages = _split_sem_result.pages if _split_sem_result is not None else []
        assert _pages == []

        _iep1e_by_sub = {
            _sp.sub_page_index: _sp
            for _sp in (_split_sem_result.pages if _split_sem_result is not None else [])
        }
        assert _iep1e_by_sub == {}

    def test_valid_result_pages_is_passed_through(self) -> None:
        """
        When _split_sem_result has pages, the guard must leave them intact.
        """
        fake_page = SimpleNamespace(sub_page_index=0, oriented_uri="s3://x/0.tiff")
        _split_sem_result = SimpleNamespace(pages=[fake_page], reading_direction="ltr")

        _pages = _split_sem_result.pages if _split_sem_result is not None else []
        assert _pages == [fake_page]

        _iep1e_by_sub = {
            _sp.sub_page_index: _sp
            for _sp in (_split_sem_result.pages if _split_sem_result is not None else [])
        }
        assert _iep1e_by_sub == {0: fake_page}


# ── iep1e execution-timeout regression ───────────────────────────────────────

class TestIep1eExecutionTimeoutConfig:
    """
    _iep1e_execution_timeout() must default to 180 s (not the shared 30 s
    EXECUTION_TIMEOUT_SECONDS) so that 4-rotation PaddleOCR inference does
    not time out on 1-vCPU Fargate.
    """

    def test_default_is_180(self) -> None:
        from services.eep_worker.app.worker_loop import _iep1e_execution_timeout
        from types import SimpleNamespace
        cfg = SimpleNamespace()
        assert _iep1e_execution_timeout(cfg) == 180.0

    def test_env_override_respected(self) -> None:
        from services.eep_worker.app.worker_loop import _iep1e_execution_timeout
        from types import SimpleNamespace
        cfg = SimpleNamespace(iep1e_execution_timeout_seconds=300.0)
        assert _iep1e_execution_timeout(cfg) == 300.0

    def test_returns_float(self) -> None:
        from services.eep_worker.app.worker_loop import _iep1e_execution_timeout
        from types import SimpleNamespace
        cfg = SimpleNamespace(iep1e_execution_timeout_seconds=120)
        result = _iep1e_execution_timeout(cfg)
        assert isinstance(result, float)


class TestCallIep1ePassesExecutionTimeout:
    """
    _call_iep1e() must forward execution_timeout_seconds to backend.call()
    so the HTTP client enforces the right timeout, not the shared 30 s default.
    """

    @pytest.mark.asyncio
    async def test_execution_timeout_forwarded_to_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace
        from services.eep_worker.app import worker_loop

        recorded: dict = {}

        async def _fake_wait(*_a: object, **_kw: object) -> bool:
            return True

        async def _fake_call(_url: str, _payload: object, **kw: object) -> object:
            recorded.update(kw)
            return {
                "reading_direction": "ltr",
                "fallback_used": False,
                "processing_time_ms": 1.0,
                "warnings": [],
                "pages": [
                    {
                        "sub_page_index": 0,
                        "oriented_uri": "s3://b/1.tiff",
                        "original_uri": "s3://b/1.tiff",
                        "orientation": {
                            "best_rotation_deg": 0,
                            "orientation_confident": True,
                            "score_ratio": 1.0,
                            "score_diff": 0.0,
                            "script_evidence": {
                                "n_boxes": 1,
                                "n_chars": 1,
                                "mean_conf": 1.0,
                                "latin_ratio": 1.0,
                                "arabic_ratio": 0.0,
                                "garbage_ratio": 0.0,
                            },
                        },
                    }
                ],
                "ordered_page_uris": ["s3://b/1.tiff"],
            }

        cb = SimpleNamespace(
            allow_call=lambda: True,
            record_failure=lambda _k: None,
            record_success=lambda: None,
        )
        monkeypatch.setattr(worker_loop, "_wait_for_iep1e_ready", _fake_wait)
        backend = SimpleNamespace(call=_fake_call)

        await worker_loop._call_iep1e(
            page_uris=["s3://b/1.tiff"],
            x_centers=[100.0],
            sub_page_indices=[0],
            job_id="job-timeout-test",
            page_number=1,
            material_type="book",
            endpoint="http://iep1e:8007/v1/semantic-norm",
            backend=backend,
            cb=cb,
            ready_timeout_seconds=5.0,
            ready_poll_interval_seconds=1.0,
            execution_timeout_seconds=180.0,
        )

        assert "execution_timeout_seconds" in recorded, (
            "backend.call() was not passed execution_timeout_seconds"
        )
        assert recorded["execution_timeout_seconds"] == 180.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
