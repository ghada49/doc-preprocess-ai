"""
services/eep/app/auth.py
-------------------------
Phase 7, Packet 7.1 — Authentication and JWT support.

Responsibilities:
  - Password hashing and verification (bcrypt via passlib).
  - JWT access token creation and decoding (HS256 via python-jose).
  - POST /v1/auth/token — the only endpoint that does not require auth.

JWT payload fields (spec Section 14 / rate-limiting Section 14):
  sub  — user_id (used as caller_id for rate limiting in Packet 7.2)
  role — "user" | "admin" (used by require_user/require_admin in Packet 7.2)
  exp  — expiry timestamp

Environment variables:
  JWT_SECRET_KEY                   — signing secret; required in production
  JWT_ALGORITHM                    — default "HS256"
  JWT_ACCESS_TOKEN_EXPIRE_MINUTES  — default 60

Exported for Packet 7.2 use (not wired into endpoints here):
  decode_token(token) -> dict
  require_user       — FastAPI dependency (defined but not injected yet)
  require_admin      — FastAPI dependency (defined but not injected yet)
  CurrentUser        — Pydantic model representing verified token claims
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.db.models import User
from services.eep.app.db.session import get_session

# ── Configuration ──────────────────────────────────────────────────────────────

_SECRET_KEY: str = os.environ.get("JWT_SECRET_KEY", "dev-secret-key-change-in-production")
_ALGORITHM: str = os.environ.get("JWT_ALGORITHM", "HS256")
_ACCESS_TOKEN_EXPIRE_MINUTES: int = int(
    os.environ.get("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
)

# ── Password helpers ───────────────────────────────────────────────────────────


def get_password_hash(password: str) -> str:
    """Return bcrypt hash of *password*. Used when creating users (Packet 7.6)."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True iff *plain_password* matches the stored bcrypt *hashed_password*."""
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


# ── JWT helpers ─────────────────────────────────────────────────────────────────


def create_access_token(
    user_id: str,
    role: str,
    expires_delta: timedelta | None = None,
) -> str:
    """
    Create a signed JWT access token.

    Args:
        user_id:       Value for the ``sub`` claim (spec: rate-limit caller_id).
        role:          Value for the ``role`` claim ("user" | "admin").
        expires_delta: Override default expiry window.

    Returns:
        Encoded JWT string.
    """
    expire = datetime.now(tz=UTC) + (
        expires_delta if expires_delta is not None else timedelta(minutes=_ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """
    Decode and validate a JWT token.

    Raises:
        HTTPException 401 if the token is invalid or expired.

    Returns:
        The token payload dict with at least ``sub`` and ``role``.
    """
    try:
        payload: dict[str, Any] = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Schemas ─────────────────────────────────────────────────────────────────────


class TokenRequest(BaseModel):
    """Request body for POST /v1/auth/token."""

    username: str
    password: str


class TokenResponse(BaseModel):
    """Response body for POST /v1/auth/token."""

    access_token: str
    token_type: str = "bearer"


class CurrentUser(BaseModel):
    """Verified token claims — passed to endpoint handlers by require_user/require_admin."""

    user_id: str
    role: str


# ── FastAPI security dependencies (wired to endpoints in Packet 7.2) ───────────

_bearer_scheme = HTTPBearer(auto_error=False)


def _extract_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    """
    FastAPI dependency: decode the Bearer token and return verified claims.

    Raises:
        HTTPException 401 — missing or invalid token.
        HTTPException 401 — expired token.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials)
    user_id: str | None = payload.get("sub")
    role: str | None = payload.get("role")
    if user_id is None or role is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing required claims",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return CurrentUser(user_id=user_id, role=role)


def require_user(current_user: CurrentUser = Depends(_extract_current_user)) -> CurrentUser:
    """
    FastAPI dependency: require a valid JWT with any role.

    Usage (Packet 7.2+)::

        @router.get("/v1/jobs")
        def list_jobs(user: CurrentUser = Depends(require_user)):
            ...
    """
    return current_user


def require_admin(current_user: CurrentUser = Depends(_extract_current_user)) -> CurrentUser:
    """
    FastAPI dependency: require a valid JWT with role == "admin".

    Raises:
        HTTPException 403 — authenticated as a non-admin user.

    Usage (Packet 7.2+)::

        @router.get("/v1/admin/dashboard-summary")
        def dashboard(user: CurrentUser = Depends(require_admin)):
            ...
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return current_user


# ── Ownership guard ────────────────────────────────────────────────────────────


def assert_job_ownership(job: Any, user: CurrentUser) -> None:
    """
    Raise HTTP 403 if *user* does not own *job* and is not an admin.

    Args:
        job:  Any object with a ``created_by`` attribute (Job ORM row).
        user: The verified token claims from the incoming request.

    Raises:
        HTTPException 403 — authenticated as a non-admin user who does not own
                            the job (``job.created_by != user.user_id``).
    """
    if user.role == "admin":
        return
    if job.created_by != user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this job",
        )


# ── Router ─────────────────────────────────────────────────────────────────────

router = APIRouter(tags=["auth"])


@router.post(
    "/v1/auth/token",
    response_model=TokenResponse,
    summary="Issue JWT access token",
    status_code=status.HTTP_200_OK,
)
def auth_token(
    body: TokenRequest,
    db: Session = Depends(get_session),
) -> TokenResponse:
    """
    Authenticate with username and password; return a signed JWT access token.

    - Looks up the user by ``username`` in the ``users`` table.
    - Verifies the plaintext password against the stored bcrypt hash.
    - Returns 401 if the username is not found, the password is wrong, or the
      account is inactive.
    - No auth header is required on this endpoint (spec Section 14).

    Response (200)::

        {
          "access_token": "<jwt>",
          "token_type": "bearer"
        }
    """
    user: User | None = db.query(User).filter(User.username == body.username).first()

    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user_id=user.user_id, role=user.role)
    return TokenResponse(access_token=token)
