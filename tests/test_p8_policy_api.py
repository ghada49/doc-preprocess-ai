"""
tests/test_p8_policy_api.py
-----------------------------
Packet 8.2 contract tests for:
  GET   /v1/policy
  PATCH /v1/policy

Tests cover:
  - 401 when no bearer token supplied
  - 403 when a user-role caller accesses either endpoint
  - GET: 404 when no policy exists
  - GET: 200 with correct PolicyRecord schema when policy exists
  - GET: returns the most recently applied version (latest by applied_at)
  - PATCH: 200 with PolicyRecord schema on successful update
  - PATCH: auto-increments version ("v1", "v2", …)
  - PATCH: applied_by is set from JWT sub claim
  - PATCH: 400 when config_yaml is not valid YAML
  - PATCH: 400 when config_yaml top-level is not a mapping
  - PATCH: 422 when threshold_adjustment_requires_audit=true and audit_evidence absent
  - PATCH: 422 when threshold_adjustment_requires_slo_validation=true and slo_validation absent
  - PATCH: passes guardrails when evidence fields are provided
  - PATCH: skips guardrails when no current policy exists (first-time setup)
  - PATCH: skips guardrails when current policy has flags set to false

Uses a mini FastAPI app with only the policy router so real auth is tested
for 401/403 cases.  DB interactions are mocked via dependency overrides on
get_session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.eep.app.auth import CurrentUser, create_access_token, require_admin
from services.eep.app.db.session import get_session
from services.eep.app.policy_api import router

# ── Constants / helpers ────────────────────────────────────────────────────────

_TS = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)
_ADMIN_USER = CurrentUser(user_id="admin-001", role="admin")

_VALID_YAML = """\
preprocessing:
  split_confidence_threshold: 0.75
  threshold_adjustment_requires_audit: false
  threshold_adjustment_requires_slo_validation: false
layout:
  min_consensus_confidence: 0.6
"""

_STRICT_YAML = """\
preprocessing:
  split_confidence_threshold: 0.75
  threshold_adjustment_requires_audit: true
  threshold_adjustment_requires_slo_validation: true
layout:
  min_consensus_confidence: 0.6
"""


def _bearer(user_id: str = "admin-001", role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


def _make_policy_orm(
    version: str = "v1",
    config_yaml: str = _VALID_YAML,
    applied_by: str = "admin-001",
    justification: str = "initial",
    applied_at: datetime = _TS,
) -> MagicMock:
    pv = MagicMock()
    pv.version = version
    pv.config_yaml = config_yaml
    pv.applied_by = applied_by
    pv.justification = justification
    pv.applied_at = applied_at
    return pv


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def mini_app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture()
def inject_admin(mini_app: FastAPI):
    """Inject a mock session and bypass require_admin; yield a setup callable."""

    def _setup(session: MagicMock) -> TestClient:
        mini_app.dependency_overrides[get_session] = lambda: session
        mini_app.dependency_overrides[require_admin] = lambda: _ADMIN_USER
        return TestClient(mini_app, raise_server_exceptions=False)

    yield _setup
    mini_app.dependency_overrides.pop(get_session, None)
    mini_app.dependency_overrides.pop(require_admin, None)


def _bare_session() -> MagicMock:
    """Do-nothing session mock for auth-only tests."""
    s = MagicMock(spec=[])
    return s


# ── Auth guard tests (no session injection needed) ─────────────────────────────


class TestAuthGuards:
    @pytest.fixture(autouse=True)
    def _client(self, mini_app: FastAPI) -> TestClient:
        # Ensure no overrides leak in
        mini_app.dependency_overrides.clear()
        return TestClient(mini_app, raise_server_exceptions=False)

    def test_get_policy_401_no_token(self, _client: TestClient) -> None:
        resp = _client.get("/v1/policy")
        assert resp.status_code == 401

    def test_patch_policy_401_no_token(self, _client: TestClient) -> None:
        resp = _client.patch("/v1/policy", json={"config_yaml": _VALID_YAML, "justification": "x"})
        assert resp.status_code == 401

    def test_get_policy_403_user_role(self, mini_app: FastAPI) -> None:
        mini_app.dependency_overrides[get_session] = lambda: _bare_session()
        client = TestClient(mini_app, raise_server_exceptions=False)
        try:
            resp = client.get("/v1/policy", headers=_bearer(role="user"))
            assert resp.status_code == 403
        finally:
            mini_app.dependency_overrides.pop(get_session, None)

    def test_patch_policy_403_user_role(self, mini_app: FastAPI) -> None:
        mini_app.dependency_overrides[get_session] = lambda: _bare_session()
        client = TestClient(mini_app, raise_server_exceptions=False)
        try:
            resp = client.patch(
                "/v1/policy",
                json={"config_yaml": _VALID_YAML, "justification": "x"},
                headers=_bearer(role="user"),
            )
            assert resp.status_code == 403
        finally:
            mini_app.dependency_overrides.pop(get_session, None)


# ── GET /v1/policy ─────────────────────────────────────────────────────────────


class TestGetPolicy:
    def test_404_when_no_policy_exists(self, inject_admin) -> None:
        session = MagicMock()
        session.query.return_value.order_by.return_value.first.return_value = None
        client = inject_admin(session)
        resp = client.get("/v1/policy")
        assert resp.status_code == 404
        assert "No policy" in resp.json()["detail"]

    def test_200_returns_policy_record(self, inject_admin) -> None:
        pv = _make_policy_orm()
        session = MagicMock()
        session.query.return_value.order_by.return_value.first.return_value = pv
        client = inject_admin(session)
        resp = client.get("/v1/policy")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "v1"
        assert data["config_yaml"] == _VALID_YAML
        assert data["applied_by"] == "admin-001"
        assert data["justification"] == "initial"
        assert "applied_at" in data

    def test_response_schema_fields(self, inject_admin) -> None:
        pv = _make_policy_orm(version="v3", justification="third update")
        session = MagicMock()
        session.query.return_value.order_by.return_value.first.return_value = pv
        client = inject_admin(session)
        resp = client.get("/v1/policy")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"version", "config_yaml", "applied_at", "applied_by", "justification"}

    def test_returns_latest_record_by_applied_at(self, inject_admin) -> None:
        # The query is ordered by applied_at DESC; most recent is returned first.
        # We verify the endpoint uses order_by and takes first().
        pv_latest = _make_policy_orm(version="v2", applied_at=datetime(2026, 4, 1, tzinfo=timezone.utc))
        session = MagicMock()
        session.query.return_value.order_by.return_value.first.return_value = pv_latest
        client = inject_admin(session)
        resp = client.get("/v1/policy")
        assert resp.status_code == 200
        assert resp.json()["version"] == "v2"


# ── PATCH /v1/policy ──────────────────────────────────────────────────────────


class TestUpdatePolicy:
    def _session_for_patch(
        self,
        existing: MagicMock | None = None,
        count: int = 0,
    ) -> MagicMock:
        """Build a mock session for PATCH tests."""
        session = MagicMock()
        # _current_policy query
        session.query.return_value.order_by.return_value.first.return_value = existing
        # _next_version count
        session.query.return_value.count.return_value = count
        session.add = MagicMock()
        session.commit = MagicMock()

        def _refresh(obj: Any) -> None:
            # Simulate DB populating applied_at after commit
            obj.applied_at = _TS

        session.refresh = _refresh
        return session

    def test_200_on_first_policy_creation(self, inject_admin) -> None:
        session = self._session_for_patch(existing=None, count=0)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={
                "config_yaml": _VALID_YAML,
                "justification": "initial policy",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "v1"
        assert data["justification"] == "initial policy"
        assert data["applied_by"] == "admin-001"

    def test_version_increments_with_count(self, inject_admin) -> None:
        # count=3 means 3 existing records → next version = "v4"
        session = self._session_for_patch(existing=None, count=3)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={"config_yaml": _VALID_YAML, "justification": "update"},
        )
        assert resp.status_code == 200
        assert resp.json()["version"] == "v4"

    def test_applied_by_from_jwt_sub(self, mini_app: FastAPI) -> None:
        caller = CurrentUser(user_id="specific-admin", role="admin")
        session = self._session_for_patch(existing=None, count=0)
        mini_app.dependency_overrides[get_session] = lambda: session
        mini_app.dependency_overrides[require_admin] = lambda: caller
        client = TestClient(mini_app, raise_server_exceptions=False)
        try:
            resp = client.patch(
                "/v1/policy",
                json={"config_yaml": _VALID_YAML, "justification": "test"},
            )
            assert resp.status_code == 200
            assert resp.json()["applied_by"] == "specific-admin"
        finally:
            mini_app.dependency_overrides.pop(get_session, None)
            mini_app.dependency_overrides.pop(require_admin, None)

    def test_response_schema_fields(self, inject_admin) -> None:
        session = self._session_for_patch(existing=None, count=0)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={"config_yaml": _VALID_YAML, "justification": "test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"version", "config_yaml", "applied_at", "applied_by", "justification"}

    def test_400_invalid_yaml(self, inject_admin) -> None:
        # Simulate no existing policy (count doesn't matter here since we 400 early)
        session = self._session_for_patch(existing=None, count=0)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={
                "config_yaml": "key: [unclosed bracket",
                "justification": "bad yaml",
            },
        )
        assert resp.status_code == 400
        assert "not valid YAML" in resp.json()["detail"]

    def test_400_yaml_not_mapping(self, inject_admin) -> None:
        session = self._session_for_patch(existing=None, count=0)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={
                "config_yaml": "- item1\n- item2\n",
                "justification": "list not dict",
            },
        )
        assert resp.status_code == 400
        assert "mapping" in resp.json()["detail"]

    # ── Guardrail: threshold_adjustment_requires_audit ─────────────────────────

    def test_422_when_audit_required_and_missing(self, inject_admin) -> None:
        existing = _make_policy_orm(config_yaml=_STRICT_YAML)
        session = self._session_for_patch(existing=existing, count=1)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={
                "config_yaml": _VALID_YAML,
                "justification": "update",
                # audit_evidence absent
                "slo_validation": "slo evidence here",
            },
        )
        assert resp.status_code == 422
        assert "audit_evidence" in resp.json()["detail"]

    def test_422_when_slo_required_and_missing(self, inject_admin) -> None:
        existing = _make_policy_orm(config_yaml=_STRICT_YAML)
        session = self._session_for_patch(existing=existing, count=1)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={
                "config_yaml": _VALID_YAML,
                "justification": "update",
                "audit_evidence": "audit evidence here",
                # slo_validation absent
            },
        )
        assert resp.status_code == 422
        assert "slo_validation" in resp.json()["detail"]

    def test_200_when_both_guardrails_satisfied(self, inject_admin) -> None:
        existing = _make_policy_orm(config_yaml=_STRICT_YAML)
        session = self._session_for_patch(existing=existing, count=1)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={
                "config_yaml": _VALID_YAML,
                "justification": "update with evidence",
                "audit_evidence": "auditor reviewed sampling week 12",
                "slo_validation": "SLO within bounds per dashboard",
            },
        )
        assert resp.status_code == 200

    def test_guardrails_skipped_when_no_current_policy(self, inject_admin) -> None:
        # First-time setup: no existing policy → guardrail check skipped
        # even if we don't supply audit_evidence / slo_validation.
        session = self._session_for_patch(existing=None, count=0)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={
                "config_yaml": _STRICT_YAML,
                "justification": "bootstrap",
                # no audit_evidence, no slo_validation
            },
        )
        assert resp.status_code == 200

    def test_guardrails_skipped_when_flags_false(self, inject_admin) -> None:
        # Current policy has both guardrail flags = false
        existing = _make_policy_orm(config_yaml=_VALID_YAML)
        session = self._session_for_patch(existing=existing, count=1)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={
                "config_yaml": _VALID_YAML,
                "justification": "routine update",
                # no audit_evidence, no slo_validation — should be fine
            },
        )
        assert resp.status_code == 200

    def test_db_add_and_commit_called(self, inject_admin) -> None:
        session = self._session_for_patch(existing=None, count=0)
        client = inject_admin(session)
        client.patch(
            "/v1/policy",
            json={"config_yaml": _VALID_YAML, "justification": "persist check"},
        )
        session.add.assert_called_once()
        session.commit.assert_called_once()

    def test_config_yaml_stored_verbatim(self, inject_admin) -> None:
        """The config_yaml passed in must be stored and returned exactly."""
        session = self._session_for_patch(existing=None, count=0)
        client = inject_admin(session)
        resp = client.patch(
            "/v1/policy",
            json={"config_yaml": _VALID_YAML, "justification": "verbatim check"},
        )
        assert resp.status_code == 200
        assert resp.json()["config_yaml"] == _VALID_YAML
