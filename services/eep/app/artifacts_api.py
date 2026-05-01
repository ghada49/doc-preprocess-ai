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

import io
import hashlib
import json
import logging
import os
from typing import Literal, Protocol, cast
from urllib.parse import urlparse

import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_user
from services.eep.app.db.models import Job, JobPage, PageLineage, ServiceInvocation
from services.eep.app.db.session import get_session
from shared.io.storage import get_backend, rewrite_presigned_url_for_public_endpoint

logger = logging.getLogger(__name__)
router = APIRouter(tags=["artifacts"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BUCKET: str = os.environ.get("S3_BUCKET_NAME", "libraryai")
_READ_EXPIRES_IN: int = int(os.environ.get("ARTIFACT_READ_PRESIGN_EXPIRES_SECONDS", "300"))


def _s3_access_key() -> str | None:
    return os.environ.get("S3_ACCESS_KEY") or os.environ.get("S3_ACCESS_KEY_ID")


def _s3_secret_key() -> str | None:
    return os.environ.get("S3_SECRET_KEY") or os.environ.get("S3_SECRET_ACCESS_KEY")


class _S3Client(Protocol):
    def generate_presigned_url(self, client_method: str, **kwargs: object) -> str: ...


def _s3_client() -> _S3Client:
    """Return a boto3 S3 client using the canonical env-var configuration."""
    return cast(
        _S3Client,
        boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            aws_access_key_id=_s3_access_key(),
            aws_secret_access_key=_s3_secret_key(),
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


class ArtifactPreviewRequest(BaseModel):
    """
    Request body for POST /v1/artifacts/preview.

    Fields:
        uri         — s3:// URI of the artifact (must be known to the system)
        page_index  — 0-indexed TIFF page to render (default: 0)
        max_width   — if set, downscale so width <= max_width px (aspect-ratio preserved)
    """

    uri: str
    page_index: int = 0
    max_width: int | None = 1600
    return_url: bool = False


class ArtifactPreviewResponse(BaseModel):
    preview_url: str
    preview_uri: str
    expires_in: int
    width: int
    height: int
    source_width: int
    source_height: int
    scale_x: float
    scale_y: float
    cache_hit: bool
    format: Literal["png"] = "png"


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


def _normalize_preview_max_width(max_width: int | None) -> int | None:
    if max_width is None:
        return None
    return max(1, min(int(max_width), 2400))


def _preview_cache_uri(
    source_uri: str,
    source_identity: str,
    page_index: int,
    max_width: int | None,
) -> str | None:
    parsed = urlparse(source_uri)
    if parsed.scheme != "s3":
        return None
    width_part = "original" if max_width is None else str(max_width)
    digest_input = f"{source_uri}|{source_identity}".encode("utf-8")
    digest = hashlib.sha256(digest_input).hexdigest()[:24]
    return f"s3://{parsed.netloc}/previews/artifacts/{digest}/p{page_index}-w{width_part}.png"


def _preview_metadata_uri(preview_uri: str) -> str:
    return f"{preview_uri}.json"


def _s3_object_exists(uri: str) -> bool:
    bucket, key = _split_s3_uri(uri)
    try:
        s3 = _s3_client()
        head_object = getattr(s3, "head_object")
        head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def _s3_object_identity(uri: str) -> str:
    bucket, key = _split_s3_uri(uri)
    try:
        s3 = _s3_client()
        head_object = getattr(s3, "head_object")
        response = head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        logger.error("artifact_preview: source head failed uri=%s - %s", uri, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable; could not inspect artifact.",
        ) from exc

    etag = str(response.get("ETag") or "").strip('"')
    version_id = str(response.get("VersionId") or "")
    last_modified = str(response.get("LastModified") or "")
    content_length = str(response.get("ContentLength") or "")
    return "|".join((etag, version_id, last_modified, content_length))


def _split_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Unsupported URI scheme {parsed.scheme!r}; only s3:// is supported.")
    return parsed.netloc, parsed.path.lstrip("/")


def _put_s3_preview(uri: str, data: bytes, content_type: str) -> None:
    bucket, key = _split_s3_uri(uri)
    s3 = _s3_client()
    put_object = getattr(s3, "put_object")
    put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="private, max-age=31536000, immutable",
    )


def _presign_s3_uri(uri: str, expires_in: int) -> str:
    bucket, key = _split_s3_uri(uri)
    s3 = _s3_client()
    read_url: str = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )
    return rewrite_presigned_url_for_public_endpoint(read_url)


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

    rectified_invocation: ServiceInvocation | None = (
        db.query(ServiceInvocation)
        .filter(ServiceInvocation.metrics["rectified_image_uri"].astext == uri)
        .first()
    )
    if rectified_invocation is not None:
        lineage = db.get(PageLineage, rectified_invocation.lineage_id)
        if lineage is not None:
            return lineage.job_id

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
        read_url = rewrite_presigned_url_for_public_endpoint(read_url)
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


@router.post(
    "/v1/artifacts/preview",
    status_code=200,
    summary="Stream a stored artifact as a browser-displayable PNG",
    response_class=StreamingResponse,
)
def artifact_preview(
    body: ArtifactPreviewRequest,
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> StreamingResponse | JSONResponse:
    """
    Download the artifact at *uri*, convert it to PNG in memory, and stream
    the PNG bytes back.  No files are written to disk or re-stored.

    Browsers cannot render TIFF in ``<img>`` tags; this endpoint acts as a
    transcoding proxy so the frontend can display any TIFF artifact without
    client-side decoder libraries.

    **Auth:** JWT bearer required.  Same ownership rules as presign-read.
    """
    try:
        from PIL import Image  # noqa: PLC0415 — optional dep, imported lazily
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Image preview unavailable: Pillow is not installed on this server.",
        )

    uri = body.uri

    # ── Validate URI ──────────────────────────────────────────────────────────
    # s3:// URIs are validated for bucket correctness; file:// URIs are allowed
    # for local development and CI where artifacts live on the local filesystem.
    _parsed_uri = urlparse(uri)
    if _parsed_uri.scheme == "s3":
        try:
            _uri_to_s3_key(uri, _BUCKET)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    elif _parsed_uri.scheme not in ("file",):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported URI scheme {_parsed_uri.scheme!r}; expected s3:// or file://.",
        )

    # ── Authorize ─────────────────────────────────────────────────────────────
    _assert_uri_access(db, uri, user)

    max_width = _normalize_preview_max_width(body.max_width)
    source_identity = (
        _s3_object_identity(uri)
        if body.return_url and _parsed_uri.scheme == "s3"
        else None
    )
    cache_uri = (
        _preview_cache_uri(uri, source_identity, body.page_index, max_width)
        if source_identity
        else None
    )
    metadata_uri = _preview_metadata_uri(cache_uri) if cache_uri else None

    if cache_uri and metadata_uri and _s3_object_exists(cache_uri):
        try:
            metadata = json.loads(get_backend(metadata_uri).get_bytes(metadata_uri).decode("utf-8"))
            preview_url = _presign_s3_uri(cache_uri, _READ_EXPIRES_IN)
            logger.info(
                "artifact_preview: cache_hit user=%s uri=%s preview_uri=%s",
                user.user_id,
                uri,
                cache_uri,
            )
            return JSONResponse(
                ArtifactPreviewResponse(
                    preview_url=preview_url,
                    preview_uri=cache_uri,
                    expires_in=_READ_EXPIRES_IN,
                    width=int(metadata["width"]),
                    height=int(metadata["height"]),
                    source_width=int(metadata["source_width"]),
                    source_height=int(metadata["source_height"]),
                    scale_x=float(metadata["scale_x"]),
                    scale_y=float(metadata["scale_y"]),
                    cache_hit=True,
                ).model_dump()
            )
        except Exception as exc:
            logger.warning(
                "artifact_preview: cached preview metadata unavailable uri=%s preview_uri=%s - %s",
                uri,
                cache_uri,
                exc,
            )

    # ── Fetch raw bytes ────────────────────────────────────────────────────────
    try:
        raw_bytes: bytes = get_backend(uri).get_bytes(uri)
    except Exception as exc:
        logger.error("artifact_preview: storage fetch failed uri=%s — %s", uri, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable; could not download artifact.",
        ) from exc

    # ── Decode → PNG ───────────────────────────────────────────────────────────
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        if body.page_index > 0:
            try:
                img.seek(body.page_index)
            except EOFError:
                img.seek(0)
        original_width = int(img.width)
        original_height = int(img.height)
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        if max_width and img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        preview_width = int(img.width)
        preview_height = int(img.height)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
    except Exception as exc:
        logger.error("artifact_preview: decode/convert failed uri=%s — %s", uri, exc)
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Could not decode the artifact as an image.",
        ) from exc

    scale_x = preview_width / original_width if original_width > 0 else 1.0
    scale_y = preview_height / original_height if original_height > 0 else 1.0
    preview_bytes = buf.getvalue()

    if cache_uri and metadata_uri:
        try:
            metadata = {
                "width": preview_width,
                "height": preview_height,
                "source_width": original_width,
                "source_height": original_height,
                "scale_x": scale_x,
                "scale_y": scale_y,
                "source_uri": uri,
                "source_identity": source_identity,
                "page_index": body.page_index,
                "max_width": max_width,
                "format": "png",
            }
            _put_s3_preview(cache_uri, preview_bytes, "image/png")
            _put_s3_preview(
                metadata_uri,
                json.dumps(metadata, separators=(",", ":")).encode("utf-8"),
                "application/json",
            )
            preview_url = _presign_s3_uri(cache_uri, _READ_EXPIRES_IN)
            logger.info(
                "artifact_preview: cache_miss user=%s uri=%s preview_uri=%s",
                user.user_id,
                uri,
                cache_uri,
            )
            return JSONResponse(
                ArtifactPreviewResponse(
                    preview_url=preview_url,
                    preview_uri=cache_uri,
                    expires_in=_READ_EXPIRES_IN,
                    width=preview_width,
                    height=preview_height,
                    source_width=original_width,
                    source_height=original_height,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    cache_hit=False,
                ).model_dump()
            )
        except Exception as exc:
            logger.warning(
                "artifact_preview: cache write/presign failed uri=%s preview_uri=%s - %s; falling back to stream",
                uri,
                cache_uri,
                exc,
            )

    logger.info("artifact_preview: stream user=%s uri=%s", user.user_id, uri)
    return StreamingResponse(
        io.BytesIO(preview_bytes),
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Original-Width": str(original_width),
            "X-Original-Height": str(original_height),
            "X-Preview-Width": str(preview_width),
            "X-Preview-Height": str(preview_height),
            "X-Preview-Scale-X": str(scale_x),
            "X-Preview-Scale-Y": str(scale_y),
        },
    )
