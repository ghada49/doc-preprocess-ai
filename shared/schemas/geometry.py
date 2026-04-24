"""
shared.schemas.geometry
-----------------------
IEP1A / IEP1B geometry request and response schemas.

Both IEP1A (YOLOv8-seg) and IEP1B (YOLOv8-pose) share the identical
GeometryRequest / GeometryResponse contract.

Exported:
    GeometryRequest   — request sent to IEP1A or IEP1B
    PageRegion        — single detected page region within a GeometryResponse
    GeometryResponse  — response from IEP1A or IEP1B
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

MaterialType = Literal["book", "newspaper", "archival_document", "microfilm"]


class GeometryRequest(BaseModel):
    """
    Request sent to IEP1A (POST /v1/geometry) or IEP1B (POST /v1/geometry).

    Fields:
        job_id        — job identifier
        page_number   — 1-indexed page number (>= 1)
        image_uri     — URI of the proxy/downscaled image
        material_type — one of book, newspaper, archival_document
    """

    job_id: str
    page_number: Annotated[int, Field(ge=1)]
    image_uri: str
    material_type: MaterialType


class PageRegion(BaseModel):
    """
    Single detected page region within a GeometryResponse.

    Fields:
        region_id         — e.g. "page_0", "page_1"
        geometry_type     — representation origin: quadrilateral, mask_ref, or bbox
        corners           — 4 (x, y) corner pairs when geometry_type == "quadrilateral"
        bbox              — bounding box as (x_min, y_min, x_max, y_max) in pixels;
                            always present in practice
        confidence        — per-instance detection confidence in [0, 1]
        page_area_fraction — detected page area as fraction of full image area in [0, 1]

    Validator:
        When geometry_type == "quadrilateral", corners must contain exactly 4 points.
    """

    region_id: str
    geometry_type: Literal["quadrilateral", "mask_ref", "bbox"]
    corners: list[tuple[float, float]] | None = None
    bbox: tuple[int, int, int, int] | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    page_area_fraction: Annotated[float, Field(ge=0.0, le=1.0)]

    @model_validator(mode="after")
    def check_quadrilateral_corners(self) -> PageRegion:
        if self.geometry_type == "quadrilateral":
            if self.corners is None or len(self.corners) != 4:
                raise ValueError(
                    "geometry_type='quadrilateral' requires exactly 4 corners; "
                    f"got {len(self.corners) if self.corners is not None else None}"
                )
        return self


class GeometryResponse(BaseModel):
    """
    Response from IEP1A or IEP1B geometry inference.

    Fields:
        page_count                  — number of detected pages (1 or 2)
        pages                       — list of PageRegion; length must equal page_count
        split_required              — True when page_count == 2
        split_x                     — horizontal split coordinate in pixels (>= 0);
                                      None for single-page scans
        geometry_confidence         — min confidence across all detected instances [0, 1]
        tta_structural_agreement_rate — fraction of TTA passes agreeing on
                                        page_count + split_required [0, 1]
        tta_prediction_variance     — inter-pass variance of geometry predictions (>= 0)
        tta_passes                  — number of TTA passes performed (>= 1)
        uncertainty_flags           — advisory flags; empty list if none
        warnings                    — advisory messages; empty list if none
        processing_time_ms          — wall-clock elapsed time in ms (>= 0)

    Validator:
        len(pages) must equal page_count.
    """

    page_count: Annotated[int, Field(ge=1, le=2)]
    pages: list[PageRegion]
    split_required: bool
    split_x: Annotated[int, Field(ge=0)] | None = None
    geometry_confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    tta_structural_agreement_rate: Annotated[float, Field(ge=0.0, le=1.0)]
    tta_prediction_variance: Annotated[float, Field(ge=0.0)]
    tta_passes: Annotated[int, Field(ge=1)]
    uncertainty_flags: list[str]
    warnings: list[str]
    processing_time_ms: Annotated[float, Field(ge=0.0)]

    @model_validator(mode="after")
    def pages_match_page_count(self) -> GeometryResponse:
        if len(self.pages) != self.page_count:
            raise ValueError(
                f"len(pages) ({len(self.pages)}) must equal page_count ({self.page_count})"
            )
        return self
