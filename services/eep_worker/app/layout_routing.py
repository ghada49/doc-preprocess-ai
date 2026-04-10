"""
services/eep_worker/app/layout_routing.py
-----------------------------------------
Worker-facing routing helper for IEP2 layout adjudication.

IEP2 is display-producing, not review-routing. This module converts a
LayoutAdjudicationResult into the single accepted-path decision the worker
should persist. Review reasons are never emitted from this helper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from shared.schemas.layout import LayoutAdjudicationResult, Region

__all__ = [
    "LayoutRoutingDecision",
    "build_layout_routing_decision",
]


@dataclass(frozen=True)
class LayoutRoutingDecision:
    """Concrete worker action derived from layout adjudication."""

    next_state: Literal["accepted"]
    acceptance_decision: Literal["accepted"]
    routing_path: Literal["layout_adjudication"]
    acceptance_reason: str
    review_reason: None
    final_layout_result: list[Region] = field(default_factory=list)
    gate_results: dict[str, Any] = field(default_factory=dict)


def _acceptance_reason(adjudication: LayoutAdjudicationResult) -> str:
    if adjudication.layout_decision_source == "local_agreement":
        return "layout accepted from local agreement"
    if adjudication.layout_decision_source == "google_document_ai":
        return "layout accepted from Google Document AI"
    if adjudication.layout_decision_source == "local_fallback_unverified":
        return "layout accepted from unverified local fallback after Google hard failure"
    return "layout accepted from legacy adjudication fallback"


def build_layout_routing_decision(
    adjudication: LayoutAdjudicationResult,
) -> LayoutRoutingDecision:
    """
    Translate adjudication output into the worker's no-review accepted path.

    The full adjudication payload is preserved in gate_results for lineage/UI
    consumers, while the worker transition is always ``layout_detection →
    accepted`` for IEP2.
    """
    return LayoutRoutingDecision(
        next_state="accepted",
        acceptance_decision="accepted",
        routing_path="layout_adjudication",
        acceptance_reason=_acceptance_reason(adjudication),
        review_reason=None,
        final_layout_result=list(adjudication.final_layout_result),
        gate_results={"layout_adjudication": adjudication.model_dump(mode="json")},
    )
