"""
services/eep/app/correction/apply.py
--------------------------------------
Single-page and reviewer-driven split correction apply path.

Implements:
  POST /v1/jobs/{job_id}/pages/{page_number}/correction

Applies human correction inputs for pages in pending_human_correction.
Parent-page review is structure-first: reviewers choose single page vs spread,
and split_x is retained only as an internal or advanced fallback input.

──────────────────────────────────────────────────────────────────────────
Single-page path (child correction or parent kept as a single page)
──────────────────────────────────────────────────────────────────────────
  - The correction is authoritative; processing is not re-run.
  - The existing artifact is copied to a derived corrected URI via the
    storage backend and the URI is recorded in lineage.
  - State: pending_human_correction → layout_detection (layout mode) or
           pending_human_correction → accepted (preprocess-only mode).
  - In layout mode the page is enqueued to Redis for async IEP2 processing.
  - IEP2 is NEVER run inline while the page is still in human review.

──────────────────────────────────────────────────────────────────────────
Split path (reviewer selects a two-page spread or supplies an explicit split_x fallback)
──────────────────────────────────────────────────────────────────────────
  - Two child sub-pages (sub_page_index 0 and 1) are created or reused
    idempotently.
  - Each child stores its own independent parent-space selection geometry
    while previewing the shared parent artifact.
  - The real child artifact is written to a child-specific corrected URI only
    when that child page is later submitted.
  - Each child remains in pending_human_correction so the reviewer can correct
    Page 0 and Page 1 separately.
  - Parent lineage is NOT modified; it remains the retained lineage record
    for the original OTIFF.
  - The parent may transition to "split" in the same request once the child
    review units satisfy the worker-terminal closure rule used elsewhere in
    the split flow.

──────────────────────────────────────────────────────────────────────────
Shared invariants (non-negotiable)
──────────────────────────────────────────────────────────────────────────
  - Only pages in pending_human_correction are accepted (409 otherwise).
  - All state transitions use advance_page_state() (state machine is never
    bypassed).
  - Lineage rows must exist; missing rows are data-integrity failures (500).
  - Storage writes happen before DB commit; the DB never points to a URI
    that was not written.

Error responses:
  404 — job not found / page not found
  409 — page not in pending_human_correction state
  422 — invalid request body (Pydantic validation)
  500 — data-integrity failure: lineage row missing or page has no source
        artifact URI

Auth:
  Enforced in Phase 7 (Packet 7.1) — not yet active.

Exported:
  router — FastAPI APIRouter (mount at app level in main.py)
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

import cv2
import numpy as np
import redis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import desc
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, assert_job_ownership, require_user
from services.eep.app.correction.workspace_assembly import _fetch_iep1d_rectified_uri
from services.eep.app.db.lineage import confirm_preprocessed_artifact, update_lineage_completion
from services.eep.app.db.models import Job, JobPage, PageLineage, QualityGateLog
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.db.session import get_session
from services.eep.app.queue import enqueue_page_task
from services.eep.app.redis_client import get_redis
from shared.io.storage import get_backend
from shared.normalization.deskew import apply_affine_deskew
from shared.normalization.perspective import four_point_transform
from shared.schemas.queue import PageTask

logger = logging.getLogger(__name__)
router = APIRouter()
PageStructure = Literal["single", "spread"]
SelectionMode = Literal["rect", "quad"]
QuadPoint = tuple[float, float]
_GEOMETRY_GATE_TYPES = frozenset(
    {
        "geometry_selection",
        "geometry_selection_post_rectification",
    }
)


# ── Request schema ──────────────────────────────────────────────────────────────


class CorrectionApplyRequest(BaseModel):
    """
    Request body for POST /v1/jobs/{job_id}/pages/{page_number}/correction.

    Fields:
        crop_box     — [x_min, y_min, x_max, y_max]; exactly 4 non-negative integers
                       with x_min < x_max and y_min < y_max.
        deskew_angle — rotation correction angle in degrees; null means no deskew.
        page_structure — reviewer-facing structural choice for parent pages.
                         "spread" creates or reuses Page 0 / Page 1 children.
        split_x      — optional internal or advanced split boundary override.
                       Normal reviewer UX should prefer page_structure over
                       direct split_x entry. Ignored when correcting a child
                       sub-page (split already occurred).
        source_artifact_uri — optional explicit source artifact chosen by the
                       reviewer. When provided, the correction is derived from
                       this displayed source rather than implicitly from the
                       page's current artifact.
        notes        — optional reviewer notes; stored in lineage.reviewer_notes.
    """

    crop_box: list[int] | None = None
    deskew_angle: float | None = None
    page_structure: PageStructure | None = None
    split_x: int | None = None
    selection_mode: SelectionMode | None = None
    quad_points: list[QuadPoint] | None = None
    source_artifact_uri: str | None = None
    notes: str | None = None

    @field_validator("crop_box")
    @classmethod
    def validate_crop_box(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        if len(v) != 4:
            raise ValueError(f"crop_box must have exactly 4 integers; got {len(v)}")
        x_min, y_min, x_max, y_max = v
        if any(val < 0 for val in v):
            raise ValueError("crop_box values must be non-negative")
        if x_min >= x_max:
            raise ValueError("crop_box x_min must be less than x_max")
        if y_min >= y_max:
            raise ValueError("crop_box y_min must be less than y_max")
        return v

    @field_validator("quad_points")
    @classmethod
    def validate_quad_points(cls, v: list[QuadPoint] | None) -> list[QuadPoint] | None:
        if v is None:
            return None
        if len(v) != 4:
            raise ValueError(f"quad_points must have exactly 4 points; got {len(v)}")
        normalized: list[QuadPoint] = []
        for idx, point in enumerate(v):
            if len(point) != 2:
                raise ValueError(f"quad_points[{idx}] must contain exactly 2 coordinates")
            x, y = float(point[0]), float(point[1])
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("quad_points values must be finite numbers")
            if x < 0 or y < 0:
                raise ValueError("quad_points values must be non-negative")
            normalized.append((x, y))
        return normalized

    @model_validator(mode="after")
    def validate_selection(self) -> CorrectionApplyRequest:
        if self.selection_mode == "quad" and self.quad_points is None:
            raise ValueError("quad_points are required when selection_mode='quad'")
        if self.quad_points is not None and self.selection_mode is None:
            self.selection_mode = "quad"
        return self


# ── Response schema ─────────────────────────────────────────────────────────────


class CorrectionApplyResponse(BaseModel):
    status: str = "ok"


# ── Internal helpers ────────────────────────────────────────────────────────────


def _fetch_job_or_404(db: Session, job_id: str) -> Job:
    """Return the Job row or raise HTTP 404."""
    job: Job | None = db.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} not found.",
        )
    return job


def _derive_corrected_uri(base_uri: str | None) -> str | None:
    """
    Return a derived artifact URI for the single-page corrected output.

    Inserts '_corrected' before the file extension if one exists, otherwise
    appends it. Returns None when base_uri is None.

    Examples:
        's3://bucket/norm.tiff' → 's3://bucket/norm_corrected.tiff'
        's3://bucket/page'      → 's3://bucket/page_corrected'
    """
    if base_uri is None:
        return None
    dot = base_uri.rfind(".")
    slash = base_uri.rfind("/")
    if dot > slash:
        return base_uri[:dot] + "_corrected" + base_uri[dot:]
    return base_uri + "_corrected"


def _derive_child_corrected_uri(
    source_uri: str,
    job_id: str,
    page_number: int,
    sub_page_index: int,
) -> str:
    """
    Return the corrected artifact URI for a split child page.

    Format (spec Section 8.6):
        s3://{bucket}/jobs/{job_id}/corrected/{page_number}_{sub_page_index}.tiff
        file://jobs/{job_id}/corrected/{page_number}_{sub_page_index}.tiff  (local/CI)
    """
    parsed = urlparse(source_uri)
    if parsed.scheme == "s3":
        return (
            f"s3://{parsed.netloc}/jobs/{job_id}/corrected/" f"{page_number}_{sub_page_index}.tiff"
        )
    # file:// fallback for local development and CI
    return f"file://jobs/{job_id}/corrected/{page_number}_{sub_page_index}.tiff"


def _prior_correction_source_uri(lineage: PageLineage | None) -> str | None:
    """Return the previously selected source artifact when available."""
    if lineage is None or not isinstance(lineage.human_correction_fields, dict):
        return None
    prior_source_uri = lineage.human_correction_fields.get("source_artifact_uri")
    return prior_source_uri if isinstance(prior_source_uri, str) and prior_source_uri else None


def _fetch_latest_geometry_gate(
    db: Session,
    job_id: str,
    page_number: int,
) -> QualityGateLog | None:
    """Return the most recent geometry gate record for this page."""
    return (
        db.query(QualityGateLog)
        .filter(
            QualityGateLog.job_id == job_id,
            QualityGateLog.page_number == page_number,
            QualityGateLog.gate_type.in_(list(_GEOMETRY_GATE_TYPES)),
        )
        .order_by(desc(QualityGateLog.created_at))
        .first()
    )


def _candidate_correction_source_uris(
    db: Session,
    page: JobPage,
    lineage: PageLineage | None,
) -> list[str]:
    """Return the concrete workspace artifact URIs allowed as edit bases."""
    candidates: list[str] = []

    def add(uri: str | None) -> None:
        if isinstance(uri, str) and uri and uri not in candidates:
            candidates.append(uri)

    add(page.output_image_uri)
    add(lineage.output_image_uri if lineage is not None else None)
    add(lineage.otiff_uri if lineage is not None else None)
    add(page.input_image_uri)

    add(_prior_correction_source_uri(lineage))

    if lineage is not None and getattr(lineage, "iep1d_used", False) is True:
        add(_fetch_iep1d_rectified_uri(db, lineage.lineage_id))

    return candidates


def _resolve_correction_source_uri(
    db: Session,
    page: JobPage,
    lineage: PageLineage | None,
    requested_source_uri: str | None,
) -> str:
    """
    Resolve the artifact URI correction should read from.

    Default behavior preserves the legacy "current output first, then input"
    fallback. When the UI submits an explicit source artifact URI, it must match
    one of the real workspace sources for that page.
    """
    default_source_uri = (
        page.output_image_uri
        or (lineage.output_image_uri if lineage is not None else None)
        or _prior_correction_source_uri(lineage)
        or (lineage.otiff_uri if lineage is not None else None)
        or page.input_image_uri
    )
    if requested_source_uri is None:
        if default_source_uri is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Data-integrity failure: page {page.page_number} of job {page.job_id!r} "
                    "has no source artifact URI. Cannot create corrected artifact."
                ),
            )
        return default_source_uri

    allowed_uris = _candidate_correction_source_uris(db, page, lineage)
    if requested_source_uri not in allowed_uris:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="source_artifact_uri is not a valid editable source for this page.",
        )
    return requested_source_uri


def _split_x_from_gate(gate: QualityGateLog | None) -> int | None:
    """
    The geometry gate stores model responses in proxy-image pixel space.
    The proxy dimensions required to scale split_x to full-resolution are not
    recorded in the gate log, so the value cannot be used safely as a full-res
    coordinate.  Always returns None; _resolve_split_x falls through to the
    correct image_width // 2 default.
    """
    return None


def _resolve_split_x(
    *,
    requested_split_x: int | None,
    parent_lineage: PageLineage,
    gate: QualityGateLog | None,
    image_width: int,
) -> int:
    """Resolve the internal split boundary without requiring it in the normal UI."""
    if requested_split_x is not None:
        return requested_split_x

    hcf = parent_lineage.human_correction_fields or {}
    raw_hcf_split_x = hcf.get("split_x")
    if raw_hcf_split_x is not None:
        return int(raw_hcf_split_x)

    gate_split_x = _split_x_from_gate(gate)
    if gate_split_x is not None:
        return gate_split_x

    if image_width < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Page image width {image_width} is too small to split.",
        )
    return image_width // 2


def _decode_split_source_image(source_uri: str, data: bytes) -> np.ndarray:
    """
    Decode the parent artifact into an in-memory image array for child splitting.

    Tries cv2.imdecode first (fast path). Falls back to Pillow for formats that
    cv2 cannot decode: pyramidal TIFFs (PTIFF), CCITT G3/G4 compressed TIFFs,
    and other non-standard encodings.

    Raises HTTP 500 on decode failure because the stored parent artifact is
    expected to be a readable TIFF/PTIFF at correction time.
    """
    import io as _io  # noqa: PLC0415

    # cv2 fast path — handles most standard TIFF/JPEG encodings
    image: np.ndarray | None = None
    try:
        buf = np.frombuffer(data, dtype=np.uint8)
        image = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    except cv2.error:
        image = None

    if image is not None and image.ndim >= 2:
        return image

    # Pillow fallback for PTIFF, CCITT G4, and other formats cv2 cannot handle
    try:
        from PIL import Image as _Image  # noqa: PLC0415

        pil_img = _Image.open(_io.BytesIO(data))
        pil_img.seek(0)  # ensure first frame/page
        if pil_img.mode not in ("RGB", "RGBA", "L"):
            pil_img = pil_img.convert("RGB")
        image = np.array(pil_img)
        if image.ndim >= 2:
            return image
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Data-integrity failure: cannot decode parent artifact {source_uri!r}: {exc}",
        ) from exc

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Data-integrity failure: cannot decode parent artifact {source_uri!r}.",
    )


def _encode_tiff_image(image: np.ndarray, *, source_uri: str, context: str) -> bytes:
    """Encode a raster artifact to TIFF bytes."""
    ok, encoded = cv2.imencode(".tiff", image)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Data-integrity failure: could not encode TIFF for {context} from {source_uri!r}.",
        )
    return encoded.tobytes()


def _normalize_selection_mode(
    selection_mode: SelectionMode | None,
    quad_points: list[QuadPoint] | None,
) -> SelectionMode:
    """Resolve the effective reviewer geometry mode."""
    if quad_points is not None or selection_mode == "quad":
        return "quad"
    return "rect"


def _quad_points_from_crop_box(crop_box: list[int] | None) -> list[QuadPoint] | None:
    """Return a rectangular quad matching an axis-aligned crop box."""
    if crop_box is None:
        return None
    x1, y1, x2, y2 = crop_box
    return [
        (float(x1), float(y1)),
        (float(x2), float(y1)),
        (float(x2), float(y2)),
        (float(x1), float(y2)),
    ]


def _crop_box_from_quad_points(quad_points: list[QuadPoint] | None) -> list[int] | None:
    """Return the integer bounding box enclosing a quad."""
    if quad_points is None:
        return None
    xs = [point[0] for point in quad_points]
    ys = [point[1] for point in quad_points]
    x1 = int(math.floor(min(xs)))
    y1 = int(math.floor(min(ys)))
    x2 = int(math.ceil(max(xs)))
    y2 = int(math.ceil(max(ys)))
    return [x1, y1, x2, y2]


def _json_quad_points(quad_points: list[QuadPoint] | None) -> list[list[float]] | None:
    """Convert quad points to a JSON-serializable nested list."""
    if quad_points is None:
        return None
    return [[float(x), float(y)] for x, y in quad_points]


def _clamp_quad_points(
    quad_points: list[QuadPoint],
    *,
    width: int,
    height: int,
) -> list[QuadPoint]:
    """Clamp quad points to valid image coordinates."""
    max_x = max(0, width)
    max_y = max(0, height)
    return [
        (float(min(max(point[0], 0.0), max_x)), float(min(max(point[1], 0.0), max_y)))
        for point in quad_points
    ]


def _normalize_human_correction_image(
    *,
    source_uri: str,
    image: np.ndarray,
    crop_box: list[int] | None,
    deskew_angle: float | None,
    selection_mode: SelectionMode | None = None,
    quad_points: list[QuadPoint] | None = None,
) -> np.ndarray:
    """Apply the reviewer crop/deskew selection to image pixels."""
    height, width = image.shape[:2]
    effective_mode = _normalize_selection_mode(selection_mode, quad_points)
    if effective_mode == "quad":
        resolved_quad = quad_points or _quad_points_from_crop_box(crop_box)
        if resolved_quad is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Quadrilateral selection is required for {source_uri!r}.",
            )
        clipped_quad = _clamp_quad_points(resolved_quad, width=width, height=height)
        corrected, _, _ = four_point_transform(image, clipped_quad)
        if corrected.size == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Correction quad produced an empty image for {source_uri!r}.",
            )
        return np.ascontiguousarray(corrected)
    bbox = crop_box or [0, 0, width, height]
    if deskew_angle is not None and abs(deskew_angle) > 1e-6:
        corrected, _ = apply_affine_deskew(
            image,
            float(deskew_angle),
            (bbox[0], bbox[1], bbox[2], bbox[3]),
        )
        return corrected

    x_min = max(0, min(width - 1, int(round(bbox[0]))))
    y_min = max(0, min(height - 1, int(round(bbox[1]))))
    x_max = max(x_min + 1, min(width, int(round(bbox[2]))))
    y_max = max(y_min + 1, min(height, int(round(bbox[3]))))
    corrected = np.ascontiguousarray(image[y_min:y_max, x_min:x_max])
    if corrected.size == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Correction crop produced an empty image for {source_uri!r}.",
        )
    return corrected


def _invalidate_layout_state(page: JobPage, lineage: PageLineage) -> None:
    """Clear stale layout outputs before writing a new authoritative page artifact."""
    page.output_layout_uri = None
    page.layout_consensus_result = None
    gate_results = dict(lineage.gate_results or {})
    gate_results.pop("downsample", None)
    gate_results.pop("layout_input", None)
    gate_results.pop("layout_adjudication", None)
    lineage.gate_results = gate_results or None
    lineage.layout_artifact_state = "pending"


def _default_child_crop_box(
    *,
    split_x: int,
    image_width: int,
    image_height: int,
    sub_page_index: int,
) -> list[int]:
    """Return the initial child crop box in parent-image coordinates."""
    if split_x <= 0 or split_x >= image_width:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"split_x must be within the image width (got {split_x}, width={image_width}).",
        )
    if sub_page_index == 0:
        return [0, 0, split_x, image_height]
    return [split_x, 0, image_width, image_height]


def _default_child_quad_points(
    *,
    split_x: int,
    image_width: int,
    image_height: int,
    sub_page_index: int,
) -> list[QuadPoint]:
    """Return the initial child quad in parent-image coordinates."""
    crop_box = _default_child_crop_box(
        split_x=split_x,
        image_width=image_width,
        image_height=image_height,
        sub_page_index=sub_page_index,
    )
    quad_points = _quad_points_from_crop_box(crop_box)
    assert quad_points is not None
    return quad_points


# ── Split correction path (Packet 5.3) ──────────────────────────────────────────


def _apply_split_correction(
    db: Session,
    job: Job,
    parent: JobPage,
    body: CorrectionApplyRequest,
    r: redis.Redis,
) -> None:
    """
    Execute the reviewer-driven split path for a parent page.

    Creates two child sub-pages (left: sub_page_index=0, right: sub_page_index=1)
    and records child lineage plus parent-space crop defaults. The reviewer then
    corrects each child page separately. No child artifact is written here; each
    child preview uses the shared parent source image until submit time.

    The parent page may transition to "split" within the same request once the
    child review units satisfy the worker-terminal closure rule.

    The caller is responsible for db.commit() after this function returns.
    """
    now = datetime.now(UTC)

    # Step A — Fetch parent lineage row
    # Data-integrity requirement: parent lineage must exist.
    parent_lineage: PageLineage | None = (
        db.query(PageLineage)
        .filter(
            PageLineage.job_id == parent.job_id,
            PageLineage.page_number == parent.page_number,
            PageLineage.sub_page_index == None,  # noqa: E711
        )
        .first()
    )
    if parent_lineage is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Data-integrity failure: no lineage row for job {parent.job_id!r} "
                f"page {parent.page_number}."
            ),
        )

    # Step B — Resolve the reviewer-selected source URI. When the workspace sends
    # an explicit source_artifact_uri, split the exact displayed artifact.
    source_uri = _resolve_correction_source_uri(
        db,
        parent,
        parent_lineage,
        body.source_artifact_uri,
    )
    if source_uri is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Data-integrity failure: page {parent.page_number} of job "
                f"{parent.job_id!r} has no source artifact URI. "
                "Cannot create corrected child artifacts."
            ),
        )

    parent_backend = get_backend(source_uri)
    parent_image_bytes = parent_backend.get_bytes(source_uri)
    parent_image = _decode_split_source_image(source_uri, parent_image_bytes)
    gate = _fetch_latest_geometry_gate(db, parent.job_id, parent.page_number)
    resolved_split_x = _resolve_split_x(
        requested_split_x=body.split_x,
        parent_lineage=parent_lineage,
        gate=gate,
        image_width=int(parent_image.shape[1]),
    )
    parent_image_height = int(parent_image.shape[0])
    parent_image_width = int(parent_image.shape[1])
    correction_fields: dict[str, Any] = {
        "crop_box": body.crop_box,
        "deskew_angle": body.deskew_angle,
        "page_structure": "spread",
        "split_x": resolved_split_x,
        "source_artifact_uri": source_uri,
        "image_width": parent_image_width,
        "image_height": parent_image_height,
    }
    _invalidate_layout_state(parent, parent_lineage)
    parent_lineage.human_correction_fields = correction_fields
    parent_lineage.reviewer_notes = body.notes

    children: list[JobPage] = []

    # Step C — For each side: create/reuse child page + lineage. Children stay
    # in pending_human_correction so the reviewer can continue in Page 0 / Page 1
    # workspaces. No pre-split child artifact is written here.
    for sub_idx in (0, 1):
        # Create or reuse child JobPage (idempotent).
        child: JobPage | None = (
            db.query(JobPage)
            .filter(
                JobPage.job_id == parent.job_id,
                JobPage.page_number == parent.page_number,
                JobPage.sub_page_index == sub_idx,
            )
            .first()
        )
        if child is None:
            child = JobPage(
                page_id=str(uuid.uuid4()),
                job_id=parent.job_id,
                page_number=parent.page_number,
                sub_page_index=sub_idx,
                status="pending_human_correction",
                input_image_uri=parent.input_image_uri,
                output_image_uri=None,
            )
            db.add(child)

        child_quad_points = _default_child_quad_points(
            split_x=resolved_split_x,
            image_width=parent_image_width,
            image_height=parent_image_height,
            sub_page_index=sub_idx,
        )
        child_correction_fields = {
            **correction_fields,
            "page_structure": "single",
            "selection_mode": "quad",
            "quad_points": _json_quad_points(child_quad_points),
            "crop_box": _crop_box_from_quad_points(child_quad_points),
            "deskew_angle": None,
            "source_artifact_uri": source_uri,
            "sub_page_index": sub_idx,
            "image_width": parent_image_width,
            "image_height": parent_image_height,
        }

        # Child pages preview the shared parent source until the reviewer submits
        # a child-specific correction.
        child.output_layout_uri = None
        child.layout_consensus_result = None
        child.output_image_uri = None

        # Create or update child lineage row (idempotent).
        child_lineage: PageLineage | None = (
            db.query(PageLineage)
            .filter(
                PageLineage.job_id == parent.job_id,
                PageLineage.page_number == parent.page_number,
                PageLineage.sub_page_index == sub_idx,
            )
            .first()
        )
        if child_lineage is None:
            child_lineage = PageLineage(
                lineage_id=str(uuid.uuid4()),
                job_id=parent.job_id,
                page_number=parent.page_number,
                sub_page_index=sub_idx,
                correlation_id=parent_lineage.correlation_id,
                input_image_uri=parent_lineage.input_image_uri,
                input_image_hash=parent_lineage.input_image_hash,
                otiff_uri=parent_lineage.otiff_uri,
                material_type=parent_lineage.material_type,
                routing_path=parent_lineage.routing_path,
                policy_version=parent_lineage.policy_version,
                parent_page_id=parent.page_id,
                split_source=True,
                human_corrected=True,
                human_correction_timestamp=now,
                human_correction_fields=child_correction_fields,
                output_image_uri=None,
                reviewer_notes=body.notes,
            )
            db.add(child_lineage)
        else:
            # Idempotent update for repeated calls.
            _invalidate_layout_state(child, child_lineage)
            child_lineage.preprocessed_artifact_state = "pending"
            child_lineage.human_corrected = True
            child_lineage.human_correction_timestamp = now
            child_lineage.human_correction_fields = child_correction_fields
            child_lineage.output_image_uri = None
            if body.notes is not None:
                child_lineage.reviewer_notes = body.notes

        # Track child rows in memory for the split-parent closure check below.
        children.append(child)

    db.flush()

    advanced = advance_page_state(
        db,
        parent.page_id,
        from_state="pending_human_correction",
        to_state="split",
    )
    if not advanced:
        logger.warning(
            "Split correction parent-to-split CAS miss: job=%s page_id=%s",
            parent.job_id,
            parent.page_id,
        )

    logger.info(
        "Split correction applied: job=%s page=%d split_x=%d pipeline_mode=%s",
        parent.job_id,
        parent.page_number,
        resolved_split_x,
        job.pipeline_mode,
    )


# ── Single-page correction path (Packet 5.2) ────────────────────────────────────


def _apply_single_page_correction(
    db: Session,
    job: Job,
    page: JobPage,
    body: CorrectionApplyRequest,
    r: redis.Redis,
) -> None:
    """
    Execute the single-page correction path (Packet 5.2).

    Works for both parent pages (sub_page_index IS NULL) and child sub-pages
    (sub_page_index = 0 or 1 after a split). Callers must ensure the page is
    already in pending_human_correction before calling.

    The caller is responsible for db.commit() after this function returns.
    """
    lineage: PageLineage | None = (
        db.query(PageLineage)
        .filter(
            PageLineage.job_id == page.job_id,
            PageLineage.page_number == page.page_number,
            PageLineage.sub_page_index == page.sub_page_index,
        )
        .first()
    )

    if lineage is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Data-integrity failure: no lineage row for job {page.job_id!r} "
                f"page {page.page_number} sub_page_index {page.sub_page_index!r}. "
                "Page cannot be corrected without an existing lineage record."
            ),
        )

    # Use the selected workspace source when the UI provides one; otherwise
    # preserve the legacy current-output-first fallback behavior.
    source_uri = _resolve_correction_source_uri(
        db,
        page,
        lineage,
        body.source_artifact_uri,
    )

    if page.sub_page_index is None:
        corrected_uri = _derive_corrected_uri(source_uri)
        assert corrected_uri is not None  # guaranteed: source_uri is non-None above
    else:
        corrected_uri = _derive_child_corrected_uri(
            source_uri,
            page.job_id,
            page.page_number,
            page.sub_page_index,
        )
    src_data = get_backend(source_uri).get_bytes(source_uri)
    source_image = _decode_split_source_image(source_uri, src_data)
    corrected_image = _normalize_human_correction_image(
        source_uri=source_uri,
        image=source_image,
        crop_box=body.crop_box,
        deskew_angle=body.deskew_angle,
        selection_mode=body.selection_mode,
        quad_points=body.quad_points,
    )
    corrected_bytes = _encode_tiff_image(
        corrected_image,
        source_uri=source_uri,
        context="single-page correction",
    )
    _invalidate_layout_state(page, lineage)
    get_backend(corrected_uri).put_bytes(corrected_uri, corrected_bytes)

    now = datetime.now(UTC)
    effective_selection_mode = _normalize_selection_mode(body.selection_mode, body.quad_points)
    stored_quad_points = body.quad_points
    stored_crop_box = body.crop_box
    if effective_selection_mode == "quad":
        stored_quad_points = body.quad_points or _quad_points_from_crop_box(body.crop_box)
        stored_crop_box = _crop_box_from_quad_points(stored_quad_points)
    correction_fields: dict[str, Any] = {
        "selection_mode": effective_selection_mode,
        "quad_points": _json_quad_points(stored_quad_points),
        "crop_box": stored_crop_box,
        "deskew_angle": body.deskew_angle if effective_selection_mode == "rect" else None,
        "source_artifact_uri": source_uri,
    }

    lineage.human_corrected = True
    lineage.human_correction_timestamp = now
    lineage.human_correction_fields = correction_fields
    lineage.output_image_uri = corrected_uri
    if body.notes is not None:
        lineage.reviewer_notes = body.notes

    page.output_image_uri = corrected_uri
    confirm_preprocessed_artifact(db, lineage.lineage_id)

    # Automation-first: route directly to layout_detection (layout mode) or
    # accepted (preprocess-only mode). IEP2 is never run inline here.
    to_state = "layout_detection" if job.pipeline_mode == "layout" else "accepted"

    advanced = advance_page_state(
        db,
        page.page_id,
        from_state="pending_human_correction",
        to_state=to_state,
    )
    if not advanced:
        logger.warning(
            "Correction apply CAS miss: job=%s page_id=%s page_number=%d "
            "(concurrent update or state already changed)",
            page.job_id,
            page.page_id,
            page.page_number,
        )
        return

    page.status = to_state

    db.flush()

    if job.pipeline_mode == "layout":
        enqueue_page_task(
            r,
            PageTask(
                task_id=str(uuid.uuid4()),
                job_id=page.job_id,
                page_id=page.page_id,
                page_number=page.page_number,
                sub_page_index=page.sub_page_index,
            ),
        )
    else:
        page.acceptance_decision = "accepted"
        page.routing_path = "preprocessing_only"
        update_lineage_completion(
            db,
            lineage.lineage_id,
            acceptance_decision="accepted",
            acceptance_reason="preprocessing accepted after human correction",
            routing_path="preprocessing_only",
            total_processing_ms=page.processing_time_ms,
            output_image_uri=corrected_uri,
        )

    logger.info(
        "Correction applied: job=%s page=%d sub_page_index=%s → %s",
        page.job_id,
        page.page_number,
        page.sub_page_index,
        to_state,
    )


# ── Endpoint ────────────────────────────────────────────────────────────────────


@router.post(
    "/v1/jobs/{job_id}/pages/{page_number}/correction",
    response_model=CorrectionApplyResponse,
    status_code=status.HTTP_200_OK,
    tags=["correction"],
    summary="Apply human correction to a page",
)
def apply_correction(
    job_id: str,
    page_number: int,
    body: CorrectionApplyRequest,
    sub_page_index: int | None = Query(default=None),
    db: Session = Depends(get_session),
    r: redis.Redis = Depends(get_redis),
    user: CurrentUser = Depends(require_user),
) -> CorrectionApplyResponse:
    """
    Apply human correction inputs for a page in pending_human_correction.

    When ``sub_page_index`` is provided, the correction targets a specific child
    sub-page created by a prior split. In this mode split structure inputs are
    ignored because a child page cannot be split a second time.

    **Single-page path**
      Used for child-page corrections and for parent pages whose structure is
      kept as a single page. Processing is not re-run. The existing artifact is
      copied to a derived corrected URI and recorded in lineage. The page then
      transitions to layout_detection (layout mode) or accepted (preprocess mode)
      and is enqueued for async IEP2 processing in layout mode.

    **Split path**
      Used for parent pages when the reviewer selects ``page_structure="spread"``
      or an explicit ``split_x`` fallback is provided. Two child sub-pages are
      created or reused idempotently. Each child stores its own parent-space
      crop defaults and remains in pending_human_correction so the reviewer can
      correct Page 0 and Page 1 separately. The real cropped child artifact is
      generated only when that child is submitted. The parent may transition to
      "split" once the child workspaces exist.

    **Error responses**

    - ``404`` — job or page not found
    - ``409`` — page is not in 'pending_human_correction' state
    - ``422`` — invalid body
    - ``500`` — data-integrity failure: lineage row missing or page has no
                source artifact URI
    """
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)

    if sub_page_index is not None:
        # ── Child sub-page correction (split already occurred) ────────────────
        # Split structure was already decided on the parent, so child requests
        # only apply normal correction fields.
        page: JobPage | None = (
            db.query(JobPage)
            .filter(
                JobPage.job_id == job_id,
                JobPage.page_number == page_number,
                JobPage.sub_page_index == sub_page_index,
            )
            .first()
        )
        if page is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Page {page_number} sub {sub_page_index} of job {job_id!r} not found.",
            )
        if page.status != "pending_human_correction":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Page {page_number} sub {sub_page_index} of job {job_id!r} is in state "
                    f"{page.status!r}, not 'pending_human_correction'."
                ),
            )
        _apply_single_page_correction(db, job, page, body, r)

    else:
        # ── Parent page correction ────────────────────────────────────────────
        page = (
            db.query(JobPage)
            .filter(
                JobPage.job_id == job_id,
                JobPage.page_number == page_number,
                JobPage.sub_page_index == None,  # noqa: E711
            )
            .first()
        )
        if page is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Page {page_number} of job {job_id!r} not found.",
            )
        if page.status != "pending_human_correction":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Page {page_number} of job {job_id!r} is in state "
                    f"{page.status!r}, not 'pending_human_correction'."
                ),
            )
        # page_structure is the primary reviewer-facing structure control for
        # parent pages. split_x remains as a compatibility or advanced fallback.
        if body.page_structure == "spread" or body.split_x is not None:
            _apply_split_correction(db, job, page, body, r)
        else:
            _apply_single_page_correction(db, job, page, body, r)

    db.commit()
    return CorrectionApplyResponse()
