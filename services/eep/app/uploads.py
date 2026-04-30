"""
services/eep/app/uploads.py
----------------------------
POST /v1/uploads/jobs/presign — generate a presigned S3 PUT URL for OTIFF upload.

The frontend calls this endpoint before creating a job to obtain a presigned
URL for direct-to-storage OTIFF upload.  After the upload the caller passes
the returned ``object_uri`` to ``POST /v1/jobs`` as a page source reference.

Staging path convention
-----------------------
A UUID is assigned at presign time.  The object is stored at:

    s3://{bucket}/uploads/{uuid}.tiff

During job intake (Phase 4 Packet 4.3a) the worker resolves the staging URI,
downloads the OTIFF, computes its SHA-256 hash, and moves it to the canonical
job-scoped input path:

    s3://{bucket}/jobs/{job_id}/input/otiff/{page_number}.tiff

S3 env vars (shared with shared/io/storage.py)
----------------------------------------------
    S3_ENDPOINT_URL          — custom endpoint URL (e.g. http://localhost:9000 for MinIO)
    S3_ACCESS_KEY            — AWS / MinIO access key ID
    S3_SECRET_KEY            — AWS / MinIO secret access key
    S3_BUCKET_NAME           — bucket name (default: "libraryai")
    S3_PRESIGN_EXPIRES_SECONDS — presigned URL TTL in seconds (default: 3600)

Auth
----
Authentication: require_user (Packet 7.2).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from services.eep.app.auth import CurrentUser, require_user
from shared.io.storage import rewrite_presigned_url_for_public_endpoint

router = APIRouter()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BUCKET: str = os.environ.get("S3_BUCKET_NAME", "libraryai")
_EXPIRES_IN: int = int(os.environ.get("S3_PRESIGN_EXPIRES_SECONDS", "3600"))


def _s3_access_key() -> str | None:
    return os.environ.get("S3_ACCESS_KEY") or os.environ.get("S3_ACCESS_KEY_ID")


def _s3_secret_key() -> str | None:
    return os.environ.get("S3_SECRET_KEY") or os.environ.get("S3_SECRET_ACCESS_KEY")


def _s3_client() -> Any:
    """Return a boto3 S3 client using the canonical env-var config."""
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "eu-central-1"),
        endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
        aws_access_key_id=_s3_access_key(),
        aws_secret_access_key=_s3_secret_key(),
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PresignUploadResponse(BaseModel):
    """
    Returned by POST /v1/uploads/jobs/presign.

    The client uploads the raw OTIFF to ``upload_url`` via an HTTP PUT request
    (no body encoding — raw bytes only), then passes ``object_uri`` as a page
    source URI in ``POST /v1/jobs``.
    """

    upload_url: str = Field(
        description=(
            "Presigned HTTP PUT URL.  Upload the OTIFF file as the raw request body "
            "with Content-Type: image/tiff."
        ),
    )
    object_uri: str = Field(
        description=(
            "S3 URI of the uploaded object.  Pass this value as a page source URI "
            "in POST /v1/jobs."
        ),
    )
    expires_in: int = Field(
        description="Seconds until the presigned upload URL expires.",
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/v1/uploads/jobs/presign",
    response_model=PresignUploadResponse,
    status_code=status.HTTP_200_OK,
    tags=["uploads"],
    summary="Generate a presigned S3 PUT URL for raw OTIFF upload",
)
async def presign_otiff_upload(
    _user: CurrentUser = Depends(require_user),
) -> PresignUploadResponse:
    """
    Generate a presigned S3 PUT URL that the frontend uses to upload a raw
    OTIFF file directly to object storage.

    **Upload flow**

    1. Call this endpoint to receive ``upload_url`` and ``object_uri``.
    2. PUT the raw OTIFF bytes to ``upload_url`` with ``Content-Type: image/tiff``.
    3. Pass ``object_uri`` as a page source URI in ``POST /v1/jobs``.

    The staging object is validated and moved to a job-scoped path by the
    EEP worker during intake (Phase 4 Packet 4.3a).

    **Auth:** enforced in Phase 7 (Packet 7.1) — not yet active.

    **Error responses**

    - ``503`` — object storage is unavailable or misconfigured.
    """
    upload_id = str(uuid.uuid4())
    object_key = f"uploads/{upload_id}.tiff"
    object_uri = f"s3://{_BUCKET}/{object_key}"

    try:
        s3 = _s3_client()
        upload_url: str = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": _BUCKET,
                "Key": object_key,
                "ContentType": "image/tiff",
            },
            ExpiresIn=_EXPIRES_IN,
        )
        upload_url = rewrite_presigned_url_for_public_endpoint(upload_url)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable; could not generate presigned URL.",
        ) from exc

    return PresignUploadResponse(
        upload_url=upload_url,
        object_uri=object_uri,
        expires_in=_EXPIRES_IN,
    )
