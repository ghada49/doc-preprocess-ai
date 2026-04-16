"""
shared.schemas.preprocessing
-----------------------------
Preprocessing pipeline schemas produced by IEP1C and consumed by EEP.

Note: PreprocessRequest has been removed (spec Section 12.1).
      The external job entry point is JobCreateRequest (eep.py).
      The internal normalization input is NormalizeRequest (normalization.py).

Exported:
    DeskewResult            — deskew operation record
    CropResult              — crop operation record
    SplitResult             — split detection record
    QualityMetrics          — artifact quality metrics
    PreprocessBranchResponse — canonical post-normalization output of IEP1C
    PreprocessError         — error response from IEP1A/IEP1B geometry inference
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from shared.schemas.ucf import BoundingBox, TransformRecord


class DeskewResult(BaseModel):
    """
    Deskew operation record from IEP1C.

    Fields:
        angle_deg     — applied deskew angle in degrees
        residual_deg  — remaining skew after deskew (>= 0)
        method        — e.g. "geometry_quad", "geometry_bbox"
    """

    angle_deg: float
    residual_deg: Annotated[float, Field(ge=0.0)]
    method: str


class CropResult(BaseModel):
    """
    Crop operation record from IEP1C.

    Fields:
        crop_box      — applied crop bounds (BoundingBox from ucf.py)
        border_score  — quality signal for border accuracy in [0, 1]
        method        — e.g. "geometry_quad", "geometry_bbox"
    """

    crop_box: BoundingBox
    border_score: Annotated[float, Field(ge=0.0, le=1.0)]
    method: str


class SplitResult(BaseModel):
    """
    Split detection record from IEP1C.

    Fields:
        split_required    — True when page_count == 2
        split_x           — horizontal split coordinate in pixels (>= 0); None when no split
        split_confidence  — min(weakest_instance_confidence, tta_structural_agreement_rate);
                            None when split_required=False
        method            — e.g. "instance_boundary"
    """

    split_required: bool
    split_x: Annotated[int, Field(ge=0)] | None = None
    split_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    method: str


class QualityMetrics(BaseModel):
    """
    Artifact quality metrics computed by IEP1C on the normalized output.

    Fields:
        skew_residual       — remaining skew after normalization (>= 0)
        blur_score          — sharpness signal in [0, 1]; higher is better
        border_score        — border accuracy signal in [0, 1]; higher is better
        split_confidence    — split detection confidence in [0, 1]; None when no split
        foreground_coverage — fraction of image covered by content in [0, 1]
    """

    skew_residual: Annotated[float, Field(ge=0.0)]
    blur_score: Annotated[float, Field(ge=0.0, le=1.0)]
    border_score: Annotated[float, Field(ge=0.0, le=1.0)]
    split_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    foreground_coverage: Annotated[float, Field(ge=0.0, le=1.0)]


class PreprocessBranchResponse(BaseModel):
    """
    Canonical post-normalization output of IEP1C.

    This is the input to the EEP artifact validation gate and the canonical
    preprocessing result stored in lineage.

    Fields:
        processed_image_uri — URI of the normalized artifact in storage
        deskew              — deskew operation record
        crop                — crop operation record
        split               — split detection record
        quality             — artifact quality metrics
        transform           — full geometric transform record (from ucf.py)
        source_model        — which geometry model was selected ("iep1a" or "iep1b")
        processing_time_ms  — wall-clock elapsed time in ms (>= 0)
        warnings            — advisory messages; empty list if none
    """

    processed_image_uri: str
    deskew: DeskewResult
    crop: CropResult
    split: SplitResult
    quality: QualityMetrics
    transform: TransformRecord
    source_model: Literal["iep1a", "iep1b"]
    processing_time_ms: Annotated[float, Field(ge=0.0)]
    warnings: list[str]


class PreprocessError(BaseModel):
    """
    Error response from IEP1A or IEP1B geometry inference.

    fallback_action semantics (EEP interpretation — advisory signals only):
        RETRY            — EEP may retry within the configured retry budget.
        ESCALATE_REVIEW  — EEP must record pending_human_correction; no silent data loss.
    """

    error_code: Literal[
        "INVALID_IMAGE",
        "UNSUPPORTED_FORMAT",
        "TIMEOUT",
        "INTERNAL",
        "GEOMETRY_FAILED",
        "CLASSIFICATION_FAILED",
    ]
    error_message: str
    fallback_action: Literal["RETRY", "ESCALATE_REVIEW"]
