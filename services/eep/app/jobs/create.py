"""
services/eep/app/jobs/create.py
--------------------------------
POST /v1/jobs — create a processing job.

Responsibilities
----------------
1. Validate the request body (delegated to JobCreateRequest).
2. Insert a Job row and one JobPage row per submitted page into the DB.
   The DB is committed before any Redis write — DB is the source of truth.
3. Enqueue one PageTask per page to the main Redis queue.
4. Return a JobCreateResponse (HTTP 201).

Write ordering — DB before Redis
---------------------------------
The DB commit precedes all Redis writes intentionally.  If the process
crashes between the commit and the enqueue loop, the page rows exist in
the DB with status 'queued' and no owner.  The Phase 4 Packet 4.7 recovery
service detects these orphaned rows and re-enqueues them.  Workers check
the DB page state before performing side-effectful operations, so any
duplicate enqueue from recovery is safe (at-least-once semantics).

Error responses
---------------
    422 — request validation failure (Pydantic / FastAPI)
    503 — Redis unavailable; tasks could not be enqueued.
          If the DB commit already succeeded, the job rows exist with
          status 'queued' but some or all tasks are absent from Redis.
          A WARNING log is emitted with the partial-enqueue count.
          Packet 4.7 recovery re-enqueues any missing tasks.
    500 — unexpected DB error

Auth
----
Enforced in Phase 7 (Packet 7.1) — not yet active.
``created_by`` is set to ``None`` until JWT auth is available.
"""

from __future__ import annotations

import logging
import uuid

import redis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_user
from services.eep.app.db.models import Job, JobPage
from services.eep.app.db.session import get_session
from services.eep.app.queue import enqueue_page_task
from services.eep.app.redis_client import get_redis
from shared.schemas.eep import JobCreateRequest, JobCreateResponse
from shared.schemas.queue import PageTask

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/v1/jobs",
    response_model=JobCreateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["jobs"],
    summary="Create a processing job",
)
def create_job(
    body: JobCreateRequest,
    db: Session = Depends(get_session),
    r: redis.Redis = Depends(get_redis),
    user: CurrentUser = Depends(require_user),
) -> JobCreateResponse:
    """
    Create a new processing job.

    Inserts a Job row and one JobPage row per page, commits the DB, then
    enqueues one PageTask per page to the Redis main queue.

    **Auth:** enforced in Phase 7 (Packet 7.1) — not yet active.

    **Error responses**

    - ``422`` — validation error (bad request body)
    - ``503`` — Redis queue unavailable
    - ``500`` — unexpected database error
    """
    job_id = str(uuid.uuid4())

    # ── 1. Build Job row ───────────────────────────────────────────────────────
    job = Job(
        job_id=job_id,
        collection_id=body.collection_id,
        material_type=body.material_type,
        pipeline_mode=body.pipeline_mode,
        ptiff_qa_mode=body.ptiff_qa_mode,
        policy_version=body.policy_version,
        shadow_mode=body.shadow_mode,
        status="queued",
        page_count=len(body.pages),
        created_by=user.user_id,
    )
    db.add(job)

    # ── 2. Build one JobPage row per page; accumulate tasks ────────────────────
    tasks: list[PageTask] = []
    for page_input in body.pages:
        page_id = str(uuid.uuid4())
        db.add(
            JobPage(
                page_id=page_id,
                job_id=job_id,
                page_number=page_input.page_number,
                sub_page_index=None,
                status="queued",
                input_image_uri=page_input.input_uri,
            )
        )
        tasks.append(
            PageTask(
                task_id=str(uuid.uuid4()),
                job_id=job_id,
                page_id=page_id,
                page_number=page_input.page_number,
            )
        )

    # ── 3. Commit DB (authoritative) ───────────────────────────────────────────
    try:
        db.commit()
        db.refresh(job)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error; job could not be created.",
        ) from exc

    # ── 4. Enqueue tasks (Redis is execution mechanism only) ───────────────────
    # Track how many tasks were pushed before a failure so the warning log
    # can report the partial-enqueue count.  A failure here means the DB
    # commit already succeeded: the job rows are persisted but some or all
    # tasks are absent from Redis.  This is the expected crash-recovery
    # entry point for Packet 4.7 — workers check DB page state before acting,
    # so any duplicate enqueue from recovery is safe (at-least-once semantics).
    enqueued = 0
    try:
        for task in tasks:
            enqueue_page_task(r, task)
            enqueued += 1
    except redis.RedisError as exc:
        logger.warning(
            "create_job: DB committed for job %s but Redis enqueue failed "
            "after %d/%d tasks enqueued. Job rows are persisted with status "
            "'queued'; Packet 4.7 recovery will re-enqueue missing tasks.",
            job_id,
            enqueued,
            len(tasks),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Queue unavailable; page tasks could not be enqueued.",
        ) from exc

    return JobCreateResponse(
        job_id=job_id,
        status="queued",
        page_count=len(body.pages),
        created_at=job.created_at,
    )
