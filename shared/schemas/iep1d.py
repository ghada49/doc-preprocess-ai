"""
shared.schemas.iep1d
--------------------
IEP1D (UVDoc rectification fallback) request and response schemas.

IEP1D is invoked by EEP when artifact validation fails after IEP1C normalization
or when first-pass geometry trust is insufficient. It improves visual quality only
and does not redefine page structure.

Endpoint: POST /v1/rectify (port 8003)

Exported:
    RectifyRequest   — request sent to IEP1D
    RectifyResponse  — response from IEP1D
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

MaterialType = Literal["book", "newspaper", "archival_document"]


class RectifyRequest(BaseModel):
    """
    Request sent to IEP1D POST /v1/rectify.

    Fields:
        job_id        — job identifier
        page_number   — 1-indexed page number (>= 1)
        image_uri     — URI of the normalized artifact to rectify
        material_type — one of book, newspaper, archival_document
    """

    job_id: str
    page_number: Annotated[int, Field(ge=1)]
    image_uri: str
    material_type: MaterialType


class RectifyResponse(BaseModel):
    """
    Response from IEP1D POST /v1/rectify.

    Fields:
        rectified_image_uri       — URI of the rectified artifact in storage
        rectification_confidence  — confidence of the rectification result [0, 1]
        skew_residual_before      — skew residual before rectification (>= 0)
        skew_residual_after       — skew residual after rectification (>= 0)
        border_score_before       — border quality before rectification [0, 1]
        border_score_after        — border quality after rectification [0, 1]
        processing_time_ms        — wall-clock elapsed time in ms (>= 0)
        warnings                  — advisory messages; empty list if none
    """

    rectified_image_uri: str
    rectification_confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    skew_residual_before: Annotated[float, Field(ge=0.0)]
    skew_residual_after: Annotated[float, Field(ge=0.0)]
    border_score_before: Annotated[float, Field(ge=0.0, le=1.0)]
    border_score_after: Annotated[float, Field(ge=0.0, le=1.0)]
    processing_time_ms: Annotated[float, Field(ge=0.0)]
    warnings: list[str]
