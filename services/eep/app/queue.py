"""
services/eep/app/queue.py
--------------------------
Reliable Redis queue contract for the LibraryAI page-processing pipeline.

Safety guarantees
-----------------
1. Claim is atomic: BLMOVE (Redis >= 6.2) with BRPOPLPUSH fallback.
   A task leaves the main queue and enters the processing list in one
   server-side operation — no window where the task exists in neither list.

2. Processing list is the crash-recovery mechanism.
   If a worker crashes after claiming a task, the task remains in
   QUEUE_PAGE_TASKS_PROCESSING.  The recovery service (Phase 4 Packet 4.7)
   scans that list and re-enqueues tasks whose DB page state is still
   'queued' or 'preprocessing' without a live owner.

3. At-least-once delivery (not exactly-once).
   Every failure path (retry, dead-letter, ack) removes the task from the
   processing list and takes the next action inside a single MULTI/EXEC
   pipeline.  MULTI/EXEC atomicity holds on the Redis server: if the pipeline
   does not execute (e.g., the connection drops before EXEC), the task
   remains in the processing list for recovery.  If the pipeline executes
   successfully but the worker crashes before receiving the response, the
   task has already moved in Redis; subsequent DB-driven recovery may
   re-enqueue it, resulting in a second processing attempt.  Workers must
   therefore check DB page state before performing side-effectful operations.

4. Dead-letter queue for exhausted tasks.
   Tasks whose retry_count is already at or above max_retries are moved to
   QUEUE_DEAD_LETTER instead of being re-enqueued.

5. Redis is NOT the source of truth.
   The DB (PostgreSQL, via job_pages) is authoritative.  The queue is the
   execution scheduling mechanism only.  Every reconciliation decision must
   be driven by DB state (see reconciliation hooks below).

6. Worker ownership tracking.
   CLAIMS_KEY (a Redis hash) maps task_id → "worker_id:claimed_at_iso".
   This is best-effort metadata for the recovery service; queue safety does
   not depend on it being present.

Queue directions
----------------
  enqueue (LPUSH)  → item added to the LEFT / HEAD of the main queue
  claim   (BLMOVE) → item popped from the RIGHT / TAIL (oldest → FIFO)
                      and pushed to the LEFT of the processing list

Exports
-------
  ClaimedTask             — dataclass returned by claim_task()
  CLAIMS_KEY              — Redis hash key used for ownership tracking
  MAX_TASK_RETRIES        — default maximum retries before dead-letter

  enqueue_page_task(r, task)              — push task to main queue
  claim_task(r, worker_id, timeout)       — atomic claim; returns ClaimedTask | None
  ack_task(r, claimed)                    — mark task successfully processed
  fail_task(r, claimed, max_retries)      — retry or dead-letter on failure
  move_to_dead_letter(r, claimed)         — explicit dead-letter

  get_processing_tasks(r)                 — reconciliation hook: list in-flight
  requeue_task(r, task)                   — reconciliation hook: re-enqueue
  rebuild_queue_from_db(r, get_fn)        — reconciliation hook: DB-driven queue rebuild
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast
from uuid import uuid4

import redis

from shared.schemas.queue import (
    QUEUE_DEAD_LETTER,
    QUEUE_PAGE_TASKS,
    QUEUE_PAGE_TASKS_PROCESSING,
    PageTask,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default maximum number of retries before a task is dead-lettered.
# A task with retry_count >= MAX_TASK_RETRIES is not re-enqueued.
MAX_TASK_RETRIES: int = 3

# Redis HASH: task_id → "worker_id:claimed_at_iso"
# Enables ownership inspection without scanning the full processing list.
# Best-effort — queue safety does not depend on this key being consistent.
CLAIMS_KEY: str = "libraryai:page_tasks:claims"


# ---------------------------------------------------------------------------
# ClaimedTask
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimedTask:
    """
    Returned by claim_task().

    ``raw_json`` is the exact string stored in QUEUE_PAGE_TASKS_PROCESSING.
    It must be passed back to ack_task() / fail_task() so that LREM can
    locate and remove the entry by value.

    Do not re-serialise ``task`` to obtain the LREM search key — use
    ``raw_json`` directly to guarantee an exact byte-for-byte match.
    """

    task: PageTask
    raw_json: str
    worker_id: str
    claimed_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _blmove_safe(r: redis.Redis, timeout: float) -> str | None:
    """
    Atomically pop the oldest task from the main queue and push it to the
    processing list.

    Tries BLMOVE (Redis >= 6.2) first.  Falls back to BRPOPLPUSH on older
    Redis servers or redis-py versions that do not expose blmove.

    Direction:
      BLMOVE  QUEUE_PAGE_TASKS → QUEUE_PAGE_TASKS_PROCESSING  RIGHT → LEFT
      BRPOPLPUSH pops from RIGHT and pushes to LEFT — identical semantics.

    Returns the raw JSON string of the moved task, or None on timeout.
    """
    try:
        result = r.blmove(
            QUEUE_PAGE_TASKS,
            QUEUE_PAGE_TASKS_PROCESSING,
            timeout,  # type: ignore[arg-type]  # stubs say int; command accepts float
            src="RIGHT",
            dest="LEFT",
        )
        return result  # type: ignore[return-value]
    except (redis.ResponseError, AttributeError, TypeError):
        # Older Redis / redis-py: fall back to BRPOPLPUSH.
        # BRPOPLPUSH(src, dst) pops RIGHT from src, pushes LEFT to dst.
        result = r.brpoplpush(
            QUEUE_PAGE_TASKS,
            QUEUE_PAGE_TASKS_PROCESSING,
            timeout,
        )
        return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def enqueue_page_task(r: redis.Redis, task: PageTask) -> None:
    """
    Push a page task to the main queue.

    Tasks are LPUSH-ed (added to the left/head).  claim_task pops from the
    right/tail, giving FIFO ordering: the oldest enqueued task is claimed
    first.

    Args:
        r:    Redis client (decode_responses=True).
        task: PageTask to enqueue.  Serialised as JSON.
    """
    r.lpush(QUEUE_PAGE_TASKS, task.model_dump_json())


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


def claim_task(
    r: redis.Redis,
    worker_id: str,
    timeout: float = 5.0,
) -> ClaimedTask | None:
    """
    Block until a task is available and claim it atomically.

    The task JSON is moved from QUEUE_PAGE_TASKS to
    QUEUE_PAGE_TASKS_PROCESSING in a single atomic server-side operation
    (BLMOVE or BRPOPLPUSH).  A worker crash after this point leaves the task
    in the processing list, where the recovery service will detect and
    re-enqueue it.

    Args:
        r:         Redis client (decode_responses=True).
        worker_id: Identifier of the claiming worker (stored in CLAIMS_KEY).
        timeout:   Seconds to block waiting for a task.  Use 0 to block
                   indefinitely (not recommended in production workers; use
                   a short timeout and loop to remain interruptible).

    Returns:
        ClaimedTask if a task was claimed, None if the timeout expired.
    """
    raw_json = _blmove_safe(r, timeout)
    if raw_json is None:
        return None

    # Parse the payload.  An unparseable value is a poison pill — move it to
    # dead-letter immediately to prevent an infinite retry loop.
    try:
        task = PageTask.model_validate_json(raw_json)
    except Exception:
        logger.exception(
            "claim_task: unparseable payload in queue; moving to dead-letter: %r",
            raw_json,
        )
        pipe = r.pipeline(transaction=True)
        pipe.lrem(QUEUE_PAGE_TASKS_PROCESSING, 1, raw_json)
        pipe.lpush(QUEUE_DEAD_LETTER, raw_json)
        pipe.execute()
        return None

    claimed_at = datetime.now(timezone.utc)

    # Record ownership (best-effort; not relied on for safety).
    try:
        r.hset(
            CLAIMS_KEY,
            task.task_id,
            f"{worker_id}:{claimed_at.isoformat()}",
        )
    except redis.RedisError:
        logger.warning(
            "claim_task: could not record ownership for task %s",
            task.task_id,
        )

    return ClaimedTask(
        task=task,
        raw_json=raw_json,
        worker_id=worker_id,
        claimed_at=claimed_at,
    )


# ---------------------------------------------------------------------------
# Acknowledge
# ---------------------------------------------------------------------------


def ack_task(r: redis.Redis, claimed: ClaimedTask) -> None:
    """
    Acknowledge successful processing of a task.

    Removes the task from the processing list and deletes the ownership
    entry.  Both operations execute inside a MULTI/EXEC pipeline.

    Args:
        r:       Redis client (decode_responses=True).
        claimed: ClaimedTask returned by claim_task().
    """
    pipe = r.pipeline(transaction=True)
    pipe.lrem(QUEUE_PAGE_TASKS_PROCESSING, 1, claimed.raw_json)
    pipe.hdel(CLAIMS_KEY, claimed.task.task_id)
    pipe.execute()


# ---------------------------------------------------------------------------
# Fail / retry / dead-letter
# ---------------------------------------------------------------------------


def fail_task(
    r: redis.Redis,
    claimed: ClaimedTask,
    max_retries: int = MAX_TASK_RETRIES,
) -> None:
    """
    Handle a failed task.

    Decision rule (from PageTask schema docstring):
      if task.retry_count >= max_retries → move to dead-letter queue
      else                               → increment retry_count and re-enqueue

    MULTI/EXEC executes the processing-list removal and the next action
    (re-enqueue or dead-letter) atomically on the Redis server.  If the
    pipeline does not execute, the task remains in the processing list for
    recovery.  If the pipeline executes but the worker crashes before
    receiving confirmation, the task has already moved; DB-driven recovery
    may re-enqueue it, producing a second processing attempt (at-least-once).

    Args:
        r:           Redis client (decode_responses=True).
        claimed:     ClaimedTask returned by claim_task().
        max_retries: Maximum retry count.  Defaults to MAX_TASK_RETRIES (3).
    """
    task = claimed.task

    if task.retry_count >= max_retries:
        move_to_dead_letter(r, claimed)
        return

    retried = task.model_copy(update={"retry_count": task.retry_count + 1})
    pipe = r.pipeline(transaction=True)
    pipe.lrem(QUEUE_PAGE_TASKS_PROCESSING, 1, claimed.raw_json)
    pipe.hdel(CLAIMS_KEY, task.task_id)
    pipe.lpush(QUEUE_PAGE_TASKS, retried.model_dump_json())
    pipe.execute()

    logger.info(
        "fail_task: task %s re-enqueued (retry %d/%d)",
        task.task_id,
        task.retry_count + 1,
        max_retries,
    )


def move_to_dead_letter(r: redis.Redis, claimed: ClaimedTask) -> None:
    """
    Move a task to the dead-letter queue.

    Removes the task from QUEUE_PAGE_TASKS_PROCESSING and appends it to
    QUEUE_DEAD_LETTER inside a MULTI/EXEC pipeline.

    The caller is responsible for updating the corresponding job_pages row
    in the DB (DB is authoritative; this only affects the Redis queues).

    Args:
        r:       Redis client (decode_responses=True).
        claimed: ClaimedTask returned by claim_task().
    """
    task = claimed.task
    pipe = r.pipeline(transaction=True)
    pipe.lrem(QUEUE_PAGE_TASKS_PROCESSING, 1, claimed.raw_json)
    pipe.hdel(CLAIMS_KEY, task.task_id)
    pipe.lpush(QUEUE_DEAD_LETTER, claimed.raw_json)
    pipe.execute()

    logger.warning(
        "move_to_dead_letter: task %s dead-lettered (retry_count=%d)",
        task.task_id,
        task.retry_count,
    )


# ---------------------------------------------------------------------------
# Reconciliation entry points (Packet 4.7)
# ---------------------------------------------------------------------------


def get_processing_tasks(r: redis.Redis) -> list[PageTask]:
    """
    Return all tasks currently in the processing list.

    Used by the recovery service to detect abandoned or stuck tasks.
    Unparseable entries are logged and skipped.

    DB is authoritative: the recovery service must cross-reference these
    results against job_pages state before taking any requeue action.

    Args:
        r: Redis client (decode_responses=True).

    Returns:
        List of PageTask objects currently in QUEUE_PAGE_TASKS_PROCESSING.
    """
    raw_items: list[str] = cast(list[str], r.lrange(QUEUE_PAGE_TASKS_PROCESSING, 0, -1))
    tasks: list[PageTask] = []
    for raw in raw_items:
        try:
            tasks.append(PageTask.model_validate_json(raw))
        except Exception:
            logger.warning(
                "get_processing_tasks: unparseable item in processing list: %r",
                raw,
            )
    return tasks


def requeue_task(r: redis.Redis, task: PageTask) -> None:
    """
    Low-level recovery hook: push a task back onto the main queue.

    MUST ONLY be called after DB-authoritative reconciliation has confirmed
    both of the following:
      1. The page is in a recoverable state (DB page status is 'queued' or
         'preprocessing' with no live worker holding the task).
      2. The task is not already present in the main queue or processing list.

    This function performs no duplicate-check.  Calling it without prior DB
    reconciliation may place the same task in the queue twice, causing two
    workers to process it concurrently (at-least-once, not exactly-once).

    Args:
        r:    Redis client (decode_responses=True).
        task: PageTask to re-enqueue (typically with an updated retry_count).
    """
    r.lpush(QUEUE_PAGE_TASKS, task.model_dump_json())


def rebuild_queue_from_db(
    r: redis.Redis,
    get_queued_pages_fn: object,
) -> int:
    """
    Rebuild the main queue from DB state after a Redis restart.

    Designed to be called at worker / recovery-service startup when the main
    queue may be empty because Redis lost its data (AOF replay failed or Redis
    was freshly started).  It is safe to call at any time: pages already
    present in either queue are skipped.

    Algorithm
    ---------
    1. Scan QUEUE_PAGE_TASKS and QUEUE_PAGE_TASKS_PROCESSING to collect the
       set of page_ids already present in Redis.
    2. Call get_queued_pages_fn() to obtain all job_pages rows whose DB
       status is 'queued'.  Each row must expose:
         .page_id        (str)
         .job_id         (str)
         .page_number    (int, >= 1)
         .sub_page_index (int | None)
    3. For each such page whose page_id is absent from both Redis queues,
       enqueue a new PageTask with retry_count=0 and a fresh task_id.

    Args:
        r:
            Redis client (decode_responses=True).
        get_queued_pages_fn:
            Callable[[], Iterable[row]] — DB-authoritative query returning
            only pages with status='queued'.  Must not mutate DB state.

    Returns:
        Number of pages re-enqueued.
    """
    # ── Step 1: collect page_ids already present in Redis ────────────────────
    existing_page_ids: set[str] = set()
    for queue_key in (QUEUE_PAGE_TASKS, QUEUE_PAGE_TASKS_PROCESSING):
        raw_items: list[str] = cast(list[str], r.lrange(queue_key, 0, -1))
        for raw in raw_items:
            try:
                existing_page_ids.add(PageTask.model_validate_json(raw).page_id)
            except Exception:
                logger.warning(
                    "rebuild_queue_from_db: unparseable entry in %s — skipped",
                    queue_key,
                )

    # ── Step 2: re-enqueue any orphaned queued pages ─────────────────────────
    pages = get_queued_pages_fn()  # type: ignore[operator]
    enqueued = 0
    for page in pages:
        if page.page_id in existing_page_ids:
            continue
        task = PageTask(
            task_id=str(uuid4()),
            job_id=page.job_id,
            page_id=page.page_id,
            page_number=page.page_number,
            sub_page_index=getattr(page, "sub_page_index", None),
            retry_count=0,
        )
        requeue_task(r, task)
        existing_page_ids.add(page.page_id)  # prevent double-enqueue within this call
        enqueued += 1

    logger.info(
        "rebuild_queue_from_db: re-enqueued %d orphaned page(s) from DB " "(already_in_redis=%d)",
        enqueued,
        len(existing_page_ids) - enqueued,
    )
    return enqueued
