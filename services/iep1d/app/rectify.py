"""
services/iep1d/app/rectify.py
------------------------------
IEP1D rectification mock router — POST /v1/rectify.

Pass-through mock implementation (Packet 4.5).
Real UVDoc model internals are deferred to Phase 12 or when the model
becomes available (roadmap Section 2.3 — "Can be mocked temporarily if needed").

The mock:
  - Accepts a valid RectifyRequest and returns a valid RectifyResponse.
  - Returns the same image_uri as the rectified artifact URI (pass-through).
  - Simulates plausible quality improvement in skew and border metrics.
  - Supports failure simulation via IEP1D_SIMULATE_FAILURE env var for
    testing the rescue path's IEP1D-unavailability handling.

Configurable env vars:
    IEP1D_SIMULATE_FAILURE  — "1" | "true" | "yes"  → HTTP 500
    IEP1D_MOCK_CONFIDENCE   — float in [0, 1]        (default: 0.82)
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from shared.schemas.iep1d import RectifyRequest, RectifyResponse

router = APIRouter()

_FAILURE_VALUES: frozenset[str] = frozenset({"1", "true", "yes"})


def _simulate_failure() -> bool:
    return os.getenv("IEP1D_SIMULATE_FAILURE", "").lower() in _FAILURE_VALUES


@router.post("/v1/rectify", response_model=RectifyResponse)
async def rectify(request: RectifyRequest) -> RectifyResponse:
    """
    Pass-through mock rectification endpoint.

    Returns deterministic mock quality metrics that simulate a modest
    improvement in border quality and skew residual after rectification.

    When IEP1D_SIMULATE_FAILURE is set, returns HTTP 500 with an
    error_code so that the rescue path's failure-handling branch can
    be exercised end-to-end without mocking the HTTP layer.
    """
    if _simulate_failure():
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "rectification_failed",
                "error_message": "IEP1D_SIMULATE_FAILURE is set — simulated failure",
            },
        )

    try:
        confidence = float(os.getenv("IEP1D_MOCK_CONFIDENCE", "0.82"))
        confidence = max(0.0, min(1.0, confidence))
    except ValueError:
        confidence = 0.82

    return RectifyResponse(
        rectified_image_uri=request.image_uri,  # pass-through: same artifact URI
        rectification_confidence=confidence,
        skew_residual_before=2.4,
        skew_residual_after=0.3,
        border_score_before=0.61,
        border_score_after=0.89,
        processing_time_ms=115.0,
        warnings=[],
    )
