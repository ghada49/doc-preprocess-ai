"""
tests/test_p3_2_adjudication_gate.py
--------------------------------------
P3.2 — Focused async tests for evaluate_layout_adjudication.

Tests the five key decision paths:
  1. Local agreement fast path → agreed=True, layout_decision_source="local_agreement"
  2. Local disagreement + Google success → layout_decision_source="google_document_ai"
  3. Local disagreement + Google returns empty regions → done/google_document_ai
  4. Local disagreement + Google hard-fails → done/local_fallback_unverified
  5. Single-model (iep2b=None) + Google success → layout_decision_source="google_document_ai"

Does not test any worker routing or DB logic.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.eep.app.gates.layout_gate import LayoutGateConfig, evaluate_layout_adjudication
from shared.schemas.layout import (
    LayoutAdjudicationResult,
    LayoutConfSummary,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox

DetectorType = Literal["detectron2", "doclayout_yolo", "paddleocr_pp_doclayout_v2"]


class _AdjudicationScenario(TypedDict):
    iep2a_result: LayoutDetectResponse | None
    iep2b_result: LayoutDetectResponse | None
    google_client: Any | None


# ── Helpers ────────────────────────────────────────────────────────────────────


def _bbox(
    x_min: float = 0.0, y_min: float = 0.0, x_max: float = 100.0, y_max: float = 50.0
) -> BoundingBox:
    return BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


def _region(rid: str, rtype: RegionType = RegionType.text_block) -> Region:
    return Region(id=rid, type=rtype, bbox=_bbox(), confidence=0.9)


def _conf_summary() -> LayoutConfSummary:
    return LayoutConfSummary(mean_conf=0.85, low_conf_frac=0.1)


def _detect_response(
    regions: list[Region],
    detector_type: DetectorType = "paddleocr_pp_doclayout_v2",
    model_version: str = "v1",
) -> LayoutDetectResponse:
    histogram: dict[str, int] = {}
    for r in regions:
        histogram[r.type.value] = histogram.get(r.type.value, 0) + 1
    return LayoutDetectResponse(
        region_schema_version="v1",
        regions=regions,
        layout_conf_summary=_conf_summary(),
        region_type_histogram=histogram,
        column_structure=None,
        model_version=model_version,
        detector_type=detector_type,
        processing_time_ms=100.0,
        warnings=[],
    )


def _mock_google_client(regions: list[Region] | None = None) -> MagicMock:
    """Return a mock google_client where process_layout returns a usable dict."""
    if regions is None:
        regions = [_region("r1"), _region("r2")]

    client = MagicMock()

    # process_layout is async
    google_response = {
        "elements": ["fake_elem_1", "fake_elem_2"],
        "page_width": 800,
        "page_height": 1100,
        "region_count": len(regions),
        "raw_response": object(),
    }
    client.process_layout = AsyncMock(return_value=google_response)
    client._map_google_to_canonical = MagicMock(return_value=regions)
    return client


def _mock_google_client_empty() -> MagicMock:
    """Google client that returns an empty region list."""
    client = _mock_google_client(regions=[])
    return client


def _mock_google_client_none_response() -> MagicMock:
    """Google client whose process_layout returns None (total failure)."""
    client = MagicMock()
    client.process_layout = AsyncMock(return_value=None)
    return client


def _mock_google_client_exception(exc: Exception) -> MagicMock:
    """Google client whose process_layout raises a hard failure."""
    client = MagicMock()
    client.process_layout = AsyncMock(side_effect=exc)
    return client


# Regions that agree well (identical bboxes, same type)
_AGREED_IEP2A = [_region("r1", RegionType.text_block), _region("r2", RegionType.title)]
_AGREED_IEP2B = [_region("r1", RegionType.text_block), _region("r2", RegionType.title)]

# Regions that disagree (completely disjoint bboxes)
_DISAGREE_IEP2A = [
    Region(
        id="r1",
        type=RegionType.text_block,
        bbox=BoundingBox(x_min=0, y_min=0, x_max=10, y_max=10),
        confidence=0.9,
    ),
]
_DISAGREE_IEP2B = [
    Region(
        id="r1",
        type=RegionType.text_block,
        bbox=BoundingBox(x_min=500, y_min=500, x_max=600, y_max=600),
        confidence=0.9,
    ),
    Region(
        id="r2",
        type=RegionType.image,
        bbox=BoundingBox(x_min=700, y_min=700, x_max=800, y_max=800),
        confidence=0.8,
    ),
    Region(
        id="r3",
        type=RegionType.table,
        bbox=BoundingBox(x_min=200, y_min=200, x_max=300, y_max=300),
        confidence=0.7,
    ),
]


# ── Path 1: Local agreement ────────────────────────────────────────────────────


class TestLocalAgreementPath:
    @pytest.mark.asyncio
    async def test_agreed_returns_local_agreement_source(self) -> None:
        iep2a = _detect_response(_AGREED_IEP2A)
        iep2b = _detect_response(_AGREED_IEP2B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,  # must not be called on agreement path
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page1.tiff",
        )
        assert result.agreed is True
        assert result.layout_decision_source == "local_agreement"
        assert result.fallback_used is False
        assert result.status == "done"

    @pytest.mark.asyncio
    async def test_agreed_final_layout_is_iep2a_regions(self) -> None:
        iep2a = _detect_response(_AGREED_IEP2A)
        iep2b = _detect_response(_AGREED_IEP2B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/png",
            material_type="book",
            image_uri="s3://bucket/page1.tiff",
        )
        assert len(result.final_layout_result) == len(_AGREED_IEP2A)

    @pytest.mark.asyncio
    async def test_agreed_consensus_confidence_set(self) -> None:
        iep2a = _detect_response(_AGREED_IEP2A)
        iep2b = _detect_response(_AGREED_IEP2B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/png",
            material_type="book",
            image_uri="s3://bucket/page1.tiff",
        )
        assert result.consensus_confidence is not None
        assert 0.0 <= result.consensus_confidence <= 1.0

    @pytest.mark.asyncio
    async def test_agreed_matching_fields_set(self) -> None:
        iep2a = _detect_response(_AGREED_IEP2A)
        iep2b = _detect_response(_AGREED_IEP2B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/png",
            material_type="book",
            image_uri="s3://bucket/page1.tiff",
        )
        assert result.matched_regions is not None
        assert result.mean_matched_iou is not None
        assert result.type_histogram_match is not None

    @pytest.mark.asyncio
    async def test_agreed_google_fields_null(self) -> None:
        iep2a = _detect_response(_AGREED_IEP2A)
        iep2b = _detect_response(_AGREED_IEP2B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/png",
            material_type="book",
            image_uri="s3://bucket/page1.tiff",
        )
        assert result.google_document_ai_result is None
        assert result.google_response_time_ms is None

    @pytest.mark.asyncio
    async def test_agreed_iep2b_result_preserved(self) -> None:
        iep2a = _detect_response(_AGREED_IEP2A)
        iep2b = _detect_response(_AGREED_IEP2B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/png",
            material_type="book",
            image_uri="s3://bucket/page1.tiff",
        )
        assert result.iep2a_result is not None
        assert result.iep2b_result is not None


# ── Path 2: Local disagreement + Google success ────────────────────────────────


class TestGoogleFallbackSuccess:
    @pytest.mark.asyncio
    async def test_disagreement_triggers_google_fallback(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        google_regions = [_region("r1"), _region("r2"), _region("r3")]
        client = _mock_google_client(regions=google_regions)
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=b"fake_image_bytes",
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page1.tiff",
        )
        assert result.layout_decision_source == "google_document_ai"
        assert result.fallback_used is True
        assert result.agreed is False
        assert result.status == "done"

    @pytest.mark.asyncio
    async def test_google_fallback_final_layout_from_google(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        google_regions = [_region("r1"), _region("r2")]
        client = _mock_google_client(regions=google_regions)
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=b"img",
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert len(result.final_layout_result) == 2

    @pytest.mark.asyncio
    async def test_google_fallback_matching_fields_null(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        client = _mock_google_client()
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.matched_regions is None
        assert result.mean_matched_iou is None
        assert result.type_histogram_match is None
        assert result.consensus_confidence is None

    @pytest.mark.asyncio
    async def test_google_fallback_timing_set(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        client = _mock_google_client()
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.google_response_time_ms is not None
        assert result.google_response_time_ms >= 0.0
        assert result.processing_time_ms >= result.google_response_time_ms

    @pytest.mark.asyncio
    async def test_google_fallback_audit_dict_set(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        client = _mock_google_client()
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.google_document_ai_result is not None
        assert "region_count" in result.google_document_ai_result


# ── Path 3: Local disagreement + Google returns empty regions ──────────────────


class TestGoogleFallbackEmpty:
    @pytest.mark.asyncio
    async def test_google_empty_regions_is_done_from_google(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        client = _mock_google_client_empty()
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.status == "done"
        assert result.layout_decision_source == "google_document_ai"
        assert result.fallback_used is True
        assert result.final_layout_result == []
        assert result.google_document_ai_result is not None
        assert result.google_document_ai_result["empty_result"] is True

    @pytest.mark.asyncio
    async def test_google_empty_result_preserves_audit_metadata(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        client = _mock_google_client_empty()
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.error is None
        assert result.google_document_ai_result is not None
        assert result.google_document_ai_result["region_count"] == 0
        assert result.google_document_ai_result["success"] is True


class TestGoogleHardFailureFallback:
    @pytest.mark.asyncio
    async def test_google_none_response_uses_local_fallback(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        client = _mock_google_client_none_response()
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.status == "done"
        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.fallback_used is True
        assert result.final_layout_result == iep2a.regions
        assert result.error is None
        assert result.google_document_ai_result is not None
        assert result.google_document_ai_result["hard_failure"] is True

    @pytest.mark.asyncio
    async def test_google_timeout_uses_local_fallback(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        client = _mock_google_client_exception(TimeoutError("google layout timeout"))
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.status == "done"
        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.fallback_used is True
        assert result.final_layout_result == iep2a.regions
        assert result.google_response_time_ms is not None
        assert result.google_document_ai_result is not None
        assert "timeout" in result.google_document_ai_result["error"].lower()

    @pytest.mark.asyncio
    async def test_both_local_models_empty_and_google_timeout_returns_empty_layout(self) -> None:
        iep2a = _detect_response([])
        iep2b = _detect_response([], detector_type="doclayout_yolo")
        client = _mock_google_client_exception(TimeoutError("google layout timeout"))
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.status == "done"
        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.final_layout_result == []

    @pytest.mark.asyncio
    async def test_google_timeout_uses_iep2b_when_iep2a_has_no_regions(self) -> None:
        iep2a = _detect_response([])
        iep2b_regions = [_region("r1"), _region("r2")]
        iep2b = _detect_response(iep2b_regions, detector_type="doclayout_yolo")
        client = _mock_google_client_exception(TimeoutError("google layout timeout"))
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.status == "done"
        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.final_layout_result == iep2b_regions
        assert result.google_document_ai_result is not None
        assert result.google_document_ai_result["local_fallback_source"] == "iep2b"


# ── Path 4: Local disagreement + google_client=None ───────────────────────────


class TestNoGoogleClientPath:
    @pytest.mark.asyncio
    async def test_no_google_client_with_disagreement_uses_local_fallback(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.status == "done"
        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.fallback_used is False
        assert result.final_layout_result == iep2a.regions
        assert result.google_response_time_ms is None
        assert result.google_document_ai_result is not None
        assert result.google_document_ai_result["attempted"] is False

    @pytest.mark.asyncio
    async def test_both_failed_no_google_returns_done_empty_result(self) -> None:
        """Both IEP2A and IEP2B failed (None) and Google is not available."""
        result = await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=None,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.status == "done"
        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.iep2a_region_count == 0
        assert result.iep2b_region_count is None
        assert result.final_layout_result == []


# ── Path 5: Single-model (iep2b=None) + Google success ────────────────────────


class TestSingleModelGoogleFallback:
    @pytest.mark.asyncio
    async def test_iep2b_none_falls_back_to_google(self) -> None:
        iep2a = _detect_response([_region("r1"), _region("r2")])
        google_regions = [_region("r1"), _region("r2"), _region("r3")]
        client = _mock_google_client(regions=google_regions)
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=None,
            google_client=client,
            image_bytes=b"img_bytes",
            mime_type="image/tiff",
            material_type="newspaper",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.layout_decision_source == "google_document_ai"
        assert result.status == "done"
        assert len(result.final_layout_result) == 3

    @pytest.mark.asyncio
    async def test_iep2b_none_iep2b_region_count_is_none(self) -> None:
        iep2a = _detect_response([_region("r1")])
        client = _mock_google_client()
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=None,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.iep2b_region_count is None

    @pytest.mark.asyncio
    async def test_iep2a_none_falls_back_to_google(self) -> None:
        iep2b = _detect_response([_region("r1")], detector_type="doclayout_yolo")
        google_regions = [_region("r1")]
        client = _mock_google_client(regions=google_regions)
        result = await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert result.layout_decision_source == "google_document_ai"
        assert result.status == "done"
        assert result.iep2a_region_count == 0


# ── Return type invariants ─────────────────────────────────────────────────────


class TestReturnTypeInvariants:
    @pytest.mark.asyncio
    async def test_result_is_layout_adjudication_result(self) -> None:
        iep2a = _detect_response(_AGREED_IEP2A)
        iep2b = _detect_response(_AGREED_IEP2B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/png",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert isinstance(result, LayoutAdjudicationResult)

    @pytest.mark.asyncio
    async def test_processing_time_always_set(self) -> None:
        """processing_time_ms must be >= 0 in all paths."""
        scenarios: list[_AdjudicationScenario] = [
            # local agreement
            dict(
                iep2a_result=_detect_response(_AGREED_IEP2A),
                iep2b_result=_detect_response(_AGREED_IEP2B, detector_type="doclayout_yolo"),
                google_client=None,
            ),
            # no google client
            dict(
                iep2a_result=_detect_response(_DISAGREE_IEP2A),
                iep2b_result=_detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo"),
                google_client=None,
            ),
            # google success
            dict(
                iep2a_result=_detect_response(_DISAGREE_IEP2A),
                iep2b_result=_detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo"),
                google_client=_mock_google_client(),
            ),
        ]
        for kwargs in scenarios:
            result = await evaluate_layout_adjudication(
                **kwargs,
                image_bytes=None,
                mime_type="image/tiff",
                material_type="book",
                image_uri="s3://bucket/p.tiff",
            )
            assert result.processing_time_ms >= 0.0

    @pytest.mark.asyncio
    async def test_result_is_json_serialisable(self) -> None:
        iep2a = _detect_response(_DISAGREE_IEP2A)
        iep2b = _detect_response(_DISAGREE_IEP2B, detector_type="doclayout_yolo")
        client = _mock_google_client()
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        # model_dump_json must not raise
        json_str = result.model_dump_json()
        restored = LayoutAdjudicationResult.model_validate_json(json_str)
        assert restored.layout_decision_source == result.layout_decision_source

    @pytest.mark.asyncio
    async def test_custom_config_passed_through(self) -> None:
        """Passing a very strict config makes well-matched regions disagree."""
        # Force disagreement by setting min_match_ratio=1.1 (impossible to satisfy)
        strict_cfg = LayoutGateConfig(min_match_ratio=1.1)
        iep2a = _detect_response(_AGREED_IEP2A)
        iep2b = _detect_response(_AGREED_IEP2B, detector_type="doclayout_yolo")
        client = _mock_google_client()
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/png",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
            config=strict_cfg,
        )
        # Strict config means local disagreement → Google fallback
        assert result.layout_decision_source == "google_document_ai"
