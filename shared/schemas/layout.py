"""
shared.schemas.layout
---------------------
IEP2A / IEP2B layout detection schemas and layout consensus result schema.

Both IEP2A (Detectron2) and IEP2B (DocLayout-YOLO) share the identical
LayoutDetectRequest / LayoutDetectResponse contract. The detector_type field
distinguishes which service produced the response.

Exported:
    RegionType           — canonical 5-class layout region type enum
    Region               — single layout region with id, type, bbox, confidence
    LayoutConfSummary    — mean confidence and low-confidence fraction summary
    ColumnStructure      — inferred text column structure
    LayoutDetectRequest  — request sent to IEP2A or IEP2B
    LayoutDetectResponse — response from IEP2A or IEP2B
    LayoutConsensusResult — result of the EEP layout consensus gate (IEP2A vs IEP2B)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from shared.schemas.ucf import BoundingBox

MaterialType = Literal["book", "newspaper", "archival_document"]


class RegionType(StrEnum):
    """
    Canonical 5-class layout region type ontology.

    advertisement and column_separator are excluded:
    - advertisement: no public training data
    - column_separator: column structure is inferred algorithmically via DBSCAN
    """

    text_block = "text_block"
    title = "title"  # type: ignore[assignment]  # 'title' shadows str.title() — name is spec-mandated
    table = "table"
    image = "image"
    caption = "caption"


class Region(BaseModel):
    """
    Single detected layout region.

    Fields:
        id         — sequential identifier matching ^r\\d+$; unique within page
        type       — canonical RegionType
        bbox       — bounding box (from ucf.py)
        confidence — detection confidence [0, 1]

    Validator:
        id must match the pattern ^r\\d+$ (e.g. r1, r2, r3).
    """

    id: Annotated[str, Field(pattern=r"^r\d+$")]
    type: RegionType
    bbox: BoundingBox
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]


class LayoutConfSummary(BaseModel):
    """
    Confidence summary across all detected regions in a LayoutDetectResponse.

    Fields:
        mean_conf      — mean confidence across all regions [0, 1]
        low_conf_frac  — fraction of regions with confidence < 0.5 [0, 1]
    """

    mean_conf: Annotated[float, Field(ge=0.0, le=1.0)]
    low_conf_frac: Annotated[float, Field(ge=0.0, le=1.0)]


class ColumnStructure(BaseModel):
    """
    Inferred text column structure for a page.

    Fields:
        column_count      — number of inferred text columns (>= 1)
        column_boundaries — x-coordinates of column dividers as fractions of page width;
                            length must equal column_count − 1;
                            values must be in [0, 1] and sorted ascending.
                            A single-column page has column_count=1 and column_boundaries=[].

    Validators:
        len(column_boundaries) == column_count − 1
        all values in [0, 1]
        values sorted ascending (strictly)
    """

    column_count: Annotated[int, Field(ge=1)]
    column_boundaries: list[float]

    @model_validator(mode="after")
    def check_column_boundaries(self) -> ColumnStructure:
        n = self.column_count
        bounds = self.column_boundaries
        expected_len = n - 1
        if len(bounds) != expected_len:
            raise ValueError(
                f"column_boundaries length ({len(bounds)}) must equal "
                f"column_count − 1 ({expected_len})"
            )
        for i, v in enumerate(bounds):
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"column_boundaries[{i}] = {v} must be in [0, 1]")
        for i in range(1, len(bounds)):
            if bounds[i] <= bounds[i - 1]:
                raise ValueError(
                    f"column_boundaries must be sorted strictly ascending: "
                    f"bounds[{i - 1}]={bounds[i - 1]} >= bounds[{i}]={bounds[i]}"
                )
        return self


class LayoutDetectRequest(BaseModel):
    """
    Request sent to IEP2A (POST /v1/layout-detect) or IEP2B (POST /v1/layout-detect).

    Fields:
        job_id        — job identifier
        page_number   — 1-indexed page number (>= 1)
        image_uri     — URI of the page artifact to analyse
        material_type — one of book, newspaper, archival_document
    """

    job_id: str
    page_number: Annotated[int, Field(ge=1)]
    image_uri: str
    material_type: MaterialType


class LayoutDetectResponse(BaseModel):
    """
    Response from IEP2A or IEP2B layout detection.

    Fields:
        region_schema_version — schema version tag; always "v1" currently
        regions               — list of detected canonical Region objects
        layout_conf_summary   — mean and low-conf-fraction confidence summary
        region_type_histogram — counts per RegionType string key
        column_structure      — inferred column layout; None if no text_block regions
        model_version         — model version string (e.g. git SHA or semver tag)
        detector_type         — "detectron2" or "paddleocr_pp_doclayout_v2" for IEP2A;
                                "doclayout_yolo" for IEP2B
        processing_time_ms    — wall-clock elapsed time in ms (>= 0)
        warnings              — advisory messages; empty list if none
    """

    region_schema_version: Literal["v1"]
    regions: list[Region]
    layout_conf_summary: LayoutConfSummary
    region_type_histogram: dict[str, int]
    column_structure: ColumnStructure | None = None
    model_version: str
    detector_type: Literal["detectron2", "doclayout_yolo", "paddleocr_pp_doclayout_v2"]
    processing_time_ms: Annotated[float, Field(ge=0.0)]
    warnings: list[str]

    @field_validator("region_type_histogram")
    @classmethod
    def histogram_values_non_negative(cls, v: dict[str, int]) -> dict[str, int]:
        for key, count in v.items():
            if count < 0:
                raise ValueError(f"region_type_histogram['{key}'] = {count} must be >= 0")
        return v


class LayoutConsensusResult(BaseModel):
    """
    Result of the EEP layout consensus gate comparing IEP2A and IEP2B outputs.

    Stored as JSONB in job_pages.layout_consensus_result.

    Fields:
        iep2a_region_count    — number of canonical regions from IEP2A
        iep2b_region_count    — number of canonical regions from IEP2B
        matched_regions       — number of one-to-one region matches
                                (IoU >= threshold AND same RegionType)
        unmatched_iep2a       — IEP2A regions with no IEP2B match
        unmatched_iep2b       — IEP2B regions with no IEP2A match
        mean_matched_iou      — mean IoU across matched region pairs
        type_histogram_match  — True when per-type count difference <= max_type_count_diff
        agreed                — True when match_ratio >= min_match_ratio AND
                                type_histogram_match; always False in single-model fallback
        consensus_confidence  — 0.6 * match_ratio + 0.2 * mean_iou +
                                0.2 * histogram_match (float in [0, 1])
        single_model_mode     — True when IEP2B was unavailable; agreed is
                                always False in this case (spec Section 7.4)
    """

    iep2a_region_count: int
    iep2b_region_count: int
    matched_regions: int
    unmatched_iep2a: int
    unmatched_iep2b: int
    mean_matched_iou: float
    type_histogram_match: bool
    agreed: bool
    consensus_confidence: float
    single_model_mode: bool
