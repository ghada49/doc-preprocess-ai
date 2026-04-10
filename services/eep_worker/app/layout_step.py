"""
services/eep_worker/app/layout_step.py
--------------------------------------
Smallest real worker-side orchestration for IEP2 layout completion.

This module owns the post-inference path from ``layout_detection`` to the
persisted accepted/displayable result:
  1. run layout adjudication
  2. convert it into the worker's accepted routing decision
  3. transition ``job_pages.status`` from ``layout_detection`` to ``accepted``
  4. persist the adjudication payload for display/audit
  5. update page_lineage completion metadata

The full queue-consuming task runner is still not implemented in this repo, but
this module is the concrete orchestration unit that such a runner should call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.orm import Session

from services.eep.app.db.lineage import confirm_layout_artifact, update_lineage_completion
from services.eep.app.db.models import JobPage, PageLineage
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.gates.layout_gate import evaluate_layout_adjudication
from services.eep_worker.app.google_config import get_google_worker_state
from services.eep_worker.app.layout_routing import (
    LayoutRoutingDecision,
    build_layout_routing_decision,
)
from shared.schemas.layout import (
    LayoutAdjudicationResult,
    LayoutDetectResponse,
    LayoutInputMetadata,
)
from shared.schemas.ucf import BoundingBox

__all__ = [
    "LayoutTransitionError",
    "LayoutStepResult",
    "complete_layout_detection",
    "complete_layout_detection_from_adjudication",
    "run_layout_adjudication_only",
]


_USE_WORKER_GOOGLE = object()


class LayoutTransitionError(RuntimeError):
    """Raised when the page can no longer be advanced out of layout_detection."""


@dataclass(frozen=True)
class LayoutStepResult:
    """Persisted result of the layout worker orchestration step."""

    adjudication: LayoutAdjudicationResult
    routing: LayoutRoutingDecision


async def complete_layout_detection(
    *,
    session: Session,
    page: JobPage,
    lineage_id: str,
    material_type: str,
    image_uri: str,
    iep2a_result: LayoutDetectResponse | None,
    iep2b_result: LayoutDetectResponse | None,
    image_bytes: bytes | None = None,
    mime_type: str = "image/tiff",
    google_client: Any | None | object = _USE_WORKER_GOOGLE,
    total_processing_ms: float | None = None,
    output_layout_uri: str | None = None,
    layout_input: LayoutInputMetadata | None = None,
) -> LayoutStepResult:
    """
    Adjudicate IEP2 output and persist the accepted layout result.

    The accepted/displayable layout payload is written to:
      - ``job_pages.layout_consensus_result`` for page-level quick access
      - ``page_lineage.gate_results["layout_adjudication"]`` for full audit/UI

    If ``output_layout_uri`` is supplied, the job page row is updated and the
    lineage layout artifact state is marked confirmed. The current repo does not
    yet contain the artifact-writing implementation itself.
    """
    resolved_google_client = (
        get_google_worker_state().client if google_client is _USE_WORKER_GOOGLE else google_client
    )

    adjudication = await evaluate_layout_adjudication(
        iep2a_result=iep2a_result,
        iep2b_result=iep2b_result,
        google_client=resolved_google_client,
        image_bytes=image_bytes,
        mime_type=mime_type,
        material_type=material_type,
        image_uri=image_uri,
    )

    adjudication = _apply_layout_input_and_rescale(adjudication, layout_input)
    return _persist_completed_layout(
        session=session,
        page=page,
        lineage_id=lineage_id,
        adjudication=adjudication,
        total_processing_ms=total_processing_ms,
        output_layout_uri=output_layout_uri,
    )


def complete_layout_detection_from_adjudication(
    *,
    session: Session,
    page: JobPage,
    lineage_id: str,
    adjudication: LayoutAdjudicationResult,
    total_processing_ms: float | None = None,
    output_layout_uri: str | None = None,
) -> LayoutStepResult:
    """Advance layout_detection using an already-persisted adjudication payload."""
    return _persist_completed_layout(
        session=session,
        page=page,
        lineage_id=lineage_id,
        adjudication=adjudication,
        total_processing_ms=total_processing_ms,
        output_layout_uri=output_layout_uri,
    )


async def run_layout_adjudication_only(
    *,
    session: Session,
    page: JobPage,
    lineage_id: str | None,
    material_type: str,
    image_uri: str,
    iep2a_result: LayoutDetectResponse | None,
    iep2b_result: LayoutDetectResponse | None,
    image_bytes: bytes | None = None,
    mime_type: str = "image/tiff",
    google_client: Any | None | object = _USE_WORKER_GOOGLE,
    total_processing_ms: float | None = None,
    layout_input: LayoutInputMetadata | None = None,
) -> LayoutAdjudicationResult:
    """
    Run layout adjudication and persist results WITHOUT transitioning page state.

    Called when layout should run inline during preprocessing failure, where the
    page is routing to pending_human_correction (auto_continue + layout mode).
    Stores the result in ``job_pages.layout_consensus_result`` and
    ``page_lineage.gate_results["layout_adjudication"]`` so reviewers can see
    layout output before IEP1 is fully resolved.

    Does NOT call ``advance_page_state`` — page state is left unchanged.
    """
    resolved_google_client = (
        get_google_worker_state().client if google_client is _USE_WORKER_GOOGLE else google_client
    )

    adjudication = await evaluate_layout_adjudication(
        iep2a_result=iep2a_result,
        iep2b_result=iep2b_result,
        google_client=resolved_google_client,
        image_bytes=image_bytes,
        mime_type=mime_type,
        material_type=material_type,
        image_uri=image_uri,
    )

    adjudication = _apply_layout_input_and_rescale(adjudication, layout_input)

    # Persist adjudication onto the page row without state transition.
    session.query(JobPage).filter(JobPage.page_id == page.page_id).update(
        cast(Any, {"layout_consensus_result": adjudication.model_dump(mode="json")}),
        synchronize_session="fetch",
    )

    # Persist into lineage gate_results without touching acceptance metadata.
    if lineage_id is not None:
        lineage: PageLineage | None = session.get(PageLineage, lineage_id)
        if lineage is not None:
            existing = lineage.gate_results or {}
            lineage.gate_results = {
                **existing,
                **(
                    {"layout_input": layout_input.model_dump(mode="json")}
                    if layout_input is not None
                    else {}
                ),
                "layout_adjudication": adjudication.model_dump(mode="json"),
            }

    return adjudication


def _apply_layout_input_and_rescale(
    adjudication: LayoutAdjudicationResult,
    layout_input: LayoutInputMetadata | None,
) -> LayoutAdjudicationResult:
    """Attach layout-input metadata and rescale Google coordinates when needed."""
    updated = adjudication
    if (
        layout_input is not None
        and layout_input.coordinate_rescaled
        and adjudication.layout_decision_source == "google_document_ai"
        and adjudication.final_layout_result
    ):
        _sx = layout_input.canonical_output_width / layout_input.layout_input_width
        _sy = layout_input.canonical_output_height / layout_input.layout_input_height
        rescaled_google_regions = [
            r.model_copy(
                update={
                    "bbox": BoundingBox(
                        x_min=r.bbox.x_min * _sx,
                        y_min=r.bbox.y_min * _sy,
                        x_max=r.bbox.x_max * _sx,
                        y_max=r.bbox.y_max * _sy,
                    )
                }
            )
            for r in adjudication.final_layout_result
        ]
        updated = updated.model_copy(update={"final_layout_result": rescaled_google_regions})
    if layout_input is not None:
        updated = updated.model_copy(update={"layout_input": layout_input})
    return updated


def _persist_completed_layout(
    *,
    session: Session,
    page: JobPage,
    lineage_id: str,
    adjudication: LayoutAdjudicationResult,
    total_processing_ms: float | None,
    output_layout_uri: str | None,
) -> LayoutStepResult:
    routing = build_layout_routing_decision(adjudication)
    processing_ms = (
        total_processing_ms if total_processing_ms is not None else adjudication.processing_time_ms
    )

    advanced = advance_page_state(
        session,
        page.page_id,
        "layout_detection",
        routing.next_state,
        acceptance_decision=routing.acceptance_decision,
        routing_path=routing.routing_path,
        processing_time_ms=processing_ms,
    )
    if not advanced:
        raise LayoutTransitionError(
            f"Could not advance page_id={page.page_id!r} from 'layout_detection' "
            f"to {routing.next_state!r}"
        )

    page_updates: dict[str, Any] = {
        "layout_consensus_result": adjudication.model_dump(mode="json"),
        "review_reasons": None,
    }
    if output_layout_uri is not None:
        page_updates["output_layout_uri"] = output_layout_uri

    session.query(JobPage).filter(JobPage.page_id == page.page_id).update(
        cast(Any, page_updates),
        synchronize_session="fetch",
    )

    if output_layout_uri is not None:
        confirm_layout_artifact(session, lineage_id)

    gate_results = routing.gate_results
    if adjudication.layout_input is not None:
        gate_results = {
            **gate_results,
            "layout_input": adjudication.layout_input.model_dump(mode="json"),
        }

    update_lineage_completion(
        session,
        lineage_id,
        acceptance_decision=routing.acceptance_decision,
        acceptance_reason=routing.acceptance_reason,
        routing_path=routing.routing_path,
        total_processing_ms=processing_ms,
        output_image_uri=page.output_image_uri,
        gate_results=gate_results,
    )

    return LayoutStepResult(adjudication=adjudication, routing=routing)
