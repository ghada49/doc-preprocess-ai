"""
tests/test_p1_queue_schema.py
------------------------------
Packet 1.7 validator tests for shared.schemas.queue.

Tests cover queue key constants and the PageTask payload schema.
No Redis connection is required.

Definition of done:
  - All six queue key constants are correct and namespaced under "libraryai:"
  - PageTask validates correctly for all field combinations
  - PageTask serialises/deserialises round-trip as JSON
  - PageTask rejects invalid page_number (< 1) and retry_count (< 0)
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from shared.schemas.queue import (
    QUEUE_DEAD_LETTER,
    QUEUE_PAGE_TASKS,
    QUEUE_PAGE_TASKS_PROCESSING,
    QUEUE_SHADOW_TASKS,
    QUEUE_SHADOW_TASKS_PROCESSING,
    WORKER_SLOTS_KEY,
    PageTask,
)

# ── Key constant helpers ────────────────────────────────────────────────────

ALL_KEYS = [
    QUEUE_PAGE_TASKS,
    QUEUE_PAGE_TASKS_PROCESSING,
    QUEUE_SHADOW_TASKS,
    QUEUE_SHADOW_TASKS_PROCESSING,
    QUEUE_DEAD_LETTER,
    WORKER_SLOTS_KEY,
]

# ── Queue key constants ─────────────────────────────────────────────────────


class TestQueueKeyConstants:
    def test_queue_page_tasks_value(self) -> None:
        assert QUEUE_PAGE_TASKS == "libraryai:page_tasks"

    def test_queue_page_tasks_processing_value(self) -> None:
        assert QUEUE_PAGE_TASKS_PROCESSING == "libraryai:page_tasks:processing"

    def test_queue_shadow_tasks_value(self) -> None:
        assert QUEUE_SHADOW_TASKS == "libraryai:shadow_tasks"

    def test_queue_shadow_tasks_processing_value(self) -> None:
        assert QUEUE_SHADOW_TASKS_PROCESSING == "libraryai:shadow_tasks:processing"

    def test_queue_dead_letter_value(self) -> None:
        assert QUEUE_DEAD_LETTER == "libraryai:page_tasks:dead_letter"

    def test_worker_slots_key_value(self) -> None:
        assert WORKER_SLOTS_KEY == "libraryai:worker_slots"

    @pytest.mark.parametrize("key", ALL_KEYS)
    def test_all_keys_start_with_libraryai(self, key: str) -> None:
        assert key.startswith("libraryai:"), f"Key '{key}' must start with 'libraryai:'"

    def test_all_keys_are_unique(self) -> None:
        assert len(set(ALL_KEYS)) == len(ALL_KEYS)

    def test_processing_key_extends_base_page_tasks(self) -> None:
        assert QUEUE_PAGE_TASKS_PROCESSING.startswith(QUEUE_PAGE_TASKS)

    def test_processing_key_extends_base_shadow_tasks(self) -> None:
        assert QUEUE_SHADOW_TASKS_PROCESSING.startswith(QUEUE_SHADOW_TASKS)

    def test_dead_letter_extends_page_tasks_namespace(self) -> None:
        assert QUEUE_DEAD_LETTER.startswith("libraryai:page_tasks")

    def test_all_keys_are_strings(self) -> None:
        for key in ALL_KEYS:
            assert isinstance(key, str), f"Key {key!r} must be a str"


# ── PageTask schema ─────────────────────────────────────────────────────────


class TestPageTaskCreation:
    def _make(self, **overrides: object) -> PageTask:
        defaults: dict[str, object] = {
            "task_id": "task-uuid-001",
            "job_id": "job-abc",
            "page_id": "page-xyz",
            "page_number": 1,
        }
        defaults.update(overrides)
        return PageTask(**defaults)  # type: ignore[arg-type]

    def test_minimal_valid_task(self) -> None:
        t = self._make()
        assert t.task_id == "task-uuid-001"
        assert t.job_id == "job-abc"
        assert t.page_id == "page-xyz"
        assert t.page_number == 1

    def test_retry_count_defaults_to_zero(self) -> None:
        assert self._make().retry_count == 0

    def test_sub_page_index_defaults_to_none(self) -> None:
        assert self._make().sub_page_index is None

    def test_sub_page_index_zero(self) -> None:
        assert self._make(sub_page_index=0).sub_page_index == 0

    def test_sub_page_index_one(self) -> None:
        assert self._make(sub_page_index=1).sub_page_index == 1

    def test_explicit_retry_count(self) -> None:
        assert self._make(retry_count=2).retry_count == 2

    def test_page_number_one_is_valid(self) -> None:
        t = self._make(page_number=1)
        assert t.page_number == 1

    def test_page_number_large_is_valid(self) -> None:
        t = self._make(page_number=1000)
        assert t.page_number == 1000

    def test_all_fields_set(self) -> None:
        t = self._make(sub_page_index=0, retry_count=3, page_number=7)
        assert t.sub_page_index == 0
        assert t.retry_count == 3
        assert t.page_number == 7


class TestPageTaskValidation:
    def test_page_number_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            PageTask(task_id="t", job_id="j", page_id="p", page_number=0)

    def test_page_number_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            PageTask(task_id="t", job_id="j", page_id="p", page_number=-1)

    def test_retry_count_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            PageTask(task_id="t", job_id="j", page_id="p", page_number=1, retry_count=-1)

    def test_missing_task_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            PageTask(job_id="j", page_id="p", page_number=1)  # type: ignore[call-arg]

    def test_missing_job_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            PageTask(task_id="t", page_id="p", page_number=1)  # type: ignore[call-arg]

    def test_missing_page_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            PageTask(task_id="t", job_id="j", page_number=1)  # type: ignore[call-arg]

    def test_missing_page_number_raises(self) -> None:
        with pytest.raises(ValidationError):
            PageTask(task_id="t", job_id="j", page_id="p")  # type: ignore[call-arg]


class TestPageTaskSerialization:
    def _task(self) -> PageTask:
        return PageTask(
            task_id="tid-001",
            job_id="job-001",
            page_id="page-001",
            page_number=3,
            sub_page_index=1,
            retry_count=0,
        )

    def test_model_dump_contains_all_fields(self) -> None:
        d = self._task().model_dump()
        assert "task_id" in d
        assert "job_id" in d
        assert "page_id" in d
        assert "page_number" in d
        assert "sub_page_index" in d
        assert "retry_count" in d

    def test_json_round_trip(self) -> None:
        original = self._task()
        json_str = original.model_dump_json()
        restored = PageTask.model_validate_json(json_str)
        assert restored == original

    def test_dict_round_trip(self) -> None:
        original = self._task()
        d = original.model_dump()
        restored = PageTask.model_validate(d)
        assert restored == original

    def test_json_is_valid_json_string(self) -> None:
        json_str = self._task().model_dump_json()
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)

    def test_json_contains_expected_values(self) -> None:
        d = json.loads(self._task().model_dump_json())
        assert d["task_id"] == "tid-001"
        assert d["job_id"] == "job-001"
        assert d["page_id"] == "page-001"
        assert d["page_number"] == 3
        assert d["sub_page_index"] == 1
        assert d["retry_count"] == 0

    def test_null_sub_page_index_serialises(self) -> None:
        t = PageTask(task_id="t", job_id="j", page_id="p", page_number=1)
        d = json.loads(t.model_dump_json())
        assert d["sub_page_index"] is None

    def test_round_trip_with_none_sub_page_index(self) -> None:
        t = PageTask(task_id="t", job_id="j", page_id="p", page_number=1)
        assert PageTask.model_validate_json(t.model_dump_json()) == t
