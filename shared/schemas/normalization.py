"""
shared.schemas.normalization
-----------------------------
IEP1C normalization internal schema.

NormalizeRequest is the internal input to the IEP1C shared module (not a
network endpoint). It is never transmitted over HTTP; it is constructed by EEP
and passed directly to the shared normalization module.

Exported:
    NormalizeRequest — internal input schema for the IEP1C shared module
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from shared.schemas.geometry import GeometryResponse

MaterialType = Literal["book", "newspaper", "archival_document"]


class NormalizeRequest(BaseModel):
    """
    Internal input to the IEP1C shared normalization module.

    Constructed by EEP after geometry selection; never transmitted over HTTP.

    Fields:
        job_id            — job identifier
        page_number       — 1-indexed page number (>= 1)
        image_uri         — URI of the full-resolution OTIFF or rectified artifact
        material_type     — one of book, newspaper, archival_document
        selected_geometry — the GeometryResponse from whichever model was selected
        source_model      — which geometry model produced the selected_geometry
    """

    job_id: str
    page_number: Annotated[int, Field(ge=1)]
    image_uri: str
    material_type: MaterialType
    selected_geometry: GeometryResponse
    source_model: Literal["iep1a", "iep1b"]
