"""
services/eep/app/policy_api.py
--------------------------------
Packet 8.2 — Policy read/update endpoints.

Implements:
  GET   /v1/policy   — read current policy (admin only)
  PATCH /v1/policy   — update policy (admin only)

--- GET /v1/policy ---

Returns the most recently applied policy version record.
404 if no policy has been applied yet.

Response (200): PolicyRecord — {version, config_yaml, applied_at, applied_by, justification}

--- PATCH /v1/policy ---

Creates a new policy_versions row, bumping the version.

Request body fields:
  config_yaml   — full policy YAML string (must be valid YAML)
  justification — reason for the change (required)
  audit_evidence     — required if current policy has
                       threshold_adjustment_requires_audit=true
  slo_validation     — required if current policy has
                       threshold_adjustment_requires_slo_validation=true

Guardrail enforcement (spec Section 8.4):
  "A threshold change must be rejected unless supporting audit evidence
   and SLO validation are recorded."
  Both flags are read from the CURRENT active policy's config_yaml.
  If no active policy exists, guardrail checks are skipped (first-time setup).

Version generation: "v{N}" where N = count of existing records + 1.
applied_by is set from the caller's JWT sub claim (CurrentUser.user_id).

Response (200): PolicyRecord with the newly applied version.

--- Error responses ---
  400 — config_yaml is not valid YAML
  401 — missing or invalid bearer token
  403 — caller does not have the 'admin' role
  404 — (GET only) no policy has been applied yet
  422 — guardrail not satisfied (audit_evidence or slo_validation missing)

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_admin
from services.eep.app.db.models import PolicyVersion
from services.eep.app.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["policy"])


# ── Request / response schemas ────────────────────────────────────────────────


class PolicyRecord(BaseModel):
    """Policy version record returned by both policy endpoints."""

    version: str
    config_yaml: str
    applied_at: datetime
    applied_by: str
    justification: str


class UpdatePolicyRequest(BaseModel):
    """Request body for PATCH /v1/policy."""

    config_yaml: str
    justification: str
    audit_evidence: str | None = None
    slo_validation: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────────────


def _to_record(pv: PolicyVersion) -> PolicyRecord:
    return PolicyRecord(
        version=pv.version,
        config_yaml=pv.config_yaml,
        applied_at=pv.applied_at,
        applied_by=pv.applied_by,
        justification=pv.justification,
    )


def _current_policy(db: Session) -> PolicyVersion | None:
    """Return the most recently applied policy record, or None."""
    return (
        db.query(PolicyVersion)
        .order_by(PolicyVersion.applied_at.desc())
        .first()
    )


def _parse_yaml_or_400(config_yaml: str) -> dict[str, Any]:
    """Parse YAML string; raise HTTP 400 if invalid."""
    try:
        parsed = yaml.safe_load(config_yaml)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"config_yaml is not valid YAML: {exc}",
        )
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="config_yaml must be a YAML mapping (dict) at the top level",
        )
    return parsed


def _next_version(db: Session) -> str:
    """Return the next version string ("v1", "v2", …)."""
    count: int = db.query(PolicyVersion).count()
    return f"v{count + 1}"


def _enforce_guardrails(
    current_config: dict[str, Any],
    body: UpdatePolicyRequest,
) -> None:
    """
    Enforce threshold-adjustment guardrails from the current active policy.

    spec Section 8.4:
      threshold_adjustment_requires_audit: true
        → audit_evidence must be provided in the PATCH request.
      threshold_adjustment_requires_slo_validation: true
        → slo_validation must be provided in the PATCH request.
    """
    preprocessing = current_config.get("preprocessing", {}) or {}

    if preprocessing.get("threshold_adjustment_requires_audit", False):
        if not body.audit_evidence:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Current policy requires audit_evidence for threshold changes "
                    "(threshold_adjustment_requires_audit=true)"
                ),
            )

    if preprocessing.get("threshold_adjustment_requires_slo_validation", False):
        if not body.slo_validation:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Current policy requires slo_validation for threshold changes "
                    "(threshold_adjustment_requires_slo_validation=true)"
                ),
            )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get(
    "/v1/policy",
    response_model=PolicyRecord,
    status_code=200,
    summary="Read current policy",
)
def get_policy(
    db: Session = Depends(get_session),
    _user: CurrentUser = Depends(require_admin),
) -> PolicyRecord:
    """
    Return the most recently applied policy version.

    Returns 404 if no policy has been applied yet.

    **Auth:** admin role required.
    """
    pv = _current_policy(db)
    if pv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No policy has been applied yet",
        )
    logger.debug("get_policy: version=%s applied_by=%s", pv.version, pv.applied_by)
    return _to_record(pv)


@router.patch(
    "/v1/policy",
    response_model=PolicyRecord,
    status_code=200,
    summary="Update policy",
)
def update_policy(
    body: UpdatePolicyRequest,
    db: Session = Depends(get_session),
    caller: CurrentUser = Depends(require_admin),
) -> PolicyRecord:
    """
    Apply a new policy version.

    Validates that ``config_yaml`` is well-formed YAML.  If the current active
    policy has ``threshold_adjustment_requires_audit=true`` or
    ``threshold_adjustment_requires_slo_validation=true``, the corresponding
    fields must be present in the request body (spec Section 8.4).

    Version is auto-incremented ("v1", "v2", …).
    ``applied_by`` is set from the caller's JWT ``sub`` claim.

    **Auth:** admin role required.
    """
    # Validate incoming YAML before anything else
    _parse_yaml_or_400(body.config_yaml)

    # Enforce guardrails from current active policy (if any)
    current = _current_policy(db)
    if current is not None:
        current_config = _parse_yaml_or_400(current.config_yaml)
        _enforce_guardrails(current_config, body)

    new_version = _next_version(db)

    pv = PolicyVersion(
        version=new_version,
        config_yaml=body.config_yaml,
        applied_by=caller.user_id,
        justification=body.justification,
    )
    db.add(pv)
    db.commit()
    db.refresh(pv)

    logger.info(
        "update_policy: version=%s applied_by=%s",
        pv.version,
        pv.applied_by,
    )
    return _to_record(pv)
