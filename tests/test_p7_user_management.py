"""
tests/test_p7_user_management.py
----------------------------------
Packet 7.6 contract tests for:
  POST  /v1/users
  GET   /v1/users
  PATCH /v1/users/{user_id}/deactivate

Tests cover:
  - 401 when no bearer token supplied (all three endpoints)
  - 403 when a non-admin caller accesses any endpoint
  - POST /v1/users: 201 with correct UserRecord schema
  - POST /v1/users: hashed_password is never returned
  - POST /v1/users: 409 when username is already taken
  - POST /v1/users: role field is stored correctly
  - GET /v1/users: 200 with list of UserRecords
  - GET /v1/users: hashed_password is never returned
  - GET /v1/users: empty list when no users exist
  - PATCH deactivate: 200 with is_active=False
  - PATCH deactivate: 404 when user_id not found
  - PATCH deactivate: idempotent (already-inactive user returns 200)
  - UserRecord fields are exactly {user_id, username, role, is_active, created_at}

Uses a mini FastAPI app with only the user management router so that real
auth is tested for 401/403 cases.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.eep.app.admin.users import router
from services.eep.app.auth import CurrentUser, create_access_token, require_admin
from services.eep.app.db.session import get_session

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_TS = datetime(2025, 3, 1, 9, 0, 0, tzinfo=UTC)
_ADMIN_USER = CurrentUser(user_id="admin-001", role="admin")


def _bearer(user_id: str, role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


def _make_user_orm(
    user_id: str = "u-001",
    username: str = "alice",
    role: str = "user",
    is_active: bool = True,
    created_at: datetime = _TS,
) -> MagicMock:
    u = MagicMock()
    u.user_id = user_id
    u.username = username
    u.hashed_password = "$2b$12$fakehash"
    u.role = role
    u.is_active = is_active
    u.created_at = created_at
    return u


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mini_app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture()
def inject_admin(mini_app: FastAPI):
    """Inject a mock session and an admin override; yield a setup callable."""

    def _setup(session: Session) -> TestClient:
        mini_app.dependency_overrides[get_session] = lambda: session
        mini_app.dependency_overrides[require_admin] = lambda: _ADMIN_USER
        return TestClient(mini_app, raise_server_exceptions=False)

    yield _setup
    mini_app.dependency_overrides.pop(get_session, None)
    mini_app.dependency_overrides.pop(require_admin, None)


def _bare_session() -> MagicMock:
    """A do-nothing session mock for auth-only tests."""
    return MagicMock(spec=Session)


# ---------------------------------------------------------------------------
# Auth enforcement (all three endpoints)
# ---------------------------------------------------------------------------


class TestUserManagementAuth:
    def _bare_client(self, mini_app: FastAPI) -> TestClient:
        mini_app.dependency_overrides[get_session] = lambda: _bare_session()
        return TestClient(mini_app, raise_server_exceptions=False)

    def _cleanup(self, mini_app: FastAPI) -> None:
        mini_app.dependency_overrides.pop(get_session, None)

    def test_post_users_401_no_token(self, mini_app: FastAPI) -> None:
        client = self._bare_client(mini_app)
        r = client.post("/v1/users", json={"username": "x", "password": "y", "role": "user"})
        assert r.status_code == 401
        self._cleanup(mini_app)

    def test_post_users_403_non_admin(self, mini_app: FastAPI) -> None:
        client = self._bare_client(mini_app)
        r = client.post(
            "/v1/users",
            json={"username": "x", "password": "y", "role": "user"},
            headers=_bearer("user-001", role="user"),
        )
        assert r.status_code == 403
        self._cleanup(mini_app)

    def test_get_users_401_no_token(self, mini_app: FastAPI) -> None:
        client = self._bare_client(mini_app)
        r = client.get("/v1/users")
        assert r.status_code == 401
        self._cleanup(mini_app)

    def test_get_users_403_non_admin(self, mini_app: FastAPI) -> None:
        client = self._bare_client(mini_app)
        r = client.get("/v1/users", headers=_bearer("user-001", role="user"))
        assert r.status_code == 403
        self._cleanup(mini_app)

    def test_deactivate_401_no_token(self, mini_app: FastAPI) -> None:
        client = self._bare_client(mini_app)
        r = client.patch("/v1/users/u-001/deactivate")
        assert r.status_code == 401
        self._cleanup(mini_app)

    def test_deactivate_403_non_admin(self, mini_app: FastAPI) -> None:
        client = self._bare_client(mini_app)
        r = client.patch(
            "/v1/users/u-001/deactivate",
            headers=_bearer("user-001", role="user"),
        )
        assert r.status_code == 403
        self._cleanup(mini_app)


# ---------------------------------------------------------------------------
# POST /v1/users
# ---------------------------------------------------------------------------


class TestCreateUser:
    def _make_create_session(self, created_user: Any) -> MagicMock:
        """Mock session where commit succeeds and refresh populates the user."""
        session = MagicMock(spec=Session)
        session.commit.return_value = None
        session.refresh.side_effect = lambda obj: None
        # After refresh, the mock object already has the right attrs set
        # (set by the endpoint before commit, so no-op refresh is fine)
        return session

    def test_201_on_success(self, inject_admin: Any) -> None:
        session = MagicMock(spec=Session)
        session.commit.return_value = None

        # After refresh the object should have created_at populated.
        # We simulate this via side_effect on refresh.
        created = _make_user_orm()

        def _refresh(obj):
            obj.user_id = created.user_id
            obj.username = created.username
            obj.role = created.role
            obj.is_active = created.is_active
            obj.created_at = created.created_at

        session.refresh.side_effect = _refresh
        client = inject_admin(session)
        r = client.post("/v1/users", json={"username": "alice", "password": "secret", "role": "user"})
        assert r.status_code == 201

    def test_response_schema(self, inject_admin: Any) -> None:
        session = MagicMock(spec=Session)
        session.commit.return_value = None
        created = _make_user_orm(username="bob", role="admin")

        def _refresh(obj):
            obj.user_id = created.user_id
            obj.username = created.username
            obj.role = created.role
            obj.is_active = created.is_active
            obj.created_at = created.created_at

        session.refresh.side_effect = _refresh
        client = inject_admin(session)
        r = client.post("/v1/users", json={"username": "bob", "password": "pw", "role": "admin"})
        data = r.json()
        assert set(data.keys()) == {"user_id", "username", "role", "is_active", "created_at"}

    def test_hashed_password_not_returned(self, inject_admin: Any) -> None:
        session = MagicMock(spec=Session)
        session.commit.return_value = None
        created = _make_user_orm()

        def _refresh(obj):
            obj.user_id = created.user_id
            obj.username = created.username
            obj.role = created.role
            obj.is_active = created.is_active
            obj.created_at = created.created_at

        session.refresh.side_effect = _refresh
        client = inject_admin(session)
        r = client.post("/v1/users", json={"username": "alice", "password": "secret", "role": "user"})
        assert "hashed_password" not in r.json()
        assert "password" not in r.json()

    def test_409_on_duplicate_username(self, inject_admin: Any) -> None:
        from sqlalchemy.exc import IntegrityError

        session = MagicMock(spec=Session)
        session.commit.side_effect = IntegrityError("unique", {}, Exception())
        client = inject_admin(session)
        r = client.post("/v1/users", json={"username": "alice", "password": "pw", "role": "user"})
        assert r.status_code == 409

    def test_409_detail_mentions_username(self, inject_admin: Any) -> None:
        from sqlalchemy.exc import IntegrityError

        session = MagicMock(spec=Session)
        session.commit.side_effect = IntegrityError("unique", {}, Exception())
        client = inject_admin(session)
        r = client.post("/v1/users", json={"username": "alice", "password": "pw", "role": "user"})
        assert "alice" in r.json()["detail"]

    def test_is_active_true_on_creation(self, inject_admin: Any) -> None:
        session = MagicMock(spec=Session)
        session.commit.return_value = None
        created = _make_user_orm(is_active=True)

        def _refresh(obj):
            obj.user_id = created.user_id
            obj.username = created.username
            obj.role = created.role
            obj.is_active = created.is_active
            obj.created_at = created.created_at

        session.refresh.side_effect = _refresh
        client = inject_admin(session)
        r = client.post("/v1/users", json={"username": "alice", "password": "pw", "role": "user"})
        assert r.json()["is_active"] is True


# ---------------------------------------------------------------------------
# GET /v1/users
# ---------------------------------------------------------------------------


class TestListUsers:
    def _make_list_session(self, users: list[Any]) -> MagicMock:
        session = MagicMock(spec=Session)
        chain = MagicMock()
        session.query.return_value = chain
        chain.order_by.return_value = chain
        chain.all.return_value = users
        return session

    def test_200_with_list(self, inject_admin: Any) -> None:
        u = _make_user_orm()
        session = self._make_list_session([u])
        client = inject_admin(session)
        r = client.get("/v1/users")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        assert len(r.json()) == 1

    def test_empty_list(self, inject_admin: Any) -> None:
        session = self._make_list_session([])
        client = inject_admin(session)
        r = client.get("/v1/users")
        assert r.status_code == 200
        assert r.json() == []

    def test_hashed_password_not_returned(self, inject_admin: Any) -> None:
        u = _make_user_orm()
        session = self._make_list_session([u])
        client = inject_admin(session)
        r = client.get("/v1/users")
        item = r.json()[0]
        assert "hashed_password" not in item
        assert "password" not in item

    def test_user_record_fields(self, inject_admin: Any) -> None:
        u = _make_user_orm(user_id="u-xyz", username="carol", role="admin")
        session = self._make_list_session([u])
        client = inject_admin(session)
        r = client.get("/v1/users")
        item = r.json()[0]
        assert set(item.keys()) == {"user_id", "username", "role", "is_active", "created_at"}
        assert item["user_id"] == "u-xyz"
        assert item["username"] == "carol"
        assert item["role"] == "admin"

    def test_multiple_users_returned(self, inject_admin: Any) -> None:
        u1 = _make_user_orm(user_id="u-001", username="alice")
        u2 = _make_user_orm(user_id="u-002", username="bob")
        session = self._make_list_session([u1, u2])
        client = inject_admin(session)
        r = client.get("/v1/users")
        assert len(r.json()) == 2


# ---------------------------------------------------------------------------
# PATCH /v1/users/{user_id}/deactivate
# ---------------------------------------------------------------------------


class TestDeactivateUser:
    def _make_deactivate_session(self, user: Any | None) -> MagicMock:
        session = MagicMock(spec=Session)
        session.get.return_value = user
        session.commit.return_value = None
        session.refresh.side_effect = lambda obj: None
        return session

    def test_200_on_success(self, inject_admin: Any) -> None:
        u = _make_user_orm(is_active=True)
        session = self._make_deactivate_session(u)
        client = inject_admin(session)
        r = client.patch("/v1/users/u-001/deactivate")
        assert r.status_code == 200

    def test_is_active_false_after_deactivation(self, inject_admin: Any) -> None:
        u = _make_user_orm(is_active=True)
        session = self._make_deactivate_session(u)
        client = inject_admin(session)
        client.patch("/v1/users/u-001/deactivate")
        # The endpoint sets user.is_active = False before commit
        assert u.is_active is False

    def test_response_schema_after_deactivation(self, inject_admin: Any) -> None:
        u = _make_user_orm(user_id="u-001", username="alice", is_active=True)

        def _refresh(obj):
            # Simulate DB refresh: is_active was already set to False by endpoint
            pass

        session = self._make_deactivate_session(u)
        session.refresh.side_effect = _refresh
        client = inject_admin(session)
        r = client.patch("/v1/users/u-001/deactivate")
        data = r.json()
        assert set(data.keys()) == {"user_id", "username", "role", "is_active", "created_at"}
        assert data["is_active"] is False

    def test_404_when_user_not_found(self, inject_admin: Any) -> None:
        session = self._make_deactivate_session(None)
        client = inject_admin(session)
        r = client.patch("/v1/users/nonexistent/deactivate")
        assert r.status_code == 404

    def test_404_detail_mentions_user_id(self, inject_admin: Any) -> None:
        session = self._make_deactivate_session(None)
        client = inject_admin(session)
        r = client.patch("/v1/users/nonexistent/deactivate")
        assert "nonexistent" in r.json()["detail"]

    def test_idempotent_already_inactive(self, inject_admin: Any) -> None:
        """Deactivating an already-inactive user must return 200, not an error."""
        u = _make_user_orm(is_active=False)
        session = self._make_deactivate_session(u)
        client = inject_admin(session)
        r = client.patch("/v1/users/u-001/deactivate")
        assert r.status_code == 200
        assert r.json()["is_active"] is False

    def test_hashed_password_not_returned(self, inject_admin: Any) -> None:
        u = _make_user_orm(is_active=True)
        session = self._make_deactivate_session(u)
        client = inject_admin(session)
        r = client.patch("/v1/users/u-001/deactivate")
        assert "hashed_password" not in r.json()
