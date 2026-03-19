"""
shared.schemas.ucf
------------------
Universal Collection Framework — foundational geometric and dimensional types
shared across all IEP services and EEP schemas.

Exported:
    Dimensions              — width × height of an image in pixels
    BoundingBox             — axis-aligned bounding box; x_min < x_max, y_min < y_max
    TransformRecord         — full geometric transform record; crop_box within original_dimensions
    ProcessingContext       — canonical processing context;
                             canonical_dimensions == post_preprocessing_dimensions
    validate_bbox_in_context — validates a BoundingBox lies within ProcessingContext
                               canonical dimensions
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, model_validator


class Dimensions(BaseModel):
    """Width × height of an image in pixels. Both dimensions must be >= 1."""

    width: Annotated[int, Field(ge=1)]
    height: Annotated[int, Field(ge=1)]


class BoundingBox(BaseModel):
    """
    Axis-aligned bounding box in pixel or float coordinates.

    Spec validators:
      - x_min < x_max
      - y_min < y_max
    """

    x_min: float
    y_min: float
    x_max: float
    y_max: float

    @model_validator(mode="after")
    def check_ordering(self) -> BoundingBox:
        if self.x_min >= self.x_max:
            raise ValueError(
                f"x_min ({self.x_min}) must be strictly less than x_max ({self.x_max})"
            )
        if self.y_min >= self.y_max:
            raise ValueError(
                f"y_min ({self.y_min}) must be strictly less than y_max ({self.y_max})"
            )
        return self


class TransformRecord(BaseModel):
    """
    Full geometric transform record produced by IEP1C normalization.

    Records the original image dimensions, the crop box applied, the deskew angle,
    and the resulting output dimensions.

    Spec validator:
      - crop_box must lie within original_dimensions
        (0 <= x_min, 0 <= y_min, x_max <= width, y_max <= height)
    """

    original_dimensions: Dimensions
    crop_box: BoundingBox
    deskew_angle_deg: float
    post_preprocessing_dimensions: Dimensions

    @model_validator(mode="after")
    def crop_box_within_original(self) -> TransformRecord:
        w = self.original_dimensions.width
        h = self.original_dimensions.height
        cb = self.crop_box
        if cb.x_min < 0 or cb.y_min < 0 or cb.x_max > w or cb.y_max > h:
            raise ValueError(
                f"crop_box ({cb.x_min}, {cb.y_min}, {cb.x_max}, {cb.y_max}) "
                f"must lie within original_dimensions ({w}×{h})"
            )
        return self


class ProcessingContext(BaseModel):
    """
    Canonical processing context for a page artifact.

    Spec validator:
      - canonical_dimensions must equal transform.post_preprocessing_dimensions
    """

    canonical_dimensions: Dimensions
    transform: TransformRecord

    @model_validator(mode="after")
    def canonical_matches_post(self) -> ProcessingContext:
        cd = self.canonical_dimensions
        pd = self.transform.post_preprocessing_dimensions
        if cd.width != pd.width or cd.height != pd.height:
            raise ValueError(
                f"canonical_dimensions ({cd.width}×{cd.height}) must equal "
                f"transform.post_preprocessing_dimensions ({pd.width}×{pd.height})"
            )
        return self


def validate_bbox_in_context(bbox: BoundingBox, ctx: ProcessingContext) -> None:
    """
    Validate that bbox lies within the canonical dimensions of ctx.

    Raises:
        ValueError: if bbox is out of bounds.
    """
    w = ctx.canonical_dimensions.width
    h = ctx.canonical_dimensions.height
    if bbox.x_min < 0 or bbox.y_min < 0 or bbox.x_max > w or bbox.y_max > h:
        raise ValueError(
            f"bbox ({bbox.x_min}, {bbox.y_min}, {bbox.x_max}, {bbox.y_max}) "
            f"is outside canonical_dimensions ({w}×{h})"
        )
