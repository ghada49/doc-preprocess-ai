"""
tests/test_p4_watchdog.py
---------------------------
Packet 4.7 — watchdog and recovery reconciler tests.

Covers:

  TestTaskWatchdog (8 tests):
    1.  register adds task to active tracking
    2.  deregister removes task from active tracking
    3.  deregister on unknown task_id is a no-op
    4.  check_stale returns empty when no tasks registered
    5.  check_stale returns empty when tasks are within timeout
    6.  check_stale returns stale task_ids when threshold exceeded
    7.  active_count property reflects registered tasks
    8.  run_watch_loop invokes on_stale callback for stale tasks

  TestReconcileOnce (12 tests):
    1.  Terminal page (accepted) → acked, removed from processing list
    2.  split page → acked (routing-terminal)
    3.  pending_human_correction page → acked
    4.  queued page → requeued with retry_count+1
    5.  queued page at max_retries → dead-lettered
    6.  preprocessing page within timeout → skipped_active
    7.  preprocessing page stale → requeued
    8.  stale preprocessing page at max_retries → dead_lettered
    9.  Page not found in DB → not_found + removed from processing list
    10. Unparseable processing-list entry → not_found + removed
    11. DLQ warning logged when size >= threshold
    12. duration_ms is non-negative
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from services.eep.app.db.models import JobPage
from services.eep_recovery.app.reconciler import (
    ReconcilerConfig,
    ReconciliationResult,
    reconcile_once,
)
from services.eep_worker.app.watchdog import StaleTaskReport, TaskWatchdog, WatchdogConfig
from shared.schemas.queue import (
    QUEUE_DEAD_LETTER,
    QUEUE_PAGE_TASKS,
    QUEUE_PAGE_TASKS_PROCESSING,
    PageTask,
)

# ── Constants ───────────────────────────────────────────────────────────────────

_TASK_ID = "task-aaaa-bbbb"
_PAGE_ID = "page-1111"
_JOB_ID = "job-2222"

_TASK = PageTask(
    task_id=_TASK_ID,
    job_id=_JOB_ID,
    page_id=_PAGE_ID,
    page_number=1,
    sub_page_index=None,
    retry_count=0,
)
_TASK_JSON = _TASK.model_dump_json()


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _make_page(
    status: str = "preprocessing",
    status_updated_at: datetime | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Build a mock JobPage with the given status and timestamps."""
    page = MagicMock(spec=JobPage)
    page.status = status
    # status_updated_at: set to "recent" (non-stale) by default
    page.status_updated_at = (
        status_updated_at if status_updated_at is not None else datetime.now(tz=timezone.utc)
    )
    page.created_at = created_at or datetime.now(tz=timezone.utc)
    return page


def _make_redis(
    processing_items: list[str] | None = None,
    dlq_size: int = 0,
) -> MagicMock:
    """Build a mock Redis client with sensible defaults."""
    r = MagicMock()
    r.lrange.return_value = processing_items or []
    r.llen.return_value = dlq_size

    # Pipeline mock that records calls and has .execute()
    pipe = MagicMock()
    r.pipeline.return_value = pipe
    pipe.execute.return_value = None
    return r


def _run_reconcile(
    processing_items: list[str] | None = None,
    page: MagicMock | None = None,
    dlq_size: int = 0,
    config: ReconcilerConfig | None = None,
) -> ReconciliationResult:
    """Run reconcile_once with mocked Redis and session."""
    r = _make_redis(processing_items=processing_items or [], dlq_size=dlq_size)
    session = MagicMock()
    session.get.return_value = page
    return reconcile_once(r, session, config)


# ── TestTaskWatchdog ────────────────────────────────────────────────────────────


class TestTaskWatchdog:
    def test_register_adds_to_active(self) -> None:
        wdog = TaskWatchdog()
        wdog.register("t1")
        assert wdog.active_count == 1

    def test_deregister_removes_from_active(self) -> None:
        wdog = TaskWatchdog()
        wdog.register("t1")
        wdog.deregister("t1")
        assert wdog.active_count == 0

    def test_deregister_unknown_is_noop(self) -> None:
        wdog = TaskWatchdog()
        wdog.deregister("nonexistent")  # must not raise
        assert wdog.active_count == 0

    def test_check_stale_empty_when_no_tasks(self) -> None:
        wdog = TaskWatchdog()
        report = wdog.check_stale()
        assert report.stale_task_ids == []
        assert report.checked_count == 0

    def test_check_stale_empty_within_timeout(self) -> None:
        wdog = TaskWatchdog(WatchdogConfig(task_timeout_seconds=900.0))
        wdog.register("t1")
        report = wdog.check_stale()
        assert report.stale_task_ids == []
        assert report.checked_count == 1

    def test_check_stale_returns_expired_tasks(self) -> None:
        wdog = TaskWatchdog(WatchdogConfig(task_timeout_seconds=0.0))
        wdog.register("t1")
        wdog.register("t2")
        # With threshold=0, any age > 0 is stale; ensure at least one tick
        time.sleep(0.01)
        report = wdog.check_stale()
        assert set(report.stale_task_ids) == {"t1", "t2"}
        assert report.checked_count == 2

    def test_active_count_property(self) -> None:
        wdog = TaskWatchdog()
        assert wdog.active_count == 0
        wdog.register("t1")
        wdog.register("t2")
        assert wdog.active_count == 2
        wdog.deregister("t1")
        assert wdog.active_count == 1

    @pytest.mark.asyncio
    async def test_run_watch_loop_calls_on_stale(self) -> None:
        """Loop invokes on_stale when stale tasks are present."""
        wdog = TaskWatchdog(WatchdogConfig(task_timeout_seconds=0.0, check_interval_seconds=0.0))
        wdog.register("t1")
        time.sleep(0.01)  # ensure it's stale

        reports: list[StaleTaskReport] = []
        call_count = 0

        async def _fake_sleep(_: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            try:
                await wdog.run_watch_loop(on_stale=reports.append)
            except asyncio.CancelledError:
                pass

        assert len(reports) == 1
        assert "t1" in reports[0].stale_task_ids


# ── TestReconcileOnce ───────────────────────────────────────────────────────────


class TestReconcileOnce:
    def test_terminal_page_accepted_is_acked(self) -> None:
        """accepted page → acked_terminal incremented, removed from processing."""
        page = _make_page(status="accepted")
        result = _run_reconcile(
            processing_items=[_TASK_JSON],
            page=page,
        )
        assert result.acked_terminal == 1
        assert result.requeued_stale == 0
        assert result.processing_list_size == 1

    def test_split_page_is_acked(self) -> None:
        """split → treated as complete (routing-terminal), acked."""
        page = _make_page(status="split")
        result = _run_reconcile(
            processing_items=[_TASK_JSON],
            page=page,
        )
        assert result.acked_terminal == 1

    def test_pending_human_correction_is_acked(self) -> None:
        """pending_human_correction → acked (worker-terminal)."""
        page = _make_page(status="pending_human_correction")
        result = _run_reconcile(
            processing_items=[_TASK_JSON],
            page=page,
        )
        assert result.acked_terminal == 1

    def test_queued_page_is_requeued(self) -> None:
        """queued page → requeued with retry_count+1."""
        page = _make_page(status="queued")
        r = _make_redis(processing_items=[_TASK_JSON])
        session = MagicMock()
        session.get.return_value = page

        result = reconcile_once(r, session, ReconcilerConfig(max_task_retries=3))

        assert result.requeued_stale == 1
        assert result.dead_lettered == 0
        # Verify LPUSH to main queue was called
        pipe = r.pipeline.return_value
        pipe.lpush.assert_called_once()
        assert pipe.lpush.call_args[0][0] == QUEUE_PAGE_TASKS

    def test_queued_page_at_max_retries_is_dead_lettered(self) -> None:
        """queued page with retry_count >= max_task_retries → dead-lettered."""
        maxed_task = PageTask(
            task_id=_TASK_ID,
            job_id=_JOB_ID,
            page_id=_PAGE_ID,
            page_number=1,
            retry_count=3,
        )
        page = _make_page(status="queued")
        r = _make_redis(processing_items=[maxed_task.model_dump_json()])
        session = MagicMock()
        session.get.return_value = page

        result = reconcile_once(r, session, ReconcilerConfig(max_task_retries=3))

        assert result.dead_lettered == 1
        assert result.requeued_stale == 0
        pipe = r.pipeline.return_value
        pipe.lpush.assert_called_once()
        assert pipe.lpush.call_args[0][0] == QUEUE_DEAD_LETTER

    def test_preprocessing_within_timeout_is_skipped(self) -> None:
        """preprocessing page updated recently → skipped_active."""
        page = _make_page(
            status="preprocessing",
            status_updated_at=datetime.now(tz=timezone.utc),
        )
        result = _run_reconcile(
            processing_items=[_TASK_JSON],
            page=page,
            config=ReconcilerConfig(task_timeout_seconds=900.0),
        )
        assert result.skipped_active == 1
        assert result.requeued_stale == 0

    def test_stale_preprocessing_page_is_requeued(self) -> None:
        """preprocessing page with old status_updated_at → requeued."""
        old_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=1000)
        page = _make_page(status="preprocessing", status_updated_at=old_ts)
        r = _make_redis(processing_items=[_TASK_JSON])
        session = MagicMock()
        session.get.return_value = page

        result = reconcile_once(r, session, ReconcilerConfig(task_timeout_seconds=900.0))

        assert result.requeued_stale == 1
        assert result.skipped_active == 0

    def test_stale_page_at_max_retries_is_dead_lettered(self) -> None:
        """Stale page with retry_count >= max_task_retries → dead-lettered."""
        old_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=1000)
        maxed_task = PageTask(
            task_id=_TASK_ID,
            job_id=_JOB_ID,
            page_id=_PAGE_ID,
            page_number=1,
            retry_count=3,
        )
        page = _make_page(status="preprocessing", status_updated_at=old_ts)
        r = _make_redis(processing_items=[maxed_task.model_dump_json()])
        session = MagicMock()
        session.get.return_value = page

        result = reconcile_once(r, session, ReconcilerConfig(task_timeout_seconds=900.0))

        assert result.dead_lettered == 1
        assert result.requeued_stale == 0

    def test_page_not_found_in_db_counted_as_not_found(self) -> None:
        """Page missing from DB → not_found incremented."""
        r = _make_redis(processing_items=[_TASK_JSON])
        session = MagicMock()
        session.get.return_value = None  # page not in DB

        result = reconcile_once(r, session)

        assert result.not_found == 1
        assert result.acked_terminal == 0

    def test_unparseable_processing_entry_counted_as_not_found(self) -> None:
        """Unparseable JSON in processing list → not_found incremented, removed."""
        bad_json = "not-valid-json"
        r = _make_redis(processing_items=[bad_json])
        session = MagicMock()

        result = reconcile_once(r, session)

        assert result.not_found == 1
        r.lrem.assert_called_once_with(QUEUE_PAGE_TASKS_PROCESSING, 1, bad_json)

    def test_dlq_warning_logged_when_above_threshold(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DLQ >= dead_letter_warning_threshold → warning logged."""
        import logging

        page = _make_page(status="accepted")
        with caplog.at_level(logging.WARNING, logger="services.eep_recovery.app.reconciler"):
            _run_reconcile(
                processing_items=[_TASK_JSON],
                page=page,
                dlq_size=150,
                config=ReconcilerConfig(dead_letter_warning_threshold=100),
            )

        assert any("dead-letter queue size" in m for m in caplog.messages)

    def test_duration_ms_is_non_negative(self) -> None:
        """duration_ms is always >= 0."""
        result = _run_reconcile(processing_items=[])
        assert result.duration_ms >= 0.0
