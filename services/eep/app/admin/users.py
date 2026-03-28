"""
services/eep/app/admin/users.py
---------------------------------
Packet 7.6 — User management endpoints.

Implements:
  POST  /v1/users
  GET   /v1/users
  PATCH /v1/users/{user_id}/deactivate

All three endpoints require `require_admin`.  Non-admin callers receive 403.

--- POST /v1/users ---

Creates a new user account.  The plaintext password is hashed via bcrypt
(get_password_hash from auth.py, spec Section 7.6) before storage.
user_id is generated as a UUID4 string.

Request body fields:
  username   — must be unique; 409 if already taken.
  password   — plaintext; stored only as bcrypt hash.
  role       — 'user' or 'admin'.

Response (201): UserRecord — user fields excluding hashed_password.

--- GET /v1/users ---

Returns all registered user accounts ordered by created_at ascending.
hashed_password is never included in any response.

Response (200): list[UserRecord]

--- PATCH /v1/users/{user_id}/deactivate ---

Sets is_active = False for the given user_id.
404 if user_id does not exist.
Idempotent: deactivating an already-inactive user is not an error.

Response (200): UserRecord with is_active = False.

--- Error responses ---
  401 — missing or invalid bearer token
  403 — caller does not have the 'admin' role
  404 — (deactivate only) user_id not found
  409 — (create only) username already in use

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, get_password_hash, require_admin
from services.eep.app.db.models import User
from services.eep.app.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])


# ── Request / response schemas ────────────────────────────────────────────────


class CreateUserRequest(BaseModel):
    """Request body for POST /v1/users."""

    username: str
    password: str
    role: str


class UserRecord(BaseModel):
    """
    User account representation returned by all user management endpoints.

    hashed_password is intentionally excluded.
    """

    user_id: str
    username: str
    role: str
    is_active: bool
    created_at: datetime


# ── Helper ────────────────────────────────────────────────────────────────────


def _to_record(user: User) -> UserRecord:
    return UserRecord(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post(
    "/v1/users",
    response_model=UserRecord,
    status_code=201,
    summary="Create user account",
)
def create_user(
    body: CreateUserRequest,
    db: Session = Depends(get_session),
    _user: CurrentUser = Depends(require_admin),
) -> UserRecord:
    """
    Create a new user account.

    The plaintext password is hashed before storage; the hash is never
    returned.  Returns 409 if the username is already taken.

    **Auth:** admin role required.
    """
    new_user = User(
        user_id=str(uuid.uuid4()),
        username=body.username,
        hashed_password=get_password_hash(body.password),
        role=body.role,
        is_active=True,
    )
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username {body.username!r} is already taken",
        )
    db.refresh(new_user)
    logger.debug("create_user: user_id=%s username=%s role=%s", new_user.user_id, new_user.username, new_user.role)
    return _to_record(new_user)


@router.get(
    "/v1/users",
    response_model=list[UserRecord],
    status_code=200,
    summary="List all user accounts",
)
def list_users(
    db: Session = Depends(get_session),
    _user: CurrentUser = Depends(require_admin),
) -> list[UserRecord]:
    """
    Return all user accounts ordered by created_at ascending.

    hashed_password is never included.

    **Auth:** admin role required.
    """
    users: list[User] = db.query(User).order_by(User.created_at.asc()).all()
    logger.debug("list_users: count=%d", len(users))
    return [_to_record(u) for u in users]


@router.patch(
    "/v1/users/{user_id}/deactivate",
    response_model=UserRecord,
    status_code=200,
    summary="Deactivate a user account",
)
def deactivate_user(
    user_id: str,
    db: Session = Depends(get_session),
    _user: CurrentUser = Depends(require_admin),
) -> UserRecord:
    """
    Set ``is_active = False`` for the given user account.

    Idempotent: deactivating an already-inactive account is not an error.
    Returns 404 if ``user_id`` does not exist.

    **Auth:** admin role required.
    """
    user: User | None = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id!r} not found",
        )
    user.is_active = False
    db.commit()
    db.refresh(user)
    logger.debug("deactivate_user: user_id=%s", user_id)
    return _to_record(user)
