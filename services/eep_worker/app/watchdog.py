"""
services/eep_worker/app/watchdog.py
-------------------------------------
Packet 4.7 — In-process task watchdog for the EEP worker.

Tracks the start time of every page task claimed by this worker process and
periodically reports tasks that have exceeded task_timeout_seconds (default
900 s, spec Section 8.4).

Design constraints (spec Sections 8.1, 8.4, 9.13):
  - The watchdog does NOT cancel tasks directly.  It reports stale task_ids
    and calls the caller-supplied on_stale callback.  Actual cancellation and
    cleanup are the task runner's responsibility.
  - State is in-process only (dict).  It is not shared across worker processes.
  - The watchdog loop runs as an asyncio background task and terminates when
    cancelled (asyncio.CancelledError propagates).

Usage::

    watchdog = TaskWatchdog()
    bg_task = asyncio.create_task(watchdog.run_watch_loop(on_stale=_handle_stale))

    # In the task runner:
    watchdog.register(task.task_id)
    try:
        await process_page(task)
    finally:
        watchdog.deregister(task.task_id)

Exported:
    WatchdogConfig  — configuration dataclass
    StaleTaskReport — result of check_stale()
    TaskWatchdog    — main watchdog class
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

__all__ = [
    "WatchdogConfig",
    "StaleTaskReport",
    "TaskWatchdog",
]

logger = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────────


@dataclasses.dataclass
class WatchdogConfig:
    """
    Configuration for TaskWatchdog.

    Defaults match the libraryai-policy ConfigMap (spec Section 8.4):
        task_timeout_seconds:     900.0
        check_interval_seconds:    30.0
    """

    task_timeout_seconds: float = 900.0
    check_interval_seconds: float = 30.0


# ── Result type ────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class StaleTaskReport:
    """
    Result of a TaskWatchdog.check_stale() call.

    Attributes:
        stale_task_ids: Task IDs that have been running longer than
                        task_timeout_seconds.  Empty list if none.
        checked_count:  Total number of active tasks checked.
        checked_at:     UTC timestamp when the check was performed.
    """

    stale_task_ids: list[str]
    checked_count: int
    checked_at: datetime


# ── Watchdog ───────────────────────────────────────────────────────────────────


class TaskWatchdog:
    """
    In-process watchdog that detects timed-out page tasks.

    One instance should be created per worker process at startup.  Tasks are
    registered when claimed and deregistered when they complete (success or
    failure, via ``try/finally`` in the task runner).

    The background loop (``run_watch_loop``) checks for stale tasks every
    ``check_interval_seconds`` and invokes the ``on_stale`` callback when any
    are found.  The loop terminates cleanly when cancelled.

    Thread/task safety: all mutation happens on the single asyncio event loop
    thread, so no locking is needed.
    """

    def __init__(self, config: WatchdogConfig | None = None) -> None:
        self._config = config or WatchdogConfig()
        # task_id → monotonic start time
        self._active: dict[str, float] = {}

    # ── Active-task management ─────────────────────────────────────────────────

    def register(self, task_id: str) -> None:
        """
        Mark *task_id* as actively processing.

        Should be called immediately after a task is claimed from the queue,
        before any side-effectful work begins.

        Args:
            task_id: UUID4 task identifier from PageTask.task_id.
        """
        self._active[task_id] = time.monotonic()

    def deregister(self, task_id: str) -> None:
        """
        Remove *task_id* from active tracking.

        Should be called in ``try/finally`` after a task completes (success,
        failure, or cancellation).

        Args:
            task_id: UUID4 task identifier previously passed to register().
        """
        self._active.pop(task_id, None)

    @property
    def active_count(self) -> int:
        """Number of tasks currently registered as active."""
        return len(self._active)

    # ── Staleness detection ────────────────────────────────────────────────────

    def check_stale(self) -> StaleTaskReport:
        """
        Scan active tasks and return any that have exceeded task_timeout_seconds.

        This is a pure, non-mutating inspection — it does not remove tasks from
        the active registry.  Callers decide what to do with stale task_ids.

        Returns:
            StaleTaskReport with stale_task_ids, checked_count, and checked_at.
        """
        now = time.monotonic()
        threshold = self._config.task_timeout_seconds
        stale = [tid for tid, started_at in self._active.items() if (now - started_at) > threshold]
        report = StaleTaskReport(
            stale_task_ids=stale,
            checked_count=len(self._active),
            checked_at=datetime.now(timezone.utc),
        )
        if stale:
            logger.warning(
                "watchdog: %d stale task(s) detected (timeout=%ss): %s",
                len(stale),
                self._config.task_timeout_seconds,
                stale,
            )
        return report

    # ── Background loop ────────────────────────────────────────────────────────

    async def run_watch_loop(
        self,
        on_stale: Callable[[StaleTaskReport], None] | None = None,
    ) -> None:
        """
        Async loop that calls check_stale() every check_interval_seconds.

        Intended to run as a background asyncio task:
            asyncio.create_task(watchdog.run_watch_loop(on_stale=handler))

        The loop runs until cancelled (asyncio.CancelledError propagates).

        Args:
            on_stale: Optional callback invoked whenever check_stale() returns
                      a non-empty stale_task_ids list.  Receives the full
                      StaleTaskReport.  Must not raise (exceptions are caught
                      and logged).
        """
        logger.info(
            "watchdog: loop started (check_interval=%.0fs, task_timeout=%.0fs)",
            self._config.check_interval_seconds,
            self._config.task_timeout_seconds,
        )
        while True:
            await asyncio.sleep(self._config.check_interval_seconds)
            report = self.check_stale()
            if report.stale_task_ids and on_stale is not None:
                try:
                    on_stale(report)
                except Exception:
                    logger.exception("watchdog: on_stale callback raised an exception")
