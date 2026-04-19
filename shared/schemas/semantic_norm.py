"""
shared.schemas.semantic_norm
-----------------------------
Schemas for IEP1E — semantic normalization step.

IEP1E resolves page orientation (0 / 90 / 180 / 270 °) and spread reading
order (LTR / RTL) using PaddleOCR as a decision signal, not a text reader.

Exported:
    ScriptEvidence          — per-page script character-ratio evidence
    PageOrientationResult   — orientation decision for one page
    SemanticNormPageResult  — per-page IEP1E output (orientation + oriented URI)
    SemanticNormResponse    — full IEP1E response (1 or 2 pages)
    SemanticNormRequest     — IEP1E request payload
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class ScriptEvidence(BaseModel):
    """
    Script character-ratio evidence extracted from OCR output for one page.

    Fields:
        arabic_ratio   — fraction of alphanumeric characters that are Arabic
        latin_ratio    — fraction of alphanumeric characters that are Latin
        garbage_ratio  — fraction of characters that are neither Arabic nor Latin
        n_boxes        — number of text boxes returned by OCR (>= 0)
        n_chars        — total alphanumeric characters seen (>= 0)
        mean_conf      — mean OCR confidence across all boxes in [0, 1]
    """

    arabic_ratio: Annotated[float, Field(ge=0.0, le=1.0)]
    latin_ratio: Annotated[float, Field(ge=0.0, le=1.0)]
    garbage_ratio: Annotated[float, Field(ge=0.0, le=1.0)]
    n_boxes: Annotated[int, Field(ge=0)]
    n_chars: Annotated[int, Field(ge=0)]
    mean_conf: Annotated[float, Field(ge=0.0, le=1.0)]


class PageOrientationResult(BaseModel):
    """
    Orientation decision for one page crop.

    Fields:
        best_rotation_deg      — chosen rotation: 0, 90, 180, or 270 degrees
        orientation_confident  — True when confidence gate passed
                                 (ratio >= 1.2 AND diff >= 15)
        score_ratio            — best_score / second_best_score
        score_diff             — best_score - second_best_score
        script_evidence        — character-ratio evidence at the best rotation
    """

    best_rotation_deg: Literal[0, 90, 180, 270]
    orientation_confident: bool
    score_ratio: float
    score_diff: float
    script_evidence: ScriptEvidence


class SemanticNormPageResult(BaseModel):
    """
    Per-page output of IEP1E.

    Fields:
        original_uri    — URI of the IEP1C-normalized artifact (input)
        oriented_uri    — URI of the orientation-corrected artifact
                          (same as original_uri when best_rotation_deg == 0)
        sub_page_index  — physical sub-page index (0 = left, 1 = right)
        orientation     — orientation decision
    """

    original_uri: str
    oriented_uri: str
    sub_page_index: int
    orientation: PageOrientationResult


class SemanticNormResponse(BaseModel):
    """
    Full IEP1E response for one page or a two-page spread.

    Fields:
        pages               — per-page results; length 1 or 2
        reading_direction   — "ltr", "rtl", or "unresolved"
        ordered_page_uris   — oriented URIs in reading order
                              (for single pages this is always [oriented_uri])
        fallback_used       — True when geometry-only fallback was used
                              (blank pages on both sides, or OCR unavailable)
        processing_time_ms  — wall-clock elapsed time in ms
        warnings            — advisory messages; empty list if none
    """

    pages: list[SemanticNormPageResult]
    reading_direction: Literal["ltr", "rtl", "unresolved"]
    ordered_page_uris: list[str]
    fallback_used: bool
    processing_time_ms: Annotated[float, Field(ge=0.0)]
    warnings: list[str]


class SemanticNormRequest(BaseModel):
    """
    IEP1E request payload.

    Fields:
        job_id            — parent job identifier
        page_number       — 1-indexed page number
        page_uris         — 1 or 2 storage URIs; physical left-first
        x_centers         — physical x-center of each page crop (pixels)
        sub_page_indices  — sub_page_index for each URI (0-based)
        material_type     — job material type string
    """

    job_id: str
    page_number: int
    page_uris: list[str]
    x_centers: list[float]
    sub_page_indices: list[int]
    material_type: str
