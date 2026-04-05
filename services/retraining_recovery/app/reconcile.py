"""
services/retraining_recovery/app/reconcile.py
-----------------------------------------------
Packet 8.5 — Retraining recovery reconciliation.

Implements a single-pass reconciliation function and an async polling loop
that detects and recovers abandoned retraining work.

Recovery logic per pass:

  1. Stuck jobs: find retraining_jobs with status='running' and
     started_at older than job_timeout_minutes → mark status='failed'.

  2. Orphaned triggers: find retraining_triggers with status='processing'
     whose linked retraining_job is either missing or failed → mark
     status='failed'.

The reconciler only transitions items that are clearly stuck.  It never
touches jobs that are within their timeout window or triggers whose linked
job is still running or completed.

Exported:
  ReconcileConfig
  ReconcileResult
  reconcile_once(db, config) → ReconcileResult
  run_reconciliation_loop(session_factory, config, interval_seconds)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from services.eep.app.db.models import RetrainingJob, RetrainingTrigger

logger = logging.getLogger(__name__)

_DEFAULT_JOB_TIMEOUT_MINUTES: int = 60  # 1 h; stub training finishes well within this


# ── Configuration + result types ─────────────────────────────────────────────


@dataclass
class ReconcileConfig:
    """Configuration for the reconciliation pass."""

    job_timeout_minutes: int = _DEFAULT_JOB_TIMEOUT_MINUTES


@dataclass
class ReconcileResult:
    """Summary of a single reconciliation pass."""

    recovered_jobs: int = 0
    recovered_triggers: int = 0


# ── Core reconciliation pass ──────────────────────────────────────────────────


def reconcile_once(db: Session, config: ReconcileConfig | None = None) -> ReconcileResult:
    """
    Perform a single reconciliation pass.

    Finds and recovers:
    - ``retraining_jobs`` stuck in ``status='running'`` beyond *job_timeout_minutes*.
    - ``retraining_triggers`` stuck in ``status='processing'`` whose linked job is
      missing or has already failed.

    Commits each class of recovery separately so a partial failure does not
    roll back already-recovered rows.

    Args:
        db:     Open SQLAlchemy session.
        config: Reconciliation parameters; defaults to ReconcileConfig().

    Returns:
        ReconcileResult with counts of recovered rows.
    """
    cfg = config or ReconcileConfig()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=cfg.job_timeout_minutes)
    result = ReconcileResult()

    # ── Pass 1: Stuck running jobs ────────────────────────────────────────────
    stuck_jobs = (
        db.query(RetrainingJob)
        .filter(
            RetrainingJob.status == "running",
            RetrainingJob.started_at < cutoff,
        )
        .all()
    )
    for job in stuck_jobs:
        job.status = "failed"
        job.error_message = (
            f"Recovered by reconciler: job exceeded {cfg.job_timeout_minutes}min timeout"
        )
        job.completed_at = now
        result.recovered_jobs += 1
        logger.warning(
            "reconcile_once: stuck job_id=%s marked failed (started_at=%s)",
            job.job_id,
            job.started_at,
        )

    if result.recovered_jobs:
        db.commit()

    # ── Pass 2: Orphaned processing triggers ──────────────────────────────────
    stuck_triggers = (
        db.query(RetrainingTrigger).filter(RetrainingTrigger.status == "processing").all()
    )
    for trigger in stuck_triggers:
        # No linked job — worker died before creating one
        if trigger.retraining_job_id is None:
            trigger.status = "failed"
            trigger.notes = "Recovered by reconciler: no linked retraining job found"
            result.recovered_triggers += 1
            logger.warning(
                "reconcile_once: trigger_id=%s marked failed (no linked job)",
                trigger.trigger_id,
            )
            continue

        linked_job = (
            db.query(RetrainingJob)
            .filter(RetrainingJob.job_id == trigger.retraining_job_id)
            .first()
        )

        if linked_job is None:
            trigger.status = "failed"
            trigger.notes = "Recovered by reconciler: linked retraining job record missing"
            result.recovered_triggers += 1
            logger.warning(
                "reconcile_once: trigger_id=%s marked failed (job_id=%s not found)",
                trigger.trigger_id,
                trigger.retraining_job_id,
            )
        elif linked_job.status == "failed":
            trigger.status = "failed"
            trigger.notes = f"Recovered by reconciler: linked job {linked_job.job_id} failed"
            result.recovered_triggers += 1
            logger.warning(
                "reconcile_once: trigger_id=%s marked failed (linked job failed)",
                trigger.trigger_id,
            )
        # linked_job.status == "running" or "completed" → leave trigger alone

    if result.recovered_triggers:
        db.commit()

    return result


# ── Async polling loop ────────────────────────────────────────────────────────


async def run_reconciliation_loop(
    session_factory: Callable[[], Session],
    config: ReconcileConfig | None = None,
    interval_seconds: float = 60.0,
) -> None:
    """
    Async loop that calls reconcile_once() every *interval_seconds*.

    Creates a fresh DB session per cycle and always closes it in a finally
    block.  Exceptions within a cycle are logged and do not stop the loop.

    Args:
        session_factory:  Callable that returns a new Session (e.g. SessionLocal).
        config:           Reconciliation parameters.
        interval_seconds: Sleep duration between passes (default 60 s).
    """
    cfg = config or ReconcileConfig()
    logger.info(
        "retraining_recovery: reconciliation loop started " "(interval=%.0fs timeout=%dmin)",
        interval_seconds,
        cfg.job_timeout_minutes,
    )
    while True:
        await asyncio.sleep(interval_seconds)
        db = session_factory()
        try:
            result = reconcile_once(db, cfg)
            if result.recovered_jobs or result.recovered_triggers:
                logger.info(
                    "retraining_recovery: pass complete — recovered_jobs=%d recovered_triggers=%d",
                    result.recovered_jobs,
                    result.recovered_triggers,
                )
        except Exception:
            logger.exception("retraining_recovery: error during reconciliation cycle")
        finally:
            db.close()
