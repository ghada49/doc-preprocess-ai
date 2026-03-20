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

Exported:
    SEMAPHORE_KEY           — Redis key constant "libraryai:worker_slots"
    WorkerConcurrencyConfig — config dataclass (mirrors libraryai-policy)
    initialize_semaphore    — set semaphore to max_slots (NX; safe to call on
                               restart without clobbering in-flight counts)
    acquire_slot            — async; DECR + backoff loop
    release_slot            — sync; INCR
    WorkerSlotContext       — async context manager; wraps acquire/release in
                               try/finally as required by spec
"""

from __future__ import annotations

import asyncio
import dataclasses

import redis as redis_lib

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


def initialize_semaphore(r: redis_lib.Redis, max_slots: int) -> None:
    """
    Initialize the Redis semaphore to *max_slots* **only if the key does not
    already exist** (SET NX semantics).

    This must be called once at worker startup.  Using NX ensures that a
    worker restart does not reset the counter while other workers still hold
    in-flight slots, which would violate the system-wide bound.

    Args:
        r:         Redis client (decode_responses=True recommended).
        max_slots: Upper bound on concurrent in-flight pages.
    """
    r.set(SEMAPHORE_KEY, max_slots, nx=True)


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
