"""
services/eep_worker/app/normalization_step.py
----------------------------------------------
Packet 4.4 — normalization and first artifact validation.

Implements Steps 4 and 5 of the EEP worker pipeline (spec Section 6.1):

  Step 4 — IEP1C normalization:
    1. Scale geometry coordinates from proxy-image space to full-resolution space.
    2. Call normalize_single_page() on the full-resolution OTIFF image.
    3. Encode the normalized output and write to storage.
    4. Assemble PreprocessBranchResponse via normalize_result_to_branch_response().

  Step 5 — First artifact validation gate:
    5. Call run_artifact_validation() (hard requirements + soft scoring).
    6. Decide accept-now vs rescue-required.

Route decision (Packet 4.4 output):
    accept_now      — geometry trust HIGH (route_decision="accepted") AND
                      artifact validation passed.  Caller routes directly to
                      layout_detection or accepted (Packet 4.6).
    rescue_required — any of: geometry trust LOW ("rectification"), no geometry
                      ("pending_human_correction"), or artifact validation failed.
                      Caller routes to the rescue/rectification path (Packet 4.5).

Caller responsibilities:
    - Obtain the full-resolution OTIFF numpy array (via decode_otiff).
    - Obtain the selected GeometryResponse and source model from GeometryInvocationResult.
    - Supply the output_uri where the normalized artifact will be written.
    - Supply an image_loader callable (see make_cv2_image_loader in artifact_validation.py).
    - Commit or roll back the DB session (not performed here).

Exported:
    NormalizationOutcome              — result dataclass
    scale_page_region                 — scale one PageRegion from proxy to full-res
    scale_geometry_response           — scale full GeometryResponse from proxy to full-res
    run_normalization_and_first_validation — main entry point
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from typing import Literal

import cv2
import numpy as np

from services.eep.app.gates.artifact_validation import (
    ArtifactImageDimensions,
    ArtifactValidationResult,
    run_artifact_validation,
)
from services.eep.app.gates.geometry_selection import PreprocessingGateConfig
from shared.io.storage import StorageBackend
from shared.normalization.normalize import (
    NormalizeResult,
    normalize_result_to_branch_response,
    normalize_single_page,
)
from shared.schemas.geometry import GeometryResponse, PageRegion
from shared.schemas.preprocessing import PreprocessBranchResponse

__all__ = [
    "NormalizationOutcome",
    "scale_page_region",
    "scale_geometry_response",
    "run_normalization_and_first_validation",
]


# ── Result type ────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class NormalizationOutcome:
    """
    Result of normalization + first artifact validation (Steps 4 & 5).

    Attributes:
        branch_response:   PreprocessBranchResponse produced by IEP1C normalization.
        validation_result: ArtifactValidationResult from the artifact validation gate.
        route:             "accept_now" or "rescue_required" — the combined routing decision.
        duration_ms:       Wall-clock time from entering run_normalization_and_first_validation
                           to returning, in milliseconds.
    """

    branch_response: PreprocessBranchResponse
    validation_result: ArtifactValidationResult
    route: Literal["accept_now", "rescue_required"]
    duration_ms: float


# ── Geometry scaling ───────────────────────────────────────────────────────────


def scale_page_region(
    region: PageRegion,
    scale_x: float,
    scale_y: float,
) -> PageRegion:
    """
    Return a new PageRegion with all pixel coordinates scaled from proxy to full-res space.

    ``corners`` and ``bbox`` are scaled by the provided factors.
    ``page_area_fraction`` and ``confidence`` are dimensionless and are not modified.

    Args:
        region:  PageRegion in proxy-image pixel space.
        scale_x: Horizontal scale factor (full_res_width / proxy_width).
        scale_y: Vertical scale factor (full_res_height / proxy_height).

    Returns:
        New PageRegion instance with scaled coordinates.
    """
    scaled_corners: list[tuple[float, float]] | None = None
    if region.corners is not None:
        scaled_corners = [(x * scale_x, y * scale_y) for x, y in region.corners]

    scaled_bbox: tuple[int, int, int, int] | None = None
    if region.bbox is not None:
        x_min, y_min, x_max, y_max = region.bbox
        scaled_bbox = (
            int(x_min * scale_x),
            int(y_min * scale_y),
            int(x_max * scale_x),
            int(y_max * scale_y),
        )

    return PageRegion(
        region_id=region.region_id,
        geometry_type=region.geometry_type,
        corners=scaled_corners,
        bbox=scaled_bbox,
        confidence=region.confidence,
        page_area_fraction=region.page_area_fraction,
    )


def scale_geometry_response(
    response: GeometryResponse,
    proxy_w: int,
    proxy_h: int,
    full_w: int,
    full_h: int,
) -> GeometryResponse:
    """
    Return a new GeometryResponse with all pixel coordinates scaled to full-resolution space.

    All PageRegion coordinates (corners, bbox) and the top-level split_x are scaled.
    Scalar metrics (geometry_confidence, tta_structural_agreement_rate, etc.) are unchanged.

    Args:
        response: GeometryResponse in proxy-image pixel space.
        proxy_w:  Pixel width of the proxy image.
        proxy_h:  Pixel height of the proxy image.
        full_w:   Pixel width of the full-resolution image.
        full_h:   Pixel height of the full-resolution image.

    Returns:
        New GeometryResponse with coordinates in full-resolution pixel space.
    """
    scale_x = full_w / proxy_w
    scale_y = full_h / proxy_h

    scaled_pages = [scale_page_region(p, scale_x, scale_y) for p in response.pages]

    scaled_split_x: int | None = None
    if response.split_x is not None:
        scaled_split_x = int(response.split_x * scale_x)

    return GeometryResponse(
        page_count=response.page_count,
        pages=scaled_pages,
        split_required=response.split_required,
        split_x=scaled_split_x,
        geometry_confidence=response.geometry_confidence,
        tta_structural_agreement_rate=response.tta_structural_agreement_rate,
        tta_prediction_variance=response.tta_prediction_variance,
        tta_passes=response.tta_passes,
        uncertainty_flags=list(response.uncertainty_flags),
        warnings=list(response.warnings),
        processing_time_ms=response.processing_time_ms,
    )


# ── Image encoding ─────────────────────────────────────────────────────────────


def _encode_image_tiff(image: np.ndarray) -> bytes:
    """Encode a numpy array to TIFF bytes via OpenCV."""
    success, buf = cv2.imencode(".tiff", image)
    if not success:
        raise ValueError("cv2.imencode failed: could not encode image to TIFF")
    raw: bytes = buf.tobytes()
    return raw


# ── Route decision ─────────────────────────────────────────────────────────────


def _decide_route(
    geometry_route_decision: str,
    validation_result: ArtifactValidationResult,
) -> Literal["accept_now", "rescue_required"]:
    """
    Decide the first-pass routing based on geometry trust and artifact quality.

    accept_now:      geometry trust is HIGH ("accepted") AND artifact validation passed.
    rescue_required: geometry trust is LOW ("rectification"), no geometry was selected
                     ("pending_human_correction"), OR artifact validation failed.

    Safety invariant: only geometry_route_decision == "accepted" AND validation_result.passed
    jointly produce "accept_now".  Any deviation requires rescue.
    """
    if geometry_route_decision == "accepted" and validation_result.passed:
        return "accept_now"
    return "rescue_required"


# ── Main entry point ───────────────────────────────────────────────────────────


def run_normalization_and_first_validation(
    *,
    full_res_image: np.ndarray,
    selected_geometry: GeometryResponse,
    selected_model: Literal["iep1a", "iep1b"],
    geometry_route_decision: str,
    proxy_width: int,
    proxy_height: int,
    output_uri: str,
    storage: StorageBackend,
    image_loader: Callable[[str], ArtifactImageDimensions],
    page_index: int = 0,
    gate_config: PreprocessingGateConfig | None = None,
) -> NormalizationOutcome:
    """
    Execute Steps 4 and 5 of the EEP pipeline: normalize then validate.

    Step 4 — IEP1C Normalization:
        Scales geometry coordinates from proxy-image space to full-resolution space,
        normalizes the page using normalize_single_page(), encodes the result as TIFF,
        writes it to storage via storage.put_bytes(), and assembles a
        PreprocessBranchResponse.

    Step 5 — First Artifact Validation Gate:
        Runs run_artifact_validation() (hard requirements + soft scoring) and combines
        the result with the geometry routing decision to produce accept_now or
        rescue_required.

    Args:
        full_res_image:          H×W×C uint8 numpy array of the full-resolution OTIFF.
        selected_geometry:       GeometryResponse from the winning model (in proxy space).
        selected_model:          "iep1a" or "iep1b" — which model was selected.
        geometry_route_decision: route_decision from GeometrySelectionResult
                                 ("accepted" | "rectification" | "pending_human_correction").
        proxy_width:             Pixel width of the proxy image used for geometry inference.
        proxy_height:            Pixel height of the proxy image.
        output_uri:              Storage URI where the normalized artifact will be written.
        storage:                 StorageBackend instance (caller owns lifecycle).
        image_loader:            Callable(uri) → ArtifactImageDimensions; used by the
                                 artifact validation gate to verify the written artifact.
                                 Use make_cv2_image_loader(storage) in production.
        page_index:              Index into selected_geometry.pages (0 for unsplit pages;
                                 0 or 1 for split children in Packet 4.6).
        gate_config:             Policy thresholds; defaults to PreprocessingGateConfig().

    Returns:
        NormalizationOutcome with branch_response, validation_result, route, and duration_ms.

    Raises:
        IndexError:  If page_index is out of bounds for selected_geometry.pages.
        ValueError:  If TIFF encoding fails.
        Any exception from storage.put_bytes() or normalize_single_page() propagates unchanged.
    """
    t0 = time.monotonic()

    # Step 4a — scale geometry from proxy space to full-resolution space
    full_h, full_w = full_res_image.shape[:2]
    full_res_geometry = scale_geometry_response(
        selected_geometry,
        proxy_w=proxy_width,
        proxy_h=proxy_height,
        full_w=full_w,
        full_h=full_h,
    )

    # Step 4b — normalize the specified page region
    page = full_res_geometry.pages[page_index]
    norm_result: NormalizeResult = normalize_single_page(full_res_image, page, full_res_geometry)

    # Step 4c — encode and write the artifact
    encoded = _encode_image_tiff(norm_result.image)
    storage.put_bytes(output_uri, encoded)

    # Step 4d — assemble canonical PreprocessBranchResponse
    branch_response = normalize_result_to_branch_response(norm_result, selected_model, output_uri)

    # Step 5 — artifact validation gate
    validation_result = run_artifact_validation(
        response=branch_response,
        geometry=full_res_geometry,
        image_loader=image_loader,
        config=gate_config,
    )

    # Combined routing decision
    route = _decide_route(geometry_route_decision, validation_result)

    duration_ms = (time.monotonic() - t0) * 1000.0

    return NormalizationOutcome(
        branch_response=branch_response,
        validation_result=validation_result,
        route=route,
        duration_ms=duration_ms,
    )
