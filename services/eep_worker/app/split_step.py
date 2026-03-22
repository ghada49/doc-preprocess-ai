"""
services/eep_worker/app/split_step.py
--------------------------------------
Packet 4.6 — split handling, PTIFF QA routing, and preprocess-only stop path.

Implements two routing layers of the EEP worker pipeline:

  Split handling (spec Section 2.6 / 6.1):
    When selected_geometry.split_required=True, both child pages (left/right,
    sub_page_index 0/1) are normalized and validated independently via
    run_normalization_and_first_validation().  Each child can independently
    trigger the rescue flow (with is_split_child=True).  Processing is
    sequential (spec: left before right).

  PTIFF QA routing (spec Section 8.5):
    After any page completes successful preprocessing (route="accept_now"),
    decide_ptiff_qa_route() maps (ptiff_qa_mode × pipeline_mode) to the next
    page status:
      manual          → ptiff_qa_pending
      auto_continue + preprocess → accepted (routing_path="preprocessing_only")
      auto_continue + layout     → layout_detection

Caller responsibilities (NOT done here):
    - Create child JobPage rows (parent_page_id, sub_page_index).
    - Create child PageLineage rows (split_source=True, parent_page_id).
    - Set parent JobPage.status = "split".
    - Enqueue accepted children to Redis for downstream processing.
    - Set pending_human_correction status on failed children.
    - Commit or roll back the DB session.

Exported:
    SplitChildOutcome      — result for a single split child
    SplitOutcome           — combined result for both children
    PtiffQaRoute           — PTIFF QA routing decision
    run_split_normalization — main split entry point (async)
    decide_ptiff_qa_route  — pure PTIFF QA routing function
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from typing import Literal

import numpy as np
from sqlalchemy.orm import Session

from services.eep.app.gates.artifact_validation import (
    ArtifactImageDimensions,
    ArtifactValidationResult,
)
from services.eep.app.gates.geometry_selection import PreprocessingGateConfig
from services.eep_worker.app.circuit_breaker import CircuitBreaker
from services.eep_worker.app.intake import ProxyConfig
from services.eep_worker.app.normalization_step import (
    NormalizationOutcome,
    run_normalization_and_first_validation,
)
from services.eep_worker.app.rescue_step import RescueOutcome, run_rescue_flow
from shared.gpu.backend import GPUBackend
from shared.io.storage import StorageBackend
from shared.schemas.geometry import GeometryResponse
from shared.schemas.preprocessing import PreprocessBranchResponse

__all__ = [
    "SplitChildOutcome",
    "SplitOutcome",
    "PtiffQaRoute",
    "run_split_normalization",
    "decide_ptiff_qa_route",
]


# ── Result types ───────────────────────────────────────────────────────────────


@dataclasses.dataclass
class SplitChildOutcome:
    """
    Result of normalization + validation for one split child.

    Attributes:
        sub_page_index:   0 for left child, 1 for right child.
        route:            "accept_now" or "pending_human_correction".
        review_reason:    Canonical reason string when route is
                          "pending_human_correction"; None otherwise.
        branch_response:  PreprocessBranchResponse from IEP1C normalization.
                          None when the flow exited before normalization.
        validation_result: ArtifactValidationResult from artifact validation.
                           None when the flow exited before Step 5/7.
        used_rescue:      True if the rescue flow (Packet 4.5) was invoked for
                          this child.
    """

    sub_page_index: int
    route: Literal["accept_now", "pending_human_correction"]
    review_reason: str | None
    branch_response: PreprocessBranchResponse | None
    validation_result: ArtifactValidationResult | None
    used_rescue: bool


@dataclasses.dataclass
class SplitOutcome:
    """
    Combined result of split normalization for both children.

    Attributes:
        left:        SplitChildOutcome for sub_page_index=0.
        right:       SplitChildOutcome for sub_page_index=1.
        duration_ms: Wall-clock time from entering run_split_normalization
                     to returning, in milliseconds.
    """

    left: SplitChildOutcome
    right: SplitChildOutcome
    duration_ms: float


@dataclasses.dataclass
class PtiffQaRoute:
    """
    PTIFF QA routing decision (spec Section 8.5).

    Attributes:
        next_status:  The page status to transition to after preprocessing
                      succeeds.
        routing_path: "preprocessing_only" iff next_status=="accepted"; else
                      None.
    """

    next_status: Literal["ptiff_qa_pending", "accepted", "layout_detection"]
    routing_path: str | None


# ── PTIFF QA routing ───────────────────────────────────────────────────────────


def decide_ptiff_qa_route(
    pipeline_mode: str,
    ptiff_qa_mode: str,
) -> PtiffQaRoute:
    """
    Determine the next page status after successful preprocessing (spec Section 8.5).

    The routing table is:
        ptiff_qa_mode="manual"
            → ptiff_qa_pending (regardless of pipeline_mode)
        ptiff_qa_mode="auto_continue" + pipeline_mode="preprocess"
            → accepted, routing_path="preprocessing_only"
        ptiff_qa_mode="auto_continue" + pipeline_mode="layout"
            → layout_detection

    Args:
        pipeline_mode: "preprocess" or "layout" (from job/config).
        ptiff_qa_mode: "manual" or "auto_continue" (from config).

    Returns:
        PtiffQaRoute with next_status and routing_path.

    Raises:
        ValueError: For any unrecognised combination (guard against bad config).
    """
    if ptiff_qa_mode == "manual":
        return PtiffQaRoute(next_status="ptiff_qa_pending", routing_path=None)

    if ptiff_qa_mode == "auto_continue":
        if pipeline_mode == "preprocess":
            return PtiffQaRoute(next_status="accepted", routing_path="preprocessing_only")
        if pipeline_mode == "layout":
            return PtiffQaRoute(next_status="layout_detection", routing_path=None)
        raise ValueError(
            f"decide_ptiff_qa_route: unrecognised pipeline_mode={pipeline_mode!r} "
            f"with ptiff_qa_mode='auto_continue'"
        )

    raise ValueError(f"decide_ptiff_qa_route: unrecognised ptiff_qa_mode={ptiff_qa_mode!r}")


# ── Split child helpers ─────────────────────────────────────────────────────────


def _child_from_norm(
    sub_page_index: int,
    norm: NormalizationOutcome,
) -> SplitChildOutcome:
    """Build a SplitChildOutcome directly from a first-pass NormalizationOutcome."""
    if norm.route == "accept_now":
        return SplitChildOutcome(
            sub_page_index=sub_page_index,
            route="accept_now",
            review_reason=None,
            branch_response=norm.branch_response,
            validation_result=norm.validation_result,
            used_rescue=False,
        )
    # rescue_required — caller will invoke rescue; this path should not be hit
    # directly because run_split_normalization handles the rescue branch.
    return SplitChildOutcome(
        sub_page_index=sub_page_index,
        route="pending_human_correction",
        review_reason=None,
        branch_response=norm.branch_response,
        validation_result=norm.validation_result,
        used_rescue=False,
    )


def _child_from_rescue(
    sub_page_index: int,
    rescue: RescueOutcome,
) -> SplitChildOutcome:
    """Build a SplitChildOutcome from a RescueOutcome."""
    return SplitChildOutcome(
        sub_page_index=sub_page_index,
        route=rescue.route,
        review_reason=rescue.review_reason,
        branch_response=rescue.branch_response,
        validation_result=rescue.validation_result,
        used_rescue=True,
    )


# ── Main entry point ───────────────────────────────────────────────────────────


async def run_split_normalization(
    *,
    full_res_image: np.ndarray,
    selected_geometry: GeometryResponse,
    selected_model: Literal["iep1a", "iep1b"],
    proxy_width: int,
    proxy_height: int,
    # First-pass artifact URIs (one per child)
    left_output_uri: str,
    right_output_uri: str,
    # Rescue artifact URIs (only used if rescue is triggered)
    left_rescue_output_uri: str,
    right_rescue_output_uri: str,
    left_rectified_proxy_uri: str,
    right_rectified_proxy_uri: str,
    # Shared infrastructure
    storage: StorageBackend,
    image_loader: Callable[[str], ArtifactImageDimensions],
    # For rescue flow
    job_id: str,
    page_number: int,
    lineage_id: str,
    material_type: str,
    iep1d_endpoint: str,
    iep1a_endpoint: str,
    iep1b_endpoint: str,
    iep1d_circuit_breaker: CircuitBreaker,
    iep1a_circuit_breaker: CircuitBreaker,
    iep1b_circuit_breaker: CircuitBreaker,
    backend: GPUBackend,
    session: Session,
    proxy_config: ProxyConfig | None = None,
    gate_config: PreprocessingGateConfig | None = None,
) -> SplitOutcome:
    """
    Normalize and validate both split children independently (spec Section 2.6).

    Processing is sequential (left before right) per spec.  Each child goes
    through first-pass normalization (run_normalization_and_first_validation).
    If the first pass routes to "rescue_required", the rescue flow (run_rescue_flow)
    is invoked with is_split_child=True for that child.

    Args:
        full_res_image:         H×W×C uint8 numpy array of the full-resolution OTIFF.
        selected_geometry:      GeometryResponse from the winning model (in proxy
                                space); must have split_required=True and at least
                                2 pages.
        selected_model:         "iep1a" or "iep1b" — which model was selected.
        proxy_width:            Pixel width of the proxy image.
        proxy_height:           Pixel height of the proxy image.
        left_output_uri:        Storage URI for left child first-pass artifact.
        right_output_uri:       Storage URI for right child first-pass artifact.
        left_rescue_output_uri: Storage URI for left child rescue artifact.
        right_rescue_output_uri:Storage URI for right child rescue artifact.
        left_rectified_proxy_uri:  Storage URI for left child rectified proxy.
        right_rectified_proxy_uri: Storage URI for right child rectified proxy.
        storage:                StorageBackend instance (caller owns lifecycle).
        image_loader:           Callable(uri) → ArtifactImageDimensions.
        job_id:                 Parent job identifier.
        page_number:            1-indexed page number.
        lineage_id:             FK for ServiceInvocation audit rows.
        material_type:          Job material type string.
        iep1d_endpoint:         Full HTTP URL for IEP1D POST /v1/rectify.
        iep1a_endpoint:         Full HTTP URL for IEP1A.
        iep1b_endpoint:         Full HTTP URL for IEP1B.
        iep1d_circuit_breaker:  Per-worker CircuitBreaker for IEP1D.
        iep1a_circuit_breaker:  Per-worker CircuitBreaker for IEP1A.
        iep1b_circuit_breaker:  Per-worker CircuitBreaker for IEP1B.
        backend:                Shared GPUBackend instance.
        session:                SQLAlchemy session (caller owns commit/rollback).
        proxy_config:           ProxyConfig; defaults to ProxyConfig().
        gate_config:            Policy thresholds; defaults to PreprocessingGateConfig().

    Returns:
        SplitOutcome(left, right, duration_ms) where left.sub_page_index==0 and
        right.sub_page_index==1.

    Raises:
        Any exception from storage, normalization, or rescue propagates unchanged
        (infrastructure failures — the caller decides retry vs. fail).
    """
    t0 = time.monotonic()

    # ── Left child (page_index=0) ─────────────────────────────────────────────
    left_norm: NormalizationOutcome = run_normalization_and_first_validation(
        full_res_image=full_res_image,
        selected_geometry=selected_geometry,
        selected_model=selected_model,
        geometry_route_decision="accepted",
        proxy_width=proxy_width,
        proxy_height=proxy_height,
        output_uri=left_output_uri,
        storage=storage,
        image_loader=image_loader,
        page_index=0,
        gate_config=gate_config,
    )

    if left_norm.route == "accept_now":
        left_child = _child_from_norm(0, left_norm)
    else:
        # rescue_required
        left_rescue: RescueOutcome = await run_rescue_flow(
            artifact_uri=left_output_uri,
            is_split_child=True,
            page_index=0,
            rescue_output_uri=left_rescue_output_uri,
            rectified_proxy_uri=left_rectified_proxy_uri,
            job_id=job_id,
            page_number=page_number,
            lineage_id=lineage_id,
            material_type=material_type,
            iep1d_endpoint=iep1d_endpoint,
            iep1a_endpoint=iep1a_endpoint,
            iep1b_endpoint=iep1b_endpoint,
            iep1d_circuit_breaker=iep1d_circuit_breaker,
            iep1a_circuit_breaker=iep1a_circuit_breaker,
            iep1b_circuit_breaker=iep1b_circuit_breaker,
            backend=backend,
            session=session,
            storage=storage,
            image_loader=image_loader,
            proxy_config=proxy_config,
            gate_config=gate_config,
        )
        left_child = _child_from_rescue(0, left_rescue)

    # ── Right child (page_index=1) ────────────────────────────────────────────
    right_norm: NormalizationOutcome = run_normalization_and_first_validation(
        full_res_image=full_res_image,
        selected_geometry=selected_geometry,
        selected_model=selected_model,
        geometry_route_decision="accepted",
        proxy_width=proxy_width,
        proxy_height=proxy_height,
        output_uri=right_output_uri,
        storage=storage,
        image_loader=image_loader,
        page_index=1,
        gate_config=gate_config,
    )

    if right_norm.route == "accept_now":
        right_child = _child_from_norm(1, right_norm)
    else:
        # rescue_required
        right_rescue: RescueOutcome = await run_rescue_flow(
            artifact_uri=right_output_uri,
            is_split_child=True,
            page_index=1,
            rescue_output_uri=right_rescue_output_uri,
            rectified_proxy_uri=right_rectified_proxy_uri,
            job_id=job_id,
            page_number=page_number,
            lineage_id=lineage_id,
            material_type=material_type,
            iep1d_endpoint=iep1d_endpoint,
            iep1a_endpoint=iep1a_endpoint,
            iep1b_endpoint=iep1b_endpoint,
            iep1d_circuit_breaker=iep1d_circuit_breaker,
            iep1a_circuit_breaker=iep1a_circuit_breaker,
            iep1b_circuit_breaker=iep1b_circuit_breaker,
            backend=backend,
            session=session,
            storage=storage,
            image_loader=image_loader,
            proxy_config=proxy_config,
            gate_config=gate_config,
        )
        right_child = _child_from_rescue(1, right_rescue)

    return SplitOutcome(
        left=left_child,
        right=right_child,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )
