"""
tests/test_p7_admin_dashboard.py
----------------------------------
Packet 7.4 contract tests for:
  GET /v1/admin/dashboard-summary
  GET /v1/admin/service-health

Tests cover:
  - HTTP 200 with correct schema for both endpoints
  - 401 when no bearer token supplied
  - 403 when a non-admin user calls either endpoint
  - dashboard-summary field values (throughput, rates, counts)
  - service-health field values (stage rates, window echo)
  - service-health: default and custom window_hours
  - service-health: window_hours validation (min 1, max 720)
  - Empty DB / Redis returns zeroed-out response shapes

Uses a mini FastAPI app containing only the admin dashboard router so that
real auth enforcement is tested for 401/403 cases.  Auth-bypassed cases use
dependency_overrides directly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.eep.app.admin.dashboard import router
from services.eep.app.auth import CurrentUser, create_access_token, require_admin
from services.eep.app.db.session import get_session
from services.eep.app.redis_client import get_redis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bearer(user_id: str, role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


def _make_redis(llen_value: int = 0) -> MagicMock:
    r = MagicMock()
    r.llen.return_value = llen_value
    return r


def _make_session_scalar(*return_values: float | int) -> MagicMock:
    """
    Return a mock Session whose successive ``scalar()`` calls return *return_values*,
    in the order produced by ``get_dashboard_summary``:

    1–3. trailing terminal aggregates: n_all, n_with_ms, sum_processing_time_ms
    4–7. total_terminal, total_accepted, total_with_agreement, total_agreed
    8–10. pending_corrections_count, active_jobs_count, shadow_evaluations_count
    """
    session = MagicMock(spec=Session)
    chain = MagicMock()
    session.query.return_value = chain
    chain.filter.return_value = chain
    chain.with_entities.return_value = chain
    chain.isnot.return_value = chain
    chain.is_.return_value = chain
    chain.in_.return_value = chain
    chain.select_from.return_value = chain
    chain.all.return_value = []
    chain.scalar.side_effect = list(return_values)
    return session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mini_app() -> FastAPI:
    """Mini app with only the admin dashboard router — real auth dependency."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture()
def inject_admin(mini_app: FastAPI):
    """Override session and Redis; inject an admin user; yield TestClient."""

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
# Auth enforcement
# ---------------------------------------------------------------------------


class TestAdminDashboardAuth:
    def test_dashboard_summary_401_no_token(self, mini_app: FastAPI) -> None:
        mini_app.dependency_overrides[get_session] = lambda: MagicMock(spec=Session)
        mini_app.dependency_overrides[get_redis] = lambda: _make_redis()
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get("/v1/admin/dashboard-summary")
        assert r.status_code == 401
        mini_app.dependency_overrides.pop(get_session, None)
        mini_app.dependency_overrides.pop(get_redis, None)

    def test_dashboard_summary_401_invalid_token(self, mini_app: FastAPI) -> None:
        mini_app.dependency_overrides[get_session] = lambda: MagicMock(spec=Session)
        mini_app.dependency_overrides[get_redis] = lambda: _make_redis()
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get(
            "/v1/admin/dashboard-summary",
            headers={"Authorization": "Bearer garbage"},
        )
        assert r.status_code == 401
        mini_app.dependency_overrides.pop(get_session, None)
        mini_app.dependency_overrides.pop(get_redis, None)

    def test_dashboard_summary_403_non_admin(self, mini_app: FastAPI) -> None:
        mini_app.dependency_overrides[get_session] = lambda: MagicMock(spec=Session)
        mini_app.dependency_overrides[get_redis] = lambda: _make_redis()
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get(
            "/v1/admin/dashboard-summary",
            headers=_bearer("user-001", role="user"),
        )
        assert r.status_code == 403
        mini_app.dependency_overrides.pop(get_session, None)
        mini_app.dependency_overrides.pop(get_redis, None)

    def test_service_health_401_no_token(self, mini_app: FastAPI) -> None:
        mini_app.dependency_overrides[get_session] = lambda: MagicMock(spec=Session)
        mini_app.dependency_overrides[get_redis] = lambda: _make_redis()
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get("/v1/admin/service-health")
        assert r.status_code == 401
        mini_app.dependency_overrides.pop(get_session, None)
        mini_app.dependency_overrides.pop(get_redis, None)

    def test_service_health_403_non_admin(self, mini_app: FastAPI) -> None:
        mini_app.dependency_overrides[get_session] = lambda: MagicMock(spec=Session)
        mini_app.dependency_overrides[get_redis] = lambda: _make_redis()
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get(
            "/v1/admin/service-health",
            headers=_bearer("user-001", role="user"),
        )
        assert r.status_code == 403
        mini_app.dependency_overrides.pop(get_session, None)
        mini_app.dependency_overrides.pop(get_redis, None)


# ---------------------------------------------------------------------------
# dashboard-summary schema
# ---------------------------------------------------------------------------


class TestDashboardSummarySchema:
    def test_200_correct_schema(self, inject_admin: Any) -> None:
        """Response must contain exactly the documented dashboard-summary fields."""
        # n_all, n_ms, sum_ms, then acceptance block, then pending / jobs / shadow
        session = _make_session_scalar(120, 120, 21_600_000.0, 100, 80, 90, 70, 3, 2, 1)
        r_client = inject_admin(session, _make_redis(llen_value=4))
        resp = r_client.get("/v1/admin/dashboard-summary")
        assert resp.status_code == 200
        data = resp.json()
        expected_keys = {
            "trailing_wall_clock_pages_per_hour",
            "trailing_active_pages_per_hour",
            "auto_accept_rate",
            "structural_agreement_rate",
            "pending_corrections_count",
            "active_jobs_count",
            "active_workers_count",
            "shadow_evaluations_count",
        }
        assert set(data.keys()) == expected_keys

    def test_trailing_wall_clock_delivery_five(self, inject_admin: Any) -> None:
        """120 terminal pages in the trailing 24h window → 120 / 24 = 5.0 delivery p/h."""
        session = _make_session_scalar(120, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["trailing_wall_clock_pages_per_hour"] == 5.0
        assert data["trailing_active_pages_per_hour"] is None

    def test_trailing_active_processing_rate_twenty(self, inject_admin: Any) -> None:
        """120 terminals with 6h aggregate ``processing_time_ms`` → 20 active pages/hour."""
        session = _make_session_scalar(120, 120, 21_600_000.0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["trailing_wall_clock_pages_per_hour"] == 5.0
        assert data["trailing_active_pages_per_hour"] == 20.0

    def test_trailing_active_null_when_ms_coverage_incomplete(self, inject_admin: Any) -> None:
        session = _make_session_scalar(120, 119, 21_600_000.0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["trailing_wall_clock_pages_per_hour"] == 5.0
        assert data["trailing_active_pages_per_hour"] is None

    def test_trailing_active_null_when_sum_ms_zero(self, inject_admin: Any) -> None:
        """All terminals report ms but sum is zero → cannot divide by active time."""
        session = _make_session_scalar(120, 120, 0.0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["trailing_wall_clock_pages_per_hour"] == 5.0
        assert data["trailing_active_pages_per_hour"] is None

    def test_auto_accept_rate(self, inject_admin: Any) -> None:
        """80 accepted / 100 terminal = 0.8."""
        session = _make_session_scalar(0, 0, 0, 100, 80, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["auto_accept_rate"] == 0.8

    def test_auto_accept_rate_zero_denominator(self, inject_admin: Any) -> None:
        """0 terminal pages → auto_accept_rate = 0.0, not NaN."""
        session = _make_session_scalar(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["auto_accept_rate"] == 0.0

    def test_structural_agreement_rate(self, inject_admin: Any) -> None:
        """70 agreed / 90 with_agreement = 0.7778."""
        session = _make_session_scalar(0, 0, 0, 0, 0, 90, 70, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["structural_agreement_rate"] == round(70 / 90, 4)

    def test_active_workers_count_from_redis(self, inject_admin: Any) -> None:
        """active_workers_count must come from Redis LLEN, not DB."""
        session = _make_session_scalar(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis(llen_value=7))
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["active_workers_count"] == 7

    def test_pending_corrections_count(self, inject_admin: Any) -> None:
        session = _make_session_scalar(0, 0, 0, 0, 0, 0, 0, 12, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["pending_corrections_count"] == 12

    def test_shadow_evaluations_count(self, inject_admin: Any) -> None:
        session = _make_session_scalar(0, 0, 0, 0, 0, 0, 0, 0, 0, 5)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["shadow_evaluations_count"] == 5

    def test_all_zeros_when_db_empty(self, inject_admin: Any) -> None:
        """All DB queries return 0; Redis LLEN returns 0 — all fields must be 0."""
        session = _make_session_scalar(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis(0))
        data = client.get("/v1/admin/dashboard-summary").json()
        assert data["trailing_wall_clock_pages_per_hour"] == 0.0
        assert data["trailing_active_pages_per_hour"] is None
        assert data["auto_accept_rate"] == 0.0
        assert data["structural_agreement_rate"] == 0.0
        assert data["pending_corrections_count"] == 0
        assert data["active_jobs_count"] == 0
        assert data["active_workers_count"] == 0
        assert data["shadow_evaluations_count"] == 0


# ---------------------------------------------------------------------------
# service-health schema
# ---------------------------------------------------------------------------


class TestServiceHealthSchema:
    def _make_health_session(self, *scalars: int) -> MagicMock:
        """
        Scalar order for ``get_service_health`` (11 values):
          preprocessing total, success, rectification total, success,
          layout total, success, human_corrections_window,
          total_with_agreement_window, agreed_window, rescue_attempted, policy_skips_count.
        """
        session = MagicMock(spec=Session)
        chain = MagicMock()
        session.query.return_value = chain
        chain.filter.return_value = chain
        chain.with_entities.return_value = chain
        chain.isnot.return_value = chain
        chain.is_.return_value = chain
        chain.scalar.side_effect = list(scalars)
        chain.all.return_value = []
        return session

    def test_200_correct_schema(self, inject_admin: Any) -> None:
        """Response must contain exactly the 8 documented fields."""
        # scalar order: 3 stages × 2 (total/success) + human_corrections_window
        # + total_with_agreement_window + agreed_window + rescue_attempted + policy_skips_count
        session = self._make_health_session(10, 9, 5, 5, 20, 18, 2, 8, 6, 0, 0)
        client = inject_admin(session, _make_redis())
        resp = client.get("/v1/admin/service-health")
        assert resp.status_code == 200
        data = resp.json()
        expected_keys = {
            "preprocessing_success_rate",
            "rectification_success_rate",
            "layout_success_rate",
            "human_review_throughput_rate",
            "structural_agreement_rate",
            "rescue_rate",
            "policy_skips_count",
            "window_hours",
        }
        assert set(data.keys()) == expected_keys

    def test_default_window_hours_echoed(self, inject_admin: Any) -> None:
        """window_hours must default to 24 and be echoed back."""
        session = self._make_health_session(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/service-health").json()
        assert data["window_hours"] == 24

    def test_custom_window_hours_echoed(self, inject_admin: Any) -> None:
        """Custom window_hours=48 must be echoed back."""
        session = self._make_health_session(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/service-health", params={"window_hours": 48}).json()
        assert data["window_hours"] == 48

    def test_window_hours_min_1(self, inject_admin: Any) -> None:
        """window_hours=0 must be rejected with 422."""
        session = self._make_health_session(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        r = client.get("/v1/admin/service-health", params={"window_hours": 0})
        assert r.status_code == 422

    def test_window_hours_max_720(self, inject_admin: Any) -> None:
        """window_hours=721 must be rejected with 422."""
        session = self._make_health_session(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        r = client.get("/v1/admin/service-health", params={"window_hours": 721})
        assert r.status_code == 422

    def test_preprocessing_success_rate(self, inject_admin: Any) -> None:
        """10 invocations, 8 success → 0.8."""
        # preprocessing total=10, success=8; rest zero
        session = self._make_health_session(10, 8, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/service-health").json()
        assert data["preprocessing_success_rate"] == 0.8

    def test_zero_denominator_gives_zero_rate(self, inject_admin: Any) -> None:
        """All stages empty within window → all rates 0.0."""
        session = self._make_health_session(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/service-health").json()
        assert data["preprocessing_success_rate"] == 0.0
        assert data["rectification_success_rate"] == 0.0
        assert data["layout_success_rate"] == 0.0
        assert data["structural_agreement_rate"] == 0.0

    def test_human_review_throughput_rate(self, inject_admin: Any) -> None:
        """120 human corrections in the 24h window → 120 / 24 = 5.0 pages/hour."""
        session = self._make_health_session(0, 0, 0, 0, 0, 0, 120, 0, 0, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/service-health").json()
        assert data["human_review_throughput_rate"] == 5.0

    def test_structural_agreement_rate_windowed(self, inject_admin: Any) -> None:
        """60 agreed / 80 with_agreement = 0.75."""
        session = self._make_health_session(0, 0, 0, 0, 0, 0, 0, 80, 60, 0, 0)
        client = inject_admin(session, _make_redis())
        data = client.get("/v1/admin/service-health").json()
        assert data["structural_agreement_rate"] == 0.75
