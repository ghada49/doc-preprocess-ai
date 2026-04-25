from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace
from typing import Any

from services.eep.app.db.models import ShadowEvaluation
from services.eep_worker.app import worker_loop
from services.shadow_worker.app import main as shadow_main
from shared.schemas.queue import ShadowTask

_MIGRATION_PATH = (
    pathlib.Path(__file__).parent.parent
    / "services"
    / "eep"
    / "migrations"
    / "versions"
    / "0008_shadow_evaluations.py"
)


def _load_migration() -> object:
    spec = importlib.util.spec_from_file_location("migration_0008", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestShadowEvaluationMigration:
    def test_revision_chain(self) -> None:
        mod = _load_migration()
        assert getattr(mod, "revision", None) == "0008"
        assert getattr(mod, "down_revision", None) == "0007"

    def test_shadow_evaluations_table_sql_present(self) -> None:
        src = _MIGRATION_PATH.read_text(encoding="utf-8")
        assert "CREATE TABLE shadow_evaluations" in src
        assert "idx_shadow_evaluations_job" in src
        assert "idx_shadow_evaluations_status" in src
        for status in ["'pending'", "'completed'", "'failed'", "'no_shadow_model'"]:
            assert status in src


class TestShadowEvaluationModel:
    def test_table_has_expected_indexes(self) -> None:
        indexes = {idx.name for idx in ShadowEvaluation.__table__.indexes}
        assert "idx_shadow_evaluations_job" in indexes
        assert "idx_shadow_evaluations_status" in indexes

    def test_table_has_expected_columns(self) -> None:
        cols = {col.name for col in ShadowEvaluation.__table__.columns}
        for col in [
            "eval_id",
            "job_id",
            "page_id",
            "page_status",
            "confidence_delta",
            "status",
            "created_at",
            "completed_at",
        ]:
            assert col in cols


class _FakeQuery:
    def __init__(self, first_result: object) -> None:
        self._first_result = first_result

    def filter_by(self, **kwargs: object) -> _FakeQuery:
        return self

    def filter(self, *args: object) -> _FakeQuery:
        return self

    def order_by(self, *args: object) -> _FakeQuery:
        return self

    def first(self) -> object:
        return self._first_result


class _WorkerLoopSession:
    def __init__(
        self,
        *,
        job: object,
        page: object,
        existing_eval: ShadowEvaluation | None = None,
    ) -> None:
        self._job = job
        self._page = page
        self._existing_eval = existing_eval
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    def get(self, model: object, identifier: str) -> object | None:
        if model is worker_loop.Job:
            return self._job
        if model is worker_loop.JobPage:
            return self._page
        return None

    def query(self, model: object) -> _FakeQuery:
        assert model is worker_loop.ShadowEvaluation
        return _FakeQuery(self._existing_eval)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


class _RedisRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def lpush(self, key: str, value: str) -> None:
        self.calls.append((key, value))


class TestShadowTaskReservation:
    def test_enqueue_reserves_pending_eval_and_links_lineage(
        self,
        monkeypatch: Any,
    ) -> None:
        job = SimpleNamespace(job_id="job-1", shadow_mode=True)
        page = SimpleNamespace(
            page_id="page-1",
            job_id="job-1",
            page_number=3,
            sub_page_index=None,
            status="accepted",
        )
        lineage = SimpleNamespace(shadow_eval_id=None)
        session = _WorkerLoopSession(job=job, page=page)
        redis_client = _RedisRecorder()

        monkeypatch.setattr(worker_loop, "_find_lineage", lambda *args, **kwargs: lineage)

        worker_loop._maybe_enqueue_shadow_task(
            redis_client=redis_client,
            job_id="job-1",
            page_id="page-1",
            page_number=3,
            session_factory=lambda: session,
        )

        assert session.commits == 1
        assert len(session.added) == 1
        reserved_eval = session.added[0]
        assert isinstance(reserved_eval, ShadowEvaluation)
        assert reserved_eval.status == "pending"
        assert lineage.shadow_eval_id == reserved_eval.eval_id
        assert len(redis_client.calls) == 1
        queued_task = ShadowTask.model_validate_json(redis_client.calls[0][1])
        assert queued_task.task_id == reserved_eval.eval_id
        assert queued_task.page_status == "accepted"

    def test_enqueue_skips_already_reserved_lineage(
        self,
        monkeypatch: Any,
    ) -> None:
        job = SimpleNamespace(job_id="job-1", shadow_mode=True)
        page = SimpleNamespace(
            page_id="page-1",
            job_id="job-1",
            page_number=3,
            sub_page_index=None,
            status="accepted",
        )
        lineage = SimpleNamespace(shadow_eval_id="existing-eval")
        session = _WorkerLoopSession(job=job, page=page)
        redis_client = _RedisRecorder()

        monkeypatch.setattr(worker_loop, "_find_lineage", lambda *args, **kwargs: lineage)

        worker_loop._maybe_enqueue_shadow_task(
            redis_client=redis_client,
            job_id="job-1",
            page_id="page-1",
            page_number=3,
            session_factory=lambda: session,
        )

        assert session.added == []
        assert session.commits == 0
        assert redis_client.calls == []


class _ShadowWorkerSession:
    def __init__(
        self,
        *,
        job: object,
        page: object,
        evaluation: ShadowEvaluation,
        shadow_model: object | None,
    ) -> None:
        self._job = job
        self._page = page
        self._evaluation = evaluation
        self._shadow_model = shadow_model
        self.commits = 0

    def get(self, model: object, identifier: str) -> object | None:
        if model is shadow_main.Job:
            return self._job
        if model is shadow_main.JobPage:
            return self._page
        if model is shadow_main.ShadowEvaluation:
            return self._evaluation
        return None

    def query(self, model: object) -> _FakeQuery:
        assert model is shadow_main.ModelVersion
        return _FakeQuery(self._shadow_model)

    def add(self, obj: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


class TestShadowWorkerProcessing:
    def test_process_marks_no_shadow_model_when_candidate_missing(
        self,
        monkeypatch: Any,
    ) -> None:
        task = ShadowTask(
            task_id="eval-1",
            job_id="job-1",
            page_id="page-1",
            page_number=3,
            page_status="accepted",
        )
        evaluation = ShadowEvaluation(
            eval_id="eval-1",
            job_id="job-1",
            page_id="page-1",
            page_status="accepted",
            status="pending",
        )
        job = SimpleNamespace(job_id="job-1", shadow_mode=True)
        page = SimpleNamespace(
            page_id="page-1",
            job_id="job-1",
            page_number=3,
            sub_page_index=None,
            status="accepted",
        )
        lineage = SimpleNamespace(shadow_eval_id=None)
        session = _ShadowWorkerSession(
            job=job,
            page=page,
            evaluation=evaluation,
            shadow_model=None,
        )

        monkeypatch.setattr(
            shadow_main,
            "_find_lineage_for_page",
            lambda db, resolved_page: lineage,
        )

        shadow_main._process_shadow_task(task, session)

        assert evaluation.status == "no_shadow_model"
        assert evaluation.completed_at is not None
        assert lineage.shadow_eval_id == "eval-1"
        assert session.commits == 1
