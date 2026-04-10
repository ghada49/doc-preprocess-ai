"""
tests/test_p6_layout_integration.py
-------------------------------------
Packet 6.6 — Phase 6 layout integration tests.

Tests the IEP2A service, IEP2B service, the EEP layout consensus/adjudication
gate, and the worker-facing no-review routing helper, plus PTIFF QA state
machine enforcement.

Coverage:
  1. Dual-model agreement: real IEP2A + IEP2B mock HTTP responses fed to
     evaluate_layout_consensus produce agreed=True with confidence >= threshold.
  2. IEP2A regions selected as canonical output when consensus agrees
     (spec Section 7.4: "When agreed: use IEP2A regions as canonical layout").
  3. Disagreement by low match ratio → agreed=False → Google fallback required.
  4. Disagreement by histogram mismatch → agreed=False → Google fallback required.
  5. Single-model fallback: IEP2B unavailable (None or HTTP 500) →
     single_model_mode=True, agreed=False unconditionally → adjudication required.
  6. Layout routing enforcement: state machine permits preprocessing/rectification/
     pending_human_correction → layout_detection (automation-first model).
  7. End-to-end layout routing decisions:
     - local agreement → accepted
     - local disagreement + Google success → accepted using Google result
     - Google hard failure / unavailable → accepted using best local fallback
     - Google empty result → accepted with an empty displayed layout

Implementation note:
  The layout_detection task runner is still not fully implemented in Phase 6.
  These tests call the IEP2A/IEP2B TestClients directly, feed their outputs into
  evaluate_layout_consensus / evaluate_layout_adjudication, and then run the
  worker-facing build_layout_routing_decision() helper to verify the no-review
  accepted path.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.eep.app.gates.layout_gate import (
    LayoutGateConfig,
    evaluate_layout_adjudication,
    evaluate_layout_consensus,
)
from services.eep_worker.app.layout_routing import build_layout_routing_decision
from services.iep2a.app.detect import router as iep2a_router
from services.iep2b.app.detect import router as iep2b_router
from shared.schemas.layout import (
    LayoutAdjudicationResult,
    LayoutConfSummary,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox
from shared.state_machine import ALLOWED_TRANSITIONS, InvalidTransitionError, validate_transition

# ---------------------------------------------------------------------------
# Inline routing helpers — test scope only
# ---------------------------------------------------------------------------
# IEP2 policy:
#   adjudication always produces a displayable layout result
#   worker routing is always layout_detection → accepted


def _layout_route(
    adjudication: LayoutAdjudicationResult,
) -> tuple[str, str | None]:
    """
    Return the worker routing state for an adjudicated IEP2 result.
    """
    decision = build_layout_routing_decision(adjudication)
    return decision.next_state, decision.review_reason


# ---------------------------------------------------------------------------
# Minimal test apps (no prometheus middleware)
# ---------------------------------------------------------------------------

_iep2a_app = FastAPI()
_iep2a_app.include_router(iep2a_router)

_iep2b_app = FastAPI()
_iep2b_app.include_router(iep2b_router)

_VALID_PAYLOAD: dict[str, object] = {
    "job_id": "job-integration-p6-001",
    "page_number": 5,
    "image_uri": "s3://bucket/artifacts/page5.tiff",
    "material_type": "archival_document",
}


@pytest.fixture(scope="module")
def iep2a_client() -> TestClient:
    return TestClient(_iep2a_app)


@pytest.fixture(scope="module")
def iep2b_client() -> TestClient:
    return TestClient(_iep2b_app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_regions(response_json: dict[str, object]) -> list[Region]:
    """Deserialise Region objects from a LayoutDetectResponse JSON payload."""
    return [Region(**r) for r in cast(list[Any], response_json["regions"])]


def _make_region(
    rid: str,
    rtype: RegionType,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
    confidence: float = 0.8,
) -> Region:
    return Region(
        id=rid,
        type=rtype,
        bbox=BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max),
        confidence=confidence,
    )


def _conf_summary() -> LayoutConfSummary:
    return LayoutConfSummary(mean_conf=0.85, low_conf_frac=0.1)


def _detect_response(
    regions: list[Region],
    detector_type: str = "paddleocr_pp_doclayout_v2",
) -> LayoutDetectResponse:
    histogram: dict[str, int] = {}
    for region in regions:
        histogram[region.type.value] = histogram.get(region.type.value, 0) + 1
    return LayoutDetectResponse(
        region_schema_version="v1",
        regions=regions,
        layout_conf_summary=_conf_summary(),
        region_type_histogram=histogram,
        column_structure=None,
        model_version="test-v1",
        detector_type=detector_type,  # type: ignore[arg-type]
        processing_time_ms=100.0,
        warnings=[],
    )


def _mock_google_client(regions: list[Region] | None = None) -> MagicMock:
    if regions is None:
        regions = [_make_region("r1", RegionType.text_block, 0, 0, 100, 100)]

    client = MagicMock()
    client.process_layout = AsyncMock(
        return_value={
            "elements": [object() for _ in regions],
            "page_width": 1000,
            "page_height": 1200,
            "region_count": len(regions),
        }
    )
    client._map_google_to_canonical = MagicMock(return_value=regions)
    return client


def _mock_google_timeout() -> MagicMock:
    client = MagicMock()
    client.process_layout = AsyncMock(side_effect=TimeoutError("google layout timeout"))
    return client


def _run_adjudication(
    iep2a_result: LayoutDetectResponse | None,
    iep2b_result: LayoutDetectResponse | None,
    google_client: Any | None,
) -> LayoutAdjudicationResult:
    return asyncio.run(
        evaluate_layout_adjudication(
            iep2a_result=iep2a_result,
            iep2b_result=iep2b_result,
            google_client=google_client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="archival_document",
            image_uri=cast(str, _VALID_PAYLOAD["image_uri"]),
        )
    )


# ---------------------------------------------------------------------------
# 1. Dual-model agreement: HTTP mock responses → consensus → agreed=True
# ---------------------------------------------------------------------------


class TestDualModelAgreementIntegration:
    """
    The IEP2A and IEP2B mock templates are intentionally placed at similar
    but distinct coordinates (confirmed in Packets 6.1/6.3).  After each
    service's postprocessing pipeline, corresponding regions still have
    IoU > 0.85 and matching RegionTypes, so evaluate_layout_consensus
    produces agreed=True.
    """

    def test_iep2a_returns_200(self, iep2a_client: TestClient) -> None:
        assert iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).status_code == 200

    def test_iep2b_returns_200(self, iep2b_client: TestClient) -> None:
        assert iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).status_code == 200

    def test_iep2a_regions_are_canonical(self, iep2a_client: TestClient) -> None:
        resp = iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        canonical = {rt.value for rt in RegionType}
        for r in resp["regions"]:
            assert r["type"] in canonical

    def test_iep2b_regions_are_canonical(self, iep2b_client: TestClient) -> None:
        resp = iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        canonical = {rt.value for rt in RegionType}
        for r in resp["regions"]:
            assert r["type"] in canonical

    def test_consensus_agreed_on_default_mocks(
        self, iep2a_client: TestClient, iep2b_client: TestClient
    ) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2b_regions = _get_regions(
            iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        result = evaluate_layout_consensus(iep2a_regions, iep2b_regions)
        assert result.agreed is True

    def test_consensus_single_model_mode_false(
        self, iep2a_client: TestClient, iep2b_client: TestClient
    ) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2b_regions = _get_regions(
            iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        result = evaluate_layout_consensus(iep2a_regions, iep2b_regions)
        assert result.single_model_mode is False

    def test_consensus_confidence_above_threshold(
        self, iep2a_client: TestClient, iep2b_client: TestClient
    ) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2b_regions = _get_regions(
            iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        result = evaluate_layout_consensus(iep2a_regions, iep2b_regions)
        assert result.consensus_confidence >= LayoutGateConfig().min_consensus_confidence

    def test_type_histogram_matches(
        self, iep2a_client: TestClient, iep2b_client: TestClient
    ) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2b_regions = _get_regions(
            iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        result = evaluate_layout_consensus(iep2a_regions, iep2b_regions)
        assert result.type_histogram_match is True

    def test_unmatched_counts_zero_on_perfect_match(
        self, iep2a_client: TestClient, iep2b_client: TestClient
    ) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2b_regions = _get_regions(
            iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        result = evaluate_layout_consensus(iep2a_regions, iep2b_regions)
        assert result.unmatched_iep2a == 0
        assert result.unmatched_iep2b == 0


# ---------------------------------------------------------------------------
# 2. IEP2A regions selected as canonical output when consensus agrees
# ---------------------------------------------------------------------------


class TestIep2aCanonicalOnAgreement:
    """
    Spec Section 7.4: "When agreed: use IEP2A (Detectron2) regions as
    canonical layout."  IEP2B regions are for cross-validation only.
    """

    def test_agreed_precondition(self, iep2a_client: TestClient, iep2b_client: TestClient) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2b_regions = _get_regions(
            iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        result = evaluate_layout_consensus(iep2a_regions, iep2b_regions)
        assert result.agreed is True, "Precondition: default mocks must agree"

    def test_canonical_region_count_equals_iep2a(
        self, iep2a_client: TestClient, iep2b_client: TestClient
    ) -> None:
        iep2a_resp = iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        iep2b_regions = _get_regions(
            iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2a_regions = _get_regions(iep2a_resp)
        result = evaluate_layout_consensus(iep2a_regions, iep2b_regions)
        # Canonical output is always IEP2A regions when agreed=True.
        canonical_regions = iep2a_regions
        assert len(canonical_regions) == result.iep2a_region_count

    def test_iep2a_and_iep2b_bboxes_differ(
        self, iep2a_client: TestClient, iep2b_client: TestClient
    ) -> None:
        """
        Confirm the mock templates have distinct bbox origins so that
        'use IEP2A regions' is a meaningful assertion (not vacuous).
        """
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2b_regions = _get_regions(
            iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2a_origins = [(r.bbox.x_min, r.bbox.y_min) for r in iep2a_regions]
        iep2b_origins = [(r.bbox.x_min, r.bbox.y_min) for r in iep2b_regions]
        assert iep2a_origins != iep2b_origins, (
            "IEP2A and IEP2B mock templates must have distinct coordinates "
            "for the canonical-selection assertion to be meaningful"
        )

    def test_detector_type_iep2a_is_detectron2(self, iep2a_client: TestClient) -> None:
        resp = iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert resp["detector_type"] == "detectron2"

    def test_detector_type_iep2b_is_doclayout_yolo(self, iep2b_client: TestClient) -> None:
        resp = iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        assert resp["detector_type"] == "doclayout_yolo"


# ---------------------------------------------------------------------------
# 3. Disagreement by low match ratio
# ---------------------------------------------------------------------------


class TestDisagreementByLowMatchRatio:
    """
    IEP2A and IEP2B produce same RegionTypes but at non-overlapping positions
    (IoU = 0 for all pairs) → match_ratio = 0 < 0.7 → agreed=False.
    """

    _IEP2A: list[Region] = [
        _make_region("r1", RegionType.title, 0, 0, 100, 50),
        _make_region("r2", RegionType.text_block, 0, 60, 100, 200),
        _make_region("r3", RegionType.text_block, 0, 210, 100, 400),
        _make_region("r4", RegionType.image, 0, 410, 100, 550),
        _make_region("r5", RegionType.caption, 0, 560, 100, 600),
        _make_region("r6", RegionType.table, 0, 610, 100, 800),
    ]
    _IEP2B: list[Region] = [
        _make_region("r1", RegionType.title, 500, 0, 600, 50),
        _make_region("r2", RegionType.text_block, 500, 60, 600, 200),
        _make_region("r3", RegionType.text_block, 500, 210, 600, 400),
        _make_region("r4", RegionType.image, 500, 410, 600, 550),
        _make_region("r5", RegionType.caption, 500, 560, 600, 600),
        _make_region("r6", RegionType.table, 500, 610, 600, 800),
    ]

    def test_zero_matches(self) -> None:
        result = evaluate_layout_consensus(self._IEP2A, self._IEP2B)
        assert result.matched_regions == 0

    def test_agreed_false(self) -> None:
        result = evaluate_layout_consensus(self._IEP2A, self._IEP2B)
        assert result.agreed is False

    def test_single_model_mode_false(self) -> None:
        result = evaluate_layout_consensus(self._IEP2A, self._IEP2B)
        assert result.single_model_mode is False

    def test_disagreement_routes_through_google_and_stays_accepted(self) -> None:
        google_regions = [_make_region("r1", RegionType.text_block, 10, 10, 80, 80)]
        adjudication = _run_adjudication(
            _detect_response(self._IEP2A),
            _detect_response(self._IEP2B, detector_type="doclayout_yolo"),
            _mock_google_client(google_regions),
        )
        state, reason = _layout_route(adjudication)
        assert state == "accepted"
        assert reason is None
        assert adjudication.layout_decision_source == "google_document_ai"
        assert adjudication.final_layout_result == google_regions

    def test_iep2a_region_count_correct(self) -> None:
        result = evaluate_layout_consensus(self._IEP2A, self._IEP2B)
        assert result.iep2a_region_count == len(self._IEP2A)
        assert result.iep2b_region_count == len(self._IEP2B)

    def test_all_iep2a_unmatched(self) -> None:
        result = evaluate_layout_consensus(self._IEP2A, self._IEP2B)
        assert result.unmatched_iep2a == len(self._IEP2A)
        assert result.unmatched_iep2b == len(self._IEP2B)


# ---------------------------------------------------------------------------
# 4. Disagreement by histogram mismatch
# ---------------------------------------------------------------------------


class TestDisagreementByHistogramMismatch:
    """
    Regions overlap well (high IoU, same type), but IEP2B has 3 extra
    text_block regions.  Per-type count diff = 3 > max_type_count_diff (1)
    → type_histogram_match=False → agreed=False.
    """

    _IEP2A: list[Region] = [
        _make_region("r1", RegionType.title, 50, 20, 950, 110),
        _make_region("r2", RegionType.text_block, 50, 130, 450, 600),
        _make_region("r3", RegionType.text_block, 500, 130, 950, 600),
    ]
    # IEP2B adds 3 extra text_blocks (same bboxes as existing ones → high IoU).
    _IEP2B: list[Region] = [
        _make_region("r1", RegionType.title, 50, 20, 950, 110),
        _make_region("r2", RegionType.text_block, 50, 130, 450, 600),
        _make_region("r3", RegionType.text_block, 500, 130, 950, 600),
        _make_region("r4", RegionType.text_block, 50, 610, 450, 700),
        _make_region("r5", RegionType.text_block, 500, 610, 950, 700),
        _make_region("r6", RegionType.text_block, 50, 710, 450, 800),
    ]

    def test_histogram_match_false(self) -> None:
        result = evaluate_layout_consensus(self._IEP2A, self._IEP2B)
        assert result.type_histogram_match is False

    def test_agreed_false(self) -> None:
        result = evaluate_layout_consensus(self._IEP2A, self._IEP2B)
        assert result.agreed is False

    def test_histogram_mismatch_routes_through_google_and_stays_accepted(self) -> None:
        google_regions = [_make_region("r1", RegionType.table, 20, 20, 120, 120)]
        adjudication = _run_adjudication(
            _detect_response(self._IEP2A),
            _detect_response(self._IEP2B, detector_type="doclayout_yolo"),
            _mock_google_client(google_regions),
        )
        state, reason = _layout_route(adjudication)
        assert state == "accepted"
        assert reason is None
        assert adjudication.layout_decision_source == "google_document_ai"

    def test_single_model_mode_false(self) -> None:
        result = evaluate_layout_consensus(self._IEP2A, self._IEP2B)
        assert result.single_model_mode is False

    def test_some_regions_still_matched(self) -> None:
        # The 3 shared regions (r1 title, r2/r3 text_block) match by IoU.
        result = evaluate_layout_consensus(self._IEP2A, self._IEP2B)
        assert result.matched_regions == len(self._IEP2A)  # all IEP2A regions matched


# ---------------------------------------------------------------------------
# 5. Single-model fallback
# ---------------------------------------------------------------------------


class TestSingleModelFallbackIntegration:
    def test_single_model_mode_true_when_iep2b_none(self, iep2a_client: TestClient) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        result = evaluate_layout_consensus(iep2a_regions, None)
        assert result.single_model_mode is True

    def test_agreed_false_in_single_model(self, iep2a_client: TestClient) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        result = evaluate_layout_consensus(iep2a_regions, None)
        assert result.agreed is False

    def test_single_model_unavailable_google_routes_to_local_fallback_acceptance(
        self, iep2a_client: TestClient
    ) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        adjudication = _run_adjudication(
            _detect_response(iep2a_regions),
            None,
            None,
        )
        state, reason = _layout_route(adjudication)
        assert state == "accepted"
        assert reason is None
        assert adjudication.layout_decision_source == "local_fallback_unverified"
        assert adjudication.final_layout_result == iep2a_regions

    def test_iep2b_region_count_zero_in_single_model(self, iep2a_client: TestClient) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        result = evaluate_layout_consensus(iep2a_regions, None)
        assert result.iep2b_region_count == 0
        assert result.matched_regions == 0

    def test_iep2b_http_500_triggers_single_model_path(
        self,
        iep2a_client: TestClient,
        iep2b_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        When IEP2B returns HTTP 500, the worker treats it as unavailable and
        passes iep2b_regions=None to evaluate_layout_consensus.
        """
        monkeypatch.setenv("IEP2B_MOCK_FAIL", "true")
        iep2b_resp = iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        assert iep2b_resp.status_code == 500  # confirmed unavailable

        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        # Worker passes None for iep2b on HTTP failure and still produces a
        # displayable accepted result via local fallback if Google is absent.
        adjudication = _run_adjudication(_detect_response(iep2a_regions), None, None)
        assert adjudication.layout_decision_source == "local_fallback_unverified"
        state, reason = _layout_route(adjudication)
        assert state == "accepted"
        assert reason is None


# ---------------------------------------------------------------------------
# 6. PTIFF QA enforcement: state machine transitions
# ---------------------------------------------------------------------------


class TestLayoutRoutingEnforcement:
    """
    Automation-first routing: preprocessing/rectification route directly to
    layout_detection without any intermediate gate.  pending_human_correction
    resumes via layout_detection after correction.
    """

    # --- Allowed transitions INTO layout_detection (automation-first) ---

    def test_preprocessing_can_reach_layout_detection(self) -> None:
        assert "layout_detection" in ALLOWED_TRANSITIONS["preprocessing"]

    def test_rectification_can_reach_layout_detection(self) -> None:
        assert "layout_detection" in ALLOWED_TRANSITIONS["rectification"]

    def test_pending_human_correction_can_reach_layout_detection(self) -> None:
        assert "layout_detection" in ALLOWED_TRANSITIONS["pending_human_correction"]

    def test_validate_transition_preprocessing_to_layout_does_not_raise(self) -> None:
        validate_transition("preprocessing", "layout_detection")  # must not raise

    def test_validate_transition_rectification_to_layout_does_not_raise(self) -> None:
        validate_transition("rectification", "layout_detection")  # must not raise

    def test_validate_transition_pending_correction_to_layout_does_not_raise(self) -> None:
        validate_transition("pending_human_correction", "layout_detection")  # must not raise

    # --- Disallowed direct transitions to layout_detection ---

    def test_queued_cannot_reach_layout_detection(self) -> None:
        assert "layout_detection" not in ALLOWED_TRANSITIONS["queued"]

    def test_validate_transition_rejects_queued_to_layout(self) -> None:
        with pytest.raises(InvalidTransitionError):
            validate_transition("queued", "layout_detection")

    # --- Allowed transitions OUT of layout_detection ---

    def test_layout_detection_to_accepted_allowed(self) -> None:
        assert "accepted" in ALLOWED_TRANSITIONS["layout_detection"]

    def test_layout_detection_to_review_allowed(self) -> None:
        assert "review" in ALLOWED_TRANSITIONS["layout_detection"]

    def test_layout_detection_to_failed_allowed(self) -> None:
        assert "failed" in ALLOWED_TRANSITIONS["layout_detection"]

    def test_layout_detection_to_pending_human_correction_allowed(self) -> None:
        # user can send page to review after layout detection
        assert "pending_human_correction" in ALLOWED_TRANSITIONS["layout_detection"]

    def test_layout_detection_cannot_return_to_queued(self) -> None:
        assert "queued" not in ALLOWED_TRANSITIONS["layout_detection"]

    def test_layout_detection_is_not_terminal(self) -> None:
        from shared.state_machine import is_worker_terminal

        assert is_worker_terminal("layout_detection") is False


# ---------------------------------------------------------------------------
# 7. End-to-end layout routing decisions
# ---------------------------------------------------------------------------


class TestLayoutRoutingDecisions:
    def test_accepted_path_on_dual_model_agreement(
        self, iep2a_client: TestClient, iep2b_client: TestClient
    ) -> None:
        iep2a_result = LayoutDetectResponse.model_validate(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2b_result = LayoutDetectResponse.model_validate(
            iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        adjudication = _run_adjudication(iep2a_result, iep2b_result, None)
        state, reason = _layout_route(adjudication)
        assert state == "accepted"
        assert reason is None
        assert adjudication.layout_decision_source == "local_agreement"
        assert adjudication.final_layout_result == iep2a_result.regions

    def test_local_disagreement_uses_google_result_and_stays_accepted(self) -> None:
        # Non-overlapping regions → match_ratio=0 → agreed=False.
        iep2a = [
            _make_region("r1", RegionType.title, 0, 0, 100, 50),
            _make_region("r2", RegionType.text_block, 0, 60, 100, 200),
            _make_region("r3", RegionType.table, 0, 210, 100, 400),
        ]
        iep2b = [
            _make_region("r1", RegionType.title, 500, 0, 600, 50),
            _make_region("r2", RegionType.text_block, 500, 60, 600, 200),
            _make_region("r3", RegionType.table, 500, 210, 600, 400),
        ]
        google_regions = [_make_region("r1", RegionType.image, 10, 10, 80, 80)]
        adjudication = _run_adjudication(
            _detect_response(iep2a),
            _detect_response(iep2b, detector_type="doclayout_yolo"),
            _mock_google_client(google_regions),
        )
        state, reason = _layout_route(adjudication)
        assert state == "accepted"
        assert reason is None
        assert adjudication.layout_decision_source == "google_document_ai"
        assert adjudication.final_layout_result == google_regions

    def test_google_timeout_uses_local_fallback_and_stays_accepted(self) -> None:
        iep2a = [_make_region("r1", RegionType.title, 0, 0, 100, 50)]
        iep2b = [_make_region("r1", RegionType.title, 500, 0, 600, 50)]
        adjudication = _run_adjudication(
            _detect_response(iep2a),
            _detect_response(iep2b, detector_type="doclayout_yolo"),
            _mock_google_timeout(),
        )
        state, reason = _layout_route(adjudication)
        assert state == "accepted"
        assert reason is None
        assert adjudication.layout_decision_source == "local_fallback_unverified"
        assert adjudication.final_layout_result == iep2a

    def test_iep2a_http_500_still_allows_empty_fallback_acceptance(
        self,
        iep2a_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        IEP2A HTTP 500 means the worker cannot obtain IEP2A regions and must
        skip local consensus. Under the current IEP2 policy, adjudication still
        returns an accepted empty display fallback if no other result exists.
        """
        monkeypatch.setenv("IEP2A_MOCK_FAIL", "true")
        resp = iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        assert resp.status_code == 500
        detail = resp.json().get("detail", {})
        assert "error_code" in detail
        adjudication = _run_adjudication(None, None, None)
        state, reason = _layout_route(adjudication)
        assert state == "accepted"
        assert reason is None
        assert adjudication.final_layout_result == []

    def test_single_model_routes_to_local_fallback_acceptance(
        self, iep2a_client: TestClient
    ) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        adjudication = _run_adjudication(_detect_response(iep2a_regions), None, None)
        state, reason = _layout_route(adjudication)
        assert state == "accepted"
        assert reason is None
        assert adjudication.layout_decision_source == "local_fallback_unverified"
        assert adjudication.final_layout_result == iep2a_regions

    def test_google_empty_result_still_routes_to_accepted(self) -> None:
        iep2a_regions = [_make_region("r1", RegionType.title, 0, 0, 100, 50)]
        iep2b_regions = [_make_region("r1", RegionType.title, 500, 0, 600, 50)]
        adjudication = _run_adjudication(
            _detect_response(iep2a_regions),
            _detect_response(iep2b_regions, detector_type="doclayout_yolo"),
            _mock_google_client([]),
        )
        state, reason = _layout_route(adjudication)
        assert state == "accepted"
        assert reason is None
        assert adjudication.layout_decision_source == "google_document_ai"
        assert adjudication.final_layout_result == []

    def test_review_reason_is_always_none_for_iep2(
        self, iep2a_client: TestClient, iep2b_client: TestClient
    ) -> None:
        iep2a_regions = _get_regions(
            iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        iep2b_regions = _get_regions(
            iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        )
        adjudication = _run_adjudication(
            _detect_response(iep2a_regions),
            _detect_response(iep2b_regions, detector_type="doclayout_yolo"),
            None,
        )
        state, reason = _layout_route(adjudication)
        assert isinstance(state, str)
        assert reason is None


# ---------------------------------------------------------------------------
# 8. Model readiness — is_model_ready() contract
# ---------------------------------------------------------------------------


class TestModelReadiness:
    """
    /ready endpoint semantics: is_model_ready() must return True in stub mode
    (default) and respect the MOCK_NOT_READY flags.  In real mode with no ML
    deps loaded, it must return False.
    """

    def test_iep2a_ready_by_default_in_stub_mode(self) -> None:
        from services.iep2a.app.detect import is_model_ready as iep2a_ready

        assert iep2a_ready() is True

    def test_iep2a_not_ready_when_mock_not_ready_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP2A_MOCK_NOT_READY", "true")
        from services.iep2a.app.detect import is_model_ready as iep2a_ready

        assert iep2a_ready() is False

    def test_iep2b_ready_by_default_in_stub_mode(self) -> None:
        from services.iep2b.app.detect import is_model_ready as iep2b_ready

        assert iep2b_ready() is True

    def test_iep2b_not_ready_when_mock_not_ready_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP2B_MOCK_NOT_READY", "true")
        from services.iep2b.app.detect import is_model_ready as iep2b_ready

        assert iep2b_ready() is False

    def test_iep2a_ready_reflects_real_mode_not_loaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        In real mode with no model loaded, is_model_ready() must return False.
        """
        from services.iep2a.app.model import reset_for_testing

        reset_for_testing()
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "true")
        from services.iep2a.app.detect import is_model_ready as iep2a_ready

        assert iep2a_ready() is False

    def test_iep2b_ready_reflects_real_mode_not_loaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        In real mode with no model loaded, is_model_ready() must return False.
        """
        from services.iep2b.app.model import reset_for_testing

        reset_for_testing()
        monkeypatch.setenv("IEP2B_USE_REAL_MODEL", "true")
        from services.iep2b.app.detect import is_model_ready as iep2b_ready

        assert iep2b_ready() is False


# ---------------------------------------------------------------------------
# 9. Region postprocessing integrity — IDs and canonical types
# ---------------------------------------------------------------------------


class TestRegionPostprocessingIntegrity:
    """
    Postconditions that must hold for every response regardless of mode:
      - IDs are r1, r2, … in consecutive order (no gaps, correct prefix).
      - Every region.type is a member of the canonical RegionType enum.
    """

    def test_iep2a_region_ids_are_sequential(self, iep2a_client: TestClient) -> None:
        resp = iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        ids = [r["id"] for r in resp["regions"]]
        expected = [f"r{i + 1}" for i in range(len(ids))]
        assert ids == expected

    def test_iep2b_region_ids_are_sequential(self, iep2b_client: TestClient) -> None:
        resp = iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        ids = [r["id"] for r in resp["regions"]]
        expected = [f"r{i + 1}" for i in range(len(ids))]
        assert ids == expected

    def test_iep2a_only_canonical_types(self, iep2a_client: TestClient) -> None:
        resp = iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        canonical = {rt.value for rt in RegionType}
        for r in resp["regions"]:
            assert r["type"] in canonical

    def test_iep2b_only_canonical_types(self, iep2b_client: TestClient) -> None:
        resp = iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        canonical = {rt.value for rt in RegionType}
        for r in resp["regions"]:
            assert r["type"] in canonical

    def test_iep2a_response_has_required_fields(self, iep2a_client: TestClient) -> None:
        resp = iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        for field in (
            "regions",
            "layout_conf_summary",
            "region_type_histogram",
            "processing_time_ms",
            "detector_type",
        ):
            assert field in resp, f"Missing field: {field}"
        assert resp["detector_type"] == "detectron2"

    def test_iep2b_response_has_required_fields(self, iep2b_client: TestClient) -> None:
        resp = iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).json()
        for field in (
            "regions",
            "layout_conf_summary",
            "region_type_histogram",
            "processing_time_ms",
            "detector_type",
        ):
            assert field in resp, f"Missing field: {field}"
        assert resp["detector_type"] == "doclayout_yolo"


# ---------------------------------------------------------------------------
# 10. IEP2B native-to-canonical class mapping with realistic model outputs
# ---------------------------------------------------------------------------


class TestIep2bNativeToCanonicalMapping:
    """
    Verifies that realistic DocLayout-YOLO native class labels map correctly
    to canonical RegionType values (or None for excluded classes), and that
    inference.raw_detections_to_regions produces valid Region lists.
    """

    # --- Direct class_mapping unit tests ---

    def test_native_title_maps_to_title(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("title") == RegionType.title

    def test_native_text_maps_to_text_block(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("text") == RegionType.text_block

    def test_native_figure_maps_to_image(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("figure") == RegionType.image

    def test_native_table_maps_to_table(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("table") == RegionType.table

    def test_native_caption_maps_to_caption(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("caption") == RegionType.caption

    def test_native_section_header_maps_to_title(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("section-header") == RegionType.title

    def test_native_abandon_maps_to_none(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("abandon") is None

    def test_native_formula_maps_to_none(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("formula") is None

    def test_native_page_number_maps_to_none(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("page_number") is None

    def test_case_insensitive_lookup(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("TITLE") == RegionType.title
        assert map_native_class("Text") == RegionType.text_block
        assert map_native_class("FIGURE") == RegionType.image

    def test_unknown_class_maps_to_none(self) -> None:
        from services.iep2b.app.class_mapping import map_native_class

        assert map_native_class("some_unknown_yolo_class") is None

    # --- raw_detections_to_regions integration ---

    def test_realistic_detections_produce_canonical_regions(self) -> None:
        """
        Simulates typical DocLayout-YOLO model output with a mix of mapped
        and excluded native classes and verifies the Region list is correct.
        """
        from services.iep2b.app.inference import raw_detections_to_regions

        # 5 mappable classes + 2 excluded ("abandon", "formula").
        mock_detections: list[tuple[str, tuple[float, float, float, float], float]] = [
            ("title", (50.0, 20.0, 950.0, 110.0), 0.92),
            ("text", (50.0, 130.0, 450.0, 600.0), 0.88),
            ("figure", (500.0, 130.0, 950.0, 600.0), 0.81),
            ("table", (50.0, 620.0, 950.0, 800.0), 0.79),
            ("caption", (50.0, 810.0, 500.0, 850.0), 0.75),
            ("abandon", (0.0, 0.0, 10.0, 10.0), 0.30),  # excluded
            ("formula", (200.0, 300.0, 400.0, 350.0), 0.65),  # excluded
        ]
        regions = raw_detections_to_regions(mock_detections)

        assert len(regions) == 5
        canonical = {rt.value for rt in RegionType}
        for r in regions:
            assert r.type.value in canonical

    def test_region_ids_sequential_after_mapping(self) -> None:
        from services.iep2b.app.inference import raw_detections_to_regions

        mock_detections: list[tuple[str, tuple[float, float, float, float], float]] = [
            ("title", (0.0, 0.0, 100.0, 50.0), 0.9),
            ("abandon", (0.0, 60.0, 100.0, 80.0), 0.8),  # excluded
            ("text", (0.0, 90.0, 100.0, 200.0), 0.85),
        ]
        regions = raw_detections_to_regions(mock_detections)

        # IDs must be consecutive starting from r1, with no gap for excluded region.
        assert [r.id for r in regions] == ["r1", "r2"]

    def test_degenerate_bboxes_excluded(self) -> None:
        """Regions with x1 >= x2 or y1 >= y2 are silently dropped."""
        from services.iep2b.app.inference import raw_detections_to_regions

        mock_detections: list[tuple[str, tuple[float, float, float, float], float]] = [
            ("title", (50.0, 20.0, 950.0, 110.0), 0.9),
            ("text", (100.0, 100.0, 50.0, 200.0), 0.8),  # x1 > x2 — degenerate
            ("table", (50.0, 620.0, 950.0, 800.0), 0.7),
        ]
        regions = raw_detections_to_regions(mock_detections)
        assert len(regions) == 2

    def test_confidence_clamped_to_unit_interval(self) -> None:
        from services.iep2b.app.inference import raw_detections_to_regions

        mock_detections: list[tuple[str, tuple[float, float, float, float], float]] = [
            ("title", (0.0, 0.0, 100.0, 50.0), 1.5),  # over 1.0
            ("text", (0.0, 60.0, 100.0, 200.0), -0.1),  # below 0.0
        ]
        regions = raw_detections_to_regions(mock_detections)
        assert regions[0].confidence == 1.0
        assert regions[1].confidence == 0.0

    def test_postprocess_preserves_canonical_types(self) -> None:
        """
        After raw_detections_to_regions + postprocess_regions the output
        must still contain only canonical RegionType values.
        """
        from services.iep2b.app.inference import raw_detections_to_regions
        from services.iep2b.app.postprocess import postprocess_regions

        mock_detections: list[tuple[str, tuple[float, float, float, float], float]] = [
            ("title", (50.0, 20.0, 950.0, 110.0), 0.92),
            ("text", (50.0, 130.0, 450.0, 600.0), 0.88),
            ("text", (55.0, 135.0, 455.0, 605.0), 0.70),  # overlaps above → NMS
            ("table", (50.0, 620.0, 950.0, 800.0), 0.79),
        ]
        regions = raw_detections_to_regions(mock_detections)
        postprocessed = postprocess_regions(regions)

        canonical = {rt.value for rt in RegionType}
        for r in postprocessed:
            assert r.type.value in canonical
        # One of the two overlapping text regions should be suppressed by NMS.
        text_count = sum(1 for r in postprocessed if r.type == RegionType.text_block)
        assert text_count == 1


# ---------------------------------------------------------------------------
# 11. Real-mode toggle — model singleton and endpoint error contract
# ---------------------------------------------------------------------------


class TestRealModeToggle:
    """
    Verifies the one-line config flag IEP2A_USE_REAL_MODEL / IEP2B_USE_REAL_MODEL
    correctly toggles between stub and real paths, and that the real path
    returns HTTP 500 with error_code='model_not_ready' when ML deps are absent
    (as in the test environment).
    """

    def test_iep2a_stub_mode_is_default(self) -> None:
        from services.iep2a.app.model import use_real_model

        assert use_real_model() is False

    def test_iep2b_stub_mode_is_default(self) -> None:
        from services.iep2b.app.model import use_real_model

        assert use_real_model() is False

    def test_iep2a_real_model_not_loaded_before_first_call(self) -> None:
        from services.iep2a.app.model import is_real_model_loaded, reset_for_testing

        reset_for_testing()
        assert is_real_model_loaded() is False

    def test_iep2b_real_model_not_loaded_before_first_call(self) -> None:
        from services.iep2b.app.model import is_real_model_loaded, reset_for_testing

        reset_for_testing()
        assert is_real_model_loaded() is False

    def test_iep2a_real_mode_returns_500_when_deps_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With IEP2A_USE_REAL_MODEL=true and Detectron2/LayoutParser deps absent, the
        endpoint must return HTTP 500 with error_code='model_not_ready'.
        """
        from services.iep2a.app.model import reset_for_testing

        reset_for_testing()
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "true")
        try:
            app = FastAPI()
            from services.iep2a.app.detect import router as iep2a_detect_router

            app.include_router(iep2a_detect_router)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
            assert resp.status_code == 500
            detail = resp.json().get("detail", {})
            assert detail.get("error_code") == "model_not_ready"
        finally:
            reset_for_testing()

    def test_iep2b_real_mode_returns_500_when_deps_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With IEP2B_USE_REAL_MODEL=true and doclayout_yolo not installed, the
        endpoint must return HTTP 500 with error_code='model_not_ready'.
        """
        from services.iep2b.app.model import reset_for_testing

        reset_for_testing()
        monkeypatch.setenv("IEP2B_USE_REAL_MODEL", "true")
        try:
            app = FastAPI()
            from services.iep2b.app.detect import router as iep2b_detect_router

            app.include_router(iep2b_detect_router)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
            assert resp.status_code == 500
            detail = resp.json().get("detail", {})
            assert detail.get("error_code") == "model_not_ready"
        finally:
            reset_for_testing()

    def test_iep2a_stub_mode_unaffected_after_real_mode_test(
        self, monkeypatch: pytest.MonkeyPatch, iep2a_client: TestClient
    ) -> None:
        """
        After a real-mode attempt (which fails and sets _load_error),
        resetting the singleton and returning to stub mode must give 200.
        """
        from services.iep2a.app.model import reset_for_testing

        reset_for_testing()
        # Confirm stub mode still works.
        assert iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).status_code == 200

    def test_iep2b_stub_mode_unaffected_after_real_mode_test(
        self, monkeypatch: pytest.MonkeyPatch, iep2b_client: TestClient
    ) -> None:
        from services.iep2b.app.model import reset_for_testing

        reset_for_testing()
        assert iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD).status_code == 200


class TestIep2aStartupWarmup:
    def test_real_mode_startup_warms_detectron2_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import services.iep2a.app.backends.factory as iep2a_factory
        from services.iep2a.app import model as iep2a_model
        from services.iep2a.app.main import app as iep2a_main_app

        calls = {"count": 0}

        def _fake_get_predictor() -> object:
            calls["count"] += 1
            return object()

        iep2a_model.reset_for_testing()
        iep2a_factory.reset_for_testing()
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "true")
        monkeypatch.setenv("IEP2A_LAYOUT_BACKEND", "detectron2")
        monkeypatch.setattr(iep2a_model, "get_predictor", _fake_get_predictor)

        try:
            with TestClient(iep2a_main_app):
                pass
        finally:
            iep2a_model.reset_for_testing()
            iep2a_factory.reset_for_testing()

        assert calls["count"] == 1

    def test_real_mode_startup_failure_keeps_ready_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import services.iep2a.app.backends.factory as iep2a_factory
        from services.iep2a.app import model as iep2a_model
        from services.iep2a.app.main import app as iep2a_main_app

        calls = {"count": 0}

        def _fake_get_predictor() -> object:
            calls["count"] += 1
            raise RuntimeError("detectron2 unavailable")

        iep2a_model.reset_for_testing()
        iep2a_factory.reset_for_testing()
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "true")
        monkeypatch.setenv("IEP2A_LAYOUT_BACKEND", "detectron2")
        monkeypatch.setattr(iep2a_model, "get_predictor", _fake_get_predictor)

        try:
            with TestClient(iep2a_main_app, raise_server_exceptions=False) as client:
                resp = client.get("/ready")
        finally:
            iep2a_model.reset_for_testing()
            iep2a_factory.reset_for_testing()

        assert calls["count"] == 1
        assert resp.status_code == 503
        assert resp.json() == {"status": "not_ready"}


class TestIep2bStartupWarmup:
    def test_real_mode_startup_warms_doclayout_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.iep2b.app import model as iep2b_model
        from services.iep2b.app.main import app as iep2b_main_app

        calls = {"count": 0}

        def _fake_get_model() -> object:
            calls["count"] += 1
            return object()

        iep2b_model.reset_for_testing()
        monkeypatch.setenv("IEP2B_USE_REAL_MODEL", "true")
        monkeypatch.setattr(iep2b_model, "get_model", _fake_get_model)

        try:
            with TestClient(iep2b_main_app):
                pass
        finally:
            iep2b_model.reset_for_testing()

        assert calls["count"] == 1

    def test_real_mode_startup_failure_keeps_ready_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.iep2b.app import model as iep2b_model
        from services.iep2b.app.main import app as iep2b_main_app

        calls = {"count": 0}

        def _fake_get_model() -> object:
            calls["count"] += 1
            raise RuntimeError("doclayout weights missing")

        iep2b_model.reset_for_testing()
        monkeypatch.setenv("IEP2B_USE_REAL_MODEL", "true")
        monkeypatch.setattr(iep2b_model, "get_model", _fake_get_model)

        try:
            with TestClient(iep2b_main_app, raise_server_exceptions=False) as client:
                resp = client.get("/ready")
        finally:
            iep2b_model.reset_for_testing()

        assert calls["count"] == 1
        assert resp.status_code == 503
        assert resp.json() == {"status": "not_ready"}


class TestLocalArtifactLoaderHardening:
    def test_iep2a_baked_weights_require_version_sidecar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.iep2a.app import model as iep2a_model

        weights_path = "/opt/models/iep2a/model_final.pth"
        monkeypatch.setenv("IEP2A_WEIGHTS_PATH", weights_path)
        monkeypatch.delenv("IEP2A_LOCAL_WEIGHTS_PATH", raising=False)
        monkeypatch.delenv("IEP2A_MODEL_VERSION", raising=False)

        with pytest.raises(RuntimeError, match="Missing IEP2A model version sidecar"):
            iep2a_model._resolve_model_version(weights_path, "weights_path")

    def test_iep2b_baked_weights_require_version_sidecar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.iep2b.app import model as iep2b_model

        weights_path = "/opt/models/iep2b/doclayout_yolo_docstructbench_imgsz1024.pt"
        monkeypatch.setenv("IEP2B_WEIGHTS_PATH", weights_path)
        monkeypatch.delenv("IEP2B_LOCAL_WEIGHTS_PATH", raising=False)
        monkeypatch.delenv("IEP2B_MODEL_VERSION", raising=False)

        with pytest.raises(RuntimeError, match="Missing IEP2B model version sidecar"):
            iep2b_model._resolve_model_version(weights_path, "weights_path")

    def test_iep2a_rejects_legacy_config_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.iep2a.app import model as iep2a_model

        monkeypatch.setenv("IEP2A_CONFIG_FILE", "/tmp/legacy-config.yaml")
        monkeypatch.delenv("IEP2A_CONFIG_PATH", raising=False)

        with pytest.raises(RuntimeError, match="IEP2A_CONFIG_FILE is not supported"):
            iep2a_model._resolve_config_path()

    def test_iep2a_response_model_version_uses_loaded_artifact_metadata(
        self, monkeypatch: pytest.MonkeyPatch, iep2a_client: TestClient
    ) -> None:
        import services.iep2a.app.backends.factory as iep2a_factory
        from services.iep2a.app import model as iep2a_model

        fake_image = types.SimpleNamespace(shape=(120, 80, 3))
        fake_inference = types.ModuleType("services.iep2a.app.inference")
        fake_inference.PUBLAYNET_CLASS_MAP = {}  # type: ignore[attr-defined]
        fake_inference.load_image_from_uri = lambda _: fake_image  # type: ignore[attr-defined]
        fake_inference.run_detectron2 = lambda *_: object()  # type: ignore[attr-defined]
        fake_inference.raw_detections_to_regions = lambda *_: [  # type: ignore[attr-defined]
            Region(
                id="r1",
                type=RegionType.title,
                bbox=BoundingBox(x_min=1, y_min=1, x_max=20, y_max=20),
                confidence=0.9,
            )
        ]

        iep2a_factory.reset_for_testing()
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "true")
        monkeypatch.setenv("IEP2A_LAYOUT_BACKEND", "detectron2")
        monkeypatch.setenv("IEP2A_MODEL_VERSION", "stale-env-version")
        monkeypatch.setitem(sys.modules, "services.iep2a.app.inference", fake_inference)
        monkeypatch.setattr(iep2a_model, "get_predictor", lambda: object())
        monkeypatch.setattr(
            iep2a_model,
            "get_loaded_model_version",
            lambda: "iep2a-artifact-v2026-03-25",
        )
        iep2a_factory.initialize_backend()

        try:
            response = iep2a_client.post("/v1/layout-detect", json=_VALID_PAYLOAD)
        finally:
            iep2a_factory.reset_for_testing()

        assert response.status_code == 200
        assert response.json()["model_version"] == "iep2a-artifact-v2026-03-25"

    def test_iep2b_response_model_version_uses_loaded_artifact_metadata(
        self, monkeypatch: pytest.MonkeyPatch, iep2b_client: TestClient
    ) -> None:
        from services.iep2b.app import model as iep2b_model

        fake_inference = types.ModuleType("services.iep2b.app.inference")
        fake_inference.load_image_for_yolo = lambda _: object()  # type: ignore[attr-defined]
        fake_inference.run_doclayout_yolo = lambda *_1, **_2: object()  # type: ignore[attr-defined]
        fake_inference.raw_detections_to_regions = lambda *_: [  # type: ignore[attr-defined]
            Region(
                id="r1",
                type=RegionType.table,
                bbox=BoundingBox(x_min=1, y_min=1, x_max=20, y_max=20),
                confidence=0.9,
            )
        ]

        monkeypatch.setenv("IEP2B_USE_REAL_MODEL", "true")
        monkeypatch.setenv("IEP2B_MODEL_VERSION", "stale-env-version")
        monkeypatch.setitem(sys.modules, "services.iep2b.app.inference", fake_inference)
        monkeypatch.setattr(iep2b_model, "get_model", lambda: object())
        monkeypatch.setattr(
            iep2b_model,
            "get_loaded_model_version",
            lambda: "iep2b-artifact-v2026-03-25",
        )

        response = iep2b_client.post("/v1/layout-detect", json=_VALID_PAYLOAD)

        assert response.status_code == 200
        assert response.json()["model_version"] == "iep2b-artifact-v2026-03-25"
