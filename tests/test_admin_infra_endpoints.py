"""
tests/test_admin_infra_endpoints.py
-------------------------------------
Contract tests for the new admin infrastructure endpoints:
  GET /v1/admin/queue-status
  GET /v1/admin/service-inventory
  GET /v1/admin/deployment-status

Tests cover:
  - HTTP 200 with correct schema for all three endpoints
  - 401 when no bearer token supplied
  - 403 when a non-admin user calls the endpoints
  - Correct field values for queue-status (Redis LLEN / GET)
  - service-inventory: all catalog services present, health_signal shape
  - deployment-status: feature flags read from env vars
  - feature_flags.artifact_cleanup is always "disabled"
  - retraining_mode and golden_eval_mode reflect env vars honestly
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.eep.app.admin.infra import router
from services.eep.app.auth import CurrentUser, create_access_token, require_admin
from services.eep.app.db.session import get_session
from services.eep.app.redis_client import get_redis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bearer(user_id: str, role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


def _make_redis(
    page_tasks: int = 3,
    page_processing: int = 2,
    dead_letter: int = 1,
    shadow_tasks: int = 0,
    shadow_processing: int = 0,
    slots: str | None = "18",
) -> MagicMock:
    r = MagicMock()

    _llen_map: dict[str, int] = {}

    def _llen(key: str) -> int:
        return _llen_map.get(key, 0)

    from shared.schemas.queue import (
        QUEUE_DEAD_LETTER,
        QUEUE_PAGE_TASKS,
        QUEUE_PAGE_TASKS_PROCESSING,
        QUEUE_SHADOW_TASKS,
        QUEUE_SHADOW_TASKS_PROCESSING,
    )

    _llen_map[QUEUE_PAGE_TASKS] = page_tasks
    _llen_map[QUEUE_PAGE_TASKS_PROCESSING] = page_processing
    _llen_map[QUEUE_DEAD_LETTER] = dead_letter
    _llen_map[QUEUE_SHADOW_TASKS] = shadow_tasks
    _llen_map[QUEUE_SHADOW_TASKS_PROCESSING] = shadow_processing

    r.llen.side_effect = _llen
    r.get.return_value = slots
    return r


def _make_session_no_invocations() -> MagicMock:
    """Mock session that returns 0 for all scalar queries and None for first()."""
    session = MagicMock(spec=Session)
    chain = MagicMock()
    session.query.return_value = chain
    chain.filter.return_value = chain
    chain.with_entities.return_value = chain
    chain.isnot.return_value = chain
    chain.is_.return_value = chain
    chain.ilike.return_value = chain
    chain.asc.return_value = chain
    chain.scalar.return_value = 0
    chain.count.return_value = 0
    chain.order_by.return_value = chain
    chain.first.return_value = None

    # For text() query (alembic_version)
    result = MagicMock()
    result.fetchone.return_value = ("abc1234",)
    session.execute.return_value = result
    return session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mini_app() -> FastAPI:
    """Mini app with only the infra router — real auth dependency."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture()
def admin_client(mini_app: FastAPI):
    """Yield an admin-authenticated TestClient with DB and Redis mocked."""

    def _setup(session: Session, redis: Any) -> TestClient:
        mini_app.dependency_overrides[get_session] = lambda: session
        mini_app.dependency_overrides[get_redis] = lambda: redis
        mini_app.dependency_overrides[require_admin] = lambda: CurrentUser(
            user_id="admin-001", role="admin"
        )
        return TestClient(mini_app, raise_server_exceptions=False)

    yield _setup
    mini_app.dependency_overrides.pop(get_session, None)
    mini_app.dependency_overrides.pop(get_redis, None)
    mini_app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# Auth enforcement (shared across all three endpoints)
# ---------------------------------------------------------------------------


class TestInfraEndpointAuth:
    _ENDPOINTS = [
        "/v1/admin/queue-status",
        "/v1/admin/service-inventory",
        "/v1/admin/deployment-status",
    ]

    @pytest.mark.parametrize("path", _ENDPOINTS)
    def test_401_no_token(self, mini_app: FastAPI, path: str) -> None:
        mini_app.dependency_overrides[get_session] = lambda: _make_session_no_invocations()
        mini_app.dependency_overrides[get_redis] = lambda: _make_redis()
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get(path)
        assert r.status_code == 401
        mini_app.dependency_overrides.pop(get_session, None)
        mini_app.dependency_overrides.pop(get_redis, None)

    @pytest.mark.parametrize("path", _ENDPOINTS)
    def test_403_non_admin(self, mini_app: FastAPI, path: str) -> None:
        mini_app.dependency_overrides[get_session] = lambda: _make_session_no_invocations()
        mini_app.dependency_overrides[get_redis] = lambda: _make_redis()
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get(path, headers=_bearer("user-001", role="user"))
        assert r.status_code == 403
        mini_app.dependency_overrides.pop(get_session, None)
        mini_app.dependency_overrides.pop(get_redis, None)


# ---------------------------------------------------------------------------
# GET /v1/admin/queue-status
# ---------------------------------------------------------------------------


class TestQueueStatus:
    def test_200_schema(self, admin_client: Any) -> None:
        redis = _make_redis(page_tasks=5, page_processing=2, dead_letter=1, slots="17")
        client = admin_client(_make_session_no_invocations(), redis)
        r = client.get("/v1/admin/queue-status")
        assert r.status_code == 200
        body = r.json()
        assert body["page_tasks_queued"] == 5
        assert body["page_tasks_processing"] == 2
        assert body["page_tasks_dead_letter"] == 1
        assert body["shadow_tasks_queued"] == 0
        assert body["shadow_tasks_processing"] == 0
        assert body["worker_slots_available"] == 17
        assert body["worker_slots_max"] > 0
        assert "as_of" in body

    def test_worker_slots_none_when_key_missing(self, admin_client: Any) -> None:
        redis = _make_redis(slots=None)
        client = admin_client(_make_session_no_invocations(), redis)
        r = client.get("/v1/admin/queue-status")
        assert r.status_code == 200
        assert r.json()["worker_slots_available"] is None

    def test_dead_letter_zero(self, admin_client: Any) -> None:
        redis = _make_redis(dead_letter=0)
        client = admin_client(_make_session_no_invocations(), redis)
        r = client.get("/v1/admin/queue-status")
        assert r.status_code == 200
        assert r.json()["page_tasks_dead_letter"] == 0

    def test_dead_letter_nonzero(self, admin_client: Any) -> None:
        redis = _make_redis(dead_letter=7)
        client = admin_client(_make_session_no_invocations(), redis)
        r = client.get("/v1/admin/queue-status")
        assert r.status_code == 200
        assert r.json()["page_tasks_dead_letter"] == 7


# ---------------------------------------------------------------------------
# GET /v1/admin/service-inventory
# ---------------------------------------------------------------------------


class TestServiceInventory:
    def test_200_schema(self, admin_client: Any) -> None:
        client = admin_client(_make_session_no_invocations(), _make_redis())
        r = client.get("/v1/admin/service-inventory")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "window_hours" in body
        assert "as_of" in body
        assert body["window_hours"] == 24

    def test_all_catalog_services_present(self, admin_client: Any) -> None:
        client = admin_client(_make_session_no_invocations(), _make_redis())
        r = client.get("/v1/admin/service-inventory")
        assert r.status_code == 200
        names = {item["service_name"] for item in r.json()["items"]}
        expected = {"eep", "eep_worker", "iep0", "iep1a", "iep1b", "iep1d", "iep2a", "iep2b"}
        assert expected.issubset(names), f"Missing services: {expected - names}"

    def test_iep_services_have_invocation_pattern(self, admin_client: Any) -> None:
        client = admin_client(_make_session_no_invocations(), _make_redis())
        r = client.get("/v1/admin/service-inventory")
        items = {i["service_name"]: i for i in r.json()["items"]}
        # IEP services with invocation patterns have health_signal (may be null if no data)
        for svc in ("iep1a", "iep1b", "iep2a", "iep2b"):
            assert svc in items
            # health_signal key must exist (even if null values inside it)
            assert "health_signal" in items[svc]

    def test_item_has_required_fields(self, admin_client: Any) -> None:
        client = admin_client(_make_session_no_invocations(), _make_redis())
        r = client.get("/v1/admin/service-inventory")
        for item in r.json()["items"]:
            assert "service_name" in item
            assert "role" in item
            assert "deployment_type" in item
            assert "model_applicable" in item


# ---------------------------------------------------------------------------
# GET /v1/admin/deployment-status
# ---------------------------------------------------------------------------


class TestDeploymentStatus:
    def test_200_schema(self, admin_client: Any) -> None:
        client = admin_client(_make_session_no_invocations(), _make_redis())
        r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        body = r.json()
        assert "feature_flags" in body
        assert "alembic_version" in body
        assert "as_of" in body

    def test_artifact_cleanup_always_disabled(self, admin_client: Any) -> None:
        client = admin_client(_make_session_no_invocations(), _make_redis())
        r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        flags = r.json()["feature_flags"]
        assert flags["artifact_cleanup"] == "disabled", (
            "artifact_cleanup must always be 'disabled' — it is not implemented"
        )

    def test_retraining_mode_defaults_to_stub(self, admin_client: Any) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIBRARYAI_RETRAINING_TRAIN", None)
            client = admin_client(_make_session_no_invocations(), _make_redis())
            r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        assert r.json()["feature_flags"]["retraining_mode"] == "stub"

    def test_retraining_mode_live_when_env_set(self, admin_client: Any) -> None:
        with patch.dict(os.environ, {"LIBRARYAI_RETRAINING_TRAIN": "live"}):
            client = admin_client(_make_session_no_invocations(), _make_redis())
            r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        assert r.json()["feature_flags"]["retraining_mode"] == "live"

    def test_golden_eval_mode_defaults_to_stub(self, admin_client: Any) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIBRARYAI_RETRAINING_GOLDEN_EVAL", None)
            client = admin_client(_make_session_no_invocations(), _make_redis())
            r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        assert r.json()["feature_flags"]["golden_eval_mode"] == "stub"

    def test_golden_eval_mode_live_when_env_set(self, admin_client: Any) -> None:
        with patch.dict(os.environ, {"LIBRARYAI_RETRAINING_GOLDEN_EVAL": "live"}):
            client = admin_client(_make_session_no_invocations(), _make_redis())
            r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        assert r.json()["feature_flags"]["golden_eval_mode"] == "live"

    def test_image_tag_from_env(self, admin_client: Any) -> None:
        with patch.dict(os.environ, {"LIBRARYAI_IMAGE_TAG": "v1.2.3-abc"}):
            client = admin_client(_make_session_no_invocations(), _make_redis())
            r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        assert r.json()["image_tag"] == "v1.2.3-abc"

    def test_image_tag_null_when_not_set(self, admin_client: Any) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIBRARYAI_IMAGE_TAG", None)
            os.environ.pop("IMAGE_TAG", None)
            client = admin_client(_make_session_no_invocations(), _make_redis())
            r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        assert r.json()["image_tag"] is None

    def test_alembic_version_from_db(self, admin_client: Any) -> None:
        session = _make_session_no_invocations()
        client = admin_client(session, _make_redis())
        r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        # The mock returns "abc1234" as version_num
        assert r.json()["alembic_version"] == "abc1234"

    def test_redis_url_configured_when_env_set(self, admin_client: Any) -> None:
        with patch.dict(os.environ, {"REDIS_URL": "redis://redis:6379/0"}):
            client = admin_client(_make_session_no_invocations(), _make_redis())
            r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        assert r.json()["redis_url_configured"] is True

    def test_mlflow_tracking_uri_null_when_not_set(self, admin_client: Any) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MLFLOW_TRACKING_URI", None)
            client = admin_client(_make_session_no_invocations(), _make_redis())
            r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        body = r.json()
        assert "mlflow_tracking_uri" in body
        assert body["mlflow_tracking_uri"] is None
        assert body["mlflow_reachable"] is False

    def test_mlflow_tracking_uri_present_when_set(self, admin_client: Any) -> None:
        uri = "http://mlflow.libraryai.local:5000"
        with patch.dict(os.environ, {"MLFLOW_TRACKING_URI": uri}):
            # urllib.request.urlopen raises so mlflow_reachable=False, but URI is returned
            with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
                client = admin_client(_make_session_no_invocations(), _make_redis())
                r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        body = r.json()
        assert body["mlflow_tracking_uri"] == uri
        assert body["mlflow_reachable"] is False

    def test_mlflow_reachable_true_when_health_200(self, admin_client: Any) -> None:
        uri = "http://mlflow.libraryai.local:5000"
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        with patch.dict(os.environ, {"MLFLOW_TRACKING_URI": uri}):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                client = admin_client(_make_session_no_invocations(), _make_redis())
                r = client.get("/v1/admin/deployment-status")
        assert r.status_code == 200
        assert r.json()["mlflow_reachable"] is True

    def test_eep_port_in_service_catalog(self, admin_client: Any) -> None:
        """EEP must be listed at port 8000, not 8888."""
        client = admin_client(_make_session_no_invocations(), _make_redis())
        r = client.get("/v1/admin/service-inventory")
        assert r.status_code == 200
        items = {i["service_name"]: i for i in r.json()["items"]}
        assert "eep" in items
        assert items["eep"]["port"] == 8000
