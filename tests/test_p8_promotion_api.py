"""
tests/test_p8_promotion_api.py
--------------------------------
Packet 8.3 contract tests for:
  POST /v1/models/promote
  POST /v1/models/rollback

Tests cover:
  Auth guards (401 / 403) for both endpoints
  Promote:
    - 404 when no staging candidate
    - 409 when gate_results is None (no evaluation)
    - 409 when gate_results has a failing gate
    - 200 when all gates pass
    - 200 with force=true even when gates fail
    - force=true appends force note to notes field
    - current production model is demoted to archived
    - ModelVersionRecord schema fields are correct
    - Redis publish is called (best-effort; never fails the request)
    - Redis error does not cause promote to fail
    - 400 on invalid service name
  Rollback:
    - 404 when no archived version
    - 200 manual rollback always succeeds (no window check)
    - 409 automated rollback when promoted_at > 2h ago
    - 200 automated rollback when promoted_at < 2h ago
    - 409 automated rollback when current_prod has no promoted_at
    - archived model stage transitions to production
    - current production model transitions to archived
    - ModelVersionRecord schema fields are correct
    - Redis publish is called (best-effort)
    - 400 on invalid service name

Uses a mini FastAPI app with only the promotion router.  DB and Redis
interactions are mocked via dependency overrides.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.eep.app.auth import CurrentUser, create_access_token, require_admin
from services.eep.app.db.session import get_session
from services.eep.app.promotion_api import router
from services.eep.app.redis_client import get_redis

# ── Constants / helpers ────────────────────────────────────────────────────────

_NOW = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)
_ADMIN_USER = CurrentUser(user_id="admin-001", role="admin")

_ALL_GATES_PASS = {
    "geometry_iou": {"pass": True, "value": 0.85},
    "split_precision": {"pass": True, "value": 0.78},
    "structural_agreement_rate": {"pass": True, "value": 0.72},
    "golden_dataset": {"pass": True, "regressions": 0},
    "latency_p95": {"pass": True, "value": 2.1},
}

_ONE_GATE_FAILS = {
    "geometry_iou": {"pass": True, "value": 0.85},
    "split_precision": {"pass": False, "value": 0.68},
    "structural_agreement_rate": {"pass": True, "value": 0.72},
    "golden_dataset": {"pass": True, "regressions": 0},
    "latency_p95": {"pass": False, "value": 3.9},
}


def _make_mv(
    model_id: str = "mv-001",
    service_name: str = "iep1a",
    version_tag: str = "v1.2.0",
    stage: str = "staging",
    gate_results: Any = None,
    promoted_at: datetime | None = None,
    notes: str | None = None,
    mlflow_run_id: str | None = None,
    dataset_version: str | None = None,
    created_at: datetime = _NOW,
) -> MagicMock:
    mv = MagicMock()
    mv.model_id = model_id
    mv.service_name = service_name
    mv.version_tag = version_tag
    mv.stage = stage
    mv.gate_results = gate_results
    mv.promoted_at = promoted_at
    mv.notes = notes
    mv.mlflow_run_id = mlflow_run_id
    mv.dataset_version = dataset_version
    mv.created_at = created_at
    return mv


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def mini_app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture()
def inject_admin(mini_app: FastAPI):
    """Inject mock session, mock Redis, and bypass require_admin."""

    def _setup(session: MagicMock, r: MagicMock | None = None) -> TestClient:
        if r is None:
            r = MagicMock()
        mini_app.dependency_overrides[get_session] = lambda: session
        mini_app.dependency_overrides[get_redis] = lambda: r
        mini_app.dependency_overrides[require_admin] = lambda: _ADMIN_USER
        return TestClient(mini_app, raise_server_exceptions=False)

    yield _setup
    mini_app.dependency_overrides.pop(get_session, None)
    mini_app.dependency_overrides.pop(get_redis, None)
    mini_app.dependency_overrides.pop(require_admin, None)


def _session_for_promote(
    staging: MagicMock | None = None,
    production: MagicMock | None = None,
) -> MagicMock:
    """Build a mock session for promote tests."""
    session = MagicMock()

    def _query(model):
        q = MagicMock()

        def _filter(*args):
            # Distinguish staging vs production via stored state on session mock
            f = MagicMock()
            f.order_by.return_value.first.return_value = staging
            f.first.return_value = production
            return f

        q.filter = _filter
        return q

    session.query = _query
    session.commit = MagicMock()
    session.refresh = MagicMock()
    return session


def _session_for_rollback(
    production: MagicMock | None,
    archived: MagicMock | None,
) -> MagicMock:
    """Build a mock session for rollback tests."""
    session = MagicMock()
    call_count = [0]

    def _query(model):
        q = MagicMock()

        def _filter(*args):
            f = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                # _current_production
                f.first.return_value = production
            else:
                # _latest_archived
                f.order_by.return_value.first.return_value = archived
            return f

        q.filter = _filter
        return q

    session.query = _query
    session.commit = MagicMock()
    session.refresh = MagicMock()
    return session


# ── Auth guards ────────────────────────────────────────────────────────────────


class TestAuthGuards:
    @pytest.fixture(autouse=True)
    def _clean(self, mini_app: FastAPI) -> None:
        mini_app.dependency_overrides.clear()

    def test_promote_401_no_token(self, mini_app: FastAPI) -> None:
        client = TestClient(mini_app, raise_server_exceptions=False)
        resp = client.post("/v1/models/promote", json={"service": "iep1a"})
        assert resp.status_code == 401

    def test_rollback_401_no_token(self, mini_app: FastAPI) -> None:
        client = TestClient(mini_app, raise_server_exceptions=False)
        resp = client.post("/v1/models/rollback", json={"service": "iep1a"})
        assert resp.status_code == 401

    def test_promote_403_user_role(self, mini_app: FastAPI) -> None:
        r = MagicMock()
        mini_app.dependency_overrides[get_session] = lambda: MagicMock()
        mini_app.dependency_overrides[get_redis] = lambda: r
        client = TestClient(mini_app, raise_server_exceptions=False)
        token = create_access_token(user_id="u1", role="user")
        try:
            resp = client.post(
                "/v1/models/promote",
                json={"service": "iep1a"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 403
        finally:
            mini_app.dependency_overrides.pop(get_session, None)
            mini_app.dependency_overrides.pop(get_redis, None)

    def test_rollback_403_user_role(self, mini_app: FastAPI) -> None:
        r = MagicMock()
        mini_app.dependency_overrides[get_session] = lambda: MagicMock()
        mini_app.dependency_overrides[get_redis] = lambda: r
        client = TestClient(mini_app, raise_server_exceptions=False)
        token = create_access_token(user_id="u1", role="user")
        try:
            resp = client.post(
                "/v1/models/rollback",
                json={"service": "iep1a"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 403
        finally:
            mini_app.dependency_overrides.pop(get_session, None)
            mini_app.dependency_overrides.pop(get_redis, None)


# ── POST /v1/models/promote ────────────────────────────────────────────────────


class TestPromote:
    def test_400_invalid_service(self, inject_admin) -> None:
        session = _session_for_promote()
        client = inject_admin(session)
        resp = client.post("/v1/models/promote", json={"service": "iep2a"})
        assert resp.status_code == 422  # Pydantic validator rejects invalid service

    def test_404_no_staging_candidate(self, inject_admin) -> None:
        session = _session_for_promote(staging=None, production=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/promote", json={"service": "iep1a"})
        assert resp.status_code == 404
        assert "staging" in resp.json()["detail"].lower()

    def test_409_gate_results_none(self, inject_admin) -> None:
        staging = _make_mv(gate_results=None)
        session = _session_for_promote(staging=staging, production=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/promote", json={"service": "iep1a", "force": False})
        assert resp.status_code == 409
        assert "gate check failed" in resp.json()["detail"].lower()

    def test_409_gate_results_empty_dict(self, inject_admin) -> None:
        staging = _make_mv(gate_results={})
        session = _session_for_promote(staging=staging, production=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/promote", json={"service": "iep1a"})
        assert resp.status_code == 409

    def test_409_some_gates_fail(self, inject_admin) -> None:
        staging = _make_mv(gate_results=_ONE_GATE_FAILS)
        session = _session_for_promote(staging=staging, production=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/promote", json={"service": "iep1a"})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "split_precision" in detail
        assert "latency_p95" in detail

    def test_200_all_gates_pass(self, inject_admin) -> None:
        staging = _make_mv(gate_results=_ALL_GATES_PASS)
        session = _session_for_promote(staging=staging, production=None)
        r = MagicMock()
        client = inject_admin(session, r)
        resp = client.post("/v1/models/promote", json={"service": "iep1a"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["stage"] == "production"
        assert data["version_tag"] == "v1.2.0"

    def test_200_force_true_bypasses_gate_failure(self, inject_admin) -> None:
        staging = _make_mv(gate_results=_ONE_GATE_FAILS)
        session = _session_for_promote(staging=staging, production=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/promote", json={"service": "iep1a", "force": True})
        assert resp.status_code == 200

    def test_force_true_appends_note(self, inject_admin) -> None:
        staging = _make_mv(gate_results=_ONE_GATE_FAILS, notes="baseline note")
        session = _session_for_promote(staging=staging, production=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/promote", json={"service": "iep1a", "force": True})
        assert resp.status_code == 200
        # The notes field on the ORM object should have been appended
        assert "force-promoted" in str(staging.notes)

    def test_current_production_demoted_to_archived(self, inject_admin) -> None:
        staging = _make_mv(gate_results=_ALL_GATES_PASS)
        production = _make_mv(model_id="mv-old", stage="production")
        session = _session_for_promote(staging=staging, production=production)
        client = inject_admin(session)
        client.post("/v1/models/promote", json={"service": "iep1a"})
        assert production.stage == "archived"

    def test_no_error_when_no_current_production(self, inject_admin) -> None:
        staging = _make_mv(gate_results=_ALL_GATES_PASS)
        session = _session_for_promote(staging=staging, production=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/promote", json={"service": "iep1a"})
        assert resp.status_code == 200

    def test_redis_publish_called(self, inject_admin) -> None:
        staging = _make_mv(gate_results=_ALL_GATES_PASS)
        session = _session_for_promote(staging=staging, production=None)
        r = MagicMock()
        client = inject_admin(session, r)
        client.post("/v1/models/promote", json={"service": "iep1a"})
        r.publish.assert_called_once_with("libraryai:model_reload:iep1a", staging.version_tag)

    def test_redis_error_does_not_fail_promote(self, inject_admin) -> None:
        import redis as redis_lib
        staging = _make_mv(gate_results=_ALL_GATES_PASS)
        session = _session_for_promote(staging=staging, production=None)
        r = MagicMock()
        r.publish.side_effect = redis_lib.RedisError("connection refused")
        client = inject_admin(session, r)
        resp = client.post("/v1/models/promote", json={"service": "iep1a"})
        # Redis failure must not cause promote to fail
        assert resp.status_code == 200

    def test_response_schema_fields(self, inject_admin) -> None:
        staging = _make_mv(gate_results=_ALL_GATES_PASS)
        session = _session_for_promote(staging=staging, production=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/promote", json={"service": "iep1a"})
        assert resp.status_code == 200
        data = resp.json()
        expected_fields = {
            "model_id", "service_name", "version_tag", "stage",
            "gate_results", "promoted_at", "notes", "mlflow_run_id",
            "dataset_version", "created_at",
        }
        assert set(data.keys()) == expected_fields

    def test_iep1b_service_accepted(self, inject_admin) -> None:
        staging = _make_mv(service_name="iep1b", gate_results=_ALL_GATES_PASS)
        session = _session_for_promote(staging=staging, production=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/promote", json={"service": "iep1b"})
        assert resp.status_code == 200


# ── POST /v1/models/rollback ───────────────────────────────────────────────────


class TestRollback:
    def test_400_invalid_service(self, inject_admin) -> None:
        session = _session_for_rollback(production=None, archived=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/rollback", json={"service": "iep2b"})
        assert resp.status_code == 422

    def test_404_no_archived_version(self, inject_admin) -> None:
        production = _make_mv(stage="production")
        session = _session_for_rollback(production=production, archived=None)
        client = inject_admin(session)
        resp = client.post("/v1/models/rollback", json={"service": "iep1a", "reason": "manual"})
        assert resp.status_code == 404
        assert "archived" in resp.json()["detail"].lower()

    def test_200_manual_rollback_no_window_check(self, inject_admin) -> None:
        # promoted_at far in the past — manual should still work
        old_promoted = _NOW - timedelta(hours=48)
        production = _make_mv(stage="production", promoted_at=old_promoted)
        archived = _make_mv(model_id="mv-archived", stage="archived", version_tag="v1.0.0")
        session = _session_for_rollback(production=production, archived=archived)
        client = inject_admin(session)
        resp = client.post("/v1/models/rollback", json={"service": "iep1a", "reason": "manual"})
        assert resp.status_code == 200
        assert resp.json()["version_tag"] == "v1.0.0"

    def test_409_automated_rollback_window_expired(self, inject_admin) -> None:
        # promoted_at > 2h ago → automated rollback blocked
        old_promoted = _NOW - timedelta(hours=3)
        production = _make_mv(stage="production", promoted_at=old_promoted)
        archived = _make_mv(model_id="mv-archived", stage="archived")
        session = _session_for_rollback(production=production, archived=archived)

        with patch("services.eep.app.promotion_api.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            client = inject_admin(session)
            resp = client.post(
                "/v1/models/rollback",
                json={"service": "iep1a", "reason": "PostPromotionAcceptRateCollapse"},
            )
        assert resp.status_code == 409
        assert "window" in resp.json()["detail"].lower()

    def test_200_automated_rollback_within_window(self, inject_admin) -> None:
        # promoted_at < 2h ago → automated rollback allowed
        recent_promoted = _NOW - timedelta(hours=1)
        production = _make_mv(stage="production", promoted_at=recent_promoted)
        archived = _make_mv(model_id="mv-archived", stage="archived", version_tag="v0.9.0")
        session = _session_for_rollback(production=production, archived=archived)

        with patch("services.eep.app.promotion_api.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            client = inject_admin(session)
            resp = client.post(
                "/v1/models/rollback",
                json={"service": "iep1a", "reason": "PostPromotionAcceptRateCollapse"},
            )
        assert resp.status_code == 200

    def test_409_automated_rollback_no_promoted_at(self, inject_admin) -> None:
        production = _make_mv(stage="production", promoted_at=None)
        archived = _make_mv(model_id="mv-archived", stage="archived")
        session = _session_for_rollback(production=production, archived=archived)
        resp_client = inject_admin(session)
        resp = resp_client.post(
            "/v1/models/rollback",
            json={"service": "iep1a", "reason": "automated"},
        )
        assert resp.status_code == 409

    def test_archived_transitions_to_production(self, inject_admin) -> None:
        production = _make_mv(stage="production", promoted_at=_NOW - timedelta(minutes=1))
        archived = _make_mv(model_id="mv-archived", stage="archived")
        session = _session_for_rollback(production=production, archived=archived)
        client = inject_admin(session)
        client.post("/v1/models/rollback", json={"service": "iep1a", "reason": "manual"})
        assert archived.stage == "production"

    def test_current_production_transitions_to_archived(self, inject_admin) -> None:
        production = _make_mv(model_id="mv-prod", stage="production", promoted_at=_NOW)
        archived = _make_mv(model_id="mv-archived", stage="archived")
        session = _session_for_rollback(production=production, archived=archived)
        client = inject_admin(session)
        client.post("/v1/models/rollback", json={"service": "iep1a", "reason": "manual"})
        assert production.stage == "archived"

    def test_rollback_ok_when_no_current_production(self, inject_admin) -> None:
        archived = _make_mv(model_id="mv-archived", stage="archived")
        session = _session_for_rollback(production=None, archived=archived)
        client = inject_admin(session)
        resp = client.post("/v1/models/rollback", json={"service": "iep1a", "reason": "manual"})
        assert resp.status_code == 200

    def test_redis_publish_called_on_rollback(self, inject_admin) -> None:
        production = _make_mv(stage="production", promoted_at=_NOW)
        archived = _make_mv(model_id="mv-arch", stage="archived", version_tag="v0.8.0")
        session = _session_for_rollback(production=production, archived=archived)
        r = MagicMock()
        client = inject_admin(session, r)
        client.post("/v1/models/rollback", json={"service": "iep1a", "reason": "manual"})
        r.publish.assert_called_once_with("libraryai:model_reload:iep1a", archived.version_tag)

    def test_redis_error_does_not_fail_rollback(self, inject_admin) -> None:
        import redis as redis_lib
        production = _make_mv(stage="production", promoted_at=_NOW)
        archived = _make_mv(model_id="mv-arch", stage="archived")
        session = _session_for_rollback(production=production, archived=archived)
        r = MagicMock()
        r.publish.side_effect = redis_lib.RedisError("down")
        client = inject_admin(session, r)
        resp = client.post("/v1/models/rollback", json={"service": "iep1a", "reason": "manual"})
        assert resp.status_code == 200

    def test_response_schema_fields(self, inject_admin) -> None:
        production = _make_mv(stage="production", promoted_at=_NOW)
        archived = _make_mv(model_id="mv-arch", stage="archived")
        session = _session_for_rollback(production=production, archived=archived)
        client = inject_admin(session)
        resp = client.post("/v1/models/rollback", json={"service": "iep1a", "reason": "manual"})
        assert resp.status_code == 200
        expected = {
            "model_id", "service_name", "version_tag", "stage",
            "gate_results", "promoted_at", "notes", "mlflow_run_id",
            "dataset_version", "created_at",
        }
        assert set(resp.json().keys()) == expected
