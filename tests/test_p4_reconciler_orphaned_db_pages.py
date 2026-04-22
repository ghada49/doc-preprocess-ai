from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from services.eep.app.db.models import JobPage
from services.eep_recovery.app.reconciler import ReconcilerConfig, reconcile_once
from shared.schemas.queue import QUEUE_DEAD_LETTER, QUEUE_PAGE_TASKS, QUEUE_PAGE_TASKS_PROCESSING, PageTask


def _make_page(*, page_id: str, status: str, sub_page_index: int | None = None) -> MagicMock:
    page = MagicMock(spec=JobPage)
    page.page_id = page_id
    page.job_id = "job-1"
    page.page_number = 1
    page.sub_page_index = sub_page_index
    page.status = status
    page.status_updated_at = datetime.now(tz=timezone.utc)
    page.created_at = page.status_updated_at
    return page


def _make_redis(*, queued: list[str] | None = None, processing: list[str] | None = None, dead: list[str] | None = None) -> MagicMock:
    r = MagicMock()

    def _lrange(key: str, _start: int, _end: int) -> list[str]:
        mapping = {
            QUEUE_PAGE_TASKS: queued or [],
            QUEUE_PAGE_TASKS_PROCESSING: processing or [],
            QUEUE_DEAD_LETTER: dead or [],
        }
        return mapping.get(key, [])

    r.lrange.side_effect = _lrange
    r.llen.return_value = len(dead or [])
    return r


def test_reconciler_requeues_orphaned_layout_detection_page_when_redis_is_empty() -> None:
    r = _make_redis()
    orphan = _make_page(page_id="page-layout", status="layout_detection", sub_page_index=1)
    session = MagicMock()
    session.get.return_value = None
    session.query.return_value.filter.return_value.all.return_value = [orphan]

    result = reconcile_once(
        r,
        session,
        ReconcilerConfig(task_timeout_seconds=900.0, layout_task_timeout_seconds=180.0),
    )

    assert result.processing_list_size == 0
    assert result.requeued_orphaned == 1
    r.lpush.assert_called_once()
    queue_name, payload = r.lpush.call_args[0]
    assert queue_name == QUEUE_PAGE_TASKS
    task = PageTask.model_validate_json(payload)
    assert task.page_id == orphan.page_id
    assert task.sub_page_index == orphan.sub_page_index
    assert task.retry_count == 0


def test_reconciler_requeues_orphaned_semantic_norm_page_when_redis_is_empty() -> None:
    r = _make_redis()
    orphan = _make_page(page_id="page-semantic", status="semantic_norm", sub_page_index=0)
    session = MagicMock()
    session.get.return_value = None
    session.query.return_value.filter.return_value.all.return_value = [orphan]

    result = reconcile_once(r, session, ReconcilerConfig())

    assert result.processing_list_size == 0
    assert result.requeued_orphaned == 1
    r.lpush.assert_called_once()
    queue_name, payload = r.lpush.call_args[0]
    assert queue_name == QUEUE_PAGE_TASKS
    task = PageTask.model_validate_json(payload)
    assert task.page_id == orphan.page_id
    assert task.sub_page_index == orphan.sub_page_index
    assert task.retry_count == 0


def test_reconciler_does_not_duplicate_page_already_present_in_main_queue() -> None:
    existing = PageTask(
        task_id="task-existing",
        job_id="job-1",
        page_id="page-layout",
        page_number=1,
        sub_page_index=0,
        retry_count=0,
    ).model_dump_json()
    r = _make_redis(queued=[existing])
    orphan = _make_page(page_id="page-layout", status="layout_detection", sub_page_index=0)
    session = MagicMock()
    session.get.return_value = None
    session.query.return_value.filter.return_value.all.return_value = [orphan]

    result = reconcile_once(r, session, ReconcilerConfig())

    assert result.requeued_orphaned == 0
    r.lpush.assert_not_called()
