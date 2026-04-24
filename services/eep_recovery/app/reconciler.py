"""
services/eep_recovery/app/reconciler.py
-----------------------------------------
Packet 4.7 — DB-authoritative queue reconciliation for the EEP recovery service.

Implements the reconciliation loop described in spec Sections 8.1, 8.4, and 9.13:

  "The recovery service scans QUEUE_PAGE_TASKS_PROCESSING and re-enqueues
   tasks whose DB page state is still 'queued' or 'preprocessing' without
   a live owner."

Reconciliation logic (reconcile_once):
  For each task in QUEUE_PAGE_TASKS_PROCESSING:

  Complete states (accepted, review, failed, pending_human_correction, split):
    → Remove from processing list (worker finished but crashed before ack).
      Counted as acked_terminal.

  'queued' state:
    → Worker crashed before the CAS transition to 'preprocessing'.
    → Requeue with retry_count + 1 (or dead-letter if retries exhausted).

  Active states (preprocessing, rectification, layout_detection, semantic_norm):
    → If stale (status_updated_at older than task_timeout_seconds):
        requeue or dead-letter.
    → If not stale: skip (live worker is likely still processing).

  Page not found in DB:
    → Log error and remove from processing list. Counted as not_found.

  Unparseable processing-list entry:
    → Log warning and remove. Counted as not_found.

  Dead-letter queue size is checked at the end of every scan.  A warning is
  logged when it exceeds dead_letter_warning_threshold.

DB is authoritative (spec Section 9.8).  The reconciler normally moves tasks
between Redis queues; when a task is exhausted or already dead-lettered, it
also marks the corresponding active DB page failed so jobs reach a terminal
state instead of running forever.

Caller responsibilities:
  - Provide a Redis client (decode_responses=True).
  - Provide a SQLAlchemy Session (or session factory for the loop).
  - Provide a session that can be committed/rolled back by reconciliation.

Exported:
    ReconcilerConfig       — configuration dataclass
    ReconciliationResult   — per-cycle result dataclass
    reconcile_once         — single reconciliation pass (synchronous)
    run_reconciliation_loop — async loop wrapper
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import cast
from uuid import uuid4

import redis as redis_lib
from sqlalchemy.orm import Session

from services.eep.app.db.lineage import update_lineage_completion
from services.eep.app.db.models import Job, JobPage, PageLineage
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.jobs.summary import derive_job_status, leaf_pages_from_pages
from services.eep.app.queue import CLAIMS_KEY, MAX_TASK_RETRIES
from shared.schemas.queue import (
    QUEUE_DEAD_LETTER,
    QUEUE_PAGE_TASKS,
    QUEUE_PAGE_TASKS_PROCESSING,
    PageTask,
)

__all__ = [
    "ReconcilerConfig",
    "ReconciliationResult",
    "reconcile_once",
    "run_reconciliation_loop",
]

logger = logging.getLogger(__name__)

# ── State classification ────────────────────────────────────────────────────────

# These states mean the task finished (worker stopped processing).
# The processing-list entry is stale and should be removed.
_COMPLETE_STATES: frozenset[str] = frozenset(
    {
        "accepted",
        "review",
        "failed",
        "pending_human_correction",
        "split",
    }
)

# These states mean active processing is (or was) in progress.
# Staleness detection applies.
_ACTIVE_STATES: frozenset[str] = frozenset(
    {"preprocessing", "rectification", "layout_detection", "semantic_norm"}
)


# ── Configuration ──────────────────────────────────────────────────────────────


@dataclasses.dataclass
class ReconcilerConfig:
    """
    Configuration for the recovery reconciler.

    Defaults match the libraryai-policy ConfigMap (spec Section 8.4), with
    a shorter timeout for layout tasks so a hung IEP2 step is recovered
    faster than a long-running preprocessing task:
        task_timeout_seconds:          900.0
        layout_task_timeout_seconds:   180.0
        check_interval_seconds:         30.0
        max_task_retries:                3
        dead_letter_warning_threshold: 100
    """

    task_timeout_seconds: float = 900.0
    layout_task_timeout_seconds: float = 180.0
    check_interval_seconds: float = 30.0
    max_task_retries: int = MAX_TASK_RETRIES
    dead_letter_warning_threshold: int = 100


# ── Result type ────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class ReconciliationResult:
    """
    Summary of one reconciliation pass.

    Attributes:
        processing_list_size:  Total items in QUEUE_PAGE_TASKS_PROCESSING at
                               scan time (including unparseable entries).
        acked_terminal:        Tasks removed because the page reached a complete
                               or terminal state.
        requeued_stale:        Stale active tasks re-enqueued with
                               retry_count + 1.
        requeued_orphaned:     DB-active pages that had no Redis task and were
                               re-enqueued with a fresh task_id.
        dead_lettered:         Stale active tasks moved to QUEUE_DEAD_LETTER
                               because retry_count >= max_task_retries.
        skipped_active:        Active tasks within timeout window — left alone.
        not_found:             Tasks with no matching page in the DB or with
                               unparseable JSON.
        dead_letter_queue_size: Current length of QUEUE_DEAD_LETTER after the
                               pass (for monitoring).
        duration_ms:           Wall-clock time for this pass in milliseconds.
    """

    processing_list_size: int
    acked_terminal: int
    requeued_stale: int
    requeued_orphaned: int
    dead_lettered: int
    skipped_active: int
    not_found: int
    dead_letter_queue_size: int
    duration_ms: float


# ── Internal helpers ───────────────────────────────────────────────────────────


def _scan_processing_list(
    r: redis_lib.Redis,
) -> list[tuple[str, PageTask | None]]:
    """
    Return (raw_json, parsed_task_or_None) for every item in
    QUEUE_PAGE_TASKS_PROCESSING.

    Unparseable entries are returned with None as the task — callers should
    remove them as poison pills.
    """
    raw_items: list[str] = cast(list[str], r.lrange(QUEUE_PAGE_TASKS_PROCESSING, 0, -1))
    result: list[tuple[str, PageTask | None]] = []
    for raw in raw_items:
        try:
            result.append((raw, PageTask.model_validate_json(raw)))
        except Exception:
            logger.warning("reconciler: unparseable item in processing list: %r", raw)
            result.append((raw, None))
    return result


def _ack_from_processing(
    r: redis_lib.Redis,
    raw_json: str,
    task_id: str,
) -> None:
    """
    Atomically remove a task from QUEUE_PAGE_TASKS_PROCESSING and CLAIMS_KEY.
    """
    pipe = r.pipeline(transaction=True)
    pipe.lrem(QUEUE_PAGE_TASKS_PROCESSING, 1, raw_json)
    pipe.hdel(CLAIMS_KEY, task_id)
    pipe.execute()


def _sync_job_summary(session: Session, job_id: str) -> None:
    job = session.get(Job, job_id)
    if job is None:
        return

    pages = session.query(JobPage).filter_by(job_id=job_id).all()
    leaf_pages = leaf_pages_from_pages(pages)
    now = datetime.now(timezone.utc)

    job.accepted_count = sum(1 for page in leaf_pages if page.status == "accepted")
    job.review_count = sum(1 for page in leaf_pages if page.status == "review")
    job.failed_count = sum(1 for page in leaf_pages if page.status == "failed")
    job.pending_human_correction_count = sum(
        1 for page in leaf_pages if page.status == "pending_human_correction"
    )
    job.status = derive_job_status(leaf_pages)
    if job.status in {"done", "failed"}:
        job.completed_at = job.completed_at or now
    else:
        job.completed_at = None


def _mark_page_failed_for_dead_letter(
    session: Session,
    page: JobPage,
    *,
    reason: str,
) -> bool:
    from_state = page.status
    try:
        advanced = advance_page_state(
            session,
            page.page_id,
            from_state=from_state,
            to_state="failed",
            acceptance_decision="failed",
            routing_path="recovery_dead_letter",
        )
    except ValueError:
        logger.exception(
            "reconciler: cannot fail page %s from invalid state %r before DLQ",
            page.page_id,
            from_state,
        )
        session.rollback()
        return False

    if not advanced:
        logger.warning(
            "reconciler: page %s changed state before DLQ failure transition from %s",
            page.page_id,
            from_state,
        )
        session.rollback()
        return False

    page.status = "failed"
    page.acceptance_decision = "failed"
    page.routing_path = "recovery_dead_letter"
    page.review_reasons = [reason]
    lineage = (
        session.query(PageLineage)
        .filter(
            PageLineage.job_id == page.job_id,
            PageLineage.page_number == page.page_number,
            PageLineage.sub_page_index == page.sub_page_index,
        )
        .first()
    )
    if lineage is None:
        logger.error(
            "reconciler: no lineage row while marking page %s failed for DLQ",
            page.page_id,
        )
    else:
        update_lineage_completion(
            session,
            lineage.lineage_id,
            acceptance_decision="failed",
            acceptance_reason=reason,
            routing_path="recovery_dead_letter",
            total_processing_ms=page.processing_time_ms,
            output_image_uri=page.output_image_uri,
        )
    _sync_job_summary(session, page.job_id)
    session.commit()
    logger.error(
        "reconciler: marked page %s failed before dead-lettering task (from=%s reason=%s)",
        page.page_id,
        from_state,
        reason,
    )
    return True


def _requeue_or_dead_letter(
    r: redis_lib.Redis,
    session: Session,
    raw_json: str,
    task: PageTask,
    page: JobPage,
    max_retries: int,
    *,
    reason: str,
) -> str:
    """
    Remove task from the processing list and either re-enqueue it with
    incremented retry_count or move it to the dead-letter queue.

    Returns "requeued", "dead_lettered", or "skipped".
    """
    if task.retry_count >= max_retries:
        if not _mark_page_failed_for_dead_letter(session, page, reason=reason):
            return "skipped"
        pipe = r.pipeline(transaction=True)
        pipe.lrem(QUEUE_PAGE_TASKS_PROCESSING, 1, raw_json)
        pipe.hdel(CLAIMS_KEY, task.task_id)
        pipe.lpush(QUEUE_DEAD_LETTER, raw_json)
        pipe.execute()
        logger.warning(
            "reconciler: dead-lettered task %s for page %s (retry_count=%d)",
            task.task_id,
            task.page_id,
            task.retry_count,
        )
        return "dead_lettered"

    retried = task.model_copy(update={"retry_count": task.retry_count + 1})
    pipe = r.pipeline(transaction=True)
    pipe.lrem(QUEUE_PAGE_TASKS_PROCESSING, 1, raw_json)
    pipe.hdel(CLAIMS_KEY, task.task_id)
    pipe.lpush(QUEUE_PAGE_TASKS, retried.model_dump_json())
    pipe.execute()
    logger.info(
        "reconciler: requeued task %s for page %s (retry %d/%d)",
        task.task_id,
        task.page_id,
        task.retry_count + 1,
        max_retries,
    )
    return "requeued"


def _is_stale(page: JobPage, task_timeout_seconds: float) -> bool:
    """
    Return True when the page's last status update (or creation time) is
    older than task_timeout_seconds.

    Uses status_updated_at as the reference when available; falls back to
    created_at.  Both are expected to be timezone-aware (UTC) per the DB
    schema.  Naive datetimes are treated as UTC.
    """
    now = datetime.now(timezone.utc)
    ref: datetime | None = page.status_updated_at or page.created_at
    if ref is None:
        # No timestamp at all — conservatively treat as stale.
        return True
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    age_seconds = (now - ref).total_seconds()
    return age_seconds > task_timeout_seconds


def _timeout_seconds_for_page(page: JobPage, cfg: ReconcilerConfig) -> float:
    """Return the staleness timeout appropriate for the page's current state."""
    if page.status == "layout_detection":
        return cfg.layout_task_timeout_seconds
    return cfg.task_timeout_seconds


def _collect_redis_page_ids(r: redis_lib.Redis) -> set[str]:
    """Return page_ids present in Redis queues relevant to recovery."""
    page_ids: set[str] = set()
    for queue_key in (QUEUE_PAGE_TASKS, QUEUE_PAGE_TASKS_PROCESSING):
        raw_items: list[str] = cast(list[str], r.lrange(queue_key, 0, -1))
        for raw in raw_items:
            try:
                page_ids.add(PageTask.model_validate_json(raw).page_id)
            except Exception:
                logger.warning(
                    "reconciler: unparseable entry in %s while collecting Redis page ids: %r",
                    queue_key,
                    raw,
                )
    return page_ids


def _finalize_dead_lettered_db_pages(
    r: redis_lib.Redis,
    session: Session,
) -> int:
    """
    Repair active DB pages that already have a task sitting in the DLQ.

    The DLQ entry stays in Redis for audit; the DB page becomes authoritative
    and terminal so jobs do not remain running forever.
    """
    dlq_size: int = r.llen(QUEUE_DEAD_LETTER)  # type: ignore[assignment]
    if dlq_size <= 0:
        return 0

    finalized = 0
    raw_items: list[str] = cast(list[str], r.lrange(QUEUE_DEAD_LETTER, 0, -1))
    for raw in raw_items:
        try:
            task = PageTask.model_validate_json(raw)
        except Exception:
            continue
        page = session.get(JobPage, task.page_id)
        if page is None or page.status in _COMPLETE_STATES:
            continue
        if page.status not in _ACTIVE_STATES and page.status != "queued":
            continue
        if _mark_page_failed_for_dead_letter(
            session,
            page,
            reason="dead_letter_queue_active_page_repaired",
        ):
            finalized += 1
    return finalized


def _recover_db_orphaned_pages(
    r: redis_lib.Redis,
    session: Session,
    existing_page_ids: set[str],
) -> int:
    """
    Re-enqueue DB-authoritative recoverable pages missing from all Redis queues.

    This covers the failure mode where a page remains active in the database
    but its task has vanished from both the main queue and the processing list.
    """
    recoverable_states = (
        "queued",
        "preprocessing",
        "rectification",
        "layout_detection",
        "semantic_norm",
    )
    pages = session.query(JobPage).filter(JobPage.status.in_(recoverable_states)).all()

    requeued = 0
    for page in pages:
        if page.page_id in existing_page_ids:
            continue
        task = PageTask(
            task_id=str(uuid4()),
            job_id=page.job_id,
            page_id=page.page_id,
            page_number=page.page_number,
            sub_page_index=page.sub_page_index,
            retry_count=0,
        )
        r.lpush(QUEUE_PAGE_TASKS, task.model_dump_json())
        existing_page_ids.add(page.page_id)
        requeued += 1
        logger.warning(
            "reconciler: re-enqueued orphaned DB page %s (status=%s, page_number=%s, sub_page_index=%s)",
            page.page_id,
            page.status,
            page.page_number,
            page.sub_page_index,
        )

    return requeued


# ── Main reconciliation pass ───────────────────────────────────────────────────


def reconcile_once(
    r: redis_lib.Redis,
    session: Session,
    config: ReconcilerConfig | None = None,
) -> ReconciliationResult:
    """
    Execute one full reconciliation pass over QUEUE_PAGE_TASKS_PROCESSING.

    The pass is DB-authoritative: every action is driven by the page's current
    status in job_pages.  Redis queue entries are moved for normal recovery;
    exhausted or already dead-lettered active pages are marked failed in DB so
    they do not remain non-terminal forever.

    Args:
        r:       Redis client (decode_responses=True).
        session: SQLAlchemy session used for all DB reads.  Caller owns
                 commit/rollback lifecycle.
        config:  ReconcilerConfig; defaults to ReconcilerConfig().

    Returns:
        ReconciliationResult with per-action counters and timing.
    """
    t0 = time.monotonic()
    cfg = config or ReconcilerConfig()

    items = _scan_processing_list(r)

    acked = 0
    requeued = 0
    requeued_orphaned = 0
    dead_lettered = 0
    skipped_active = 0
    not_found = 0

    for raw_json, task in items:
        # ── Unparseable poison pill ────────────────────────────────────────────
        if task is None:
            r.lrem(QUEUE_PAGE_TASKS_PROCESSING, 1, raw_json)
            not_found += 1
            continue

        # ── DB lookup ─────────────────────────────────────────────────────────
        page: JobPage | None = session.get(JobPage, task.page_id)

        if page is None:
            logger.error(
                "reconciler: page %s (task %s) not found in DB; removing from processing list",
                task.page_id,
                task.task_id,
            )
            _ack_from_processing(r, raw_json, task.task_id)
            not_found += 1
            continue

        # ── Complete / terminal states — effective ack ─────────────────────────
        if page.status in _COMPLETE_STATES:
            logger.info(
                "reconciler: acking completed page %s (status=%s, task=%s)",
                task.page_id,
                page.status,
                task.task_id,
            )
            _ack_from_processing(r, raw_json, task.task_id)
            acked += 1

        # ── queued — worker crashed before CAS transition ─────────────────────
        elif page.status == "queued":
            logger.warning(
                "reconciler: page %s stuck in 'queued' (task %s); requeuing",
                task.page_id,
                task.task_id,
            )
            action = _requeue_or_dead_letter(
                r,
                session,
                raw_json,
                task,
                page,
                cfg.max_task_retries,
                reason="queued_task_retry_exhausted",
            )
            if action == "requeued":
                requeued += 1
            elif action == "dead_lettered":
                dead_lettered += 1
            else:
                skipped_active += 1

        # ── Active states — stale detection ───────────────────────────────────
        elif page.status in _ACTIVE_STATES:
            timeout_seconds = _timeout_seconds_for_page(page, cfg)
            if _is_stale(page, timeout_seconds):
                logger.warning(
                    "reconciler: page %s is stale (status=%s, task=%s, timeout=%.0fs); requeuing",
                    task.page_id,
                    page.status,
                    task.task_id,
                    timeout_seconds,
                )
                action = _requeue_or_dead_letter(
                    r,
                    session,
                    raw_json,
                    task,
                    page,
                    cfg.max_task_retries,
                    reason=f"stale_{page.status}_task_retry_exhausted",
                )
                if action == "requeued":
                    requeued += 1
                elif action == "dead_lettered":
                    dead_lettered += 1
                else:
                    skipped_active += 1
            else:
                skipped_active += 1

        # ── Unknown state — guard ──────────────────────────────────────────────
        else:
            logger.error(
                "reconciler: unknown page status %r for page %s (task %s); acking",
                page.status,
                task.page_id,
                task.task_id,
            )
            _ack_from_processing(r, raw_json, task.task_id)
            acked += 1

    # ── Dead-letter queue size check ──────────────────────────────────────────
    repaired_dlq_pages = _finalize_dead_lettered_db_pages(r, session)
    if repaired_dlq_pages:
        dead_lettered += repaired_dlq_pages

    existing_page_ids = _collect_redis_page_ids(r)
    requeued_orphaned = _recover_db_orphaned_pages(r, session, existing_page_ids)

    dlq_size: int = r.llen(QUEUE_DEAD_LETTER)  # type: ignore[assignment]
    if dlq_size >= cfg.dead_letter_warning_threshold:
        logger.warning(
            "reconciler: dead-letter queue size %d exceeds threshold %d",
            dlq_size,
            cfg.dead_letter_warning_threshold,
        )

    return ReconciliationResult(
        processing_list_size=len(items),
        acked_terminal=acked,
        requeued_stale=requeued,
        requeued_orphaned=requeued_orphaned,
        dead_lettered=dead_lettered,
        skipped_active=skipped_active,
        not_found=not_found,
        dead_letter_queue_size=dlq_size,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


# ── Async loop wrapper ─────────────────────────────────────────────────────────


async def run_reconciliation_loop(
    r: redis_lib.Redis,
    session_factory: Callable[[], Session],
    config: ReconcilerConfig | None = None,
) -> None:
    """
    Async loop that calls reconcile_once() every check_interval_seconds.

    Intended to run as a background asyncio task.  The loop terminates cleanly
    when cancelled (asyncio.CancelledError propagates).

    Exceptions from reconcile_once() are caught and logged; the loop continues
    on the next interval to avoid a single bad cycle stopping recovery.

    Args:
        r:               Redis client (decode_responses=True).
        session_factory: Callable that returns a new SQLAlchemy Session.
                         A fresh session is created for each cycle and closed
                         in a try/finally block.
        config:          ReconcilerConfig; defaults to ReconcilerConfig().
    """
    cfg = config or ReconcilerConfig()
    logger.info(
        "reconciler: loop started (check_interval=%.0fs, task_timeout=%.0fs)",
        cfg.check_interval_seconds,
        cfg.task_timeout_seconds,
    )
    while True:
        await asyncio.sleep(cfg.check_interval_seconds)
        session = session_factory()
        try:
            result = reconcile_once(r, session, cfg)
            logger.info(
                "reconciler: pass complete — "
                "proc_list=%d acked=%d requeued=%d orphaned=%d dead_lettered=%d "
                "skipped=%d not_found=%d dlq=%d (%.1f ms)",
                result.processing_list_size,
                result.acked_terminal,
                result.requeued_stale,
                result.requeued_orphaned,
                result.dead_lettered,
                result.skipped_active,
                result.not_found,
                result.dead_letter_queue_size,
                result.duration_ms,
            )
        except Exception:
            logger.exception("reconciler: error during reconciliation cycle")
        finally:
            session.close()
