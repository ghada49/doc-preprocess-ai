"""
shared.schemas.queue
--------------------
Queue task payload schema and Redis key constants for the LibraryAI pipeline.

Redis key constants (spec Section 8.1, 8.4):
    QUEUE_PAGE_TASKS              — main page processing queue  (LIST)
    QUEUE_PAGE_TASKS_PROCESSING   — in-flight list; BLMOVE destination (LIST)
    QUEUE_SHADOW_TASKS            — shadow evaluation queue     (LIST)
    QUEUE_SHADOW_TASKS_PROCESSING — shadow in-flight list       (LIST)
    QUEUE_DEAD_LETTER             — dead-letter queue for exhausted retries (LIST)
    WORKER_SLOTS_KEY              — worker concurrency semaphore (STRING / counter)

Exported models:
    PageTask   — Pydantic model serialized as JSON into QUEUE_PAGE_TASKS.
                 Workers use page_id to load all processing state from the DB.
    ShadowTask — Pydantic model serialized as JSON into QUEUE_SHADOW_TASKS.
                 Shadow worker uses page_id to load lineage and write
                 shadow_evaluations.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

# ── Redis key constants ─────────────────────────────────────────────────────
# All keys share the "libraryai:" namespace (spec Section 8.1).

QUEUE_PAGE_TASKS: str = "libraryai:page_tasks"
QUEUE_PAGE_TASKS_PROCESSING: str = "libraryai:page_tasks:processing"

QUEUE_SHADOW_TASKS: str = "libraryai:shadow_tasks"
QUEUE_SHADOW_TASKS_PROCESSING: str = "libraryai:shadow_tasks:processing"

# Tasks moved here after retry_count reaches max_task_retries (default 3).
QUEUE_DEAD_LETTER: str = "libraryai:page_tasks:dead_letter"

# Redis STRING counter: initialised to max_concurrent_pages (default 20).
# Workers DECR before processing and INCR in try/finally on completion.
WORKER_SLOTS_KEY: str = "libraryai:worker_slots"


# ── PageTask ────────────────────────────────────────────────────────────────


class PageTask(BaseModel):
    """
    Payload pushed to QUEUE_PAGE_TASKS (serialised as JSON).

    Intentionally minimal — workers call ``page_id`` to look up all
    processing state from ``job_pages`` and ``page_lineage``.

    Fields:
        task_id        — UUID4 assigned at enqueue time; enables idempotency
                         checks in the worker and recovery service.
        job_id         — parent job identifier (jobs.job_id).
        page_id        — primary key of the job_pages record.
        page_number    — 1-indexed; denormalised for log / monitoring context.
        sub_page_index — 0 (left) or 1 (right) for split children;
                         None for original (unsplit) pages.
        retry_count    — 0 on the first attempt; incremented each time the
                         task is nack-ed and requeued.  Tasks with
                         retry_count >= max_task_retries are routed to
                         QUEUE_DEAD_LETTER instead of re-enqueued.
    """

    task_id: str
    job_id: str
    page_id: str
    page_number: Annotated[int, Field(ge=1)]
    sub_page_index: int | None = None
    retry_count: Annotated[int, Field(ge=0)] = 0


# ── ShadowTask ───────────────────────────────────────────────────────────────


class ShadowTask(BaseModel):
    """
    Payload pushed to QUEUE_SHADOW_TASKS (serialised as JSON).

    Enqueued by the eep-worker after a page from a shadow_mode=True job
    reaches a terminal state (accepted, review, failed).  The shadow worker
    uses page_id to load page_lineage data and write a shadow_evaluations row.

    Fields:
        task_id     — UUID4 assigned at enqueue time. This also serves as the
                      shadow_evaluations.eval_id reserved by the live worker.
        job_id      — parent job identifier (jobs.job_id).
        page_id     — primary key of the job_pages record.
        page_number — 1-indexed; denormalised for logging.
        page_status — terminal status of the page at enqueue time
                      (accepted | review | failed).
        retry_count — 0 on first attempt; incremented on re-enqueue.
    """

    task_id: str
    job_id: str
    page_id: str
    page_number: Annotated[int, Field(ge=1)]
    page_status: str
    retry_count: Annotated[int, Field(ge=0)] = 0
