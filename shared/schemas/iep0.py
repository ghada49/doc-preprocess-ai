"""
shared.schemas.iep0
--------------------
IEP0 (material-type classification) request and response schemas.

IEP0 is invoked by the EEP worker before geometry inference (IEP1A/IEP1B)
to classify the input image as one of: book, newspaper, or microfilm.

The service supports both single-image classification and batch classification
with majority voting.  When multiple images are sent, IEP0 classifies each
independently and returns the majority-voted result.

Endpoint: POST /v1/classify       (single image)
          POST /v1/classify-batch  (multiple images → majority vote)

Exported:
    ClassifyRequest       — single-image request
    ClassifyResponse      — single-image response
    BatchClassifyRequest  — batch request (list of images)
    BatchClassifyResponse — batch response with majority vote
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

MaterialType = Literal["book", "newspaper", "microfilm"]


class ClassifyRequest(BaseModel):
    """
    Request sent to IEP0 POST /v1/classify.

    Fields:
        job_id      — job identifier
        page_number — 1-indexed page number (>= 1)
        image_uri   — URI of the proxy/downscaled image to classify
    """

    job_id: str
    page_number: Annotated[int, Field(ge=1)]
    image_uri: str


class ClassifyResponse(BaseModel):
    """
    Response from IEP0 POST /v1/classify.

    Fields:
        material_type      — predicted material type
        confidence         — classification confidence [0, 1]
        probabilities      — per-class probabilities
        processing_time_ms — wall-clock elapsed time in ms (>= 0)
        warnings           — advisory messages; empty list if none
    """

    material_type: MaterialType
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    probabilities: dict[str, float]
    processing_time_ms: Annotated[float, Field(ge=0.0)]
    warnings: list[str]


class BatchClassifyRequest(BaseModel):
    """
    Request sent to IEP0 POST /v1/classify-batch.

    Sends multiple images for classification.  IEP0 classifies each and
    returns the majority-voted material type.

    Fields:
        job_id     — job identifier
        image_uris — list of image URIs to classify (1–50)
    """

    job_id: str
    image_uris: Annotated[list[str], Field(min_length=1, max_length=50)]


class BatchClassifyResponse(BaseModel):
    """
    Response from IEP0 POST /v1/classify-batch.

    Fields:
        material_type       — majority-voted material type
        confidence          — average confidence of the winning class
        vote_counts         — per-class vote counts
        per_image_results   — individual classification results
        sample_size         — number of images classified
        processing_time_ms  — total wall-clock elapsed time in ms
        warnings            — advisory messages
    """

    material_type: MaterialType
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    vote_counts: dict[str, int]
    per_image_results: list[ClassifyResponse]
    sample_size: int
    processing_time_ms: Annotated[float, Field(ge=0.0)]
    warnings: list[str]
