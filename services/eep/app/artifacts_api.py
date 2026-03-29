"""
services/eep/app/artifacts_api.py
-----------------------------------
Browser-safe artifact read access endpoint.

Implements:
  POST /v1/artifacts/presign-read  — Return a short-lived signed GET URL for
                                     browser image display or download.

Security model:
  - Regular users (role='user') may only presign artifacts belonging to their
    own jobs. Ownership is verified by looking up the artifact URI in
    page_lineage or job_pages and confirming that the parent job.created_by
    matches the caller's user_id.
  - Admin users may presign any artifact URI known to the system.
  - URIs not found in any DB table are rejected with 404, preventing this
    endpoint from becoming an unrestricted storage proxy.

Supported artifact types (determined by URI path prefix convention):
  - Original OTIFF:          s3://{bucket}/jobs/{job_id}/input/otiff/…
  - Preprocessing output:    s3://{bucket}/jobs/{job_id}/output/…
  - Correction output:       s3://{bucket}/jobs/{job_id}/corrections/…
  - Split child artifact:    s3://{bucket}/jobs/{job_id}/splits/…
  - Layout JSON:             s3://{bucket}/jobs/{job_id}/layout/…
  - Upload staging:          s3://{bucket}/uploads/…

The lookup strategy:
  1. Search page_lineage for the URI (otiff_uri, output_image_uri).
  2. Search job_pages for the URI (input_image_uri, output_image_uri, output_layout_uri).
  3. If found, extract job_id and enforce ownership.
  4. Generate a presigned GET URL via boto3 (MinIO/S3 compatible).

Storage configuration:
  S3_ENDPOINT_URL          — custom endpoint URL (e.g. http://localhost:9000 for MinIO)
  S3_ACCESS_KEY            — AWS / MinIO access key
  S3_SECRET_KEY            — AWS / MinIO secret key
  S3_BUCKET_NAME           — bucket name (default: "libraryai")
  ARTIFACT_READ_PRESIGN_EXPIRES_SECONDS — URL TTL in seconds (default: 300)

Auth: require_user — ownership-scoped for regular users; admin sees all.

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, cast
from urllib.parse import urlparse

import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_user
from services.eep.app.db.models import Job, JobPage, PageLineage
from services.eep.app.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["artifacts"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BUCKET: str = os.environ.get("S3_BUCKET_NAME", "libraryai")
_READ_EXPIRES_IN: int = int(os.environ.get("ARTIFACT_READ_PRESIGN_EXPIRES_SECONDS", "300"))


class _S3Client(Protocol):
    def generate_presigned_url(self, client_method: str, **kwargs: object) -> str: ...


def _s3_client() -> _S3Client:
    """Return a boto3 S3 client using the canonical env-var configuration."""
    return cast(
        _S3Client,
        boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY"),
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY"),
        ),
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ArtifactPresignReadRequest(BaseModel):
    """
    Request body for POST /v1/artifacts/presign-read.

    Fields:
        uri         — s3:// URI of the artifact (must be known to the system)
        expires_in  — optional override for URL TTL in seconds (max 3600).
                      Defaults to ARTIFACT_READ_PRESIGN_EXPIRES_SECONDS (300s).
    """

    uri: str
    expires_in: int | None = None


class ArtifactPresignReadResponse(BaseModel):
    """
    Response for POST /v1/artifacts/presign-read.

    Fields:
        uri         — the original s3:// URI that was requested
        read_url    — presigned HTTPS GET URL for browser display / download
        expires_in  — seconds until the read_url expires
        content_type_hint — suggested Content-Type for browser display
                            (image/tiff, application/json, etc.)
    """

    uri: str
    read_url: str
    expires_in: int
    content_type_hint: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Content-type hints by file extension.
_CONTENT_TYPE_MAP: dict[str, str] = {
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def _content_type_hint(uri: str) -> str:
    path = urlparse(uri).path.lower()
    for ext, ct in _CONTENT_TYPE_MAP.items():
        if path.endswith(ext):
            return ct
    return _DEFAULT_CONTENT_TYPE


def _uri_to_s3_key(uri: str, bucket: str) -> str:
    """
    Convert an s3://bucket/key URI to its key component.

    Raises ValueError when the URI scheme is not s3 or the bucket does not match.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Unsupported URI scheme {parsed.scheme!r}; only s3:// is supported.")
    uri_bucket = parsed.netloc
    if uri_bucket != bucket:
        raise ValueError(f"URI bucket {uri_bucket!r} does not match configured bucket {bucket!r}.")
    # Remove leading slash from path to get the S3 key
    return parsed.path.lstrip("/")


def _resolve_job_id_for_uri(db: Session, uri: str) -> str | None:
    """
    Look up the job_id associated with the given artifact URI.

    Searches page_lineage and job_pages tables for any column that could
    store this URI. Returns the job_id string, or None if not found.

    This search is the authorization gate — URIs not in the DB are rejected.
    """
    # Search page_lineage (otiff_uri, output_image_uri)
    lineage: PageLineage | None = (
        db.query(PageLineage)
        .filter((PageLineage.otiff_uri == uri) | (PageLineage.output_image_uri == uri))
        .first()
    )
    if lineage is not None:
        return lineage.job_id

    # Search job_pages (input_image_uri, output_image_uri, output_layout_uri)
    page: JobPage | None = (
        db.query(JobPage)
        .filter(
            (JobPage.input_image_uri == uri)
            | (JobPage.output_image_uri == uri)
            | (JobPage.output_layout_uri == uri)
        )
        .first()
    )
    if page is not None:
        return page.job_id

    return None


def _assert_uri_access(db: Session, uri: str, user: CurrentUser) -> None:
    """
    Enforce that the caller has access to the artifact at *uri*.

    Admin users: unconditionally permitted.
    Regular users: permitted only if the artifact belongs to one of their jobs.

    Raises HTTPException 404 when the URI is not found in the DB (prevents
    guessing / brute-forcing of arbitrary storage paths).
    Raises HTTPException 403 when the URI belongs to another user's job.
    """
    if user.role == "admin":
        # Admins: still require the URI to be in the DB, but no ownership check.
        job_id = _resolve_job_id_for_uri(db, uri)
        if job_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Artifact URI not found in system records.",
            )
        return

    # Non-admin: URI must be in DB and belong to caller's job.
    job_id = _resolve_job_id_for_uri(db, uri)
    if job_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact URI not found in system records.",
        )

    job: Job | None = db.get(Job, job_id)
    if job is None or job.created_by != user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this artifact.",
        )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/v1/artifacts/presign-read",
    response_model=ArtifactPresignReadResponse,
    status_code=200,
    summary="Generate a presigned read URL for a stored artifact",
)
def presign_artifact_read(
    body: ArtifactPresignReadRequest,
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> ArtifactPresignReadResponse:
    """
    Return a short-lived presigned GET URL for a stored artifact URI.

    The URI must be an ``s3://`` URI already known to the system (present in
    ``page_lineage`` or ``job_pages``).  Unknown URIs are rejected with 404.

    Regular users may only access artifacts belonging to their own jobs.
    Admin users may access any artifact in the system.

    Use ``expires_in`` to request a shorter TTL (max 3600 s); the server
    enforces the configured default ceiling.

    **Auth:** JWT bearer required. Ownership-scoped for regular users.

    **Error responses**

    - ``400`` — URI scheme is not s3:// or bucket mismatch
    - ``403`` — artifact belongs to another user's job
    - ``404`` — URI not found in system records
    - ``503`` — object storage unavailable
    """
    uri = body.uri

    # ── Determine S3 key (validates URI format) ────────────────────────────
    try:
        key = _uri_to_s3_key(uri, _BUCKET)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # ── Authorization: ownership check ─────────────────────────────────────
    _assert_uri_access(db, uri, user)

    # ── Determine TTL ──────────────────────────────────────────────────────
    requested_ttl = body.expires_in
    ttl = _READ_EXPIRES_IN
    if requested_ttl is not None:
        ttl = max(1, min(requested_ttl, 3600))

    # ── Generate presigned GET URL ─────────────────────────────────────────
    try:
        s3 = _s3_client()
        read_url: str = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": _BUCKET, "Key": key},
            ExpiresIn=ttl,
        )
    except Exception as exc:
        logger.error("presign_artifact_read: storage error for key=%s — %s", key, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable; could not generate presigned URL.",
        ) from exc

    logger.info(
        "presign_artifact_read: user=%s role=%s uri=%s ttl=%d",
        user.user_id,
        user.role,
        uri,
        ttl,
    )

    return ArtifactPresignReadResponse(
        uri=uri,
        read_url=read_url,
        expires_in=ttl,
        content_type_hint=_content_type_hint(uri),
    )
