"""
tests/test_p4_concurrency.py
-----------------------------
Packet 4.1 — worker concurrency semaphore tests.

Covers:
  - SEMAPHORE_KEY constant
  - initialize_semaphore / reconcile_semaphore: set value on fresh boot,
    preserve in-flight counts on restart, recover from slot leaks
  - WorkerConcurrencyConfig: default values
  - acquire_slot: DECR when slots available; INCR-back + backoff when unavailable
  - backoff schedule: 1 s → 2 s → 4 s → 8 s (capped)
  - release_slot: INCR
  - WorkerSlotContext: acquire on enter, release on exit, release on exception

All tests use fakeredis — no live Redis required.
asyncio.sleep is patched so backoff tests run instantly.
"""

from __future__ import annotations

from unittest.mock import patch

import fakeredis
import pytest

from services.eep_worker.app.concurrency import (
    SEMAPHORE_KEY,
    WorkerConcurrencyConfig,
    WorkerSlotContext,
    acquire_slot,
    initialize_semaphore,
    reconcile_semaphore,
    release_slot,
)
from shared.schemas.queue import QUEUE_PAGE_TASKS_PROCESSING

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def r() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


def _slot_count(r: fakeredis.FakeRedis) -> int:
    return int(r.get(SEMAPHORE_KEY))  # type: ignore[arg-type]


# ── SEMAPHORE_KEY ──────────────────────────────────────────────────────────────


class TestSemaphoreKey:
    def test_key_matches_spec(self) -> None:
        assert SEMAPHORE_KEY == "libraryai:worker_slots"


# ── WorkerConcurrencyConfig ────────────────────────────────────────────────────


class TestWorkerConcurrencyConfig:
    def test_default_max_concurrent_pages(self) -> None:
        assert WorkerConcurrencyConfig().max_concurrent_pages == 20

    def test_custom_max_concurrent_pages(self) -> None:
        assert WorkerConcurrencyConfig(max_concurrent_pages=5).max_concurrent_pages == 5


# ── initialize_semaphore / reconcile_semaphore ─────────────────────────────────


def _push_in_flight(r: fakeredis.FakeRedis, n: int) -> None:
    """Simulate *n* tasks currently parked in QUEUE_PAGE_TASKS_PROCESSING."""
    for i in range(n):
        r.lpush(QUEUE_PAGE_TASKS_PROCESSING, f"fake-task-{i}")


class TestInitializeSemaphoreFreshBoot:
    """First call on an empty Redis must initialise to max_slots."""

    def test_sets_key_when_absent(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 20)
        assert _slot_count(r) == 20

    def test_custom_max_slots(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 7)
        assert _slot_count(r) == 7

    def test_idempotent_on_full_count(self, r: fakeredis.FakeRedis) -> None:
        """Calling initialize twice on an idle cluster is a no-op."""
        initialize_semaphore(r, 10)
        initialize_semaphore(r, 10)
        assert _slot_count(r) == 10


class TestInitializeSemaphorePreservesInFlight:
    """Worker restart must not clobber slots held by other live workers.

    Production invariant: every claimed-but-not-acked task lives in
    QUEUE_PAGE_TASKS_PROCESSING and exactly one slot is held against it.
    Restarting a worker therefore must leave the counter at
    (max - LLEN(processing)).
    """

    def test_does_not_overwrite_when_in_flight_tracked(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 20)
        r.decr(SEMAPHORE_KEY)  # one slot acquired
        _push_in_flight(r, 1)  # ...for the task now in the processing list
        initialize_semaphore(r, 20)
        assert _slot_count(r) == 19

    def test_no_op_when_counter_consistent_with_many_in_flight(self, r: fakeredis.FakeRedis) -> None:
        r.set(SEMAPHORE_KEY, 15)
        _push_in_flight(r, 5)
        initialize_semaphore(r, 20)
        assert _slot_count(r) == 15

    def test_no_op_when_counter_above_expected(self, r: fakeredis.FakeRedis) -> None:
        """If counter is transiently higher than expected (mid-release race),
        leave it alone — never *lower* the counter."""
        r.set(SEMAPHORE_KEY, 20)
        _push_in_flight(r, 3)  # expected would be 17, but counter is at 20
        initialize_semaphore(r, 20)
        assert _slot_count(r) == 20


class TestInitializeSemaphoreSelfHeal:
    """The bug we just lived through: workers crash mid-task and leak slots.

    Without self-heal the counter eventually hits 0 (or negative) and every
    acquire_slot call goes into infinite backoff because subsequent worker
    restarts can no longer reset it (old NX semantics).
    """

    def test_heals_when_counter_leaked_to_zero(self, r: fakeredis.FakeRedis) -> None:
        """Counter = 0, no in-flight: pure leak — restore to max."""
        r.set(SEMAPHORE_KEY, 0)
        initialize_semaphore(r, 20)
        assert _slot_count(r) == 20

    def test_heals_when_counter_negative(self, r: fakeredis.FakeRedis) -> None:
        """A burst of acquire_slot during leak can drive the counter negative
        before INCR-back fires — restore to max."""
        r.set(SEMAPHORE_KEY, -3)
        initialize_semaphore(r, 20)
        assert _slot_count(r) == 20

    def test_partial_leak_with_some_in_flight(self, r: fakeredis.FakeRedis) -> None:
        """Counter at 5, three real in-flight tasks → expected 17 → heal to 17."""
        r.set(SEMAPHORE_KEY, 5)
        _push_in_flight(r, 3)
        initialize_semaphore(r, 20)
        assert _slot_count(r) == 17

    def test_clamps_to_zero_when_in_flight_exceeds_max(self, r: fakeredis.FakeRedis) -> None:
        """Defensive: a stale processing list with > max entries must not
        produce a negative expected slot count."""
        r.set(SEMAPHORE_KEY, -5)
        _push_in_flight(r, 25)
        initialize_semaphore(r, 20)
        assert _slot_count(r) == 0


class TestReconcileSemaphoreReport:
    """reconcile_semaphore returns a structured ReconcileResult."""

    def test_fresh_boot_reports_healed_with_max_slots(self, r: fakeredis.FakeRedis) -> None:
        result = reconcile_semaphore(r, 20)
        assert result.healed is True
        assert result.slots == 20
        assert result.in_flight == 0

    def test_consistent_state_reports_no_heal(self, r: fakeredis.FakeRedis) -> None:
        r.set(SEMAPHORE_KEY, 18)
        _push_in_flight(r, 2)
        result = reconcile_semaphore(r, 20)
        assert result.healed is False
        assert result.slots == 18
        assert result.in_flight == 2

    def test_leaked_state_reports_heal_and_new_slots(self, r: fakeredis.FakeRedis) -> None:
        r.set(SEMAPHORE_KEY, 0)
        _push_in_flight(r, 4)
        result = reconcile_semaphore(r, 20)
        assert result.healed is True
        assert result.slots == 16  # 20 - 4
        assert result.in_flight == 4

    def test_higher_counter_left_alone(self, r: fakeredis.FakeRedis) -> None:
        r.set(SEMAPHORE_KEY, 50)
        result = reconcile_semaphore(r, 20)
        assert result.healed is False
        assert result.slots == 50
        assert result.in_flight == 0

    def test_custom_processing_key_is_honoured(self, r: fakeredis.FakeRedis) -> None:
        r.lpush("custom:processing", "x", "y", "z")
        result = reconcile_semaphore(r, 10, processing_key="custom:processing")
        assert result.healed is True
        assert result.slots == 7
        assert result.in_flight == 3


# ── acquire_slot ───────────────────────────────────────────────────────────────


class TestAcquireSlotAvailable:
    async def test_decr_when_slot_available(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 5)
        await acquire_slot(r)
        assert _slot_count(r) == 4

    async def test_multiple_acquires_decrement_correctly(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 5)
        await acquire_slot(r)
        await acquire_slot(r)
        assert _slot_count(r) == 3

    async def test_no_sleep_when_slot_immediately_available(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 1)
        with patch("services.eep_worker.app.concurrency.asyncio.sleep") as mock_sleep:
            await acquire_slot(r)
        mock_sleep.assert_not_called()


class TestAcquireSlotUnavailable:
    async def test_increments_back_when_no_slot(self, r: fakeredis.FakeRedis) -> None:
        """If DECR goes negative the slot must be returned immediately."""
        initialize_semaphore(r, 0)
        attempts: list[int] = []

        async def mock_sleep(seconds: float) -> None:
            attempts.append(len(attempts))
            if len(attempts) == 1:
                r.incr(SEMAPHORE_KEY)  # release a slot after first wait

        with patch("services.eep_worker.app.concurrency.asyncio.sleep", side_effect=mock_sleep):
            await acquire_slot(r)

        assert len(attempts) == 1
        # After acquire the count should be 0 (we were at 0, gave one back, then took it)
        assert _slot_count(r) == 0

    async def test_backoff_first_retry_is_1s(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 0)
        sleep_args: list[float] = []

        async def mock_sleep(seconds: float) -> None:
            sleep_args.append(seconds)
            r.incr(SEMAPHORE_KEY)  # make slot available after first wait

        with patch("services.eep_worker.app.concurrency.asyncio.sleep", side_effect=mock_sleep):
            await acquire_slot(r)

        assert sleep_args[0] == 1.0

    async def test_backoff_sequence_1_2_4_8(self, r: fakeredis.FakeRedis) -> None:
        """Backoff must follow 1 s → 2 s → 4 s → 8 s schedule."""
        initialize_semaphore(r, 0)
        attempt = 0
        sleep_args: list[float] = []

        async def mock_sleep(seconds: float) -> None:
            nonlocal attempt
            sleep_args.append(seconds)
            attempt += 1
            if attempt >= 4:
                r.incr(SEMAPHORE_KEY)

        with patch("services.eep_worker.app.concurrency.asyncio.sleep", side_effect=mock_sleep):
            await acquire_slot(r)

        assert sleep_args == [1.0, 2.0, 4.0, 8.0]

    async def test_backoff_caps_at_8s(self, r: fakeredis.FakeRedis) -> None:
        """After the 4th retry all subsequent sleeps must be 8 s."""
        initialize_semaphore(r, 0)
        attempt = 0
        sleep_args: list[float] = []

        async def mock_sleep(seconds: float) -> None:
            nonlocal attempt
            sleep_args.append(seconds)
            attempt += 1
            if attempt >= 7:
                r.incr(SEMAPHORE_KEY)

        with patch("services.eep_worker.app.concurrency.asyncio.sleep", side_effect=mock_sleep):
            await acquire_slot(r)

        # First four entries follow the schedule; everything after is 8 s
        assert sleep_args[:4] == [1.0, 2.0, 4.0, 8.0]
        assert all(s == 8.0 for s in sleep_args[3:])

    async def test_slot_count_consistent_after_backoff(self, r: fakeredis.FakeRedis) -> None:
        """Slot count must be exactly (initial - 1) after a successful acquire via backoff."""
        initialize_semaphore(r, 2)
        # Consume both slots manually
        r.decr(SEMAPHORE_KEY)
        r.decr(SEMAPHORE_KEY)
        assert _slot_count(r) == 0

        async def mock_sleep(_: float) -> None:
            r.incr(SEMAPHORE_KEY)  # free a slot

        with patch("services.eep_worker.app.concurrency.asyncio.sleep", side_effect=mock_sleep):
            await acquire_slot(r)

        assert _slot_count(r) == 0  # one slot freed, then immediately re-acquired


# ── release_slot ───────────────────────────────────────────────────────────────


class TestReleaseSlot:
    def test_incr_after_acquire(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 5)
        r.decr(SEMAPHORE_KEY)
        release_slot(r)
        assert _slot_count(r) == 5

    def test_incr_from_zero(self, r: fakeredis.FakeRedis) -> None:
        r.set(SEMAPHORE_KEY, 0)
        release_slot(r)
        assert _slot_count(r) == 1

    def test_multiple_releases(self, r: fakeredis.FakeRedis) -> None:
        r.set(SEMAPHORE_KEY, 0)
        release_slot(r)
        release_slot(r)
        assert _slot_count(r) == 2


# ── WorkerSlotContext ──────────────────────────────────────────────────────────


class TestWorkerSlotContext:
    async def test_acquires_slot_on_enter(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 3)
        async with WorkerSlotContext(r):
            assert _slot_count(r) == 2

    async def test_releases_slot_on_clean_exit(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 3)
        async with WorkerSlotContext(r):
            pass
        assert _slot_count(r) == 3

    async def test_releases_slot_on_exception(self, r: fakeredis.FakeRedis) -> None:
        """Release must fire in try/finally — spec Section 8.1."""
        initialize_semaphore(r, 3)
        with pytest.raises(RuntimeError):
            async with WorkerSlotContext(r):
                raise RuntimeError("task processing failed")
        assert _slot_count(r) == 3

    async def test_nested_contexts_decrement_correctly(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 4)
        async with WorkerSlotContext(r):
            async with WorkerSlotContext(r):
                assert _slot_count(r) == 2
            assert _slot_count(r) == 3
        assert _slot_count(r) == 4

    async def test_returns_self(self, r: fakeredis.FakeRedis) -> None:
        initialize_semaphore(r, 1)
        async with WorkerSlotContext(r) as ctx:
            assert isinstance(ctx, WorkerSlotContext)
