"""
services/eep/app/jobs/download.py
----------------------------------
Packet 5.0c — Job output download: presigned manifest and ZIP streaming.

Implements two complementary download strategies for the "download whole
collection output" librarian requirement:

  GET /v1/jobs/{job_id}/output/download-manifest
      Returns a JSON manifest of all output PTIFFs with individual presigned
      S3 GET URLs.  Each entry includes page number, status, a suggested
      filename, and a direct download URL.  Scalable to any collection size;
      recommended for large (100+ page) jobs.

  GET /v1/jobs/{job_id}/output/download.zip
      Streams a ZIP archive of all available output PTIFFs (falling back to
      OTIFFs when PTIFF not yet available) directly through the server.  Each
      file inside the ZIP is named by the canonical convention:
          page_{page_number:04d}.tiff              — whole page
          page_{page_number:04d}_{sub_index}.tiff  — split child
      The response begins streaming immediately; Content-Length is unknown
      (chunked transfer encoding).  Recommended for small to medium jobs
      (< 50 pages or < 500 MB estimated).  For larger jobs, use the manifest
      endpoint.

Scope:
  Only pages that have an output_image_uri (PTIFF) are included in the
  manifest download URLs and the ZIP.  Pages with no output yet appear in
  the manifest with download_url=null and are silently skipped in the ZIP.
  Pages in any status are included (not restricted to accepted-only) so the
  librarian can download a work-in-progress batch for review.

Security:
  - require_user: regular users are restricted to their own jobs via
    assert_job_ownership.  Admin users may download any job.
  - ZIP streaming fetches bytes through the server-side storage backend
    (no presigning); the caller receives data via the HTTP response, never
    a direct S3 URL.

Error responses:
  404 — job not found
  404 — job has no pages with output images (download.zip only)
  503 — storage unavailable

Exported:
  router — FastAPI APIRouter (mount in main.py)
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse

import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, assert_job_ownership, require_user
from services.eep.app.db.models import Job, JobPage
from services.eep.app.db.session import get_session
from shared.io.storage import get_backend, rewrite_presigned_url_for_public_endpoint

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BUCKET: str = os.environ.get("S3_BUCKET_NAME", "libraryai")
_READ_EXPIRES_IN: int = int(os.environ.get("ARTIFACT_READ_PRESIGN_EXPIRES_SECONDS", "300"))

# Warn in the manifest when the collection is large. Not a hard limit.
_LARGE_COLLECTION_PAGE_THRESHOLD = 100


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class DownloadArtifact(BaseModel):
    """
    Per-page entry in the download manifest.

    ``download_url`` is a presigned S3 GET URL valid for ``expires_in``
    seconds.  It is null when the page has no output_image_uri yet (e.g.
    still processing, failed, or in pending_human_correction).

    ``source`` indicates which artifact was presigned:
      "ptiff"  — output_image_uri (preprocessed output, preferred)
      "otiff"  — input_image_uri  (original, fallback when ptiff missing)
      null     — no artifact available
    """

    page_number: int
    sub_page_index: int | None
    status: str
    filename: str              # Suggested filename for the downloaded file
    source: str | None         # "ptiff" | "otiff" | null
    output_image_uri: str | None
    download_url: str | None   # Presigned GET URL; null when no artifact
    expires_in: int            # Seconds until download_url expires (0 when null)


class DownloadManifestResponse(BaseModel):
    """Response for GET /v1/jobs/{job_id}/output/download-manifest."""

    job_id: str
    collection_id: str
    material_type: str
    total_pages: int
    pages_with_output: int
    pages_without_output: int
    large_collection_warning: str | None  # Non-null when total_pages >= threshold
    generated_at: str          # ISO 8601 UTC timestamp
    artifacts: list[DownloadArtifact]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_job_or_404(db: Session, job_id: str) -> Job:
    job: Job | None = db.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} not found.",
        )
    return job


def _leaf_pages_ordered(db: Session, job_id: str) -> list[JobPage]:
    """Return all non-split leaf pages sorted by (page_number, sub_page_index)."""
    rows: list[JobPage] = (
        db.query(JobPage)
        .filter(JobPage.job_id == job_id, JobPage.status != "split")
        .all()
    )
    rows.sort(key=lambda p: (p.page_number, p.sub_page_index if p.sub_page_index is not None else -1))
    return rows


def _canonical_filename(page: JobPage) -> str:
    """
    Return the suggested download filename for *page*.

    Format:
      page_0001.tiff          — whole page (sub_page_index IS NULL)
      page_0001_0.tiff        — left split child (sub_page_index == 0)
      page_0001_1.tiff        — right split child (sub_page_index == 1)
    """
    if page.sub_page_index is not None:
        return f"page_{page.page_number:04d}_{page.sub_page_index}.tiff"
    return f"page_{page.page_number:04d}.tiff"


def _preferred_uri(page: JobPage) -> tuple[str | None, str | None]:
    """
    Return (uri, source_label) for the best available artifact for *page*.

    Prefers output_image_uri (PTIFF).  Falls back to input_image_uri (OTIFF).
    Returns (None, None) when neither is available.
    """
    if page.output_image_uri:
        return page.output_image_uri, "ptiff"
    if page.input_image_uri:
        return page.input_image_uri, "otiff"
    return None, None


def _presign_uri(uri: str) -> str | None:
    """
    Generate a presigned GET URL for *uri*.  Returns None on failure.
    """
    parsed = urlparse(uri)

    if parsed.scheme == "file":
        return uri  # Local dev: return file:// URI unchanged.

    if parsed.scheme != "s3":
        logger.debug("download: unsupported scheme %s for presigning", parsed.scheme)
        return None

    if parsed.netloc != _BUCKET:
        logger.debug(
            "download: bucket mismatch (uri=%s expected=%s)", parsed.netloc, _BUCKET
        )
        return None

    key = parsed.path.lstrip("/")
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY") or os.environ.get("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY") or os.environ.get("S3_SECRET_ACCESS_KEY"),
        )
        url: str = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": _BUCKET, "Key": key},
            ExpiresIn=_READ_EXPIRES_IN,
        )
        return rewrite_presigned_url_for_public_endpoint(url)
    except Exception as exc:
        logger.warning("download: presign failed uri=%s — %s", uri, exc)
        return None


# ---------------------------------------------------------------------------
# Manifest endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/v1/jobs/{job_id}/output/download-manifest",
    response_model=DownloadManifestResponse,
    status_code=status.HTTP_200_OK,
    tags=["jobs", "download"],
    summary="Get presigned download URLs for all output PTIFFs in a job",
)
def download_manifest(
    job_id: str,
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> DownloadManifestResponse:
    """
    Return a JSON manifest of all pages with individual presigned S3 GET URLs
    for their output PTIFF (or OTIFF fallback when PTIFF is not yet available).

    **Recommended for large collections (100+ pages).**  The caller can use
    the presigned URLs to download files individually or in parallel without
    routing data through this server.

    Each presigned URL is valid for ``ARTIFACT_READ_PRESIGN_EXPIRES_SECONDS``
    seconds (default 300 s). Request a fresh manifest when URLs have expired.

    Pages with no artifact yet (still processing, failed, etc.) appear in the
    manifest with ``download_url: null``.

    **Error responses**

    - ``404`` — job not found
    """
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)

    pages = _leaf_pages_ordered(db, job_id)
    artifacts: list[DownloadArtifact] = []
    pages_with_output = 0

    for page in pages:
        uri, source = _preferred_uri(page)
        download_url: str | None = None
        expires_in = 0

        if uri:
            download_url = _presign_uri(uri)
            if download_url:
                pages_with_output += 1
                expires_in = _READ_EXPIRES_IN

        artifacts.append(
            DownloadArtifact(
                page_number=page.page_number,
                sub_page_index=page.sub_page_index,
                status=page.status,
                filename=_canonical_filename(page),
                source=source if download_url else None,
                output_image_uri=page.output_image_uri,
                download_url=download_url,
                expires_in=expires_in,
            )
        )

    pages_without_output = len(pages) - pages_with_output
    large_collection_warning: str | None = None
    if len(pages) >= _LARGE_COLLECTION_PAGE_THRESHOLD:
        large_collection_warning = (
            f"This collection has {len(pages)} pages. Presigned URLs expire in "
            f"{_READ_EXPIRES_IN} seconds. For large downloads, consider fetching "
            "the manifest in batches or using the individual presigned URLs in "
            "parallel rather than the /download.zip streaming endpoint."
        )

    logger.info(
        "download_manifest: user=%s job=%s total=%d with_output=%d",
        user.user_id,
        job_id,
        len(pages),
        pages_with_output,
    )

    return DownloadManifestResponse(
        job_id=job_id,
        collection_id=job.collection_id,
        material_type=job.material_type,
        total_pages=len(pages),
        pages_with_output=pages_with_output,
        pages_without_output=pages_without_output,
        large_collection_warning=large_collection_warning,
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# ZIP streaming endpoint
# ---------------------------------------------------------------------------


def _iter_zip_bytes(pages: list[JobPage]) -> bytes:
    """
    Build a ZIP archive in memory and return the bytes.

    Fetches each artifact via the storage backend (S3/file).  Pages with no
    available artifact are silently skipped.  Files are stored uncompressed
    (ZIP_STORED) since TIFF files are already binary and do not compress well;
    this keeps memory usage lower and CPU cost negligible.
    """
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for page in pages:
            uri, _source = _preferred_uri(page)
            if not uri:
                continue
            try:
                raw_bytes: bytes = get_backend(uri).get_bytes(uri)
            except Exception as exc:
                logger.warning(
                    "download.zip: skipping page %d (sub=%s) — storage error: %s",
                    page.page_number,
                    page.sub_page_index,
                    exc,
                )
                continue
            filename = _canonical_filename(page)
            zf.writestr(filename, raw_bytes)
            logger.debug("download.zip: added %s (%d bytes)", filename, len(raw_bytes))

    buf.seek(0)
    return buf.read()


@router.get(
    "/v1/jobs/{job_id}/output/download.zip",
    status_code=status.HTTP_200_OK,
    tags=["jobs", "download"],
    summary="Download all output PTIFFs as a ZIP archive",
    response_class=StreamingResponse,
)
def download_zip(
    job_id: str,
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> StreamingResponse:
    """
    Stream a ZIP archive containing all available output PTIFFs for the job.

    Files inside the ZIP follow the canonical naming convention:

    ```
    page_0001.tiff          — whole page
    page_0001_0.tiff        — left split child
    page_0001_1.tiff        — right split child
    ```

    Pages with no output artifact yet (still processing, failed) are silently
    omitted from the ZIP.  If no pages have any output at all, a ``404`` is
    returned.

    **Recommended for small to medium jobs (< 50 pages).**  For large
    collections, use ``GET /v1/jobs/{job_id}/output/download-manifest`` to
    obtain per-file presigned URLs and download in parallel.

    **Error responses**

    - ``404`` — job not found
    - ``404`` — no pages have output artifacts yet
    - ``503`` — storage unavailable during ZIP assembly
    """
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)

    pages = _leaf_pages_ordered(db, job_id)

    # Only include pages that have at least one artifact available.
    pages_with_artifacts = [p for p in pages if _preferred_uri(p)[0] is not None]
    if not pages_with_artifacts:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Job {job_id!r} has no output artifacts available for download yet. "
                "Pages may still be processing."
            ),
        )

    logger.info(
        "download.zip: user=%s job=%s pages_total=%d pages_with_artifacts=%d",
        user.user_id,
        job_id,
        len(pages),
        len(pages_with_artifacts),
    )

    try:
        zip_bytes = _iter_zip_bytes(pages_with_artifacts)
    except Exception as exc:
        logger.error("download.zip: ZIP assembly failed job=%s — %s", job_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable; could not assemble ZIP archive.",
        ) from exc

    # Suggest a filename: {collection_id}_{job_id_short}.zip
    job_id_short = job_id[:8] if len(job_id) >= 8 else job_id
    suggested_filename = f"{job.collection_id}_{job_id_short}.zip"

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{suggested_filename}"',
            "Content-Length": str(len(zip_bytes)),
            "Cache-Control": "no-store",
        },
    )
