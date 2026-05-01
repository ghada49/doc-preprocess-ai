"""
services/eep_worker/app/concurrency.py
---------------------------------------
Redis-based worker concurrency semaphore for the EEP processing pipeline.

Implements the slot model from spec Section 8.1:

  Key:      libraryai:worker_slots
  Acquire:  DECR; if result < 0, INCR back and wait (exponential backoff)
  Release:  INCR  — MUST happen in try/finally (spec Section 8.1)
  Max:      config.max_concurrent_pages (default 20, spec Section 8.4)
  Backoff:  1 s → 2 s → 4 s → 8 s (max, then 8 s on every subsequent retry)

The semaphore is shared across all worker processes on the same Redis instance,
bounding total in-flight pages system-wide at max_concurrent_pages.

Self-healing
------------
``initialize_semaphore`` is the single startup entry point and is also safe to
call periodically.  It reconciles the slot counter against the canonical
in-flight signal — the length of ``QUEUE_PAGE_TASKS_PROCESSING`` — and restores
slots that have leaked due to worker crashes between ``acquire_slot`` and
``release_slot``.

The reconciliation is *conservative*: the counter is **only ever raised** to the
expected value (``max_slots − LLEN(processing)``), never lowered.  This
guarantees we cannot over-allocate slots while other workers are mid-claim, but
will always recover from the slot-leak failure mode that previously required a
manual ``SET libraryai:worker_slots <max>`` to unstick the cluster.

Exported:
    SEMAPHORE_KEY           — Redis key constant "libraryai:worker_slots"
    WorkerConcurrencyConfig — config dataclass (mirrors libraryai-policy)
    initialize_semaphore    — startup reconcile against the processing list
    reconcile_semaphore     — same logic, returns a structured report (testing
                              and periodic self-heal callers)
    acquire_slot            — async; DECR + backoff loop
    release_slot            — sync; INCR
    WorkerSlotContext       — async context manager; wraps acquire/release in
                              try/finally as required by spec
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import NamedTuple

import redis as redis_lib

from shared.schemas.queue import QUEUE_PAGE_TASKS_PROCESSING

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

SEMAPHORE_KEY: str = "libraryai:worker_slots"

# Exponential backoff steps (seconds).  After the last step the wait stays at
# the maximum (8 s).  Source: spec Section 8.1 "(1s, 2s, 4s, 8s max)".
_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)


# ── Configuration ──────────────────────────────────────────────────────────────


@dataclasses.dataclass
class WorkerConcurrencyConfig:
    """
    Configuration for the worker concurrency semaphore.

    Defaults match the libraryai-policy ConfigMap (spec Section 8.4):
        max_concurrent_pages: 20
    """

    max_concurrent_pages: int = 20


# ── Semaphore primitives ───────────────────────────────────────────────────────


class ReconcileResult(NamedTuple):
    """
    Result of a semaphore reconciliation pass.

    Attributes:
        healed:    True iff the counter was below the expected value and was
                   raised by this call.  False means the counter was already
                   consistent with the in-flight signal (no write was issued).
        slots:     Final value of ``libraryai:worker_slots`` after the call.
        in_flight: Number of tasks observed in ``QUEUE_PAGE_TASKS_PROCESSING``
                   (the signal we reconciled against).
    """

    healed: bool
    slots: int
    in_flight: int


def reconcile_semaphore(
    r: redis_lib.Redis,
    max_slots: int,
    *,
    processing_key: str = QUEUE_PAGE_TASKS_PROCESSING,
) -> ReconcileResult:
    """
    Reconcile the slot counter against the in-flight processing list.

    Computes ``expected = max(0, max_slots − LLEN(processing_key))``.  If the
    current counter is missing or strictly less than ``expected`` the counter
    is overwritten with ``expected``.  Otherwise no write occurs.

    This is the recovery path for the slot-leak failure mode that arises when
    a worker crashes (or is forcibly terminated) between ``acquire_slot`` and
    ``release_slot``: the orphaned task remains in the processing list (where
    eep-recovery will eventually re-enqueue it) but the slot is never returned.
    Without this reconciliation a sufficiently leaky cluster ends up with
    ``worker_slots <= 0`` and every claim attempt loops in the backoff sleep.

    Conservative-by-design: the counter is **only ever raised**.  If the
    counter is already higher than ``expected`` (which can transiently happen
    when other workers are mid-acquire/release) we leave it alone.

    Args:
        r:              Redis client.
        max_slots:      Upper bound on concurrent in-flight pages.
        processing_key: Name of the in-flight queue list.  Defaults to the
                        production queue; tests may pass an alternate key.

    Returns:
        ReconcileResult describing the action taken.
    """
    in_flight_raw = r.llen(processing_key)
    in_flight = int(in_flight_raw) if in_flight_raw is not None else 0

    expected = max_slots - in_flight
    if expected < 0:
        expected = 0

    raw_current = r.get(SEMAPHORE_KEY)
    if raw_current is None:
        current: int | None = None
    else:
        try:
            current = int(raw_current)
        except (TypeError, ValueError):
            current = None

    if current is None or current < expected:
        r.set(SEMAPHORE_KEY, expected)
        if current is not None:
            logger.warning(
                "concurrency: semaphore drifted current=%d expected=%d "
                "in_flight=%d max=%d — reconciled to %d",
                current,
                expected,
                in_flight,
                max_slots,
                expected,
            )
        return ReconcileResult(healed=True, slots=expected, in_flight=in_flight)

    return ReconcileResult(healed=False, slots=current, in_flight=in_flight)


def initialize_semaphore(
    r: redis_lib.Redis,
    max_slots: int,
    *,
    processing_key: str = QUEUE_PAGE_TASKS_PROCESSING,
) -> None:
    """
    Initialize (or reconcile) the Redis semaphore at worker startup.

    Reads the in-flight processing list and ensures the counter is at least
    ``max_slots − LLEN(processing_key)``.  Concretely this means:

      * Fresh boot, no work in flight → counter is set to ``max_slots``.
      * Worker restart with N tasks still in the processing list and a healthy
        counter at ``max_slots − N`` → no-op, in-flight slots are preserved.
      * Worker restart after a crash that leaked slots (counter < expected) →
        counter is restored to ``expected``, ending the backoff-loop deadlock.

    Args:
        r:              Redis client (decode_responses=True recommended).
        max_slots:      Upper bound on concurrent in-flight pages.
        processing_key: Name of the in-flight queue list.  Defaults to the
                        production queue; tests may pass an alternate key.
    """
    reconcile_semaphore(r, max_slots, processing_key=processing_key)


async def acquire_slot(r: redis_lib.Redis) -> None:
    """
    Acquire one semaphore slot.

    Algorithm (spec Section 8.1):
      1. DECR libraryai:worker_slots.
      2. If result >= 0: slot acquired, return.
      3. Else: INCR (give the slot back) and sleep for the current backoff
         duration before retrying.

    Backoff schedule: 1 s, 2 s, 4 s, 8 s, 8 s, … (capped at 8 s).

    Args:
        r: Redis client.
    """
    attempt = 0
    while True:
        remaining: int = r.decr(SEMAPHORE_KEY)  # type: ignore[assignment]
        if remaining >= 0:
            return
        # No slot available — give it back and wait.
        r.incr(SEMAPHORE_KEY)
        backoff = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
        await asyncio.sleep(backoff)
        attempt += 1


def release_slot(r: redis_lib.Redis) -> None:
    """
    Release one semaphore slot (INCR libraryai:worker_slots).

    Must be called in a try/finally block by every caller (spec Section 8.1).
    WorkerSlotContext enforces this automatically.

    Args:
        r: Redis client.
    """
    r.incr(SEMAPHORE_KEY)


# ── Context manager ────────────────────────────────────────────────────────────


class WorkerSlotContext:
    """
    Async context manager that acquires a semaphore slot on ``__aenter__`` and
    releases it on ``__aexit__``.

    The release is unconditional (fires on both normal exit and exception),
    satisfying the spec Section 8.1 requirement that slot release must happen
    in ``try/finally``.

    Usage::

        async with WorkerSlotContext(redis_client):
            await process_page(task)
    """

    def __init__(self, r: redis_lib.Redis) -> None:
        self._r = r

    async def __aenter__(self) -> WorkerSlotContext:
        await acquire_slot(self._r)
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> None:
        release_slot(self._r)
