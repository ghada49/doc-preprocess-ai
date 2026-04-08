"""
services/eep/app/gates/layout_gate.py
--------------------------------------
EEP layout consensus gate (Packet 6.5) and layout adjudication gate (P3.2).

evaluate_layout_consensus — spec Section 7.4: greedy IoU-based one-to-one
matching between IEP2A and IEP2B canonical region lists, yielding a
LayoutConsensusResult that drives the downstream routing decision.

evaluate_layout_adjudication — P3.2: wraps local consensus as a fast path
and falls back to Google Document AI when local detectors disagree or are
unavailable. If Google hard-fails, the gate still returns a displayable local
fallback result. Returns a LayoutAdjudicationResult.

Algorithm summary (spec Section 7.4):
  1. Greedy one-to-one matching by descending IoU.
     A match requires IoU >= match_iou_threshold AND same canonical RegionType.
  2. match_ratio = matched / max(len(iep2a), len(iep2b))
  3. type_histogram_match: for every RegionType in either histogram,
     |count_a - count_b| <= max_type_count_diff
  4. agreed = match_ratio >= min_match_ratio AND type_histogram_match
  5. consensus_confidence = 0.6*match_ratio + 0.2*mean_iou + 0.2*histogram_flag
     where histogram_flag = 1.0 if type_histogram_match else 0.0
  6. Single-model fallback (iep2b_regions is None): agreed=False unconditionally.

IEP2 policy after this gate:
  local agreement                          → use IEP2A regions
  local disagreement / local detector miss → try Google Document AI
  Google technical success                 → use Google's result (including empty)
  Google hard failure                      → use best available local result
  review routing                           → never for IEP2

Exported:
    LayoutGateConfig              — policy thresholds (spec Section 8.4 defaults)
    evaluate_layout_consensus     — original local-only entry point
    evaluate_layout_adjudication  — new adjudication entry point (P3.2)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from services.eep.app.google.document_ai import _derive_empty_reason
from shared.metrics import GOOGLE_LAYOUT_ADJUDICATION_DECISIONS
from shared.schemas.layout import (
    LayoutAdjudicationResult,
    LayoutConsensusResult,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class LayoutGateConfig:
    """Policy thresholds for the layout consensus gate (spec Section 8.4)."""

    match_iou_threshold: float = 0.5
    min_match_ratio: float = 0.7
    max_type_count_diff: int = 1
    min_consensus_confidence: float = 0.6


# ---------------------------------------------------------------------------
# IoU utility
# ---------------------------------------------------------------------------


def _iou(a: BoundingBox, b: BoundingBox) -> float:
    """Intersection-over-Union of two axis-aligned bounding boxes."""
    ix_min = max(a.x_min, b.x_min)
    iy_min = max(a.y_min, b.y_min)
    ix_max = min(a.x_max, b.x_max)
    iy_max = min(a.y_max, b.y_max)

    if ix_min >= ix_max or iy_min >= iy_max:
        return 0.0

    inter = (ix_max - ix_min) * (iy_max - iy_min)
    area_a = (a.x_max - a.x_min) * (a.y_max - a.y_min)
    area_b = (b.x_max - b.x_min) * (b.y_max - b.y_min)
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


# ---------------------------------------------------------------------------
# Greedy one-to-one matching (spec Section 7.4)
# ---------------------------------------------------------------------------


def _greedy_match(
    iep2a: Sequence[Region],
    iep2b: Sequence[Region],
    iou_threshold: float,
) -> list[tuple[int, int, float]]:
    """
    Greedy one-to-one matching by descending IoU.

    A match requires IoU >= iou_threshold AND same canonical RegionType.
    Each index (from either list) appears in at most one matched pair.

    Returns a list of (iep2a_idx, iep2b_idx, iou) triples.
    """
    candidates: list[tuple[float, int, int]] = []
    for i, ra in enumerate(iep2a):
        for j, rb in enumerate(iep2b):
            if ra.type != rb.type:
                continue
            score = _iou(ra.bbox, rb.bbox)
            if score >= iou_threshold:
                candidates.append((score, i, j))

    # Process pairs in descending IoU order (greedy).
    candidates.sort(key=lambda t: t[0], reverse=True)

    matched_a: set[int] = set()
    matched_b: set[int] = set()
    matches: list[tuple[int, int, float]] = []

    for score, i, j in candidates:
        if i in matched_a or j in matched_b:
            continue
        matched_a.add(i)
        matched_b.add(j)
        matches.append((i, j, score))

    return matches


# ---------------------------------------------------------------------------
# type_histogram_match check
# ---------------------------------------------------------------------------


def _type_histogram_match(
    iep2a: Sequence[Region],
    iep2b: Sequence[Region],
    max_diff: int,
) -> bool:
    """
    True when, for every RegionType present in either list, the absolute
    per-type count difference is <= max_diff.
    """
    hist_a: dict[RegionType, int] = {}
    for r in iep2a:
        hist_a[r.type] = hist_a.get(r.type, 0) + 1

    hist_b: dict[RegionType, int] = {}
    for r in iep2b:
        hist_b[r.type] = hist_b.get(r.type, 0) + 1

    for rtype in set(hist_a) | set(hist_b):
        if abs(hist_a.get(rtype, 0) - hist_b.get(rtype, 0)) > max_diff:
            return False
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_layout_consensus(
    iep2a_regions: Sequence[Region],
    iep2b_regions: Sequence[Region] | None,
    config: LayoutGateConfig | None = None,
) -> LayoutConsensusResult:
    """
    Compare IEP2A and IEP2B canonical region lists and return a
    LayoutConsensusResult.

    Args:
        iep2a_regions:  Post-processed canonical regions from IEP2A (PaddleOCR PP-DocLayoutV2).
        iep2b_regions:  Post-processed canonical regions from IEP2B
                        (DocLayout-YOLO), or None when IEP2B is unavailable.
        config:         Gate policy thresholds; defaults to LayoutGateConfig().

    Returns:
        LayoutConsensusResult.  agreed is True only in dual-model mode when
        match_ratio >= min_match_ratio AND type_histogram_match.
        agreed is always False in single-model fallback.
    """
    cfg = config if config is not None else LayoutGateConfig()
    n_a = len(iep2a_regions)

    # ------------------------------------------------------------------
    # Single-model fallback: IEP2B unavailable.
    # spec Section 7.4: "agreed = False unconditionally; single-model
    # auto-acceptance is prohibited."
    # ------------------------------------------------------------------
    if iep2b_regions is None:
        return LayoutConsensusResult(
            iep2a_region_count=n_a,
            iep2b_region_count=0,
            matched_regions=0,
            unmatched_iep2a=n_a,
            unmatched_iep2b=0,
            mean_matched_iou=0.0,
            type_histogram_match=False,
            agreed=False,
            consensus_confidence=0.0,
            single_model_mode=True,
        )

    # ------------------------------------------------------------------
    # Dual-model mode.
    # ------------------------------------------------------------------
    n_b = len(iep2b_regions)
    total = max(n_a, n_b)

    matches = _greedy_match(iep2a_regions, iep2b_regions, cfg.match_iou_threshold)
    matched_regions = len(matches)

    match_ratio = matched_regions / total if total > 0 else 0.0
    mean_iou = sum(m[2] for m in matches) / matched_regions if matched_regions > 0 else 0.0

    hist_match = _type_histogram_match(iep2a_regions, iep2b_regions, cfg.max_type_count_diff)
    histogram_flag = 1.0 if hist_match else 0.0

    agreed = match_ratio >= cfg.min_match_ratio and hist_match

    consensus_confidence = max(
        0.0,
        min(
            1.0,
            0.6 * match_ratio + 0.2 * mean_iou + 0.2 * histogram_flag,
        ),
    )

    return LayoutConsensusResult(
        iep2a_region_count=n_a,
        iep2b_region_count=n_b,
        matched_regions=matched_regions,
        unmatched_iep2a=n_a - matched_regions,
        unmatched_iep2b=n_b - matched_regions,
        mean_matched_iou=round(mean_iou, 6),
        type_histogram_match=hist_match,
        agreed=agreed,
        consensus_confidence=round(consensus_confidence, 6),
        single_model_mode=False,
    )


# ---------------------------------------------------------------------------
# Layout adjudication gate (P3.2)
# ---------------------------------------------------------------------------


def _best_available_local_result(
    iep2a_result: LayoutDetectResponse | None,
    iep2b_result: LayoutDetectResponse | None,
) -> tuple[list[Region], str]:
    """
    Return the best available local layout result for no-review fallback.

    Preference order is product-mandated:
      1. IEP2A if it exists and has regions
      2. IEP2B if it exists and has regions
      3. empty list
    """
    if iep2a_result is not None and iep2a_result.regions:
        return list(iep2a_result.regions), "iep2a"
    if iep2b_result is not None and iep2b_result.regions:
        return list(iep2b_result.regions), "iep2b"
    return [], "none"


def _build_local_fallback_result(
    *,
    iep2a_result: LayoutDetectResponse | None,
    iep2b_result: LayoutDetectResponse | None,
    iep2a_region_count: int,
    iep2b_region_count: int | None,
    google_attempted: bool,
    google_error: str,
    google_response_time_ms: float | None,
    google_metadata: dict[str, Any] | None,
    t_start: float,
) -> LayoutAdjudicationResult:
    """Build the no-review local fallback result for Google hard failures."""
    GOOGLE_LAYOUT_ADJUDICATION_DECISIONS.labels(source="local_fallback_unverified").inc()
    final_layout_result, local_fallback_source = _best_available_local_result(
        iep2a_result, iep2b_result
    )
    audit: dict[str, Any] = {
        "attempted": google_attempted,
        "success": False,
        "hard_failure": True,
        "empty_result": False,
        "error": google_error,
        "local_fallback_source": local_fallback_source,
    }
    if google_metadata:
        audit.update(google_metadata)

    elapsed = (time.monotonic() - t_start) * 1000.0
    return LayoutAdjudicationResult(
        agreed=False,
        consensus_confidence=None,
        layout_decision_source="local_fallback_unverified",
        fallback_used=google_attempted,
        iep2a_region_count=iep2a_region_count,
        iep2b_region_count=iep2b_region_count,
        matched_regions=None,
        mean_matched_iou=None,
        type_histogram_match=None,
        iep2a_result=iep2a_result,
        iep2b_result=iep2b_result,
        google_document_ai_result=audit,
        final_layout_result=final_layout_result,
        status="done",
        error=None,
        processing_time_ms=round(elapsed, 2),
        google_response_time_ms=(
            round(google_response_time_ms, 2) if google_response_time_ms is not None else None
        ),
    )


async def evaluate_layout_adjudication(
    iep2a_result: LayoutDetectResponse | None,
    iep2b_result: LayoutDetectResponse | None,
    google_client: Any | None,
    image_bytes: bytes | None,
    mime_type: str,
    material_type: str,
    image_uri: str,
    config: LayoutGateConfig | None = None,
) -> LayoutAdjudicationResult:
    """
    Run the full layout adjudication gate (P3.2 / spec Section 7.4–7.5).

    Decision tree:
      1. Both IEP2A and IEP2B succeeded → run local consensus.
         a. agreed=True → fast path: return local_agreement result.
         b. agreed=False → fall through to Google.
      2. Either or both IEP2 detectors unavailable / failed → fall through
         to Google (single-model or dual-failed path).
      3. Google fallback:
         a. Google technically succeeds → google_document_ai result
            (including a valid empty layout).
         b. Google hard-fails (no client, timeout, auth/API error, no response)
            → return local_fallback_unverified using the best available local result.

    Args:
        iep2a_result   — LayoutDetectResponse from IEP2A, or None if it failed.
        iep2b_result   — LayoutDetectResponse from IEP2B, or None if it failed.
        google_client  — initialized CallGoogleDocumentAI instance, or None.
        image_bytes    — raw page image bytes passed to Google (if available).
        mime_type      — MIME type of image_bytes (e.g. "image/tiff").
        material_type  — document material type hint ("book" / "newspaper" / …).
        image_uri      — URI of the page image (used as fallback / for logging).
        config         — gate policy thresholds; defaults to LayoutGateConfig().

    Returns:
        LayoutAdjudicationResult capturing the decision and all audit fields.
    """
    cfg = config if config is not None else LayoutGateConfig()
    t_start = time.monotonic()

    iep2a_region_count = len(iep2a_result.regions) if iep2a_result is not None else 0
    iep2b_region_count = len(iep2b_result.regions) if iep2b_result is not None else None

    # ── Path 1: Local consensus (both detectors available) ─────────────────
    if iep2a_result is not None and iep2b_result is not None:
        consensus = evaluate_layout_consensus(iep2a_result.regions, iep2b_result.regions, cfg)
        if consensus.agreed:
            elapsed = (time.monotonic() - t_start) * 1000.0
            logger.info(
                "layout_adjudication: local agreement — "
                "matched=%d iou=%.3f conf=%.3f image_uri=%s",
                consensus.matched_regions,
                consensus.mean_matched_iou,
                consensus.consensus_confidence,
                image_uri,
            )
            GOOGLE_LAYOUT_ADJUDICATION_DECISIONS.labels(source="local_agreement").inc()
            return LayoutAdjudicationResult(
                agreed=True,
                consensus_confidence=consensus.consensus_confidence,
                layout_decision_source="local_agreement",
                fallback_used=False,
                iep2a_region_count=iep2a_region_count,
                iep2b_region_count=iep2b_region_count,
                matched_regions=consensus.matched_regions,
                mean_matched_iou=consensus.mean_matched_iou,
                type_histogram_match=consensus.type_histogram_match,
                iep2a_result=iep2a_result,
                iep2b_result=iep2b_result,
                google_document_ai_result=None,
                final_layout_result=list(iep2a_result.regions),
                status="done",
                error=None,
                processing_time_ms=round(elapsed, 2),
                google_response_time_ms=None,
            )
        # Local detectors disagreed — fall through to Google.
        logger.info(
            "layout_adjudication: local disagreement (conf=%.3f) — "
            "falling back to Google, image_uri=%s",
            consensus.consensus_confidence,
            image_uri,
        )
    else:
        # One or both detectors unavailable.
        logger.info(
            "layout_adjudication: IEP2A=%s IEP2B=%s — falling back to Google, image_uri=%s",
            "ok" if iep2a_result is not None else "None",
            "ok" if iep2b_result is not None else "None",
            image_uri,
        )

    # ── Path 2: Google fallback ─────────────────────────────────────────────
    if google_client is None:
        logger.warning(
            "layout_adjudication: Google client not available — using local fallback, image_uri=%s",
            image_uri,
        )
        GOOGLE_LAYOUT_ADJUDICATION_DECISIONS.labels(source="google_skipped").inc()
        return _build_local_fallback_result(
            iep2a_result=iep2a_result,
            iep2b_result=iep2b_result,
            iep2a_region_count=iep2a_region_count,
            iep2b_region_count=iep2b_region_count,
            google_attempted=False,
            google_error="Google Document AI not available",
            google_response_time_ms=None,
            google_metadata={"google_available": False},
            t_start=t_start,
        )

    # Call Google Document AI.
    t_google_start = time.monotonic()
    try:
        google_response = await google_client.process_layout(
            image_uri=image_uri,
            material_type=material_type,
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
    except Exception as exc:  # noqa: BLE001
        google_elapsed = (time.monotonic() - t_google_start) * 1000.0
        logger.warning(
            "layout_adjudication: Google hard failure — using local fallback, "
            "error=%s image_uri=%s",
            exc,
            image_uri,
        )
        return _build_local_fallback_result(
            iep2a_result=iep2a_result,
            iep2b_result=iep2b_result,
            iep2a_region_count=iep2a_region_count,
            iep2b_region_count=iep2b_region_count,
            google_attempted=True,
            google_error=str(exc),
            google_response_time_ms=google_elapsed,
            google_metadata={"google_available": True},
            t_start=t_start,
        )
    google_elapsed = (time.monotonic() - t_google_start) * 1000.0

    if not google_response:
        logger.warning(
            "layout_adjudication: Google returned no response — using local fallback, image_uri=%s",
            image_uri,
        )
        return _build_local_fallback_result(
            iep2a_result=iep2a_result,
            iep2b_result=iep2b_result,
            iep2a_region_count=iep2a_region_count,
            iep2b_region_count=iep2b_region_count,
            google_attempted=True,
            google_error="Google Document AI returned no response",
            google_response_time_ms=google_elapsed,
            google_metadata={"google_available": True},
            t_start=t_start,
        )

    # Map Google response to canonical regions.
    elements = google_response.get("elements", [])
    page_width = google_response.get("page_width", 1)
    page_height = google_response.get("page_height", 1)
    try:
        canonical_regions: list[Region] = google_client._map_google_to_canonical(
            elements, page_width, page_height
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "layout_adjudication: Google canonical mapping failed — using local fallback, "
            "error=%s image_uri=%s",
            exc,
            image_uri,
        )
        return _build_local_fallback_result(
            iep2a_result=iep2a_result,
            iep2b_result=iep2b_result,
            iep2a_region_count=iep2a_region_count,
            iep2b_region_count=iep2b_region_count,
            google_attempted=True,
            google_error=f"Google canonical mapping failed: {exc}",
            google_response_time_ms=google_elapsed,
            google_metadata={
                "google_available": True,
                "page_width": page_width,
                "page_height": page_height,
                "region_count": google_response.get("region_count", len(elements)),
            },
            t_start=t_start,
        )

    # Store a compact audit summary (raw_response is not JSON-serialisable).
    google_audit: dict[str, Any] = {
        "attempted": True,
        "success": True,
        "hard_failure": False,
        "empty_result": len(canonical_regions) == 0,
        "region_count": google_response.get("region_count", len(elements)),
        "page_width": page_width,
        "page_height": page_height,
        "document_layout_block_count": int(google_response.get("document_layout_block_count", 0)),
        "pages_count": int(google_response.get("pages_count", 0)),
        "text_length": int(google_response.get("text_length", 0)),
        "document_layout_blocks_have_geometry": bool(
            google_response.get("document_layout_blocks_have_geometry", False)
        ),
        "empty_reason": _derive_empty_reason(
            canonical_region_count=len(canonical_regions),
            document_layout_block_count=int(
                google_response.get("document_layout_block_count", 0)
            ),
            pages_count=int(google_response.get("pages_count", 0)),
            text_length=int(google_response.get("text_length", 0)),
            document_layout_blocks_have_geometry=bool(
                google_response.get("document_layout_blocks_have_geometry", False)
            ),
        ),
    }

    elapsed = (time.monotonic() - t_start) * 1000.0

    if not canonical_regions:
        logger.info(
            "layout_adjudication: Google returned 0 canonical regions — "
            "using empty Google result, image_uri=%s",
            image_uri,
        )
        GOOGLE_LAYOUT_ADJUDICATION_DECISIONS.labels(source="google_document_ai").inc()
        return LayoutAdjudicationResult(
            agreed=False,
            consensus_confidence=None,
            layout_decision_source="google_document_ai",
            fallback_used=False,  # Google was consulted and responded — not a fallback
            iep2a_region_count=iep2a_region_count,
            iep2b_region_count=iep2b_region_count,
            matched_regions=None,
            mean_matched_iou=None,
            type_histogram_match=None,
            iep2a_result=iep2a_result,
            iep2b_result=iep2b_result,
            google_document_ai_result=google_audit,
            final_layout_result=[],
            status="done",
            error=None,
            processing_time_ms=round(elapsed, 2),
            google_response_time_ms=round(google_elapsed, 2),
        )

    logger.info(
        "layout_adjudication: Google success — %d regions, google_ms=%.0f, image_uri=%s",
        len(canonical_regions),
        google_elapsed,
        image_uri,
    )
    GOOGLE_LAYOUT_ADJUDICATION_DECISIONS.labels(source="google_document_ai").inc()
    return LayoutAdjudicationResult(
        agreed=False,
        consensus_confidence=None,
        layout_decision_source="google_document_ai",
        fallback_used=False,  # Google was consulted and succeeded — not a fallback
        iep2a_region_count=iep2a_region_count,
        iep2b_region_count=iep2b_region_count,
        matched_regions=None,
        mean_matched_iou=None,
        type_histogram_match=None,
        iep2a_result=iep2a_result,
        iep2b_result=iep2b_result,
        google_document_ai_result=google_audit,
        final_layout_result=canonical_regions,
        status="done",
        error=None,
        processing_time_ms=round(elapsed, 2),
        google_response_time_ms=round(google_elapsed, 2),
    )
