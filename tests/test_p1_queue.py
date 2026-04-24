"""
tests/test_p1_queue.py
-----------------------
Packet 1.7a contract tests for services.eep.app.queue.

All tests use fakeredis (decode_responses=True) — no live Redis required.

Invariants verified
-------------------
1.  enqueue_page_task: LPUSH to main queue; valid JSON; round-trips to PageTask.
2.  claim_task:        BLMOVE / BRPOPLPUSH atomically moves task to processing list.
3.  claim_task:        returns None when queue is empty (timeout).
4.  claim_task:        ClaimedTask contains correct worker_id, task, claimed_at.
5.  claim_task:        records ownership in CLAIMS_KEY hash.
6.  claim_task:        poison-pill JSON is dead-lettered, not left in processing.
7.  claim_task:        FIFO order (first enqueued = first claimed).
8.  ack_task:          removes from processing list and claims hash.
9.  ack_task:          second ack is a safe no-op.
10. fail_task:         re-enqueues with retry_count+1 when retries remain.
11. fail_task:         dead-letters when retry_count >= max_retries.
12. fail_task:         task removed from processing in all branches.
13. fail_task:         claims hash cleared in all branches.
14. move_to_dead_letter: removes from processing, appends to dead-letter.
15. move_to_dead_letter: dead-letter payload is the original raw JSON.
16. Crash recovery:    task remains in processing list if ack is never called.
17. get_processing_tasks: returns parsed in-flight tasks.
18. get_processing_tasks: skips and logs corrupt entries.
19. requeue_task:      pushes task to main queue and it is claimable.
20. rebuild_queue_from_db: stub is a safe no-op.
21. _blmove_safe fallback: falls back to brpoplpush on ResponseError.
22. _blmove_safe fallback: falls back to brpoplpush on AttributeError.
"""

from __future__ import annotations

import json
import types
import uuid
from datetime import datetime

import fakeredis
import pytest
import redis

from services.eep.app.queue import (
    CLAIMS_KEY,
    MAX_TASK_RETRIES,
    ClaimedTask,
    _blmove_safe,
    ack_task,
    claim_task,
    enqueue_page_task,
    fail_task,
    get_processing_tasks,
    move_to_dead_letter,
    rebuild_queue_from_db,
    requeue_task,
)
from shared.schemas.queue import (
    QUEUE_DEAD_LETTER,
    QUEUE_PAGE_TASKS,
    QUEUE_PAGE_TASKS_PROCESSING,
    PageTask,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def r() -> fakeredis.FakeRedis:
    """Fresh in-memory Redis instance for each test."""
    return fakeredis.FakeRedis(decode_responses=True)


def _task(**overrides: object) -> PageTask:
    """Return a minimal valid PageTask with a unique task_id."""
    defaults: dict[str, object] = {
        "task_id": str(uuid.uuid4()),
        "job_id": "job-001",
        "page_id": "page-001",
        "page_number": 1,
        "retry_count": 0,
    }
    defaults.update(overrides)
    return PageTask(**defaults)  # type: ignore[arg-type]


def _enqueue_and_claim(
    r: fakeredis.FakeRedis,
    worker_id: str = "worker-1",
    **task_kwargs: object,
) -> ClaimedTask:
    """Enqueue a task and immediately claim it; asserts claim succeeds."""
    task = _task(**task_kwargs)
    enqueue_page_task(r, task)
    claimed = claim_task(r, worker_id, timeout=1.0)
    assert claimed is not None, "claim_task returned None on a non-empty queue"
    return claimed


# ---------------------------------------------------------------------------
# TestEnqueuePageTask
# ---------------------------------------------------------------------------


class TestEnqueuePageTask:
    def test_pushes_one_item_to_main_queue(self, r: fakeredis.FakeRedis) -> None:
        enqueue_page_task(r, _task())
        assert r.llen(QUEUE_PAGE_TASKS) == 1

    def test_multiple_enqueues_grow_queue(self, r: fakeredis.FakeRedis) -> None:
        for _ in range(4):
            enqueue_page_task(r, _task())
        assert r.llen(QUEUE_PAGE_TASKS) == 4

    def test_payload_is_valid_json(self, r: fakeredis.FakeRedis) -> None:
        task = _task(page_number=3)
        enqueue_page_task(r, task)
        raw = r.lrange(QUEUE_PAGE_TASKS, 0, -1)[0]  # type: ignore[index]
        parsed = json.loads(raw)
        assert parsed["task_id"] == task.task_id
        assert parsed["page_number"] == 3

    def test_payload_round_trips_to_page_task(self, r: fakeredis.FakeRedis) -> None:
        task = _task(page_number=7, sub_page_index=1, retry_count=2)
        enqueue_page_task(r, task)
        raw = r.lrange(QUEUE_PAGE_TASKS, 0, -1)[0]  # type: ignore[index]
        assert PageTask.model_validate_json(raw) == task


# ---------------------------------------------------------------------------
# TestClaimTask
# ---------------------------------------------------------------------------


class TestClaimTask:
    def test_returns_claimed_task(self, r: fakeredis.FakeRedis) -> None:
        enqueue_page_task(r, _task())
        result = claim_task(r, "worker-1", timeout=1.0)
        assert isinstance(result, ClaimedTask)

    def test_returns_none_on_empty_queue(self, r: fakeredis.FakeRedis) -> None:
        result = claim_task(r, "worker-1", timeout=0.05)
        assert result is None

    def test_task_moved_to_processing_list(self, r: fakeredis.FakeRedis) -> None:
        enqueue_page_task(r, _task())
        claim_task(r, "w", timeout=1.0)
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1

    def test_main_queue_empty_after_claim(self, r: fakeredis.FakeRedis) -> None:
        enqueue_page_task(r, _task())
        claim_task(r, "w", timeout=1.0)
        assert r.llen(QUEUE_PAGE_TASKS) == 0

    def test_claimed_task_has_correct_worker_id(self, r: fakeredis.FakeRedis) -> None:
        enqueue_page_task(r, _task())
        claimed = claim_task(r, "worker-99", timeout=1.0)
        assert claimed is not None
        assert claimed.worker_id == "worker-99"

    def test_claimed_task_matches_enqueued_task(self, r: fakeredis.FakeRedis) -> None:
        task = _task(page_number=5, retry_count=1)
        enqueue_page_task(r, task)
        claimed = claim_task(r, "w", timeout=1.0)
        assert claimed is not None
        assert claimed.task == task

    def test_claimed_task_raw_json_is_string(self, r: fakeredis.FakeRedis) -> None:
        enqueue_page_task(r, _task())
        claimed = claim_task(r, "w", timeout=1.0)
        assert claimed is not None
        assert isinstance(claimed.raw_json, str)

    def test_claimed_at_is_datetime(self, r: fakeredis.FakeRedis) -> None:
        enqueue_page_task(r, _task())
        claimed = claim_task(r, "w", timeout=1.0)
        assert claimed is not None
        assert isinstance(claimed.claimed_at, datetime)

    def test_ownership_recorded_in_claims_hash(self, r: fakeredis.FakeRedis) -> None:
        task = _task()
        enqueue_page_task(r, task)
        claimed = claim_task(r, "worker-X", timeout=1.0)
        assert claimed is not None
        entry = r.hget(CLAIMS_KEY, task.task_id)
        assert entry is not None
        assert "worker-X" in entry  # type: ignore[operator]

    def test_claimed_at_recorded_in_claims_hash(self, r: fakeredis.FakeRedis) -> None:
        task = _task()
        enqueue_page_task(r, task)
        claimed = claim_task(r, "w", timeout=1.0)
        assert claimed is not None
        entry = r.hget(CLAIMS_KEY, task.task_id)
        assert claimed.claimed_at.isoformat() in (entry or "")  # type: ignore[operator]

    def test_poison_pill_sent_to_dead_letter(self, r: fakeredis.FakeRedis) -> None:
        r.lpush(QUEUE_PAGE_TASKS, "NOT_VALID_JSON{{{")
        result = claim_task(r, "w", timeout=1.0)
        assert result is None
        assert r.llen(QUEUE_DEAD_LETTER) == 1
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 0

    def test_fifo_order_across_two_claims(self, r: fakeredis.FakeRedis) -> None:
        """First enqueued task must be first claimed."""
        t1 = _task(page_number=1)
        t2 = _task(page_number=2)
        enqueue_page_task(r, t1)
        enqueue_page_task(r, t2)
        c1 = claim_task(r, "w", timeout=1.0)
        c2 = claim_task(r, "w", timeout=1.0)
        assert c1 is not None and c2 is not None
        assert c1.task.page_number == 1
        assert c2.task.page_number == 2

    def test_processing_list_grows_with_multiple_claims(self, r: fakeredis.FakeRedis) -> None:
        for _ in range(3):
            enqueue_page_task(r, _task())
            claim_task(r, "w", timeout=1.0)
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 3


# ---------------------------------------------------------------------------
# TestAckTask
# ---------------------------------------------------------------------------


class TestAckTask:
    def test_removes_from_processing_list(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r)
        ack_task(r, claimed)
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 0

    def test_removes_from_claims_hash(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r)
        ack_task(r, claimed)
        assert r.hget(CLAIMS_KEY, claimed.task.task_id) is None

    def test_main_queue_unaffected(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r)
        ack_task(r, claimed)
        assert r.llen(QUEUE_PAGE_TASKS) == 0

    def test_second_ack_is_safe_noop(self, r: fakeredis.FakeRedis) -> None:
        """LREM on an already-removed entry returns 0; must not raise."""
        claimed = _enqueue_and_claim(r)
        ack_task(r, claimed)
        ack_task(r, claimed)  # must not raise
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 0


# ---------------------------------------------------------------------------
# TestFailTask
# ---------------------------------------------------------------------------


class TestFailTask:
    def test_requeues_when_retries_remain(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r, retry_count=0)
        fail_task(r, claimed, max_retries=3)
        assert r.llen(QUEUE_PAGE_TASKS) == 1

    def test_requeued_task_has_incremented_retry_count(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r, retry_count=1)
        fail_task(r, claimed, max_retries=3)
        raw = r.lrange(QUEUE_PAGE_TASKS, 0, -1)[0]  # type: ignore[index]
        assert PageTask.model_validate_json(raw).retry_count == 2

    def test_removed_from_processing_after_fail_with_retry(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r, retry_count=0)
        fail_task(r, claimed, max_retries=3)
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 0

    def test_claims_hash_cleared_after_fail_with_retry(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r, retry_count=0)
        fail_task(r, claimed, max_retries=3)
        assert r.hget(CLAIMS_KEY, claimed.task.task_id) is None

    def test_dead_letters_at_max_retries(self, r: fakeredis.FakeRedis) -> None:
        """retry_count == max_retries → dead-letter."""
        claimed = _enqueue_and_claim(r, retry_count=MAX_TASK_RETRIES)
        fail_task(r, claimed, max_retries=MAX_TASK_RETRIES)
        assert r.llen(QUEUE_DEAD_LETTER) == 1
        assert r.llen(QUEUE_PAGE_TASKS) == 0

    def test_dead_letters_above_max_retries(self, r: fakeredis.FakeRedis) -> None:
        """retry_count > max_retries is also dead-lettered."""
        claimed = _enqueue_and_claim(r, retry_count=MAX_TASK_RETRIES + 5)
        fail_task(r, claimed, max_retries=MAX_TASK_RETRIES)
        assert r.llen(QUEUE_DEAD_LETTER) == 1

    def test_removed_from_processing_after_dead_letter(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r, retry_count=MAX_TASK_RETRIES)
        fail_task(r, claimed, max_retries=MAX_TASK_RETRIES)
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 0

    def test_claims_hash_cleared_after_dead_letter(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r, retry_count=MAX_TASK_RETRIES)
        fail_task(r, claimed, max_retries=MAX_TASK_RETRIES)
        assert r.hget(CLAIMS_KEY, claimed.task.task_id) is None

    def test_retry_count_two_below_max_three(self, r: fakeredis.FakeRedis) -> None:
        """retry_count=2, max=3 → re-enqueue with retry_count=3."""
        claimed = _enqueue_and_claim(r, retry_count=2)
        fail_task(r, claimed, max_retries=3)
        raw = r.lrange(QUEUE_PAGE_TASKS, 0, -1)[0]  # type: ignore[index]
        assert PageTask.model_validate_json(raw).retry_count == 3

    def test_custom_max_retries_one(self, r: fakeredis.FakeRedis) -> None:
        """max_retries=1: retry_count=1 → dead-letter immediately."""
        claimed = _enqueue_and_claim(r, retry_count=1)
        fail_task(r, claimed, max_retries=1)
        assert r.llen(QUEUE_DEAD_LETTER) == 1
        assert r.llen(QUEUE_PAGE_TASKS) == 0

    def test_custom_max_retries_one_below(self, r: fakeredis.FakeRedis) -> None:
        """max_retries=1: retry_count=0 → re-enqueue once."""
        claimed = _enqueue_and_claim(r, retry_count=0)
        fail_task(r, claimed, max_retries=1)
        assert r.llen(QUEUE_PAGE_TASKS) == 1
        assert r.llen(QUEUE_DEAD_LETTER) == 0


# ---------------------------------------------------------------------------
# TestMoveToDeadLetter
# ---------------------------------------------------------------------------


class TestMoveToDeadLetter:
    def test_removes_from_processing_list(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r)
        move_to_dead_letter(r, claimed)
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 0

    def test_appends_to_dead_letter_queue(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r)
        move_to_dead_letter(r, claimed)
        assert r.llen(QUEUE_DEAD_LETTER) == 1

    def test_dead_letter_payload_round_trips(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r)
        move_to_dead_letter(r, claimed)
        raw = r.lrange(QUEUE_DEAD_LETTER, 0, -1)[0]  # type: ignore[index]
        restored = PageTask.model_validate_json(raw)
        assert restored.task_id == claimed.task.task_id

    def test_removes_from_claims_hash(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r)
        move_to_dead_letter(r, claimed)
        assert r.hget(CLAIMS_KEY, claimed.task.task_id) is None

    def test_main_queue_unaffected(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r)
        move_to_dead_letter(r, claimed)
        assert r.llen(QUEUE_PAGE_TASKS) == 0

    def test_multiple_dead_letters_stack(self, r: fakeredis.FakeRedis) -> None:
        for _ in range(3):
            move_to_dead_letter(r, _enqueue_and_claim(r))
        assert r.llen(QUEUE_DEAD_LETTER) == 3


# ---------------------------------------------------------------------------
# TestCrashRecovery
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    def test_task_stays_in_processing_after_crash(self, r: fakeredis.FakeRedis) -> None:
        """
        Simulate worker crash: task is claimed but ack/fail is never called.
        Task must remain in the processing list for the recovery service.
        """
        enqueue_page_task(r, _task())
        claimed = claim_task(r, "crashed-worker", timeout=1.0)
        assert claimed is not None
        # No ack or fail — simulates crash here.
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1
        assert r.llen(QUEUE_PAGE_TASKS) == 0

    def test_ownership_survives_crash(self, r: fakeredis.FakeRedis) -> None:
        task = _task()
        enqueue_page_task(r, task)
        claimed = claim_task(r, "crashed-worker", timeout=1.0)
        assert claimed is not None
        # Claims hash entry survives the crash — recovery can inspect it.
        assert r.hget(CLAIMS_KEY, task.task_id) is not None

    def test_recovery_can_requeue_from_processing_list(self, r: fakeredis.FakeRedis) -> None:
        """Recovery service uses get_processing_tasks + requeue_task."""
        task = _task(retry_count=0)
        enqueue_page_task(r, task)
        claim_task(r, "crashed-worker", timeout=1.0)
        # Recovery: inspect processing list, build recovery task, re-enqueue.
        in_flight = get_processing_tasks(r)
        assert len(in_flight) == 1
        recovered = in_flight[0].model_copy(update={"retry_count": in_flight[0].retry_count + 1})
        requeue_task(r, recovered)
        assert r.llen(QUEUE_PAGE_TASKS) == 1
        assert PageTask.model_validate_json(r.lrange(QUEUE_PAGE_TASKS, 0, -1)[0]).retry_count == 1  # type: ignore[index]


# ---------------------------------------------------------------------------
# TestGetProcessingTasks
# ---------------------------------------------------------------------------


class TestGetProcessingTasks:
    def test_empty_when_nothing_in_flight(self, r: fakeredis.FakeRedis) -> None:
        assert get_processing_tasks(r) == []

    def test_returns_claimed_task(self, r: fakeredis.FakeRedis) -> None:
        enqueue_page_task(r, _task())
        claimed = claim_task(r, "w", timeout=1.0)
        assert claimed is not None
        tasks = get_processing_tasks(r)
        assert len(tasks) == 1
        assert tasks[0].task_id == claimed.task.task_id

    def test_returns_all_in_flight_tasks(self, r: fakeredis.FakeRedis) -> None:
        for _ in range(3):
            enqueue_page_task(r, _task())
            claim_task(r, "w", timeout=1.0)
        assert len(get_processing_tasks(r)) == 3

    def test_skips_corrupt_entry(self, r: fakeredis.FakeRedis) -> None:
        r.lpush(QUEUE_PAGE_TASKS_PROCESSING, "CORRUPT{{{")
        assert get_processing_tasks(r) == []

    def test_valid_task_alongside_corrupt(self, r: fakeredis.FakeRedis) -> None:
        task = _task()
        r.lpush(QUEUE_PAGE_TASKS_PROCESSING, task.model_dump_json())
        r.lpush(QUEUE_PAGE_TASKS_PROCESSING, "CORRUPT")
        result = get_processing_tasks(r)
        assert len(result) == 1
        assert result[0].task_id == task.task_id

    def test_acked_task_not_in_processing(self, r: fakeredis.FakeRedis) -> None:
        claimed = _enqueue_and_claim(r)
        ack_task(r, claimed)
        assert get_processing_tasks(r) == []


# ---------------------------------------------------------------------------
# TestRequeueTask
# ---------------------------------------------------------------------------


class TestRequeueTask:
    def test_adds_to_main_queue(self, r: fakeredis.FakeRedis) -> None:
        requeue_task(r, _task(retry_count=1))
        assert r.llen(QUEUE_PAGE_TASKS) == 1

    def test_requeued_task_is_claimable(self, r: fakeredis.FakeRedis) -> None:
        task = _task(retry_count=2)
        requeue_task(r, task)
        claimed = claim_task(r, "w", timeout=1.0)
        assert claimed is not None
        assert claimed.task == task

    def test_multiple_requeues(self, r: fakeredis.FakeRedis) -> None:
        for i in range(1, 4):
            requeue_task(r, _task(retry_count=i))
        assert r.llen(QUEUE_PAGE_TASKS) == 3

    def test_requeue_without_db_check_creates_duplicate(self, r: fakeredis.FakeRedis) -> None:
        """
        Documents at-least-once delivery semantics.

        requeue_task has no duplicate guard.  If recovery calls it while the
        task is still in the processing list (e.g., a slow worker, not a
        crashed one), the task exists in both lists simultaneously.  A second
        worker can claim it from the main queue while the first still holds it
        in the processing list — two workers processing the same task.

        Workers must guard against double-processing using DB page state,
        not queue membership.
        """
        enqueue_page_task(r, _task())
        # First worker claims the task (now in processing list).
        claimed = claim_task(r, "worker-1", timeout=1.0)
        assert claimed is not None
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1
        assert r.llen(QUEUE_PAGE_TASKS) == 0

        # Recovery (incorrectly, without DB check) re-enqueues the same task.
        requeue_task(r, claimed.task)

        # Task is now in BOTH lists — at-least-once, not exactly-once.
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1
        assert r.llen(QUEUE_PAGE_TASKS) == 1


# ---------------------------------------------------------------------------
# TestRebuildQueueFromDb
# ---------------------------------------------------------------------------


def _fake_page(
    page_id: str,
    job_id: str = "job-001",
    page_number: int = 1,
    sub_page_index: int | None = None,
) -> object:
    """Minimal DB-row stub for rebuild_queue_from_db tests."""
    return types.SimpleNamespace(
        page_id=page_id,
        job_id=job_id,
        page_number=page_number,
        sub_page_index=sub_page_index,
    )


class TestRebuildQueueFromDb:
    def test_returns_zero_when_no_pages(self, r: fakeredis.FakeRedis) -> None:
        """Empty DB result → nothing enqueued, returns 0."""
        assert rebuild_queue_from_db(r, get_queued_pages_fn=lambda: []) == 0
        assert r.llen(QUEUE_PAGE_TASKS) == 0

    def test_existing_tasks_unaffected_when_no_pages(self, r: fakeredis.FakeRedis) -> None:
        """Existing main-queue entries are untouched when no pages need requeuing."""
        enqueue_page_task(r, _task())
        rebuild_queue_from_db(r, get_queued_pages_fn=lambda: [])
        assert r.llen(QUEUE_PAGE_TASKS) == 1

    def test_processing_list_unaffected_when_no_pages(self, r: fakeredis.FakeRedis) -> None:
        """Processing list is untouched when no pages need requeuing."""
        _enqueue_and_claim(r)
        rebuild_queue_from_db(r, get_queued_pages_fn=lambda: [])
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1

    def test_enqueues_orphaned_page(self, r: fakeredis.FakeRedis) -> None:
        """Page absent from Redis gets enqueued with retry_count=0."""
        page = _fake_page("orphan-page-1")
        count = rebuild_queue_from_db(r, get_queued_pages_fn=lambda: [page])
        assert count == 1
        assert r.llen(QUEUE_PAGE_TASKS) == 1
        enqueued = PageTask.model_validate_json(r.lrange(QUEUE_PAGE_TASKS, 0, -1)[0])  # type: ignore[index]
        assert enqueued.page_id == "orphan-page-1"
        assert enqueued.retry_count == 0

    def test_skips_page_already_in_main_queue(self, r: fakeredis.FakeRedis) -> None:
        """Page already present in the main queue is not re-enqueued."""
        existing = _task()  # page_id defaults to "page-001"
        enqueue_page_task(r, existing)
        page = _fake_page(existing.page_id)
        count = rebuild_queue_from_db(r, get_queued_pages_fn=lambda: [page])
        assert count == 0
        assert r.llen(QUEUE_PAGE_TASKS) == 1


# ---------------------------------------------------------------------------
# TestBlmoveSafeFallback
# ---------------------------------------------------------------------------


class TestBlmoveSafeFallback:
    def test_fallback_on_response_error(
        self, r: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """blmove raises ResponseError → brpoplpush is used instead."""
        task = _task()
        enqueue_page_task(r, task)

        def _raise_response_error(*args: object, **kwargs: object) -> None:
            raise redis.ResponseError("ERR unknown command 'blmove'")

        monkeypatch.setattr(r, "blmove", _raise_response_error)

        result = _blmove_safe(r, timeout=1.0)
        assert result is not None
        restored = PageTask.model_validate_json(result)
        assert restored.task_id == task.task_id
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1
        assert r.llen(QUEUE_PAGE_TASKS) == 0

    def test_fallback_on_attribute_error(
        self, r: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """blmove raises AttributeError (not present on client) → fallback."""
        task = _task()
        enqueue_page_task(r, task)

        def _raise_attribute_error(*args: object, **kwargs: object) -> None:
            raise AttributeError("'Redis' object has no attribute 'blmove'")

        monkeypatch.setattr(r, "blmove", _raise_attribute_error)

        result = _blmove_safe(r, timeout=1.0)
        assert result is not None
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1

    def test_returns_none_on_empty_queue(self, r: fakeredis.FakeRedis) -> None:
        result = _blmove_safe(r, timeout=0.05)
        assert result is None

    def test_blmove_preferred_when_available(
        self, r: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When blmove works, brpoplpush must NOT be called."""
        task = _task()
        enqueue_page_task(r, task)
        brpoplpush_called = False

        def _track_brpoplpush(*args: object, **kwargs: object) -> object:
            nonlocal brpoplpush_called
            brpoplpush_called = True
            return r.__class__.brpoplpush(r, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(r, "brpoplpush", _track_brpoplpush)
        _blmove_safe(r, timeout=1.0)

        assert not brpoplpush_called
