"""
tests/test_p7_auth_token.py
-----------------------------
Packet 7.1 — POST /v1/auth/token contract tests.

Covers:
  - 200 with access_token and token_type="bearer" for valid credentials
  - JWT payload contains correct sub (user_id) and role claims
  - 401 for unknown username
  - 401 for wrong password
  - 401 for inactive user account
  - 422 for missing required fields (username or password)
  - create_access_token / decode_token round-trip
  - verify_password / get_password_hash round-trip
  - require_admin raises 403 for a user-role token
  - require_user accepts any valid token role

Session is mocked; no live database required.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from services.eep.app.auth import (
    CurrentUser,
    TokenResponse,
    _ALGORITHM,
    _SECRET_KEY,
    create_access_token,
    decode_token,
    get_password_hash,
    require_admin,
    require_user,
    router as auth_router,
    verify_password,
)
from services.eep.app.db.session import get_session

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_user(
    user_id: str = "u-001",
    username: str = "alice",
    password: str = "secret",
    role: str = "user",
    is_active: bool = True,
) -> MagicMock:
    user = MagicMock()
    user.user_id = user_id
    user.username = username
    user.hashed_password = get_password_hash(password)
    user.role = role
    user.is_active = is_active
    return user


def _make_app(mock_user: MagicMock | None = None) -> TestClient:
    """Build a minimal FastAPI app with the auth router and a mocked DB session."""
    application = FastAPI()
    application.include_router(auth_router)

    mock_session = MagicMock()
    mock_query = MagicMock()
    mock_filter = MagicMock()
    mock_filter.first.return_value = mock_user
    mock_query.filter.return_value = mock_filter
    mock_session.query.return_value = mock_query

    application.dependency_overrides[get_session] = lambda: mock_session
    return TestClient(application)


# ── POST /v1/auth/token ─────────────────────────────────────────────────────────


class TestAuthTokenEndpoint:
    def test_valid_credentials_returns_200(self) -> None:
        user = _make_user()
        client = _make_app(mock_user=user)
        resp = client.post("/v1/auth/token", json={"username": "alice", "password": "secret"})
        assert resp.status_code == 200

    def test_response_has_access_token_and_token_type(self) -> None:
        user = _make_user()
        client = _make_app(mock_user=user)
        resp = client.post("/v1/auth/token", json={"username": "alice", "password": "secret"})
        body = resp.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"

    def test_returned_token_contains_correct_sub(self) -> None:
        user = _make_user(user_id="u-xyz", username="alice", password="secret", role="user")
        client = _make_app(mock_user=user)
        resp = client.post("/v1/auth/token", json={"username": "alice", "password": "secret"})
        token = resp.json()["access_token"]
        payload: dict[str, Any] = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        assert payload["sub"] == "u-xyz"

    def test_returned_token_contains_correct_role(self) -> None:
        user = _make_user(user_id="u-admin", role="admin", password="adminpass")
        client = _make_app(mock_user=user)
        resp = client.post("/v1/auth/token", json={"username": "alice", "password": "adminpass"})
        token = resp.json()["access_token"]
        payload: dict[str, Any] = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        assert payload["role"] == "admin"

    def test_returned_token_has_exp_claim(self) -> None:
        user = _make_user()
        client = _make_app(mock_user=user)
        resp = client.post("/v1/auth/token", json={"username": "alice", "password": "secret"})
        token = resp.json()["access_token"]
        payload: dict[str, Any] = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        assert "exp" in payload

    def test_wrong_password_returns_401(self) -> None:
        user = _make_user(password="correct-password")
        client = _make_app(mock_user=user)
        resp = client.post("/v1/auth/token", json={"username": "alice", "password": "wrong"})
        assert resp.status_code == 401

    def test_unknown_username_returns_401(self) -> None:
        client = _make_app(mock_user=None)  # DB returns None for unknown user
        resp = client.post("/v1/auth/token", json={"username": "nobody", "password": "x"})
        assert resp.status_code == 401

    def test_inactive_user_returns_401(self) -> None:
        user = _make_user(is_active=False)
        client = _make_app(mock_user=user)
        resp = client.post("/v1/auth/token", json={"username": "alice", "password": "secret"})
        assert resp.status_code == 401

    def test_missing_username_returns_422(self) -> None:
        client = _make_app()
        resp = client.post("/v1/auth/token", json={"password": "secret"})
        assert resp.status_code == 422

    def test_missing_password_returns_422(self) -> None:
        client = _make_app()
        resp = client.post("/v1/auth/token", json={"username": "alice"})
        assert resp.status_code == 422

    def test_empty_body_returns_422(self) -> None:
        client = _make_app()
        resp = client.post("/v1/auth/token", json={})
        assert resp.status_code == 422

    def test_response_model_is_token_response(self) -> None:
        """Ensure the response is deserializable as TokenResponse."""
        user = _make_user()
        client = _make_app(mock_user=user)
        resp = client.post("/v1/auth/token", json={"username": "alice", "password": "secret"})
        parsed = TokenResponse.model_validate(resp.json())
        assert parsed.token_type == "bearer"
        assert len(parsed.access_token) > 0


# ── create_access_token / decode_token ─────────────────────────────────────────


class TestJWTHelpers:
    def test_round_trip_preserves_sub_and_role(self) -> None:
        token = create_access_token(user_id="u-001", role="user")
        payload = decode_token(token)
        assert payload["sub"] == "u-001"
        assert payload["role"] == "user"

    def test_admin_role_preserved(self) -> None:
        token = create_access_token(user_id="u-adm", role="admin")
        payload = decode_token(token)
        assert payload["role"] == "admin"

    def test_expired_token_raises_401(self) -> None:
        from fastapi import HTTPException

        token = create_access_token(
            user_id="u-001",
            role="user",
            expires_delta=timedelta(seconds=-1),
        )
        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)
        assert exc_info.value.status_code == 401

    def test_tampered_token_raises_401(self) -> None:
        from fastapi import HTTPException

        token = create_access_token(user_id="u-001", role="user")
        bad_token = token + "tampered"
        with pytest.raises(HTTPException) as exc_info:
            decode_token(bad_token)
        assert exc_info.value.status_code == 401

    def test_wrong_secret_raises_401(self) -> None:
        from fastapi import HTTPException

        bad_token = jwt.encode({"sub": "u-1", "role": "user"}, "wrong-secret", algorithm="HS256")
        with pytest.raises(HTTPException) as exc_info:
            decode_token(bad_token)
        assert exc_info.value.status_code == 401

    def test_custom_expires_delta(self) -> None:
        import time

        token = create_access_token(
            user_id="u-001",
            role="user",
            expires_delta=timedelta(hours=2),
        )
        payload = decode_token(token)
        # exp should be roughly 2 hours from now (within 10s margin)
        assert payload["exp"] > time.time() + 7190


# ── verify_password / get_password_hash ────────────────────────────────────────


class TestPasswordHelpers:
    def test_hash_and_verify_round_trip(self) -> None:
        hashed = get_password_hash("my-password")
        assert verify_password("my-password", hashed)

    def test_wrong_password_fails_verify(self) -> None:
        hashed = get_password_hash("correct")
        assert not verify_password("wrong", hashed)

    def test_hash_is_not_plaintext(self) -> None:
        hashed = get_password_hash("plain")
        assert hashed != "plain"

    def test_two_hashes_differ(self) -> None:
        """bcrypt uses a random salt — same password produces different hashes."""
        h1 = get_password_hash("same-password")
        h2 = get_password_hash("same-password")
        assert h1 != h2

    def test_both_hashes_verify_correctly(self) -> None:
        h1 = get_password_hash("same-password")
        h2 = get_password_hash("same-password")
        assert verify_password("same-password", h1)
        assert verify_password("same-password", h2)


# ── require_user / require_admin ───────────────────────────────────────────────


class TestRBACDependencies:
    """
    Verify require_user and require_admin FastAPI dependencies work correctly.
    These are defined in Packet 7.1 and wired to endpoints in Packet 7.2.
    """

    def _make_protected_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/protected-user")
        def protected_user(user: CurrentUser = require_user.__wrapped__ if hasattr(require_user, "__wrapped__") else require_user) -> dict[str, str]:  # type: ignore[attr-defined]
            return {"user_id": user.user_id, "role": user.role}

        @app.get("/protected-admin")
        def protected_admin(user: CurrentUser = require_admin.__wrapped__ if hasattr(require_admin, "__wrapped__") else require_admin) -> dict[str, str]:  # type: ignore[attr-defined]
            return {"user_id": user.user_id, "role": user.role}

        return app

    def test_require_admin_rejects_user_role_with_403(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from fastapi import Depends

        application = FastAPI()

        @application.get("/admin-only")
        def admin_only(user: CurrentUser = Depends(require_admin)) -> dict[str, str]:
            return {"user_id": user.user_id}

        client = TestClient(application, raise_server_exceptions=False)
        user_token = create_access_token(user_id="u-user", role="user")
        resp = client.get(
            "/admin-only", headers={"Authorization": f"Bearer {user_token}"}
        )
        assert resp.status_code == 403

    def test_require_admin_accepts_admin_role(self) -> None:
        from fastapi import FastAPI, Depends
        from fastapi.testclient import TestClient

        application = FastAPI()

        @application.get("/admin-only")
        def admin_only(user: CurrentUser = Depends(require_admin)) -> dict[str, str]:
            return {"user_id": user.user_id, "role": user.role}

        client = TestClient(application)
        admin_token = create_access_token(user_id="u-adm", role="admin")
        resp = client.get(
            "/admin-only", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_require_user_accepts_user_role(self) -> None:
        from fastapi import FastAPI, Depends
        from fastapi.testclient import TestClient

        application = FastAPI()

        @application.get("/user-endpoint")
        def user_ep(user: CurrentUser = Depends(require_user)) -> dict[str, str]:
            return {"user_id": user.user_id, "role": user.role}

        client = TestClient(application)
        token = create_access_token(user_id="u-001", role="user")
        resp = client.get(
            "/user-endpoint", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "u-001"

    def test_require_user_rejects_missing_token_with_401_or_403(self) -> None:
        from fastapi import FastAPI, Depends
        from fastapi.testclient import TestClient

        application = FastAPI()

        @application.get("/user-endpoint")
        def user_ep(user: CurrentUser = Depends(require_user)) -> dict[str, str]:
            return {"user_id": user.user_id}

        client = TestClient(application, raise_server_exceptions=False)
        resp = client.get("/user-endpoint")
        assert resp.status_code in (401, 403)

    def test_require_user_rejects_expired_token_with_401(self) -> None:
        from fastapi import FastAPI, Depends
        from fastapi.testclient import TestClient

        application = FastAPI()

        @application.get("/user-endpoint")
        def user_ep(user: CurrentUser = Depends(require_user)) -> dict[str, str]:
            return {"user_id": user.user_id}

        client = TestClient(application, raise_server_exceptions=False)
        expired_token = create_access_token(
            user_id="u-001",
            role="user",
            expires_delta=timedelta(seconds=-1),
        )
        resp = client.get(
            "/user-endpoint", headers={"Authorization": f"Bearer {expired_token}"}
        )
        assert resp.status_code == 401
