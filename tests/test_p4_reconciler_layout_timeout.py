from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from services.eep.app.db.models import JobPage
from services.eep_recovery.app.reconciler import ReconcilerConfig, reconcile_once
from shared.schemas.queue import PageTask, QUEUE_PAGE_TASKS


def _make_page(*, status: str, age_seconds: float) -> MagicMock:
    page = MagicMock(spec=JobPage)
    page.status = status
    page.status_updated_at = datetime.now(tz=timezone.utc) - timedelta(seconds=age_seconds)
    page.created_at = page.status_updated_at
    return page


def _make_redis(task_json: str) -> MagicMock:
    r = MagicMock()
    r.lrange.return_value = [task_json]
    r.llen.return_value = 0
    pipe = MagicMock()
    pipe.execute.return_value = None
    r.pipeline.return_value = pipe
    return r


def test_layout_detection_uses_shorter_stale_timeout() -> None:
    task = PageTask(
        task_id="task-layout",
        job_id="job-layout",
        page_id="page-layout",
        page_number=1,
        retry_count=0,
    )
    r = _make_redis(task.model_dump_json())
    session = MagicMock()
    session.get.return_value = _make_page(status="layout_detection", age_seconds=181)

    result = reconcile_once(
        r,
        session,
        ReconcilerConfig(task_timeout_seconds=900.0, layout_task_timeout_seconds=180.0),
    )

    assert result.requeued_stale == 1
    pipe = r.pipeline.return_value
    pipe.lpush.assert_called_once()
    assert pipe.lpush.call_args[0][0] == QUEUE_PAGE_TASKS


def test_non_layout_active_states_keep_generic_timeout() -> None:
    task = PageTask(
        task_id="task-pre",
        job_id="job-pre",
        page_id="page-pre",
        page_number=1,
        retry_count=0,
    )
    r = _make_redis(task.model_dump_json())
    session = MagicMock()
    session.get.return_value = _make_page(status="preprocessing", age_seconds=181)

    result = reconcile_once(
        r,
        session,
        ReconcilerConfig(task_timeout_seconds=900.0, layout_task_timeout_seconds=180.0),
    )

    assert result.skipped_active == 1
    assert result.requeued_stale == 0
