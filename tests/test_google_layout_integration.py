"""
tests/test_google_layout_integration.py
-----------------------------------------
Integration tests: Google Document AI layout adjudication end-to-end.

These tests prove the full IEP2 adjudication pipeline behaviour WITHOUT real
Google credentials — all Google API calls are mocked.  The tests verify:

  1. IEP2A / IEP2B disagreement forces the Google path.
  2. Google path produces layout_decision_source="google_document_ai".
  3. Final layout result regions match what Google returned (type + bbox).
  4. Audit metadata dict has the required fields.
  5. Failure case A — wrong processor_id (permanent API error) →
       layout_decision_source="local_fallback_unverified", pipeline continues.
  6. Failure case B — Google disabled (google_client=None) →
       decision path is "google_skipped" → local fallback used.
  7. Prometheus counter labels increment for each decision path:
       local_agreement, google_document_ai, local_fallback_unverified,
       google_skipped.
  8. Both IEP2 detectors failed (None) + Google success →
       google_document_ai result with correct regions.
  9. complete_layout_detection() always routes to accepted, never review.
 10. Google audit metadata preserved in gate_results (layout_consensus_result).

Comparison with existing test files
-------------------------------------
  test_p3_2_adjudication_gate.py — unit tests for evaluate_layout_adjudication()
                                    in isolation (no DB, no routing).
  test_p3_2_layout_step.py       — tests complete_layout_detection() DB/routing
                                    layer; does not inspect audit metadata detail.
  test_p6_layout_downsample.py   — downsample coordinate rescaling.

This file provides complementary integration coverage: it exercises the
complete stack (gate → routing → DB mocks) while explicitly asserting audit
metadata fields, Prometheus counter labels, and failure-path region fallback.
"""
from __future__ import annotations

from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from prometheus_client import REGISTRY

from services.eep.app.gates.layout_gate import LayoutGateConfig, evaluate_layout_adjudication
from services.eep_worker.app.google_config import GoogleWorkerState
from services.eep_worker.app.layout_step import LayoutTransitionError, complete_layout_detection
from shared.schemas.layout import (
    LayoutAdjudicationResult,
    LayoutConfSummary,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox

# ── Helpers ────────────────────────────────────────────────────────────────────


def _bbox(
    x_min: float = 0.0,
    y_min: float = 0.0,
    x_max: float = 200.0,
    y_max: float = 100.0,
) -> BoundingBox:
    return BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


def _region(
    rid: str,
    rtype: RegionType = RegionType.text_block,
    x_min: float = 0.0,
    y_min: float = 0.0,
    x_max: float = 200.0,
    y_max: float = 100.0,
) -> Region:
    return Region(
        id=rid,
        type=rtype,
        bbox=BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max),
        confidence=0.9,
    )


def _conf_summary() -> LayoutConfSummary:
    return LayoutConfSummary(mean_conf=0.85, low_conf_frac=0.1)


def _detect_response(
    regions: list[Region],
    detector_type: Literal[
        "detectron2", "doclayout_yolo", "paddleocr_pp_doclayout_v2"
    ] = "paddleocr_pp_doclayout_v2",
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
        model_version="test-v1",
        detector_type=detector_type,
        processing_time_ms=50.0,
        warnings=[],
    )


# Agreed regions (identical bboxes, same type → consensus passes)
_AGREE_A = [_region("r1", RegionType.text_block), _region("r2", RegionType.title)]
_AGREE_B = [_region("r1", RegionType.text_block), _region("r2", RegionType.title)]

# Disagreed regions (completely disjoint bboxes → consensus fails)
_DISAGREE_A = [
    Region(
        id="r0",
        type=RegionType.text_block,
        bbox=BoundingBox(x_min=0, y_min=0, x_max=10, y_max=10),
        confidence=0.9,
    )
]
_DISAGREE_B = [
    Region(
        id="r0",
        type=RegionType.image,
        bbox=BoundingBox(x_min=500, y_min=500, x_max=600, y_max=600),
        confidence=0.8,
    ),
    Region(
        id="r1",
        type=RegionType.table,
        bbox=BoundingBox(x_min=700, y_min=700, x_max=800, y_max=800),
        confidence=0.7,
    ),
]

# Google canonical regions returned by the mock
_GOOGLE_REGIONS = [
    _region("r0", RegionType.title, 10, 20, 400, 60),
    _region("r1", RegionType.text_block, 10, 80, 400, 300),
    _region("r2", RegionType.image, 10, 320, 400, 600),
]


def _mock_google_client(
    regions: list[Region] | None = None,
) -> MagicMock:
    """Google client mock: process_layout returns elements; _map_google_to_canonical returns regions."""
    if regions is None:
        regions = _GOOGLE_REGIONS

    client = MagicMock()
    google_response = {
        "elements": [f"elem_{i}" for i in range(len(regions))],
        "page_width": 800,
        "page_height": 1100,
        "region_count": len(regions),
        "raw_response": object(),
    }
    client.process_layout = AsyncMock(return_value=google_response)
    client._map_google_to_canonical = MagicMock(return_value=regions)
    return client


def _mock_google_client_hard_fail(exc: Exception) -> MagicMock:
    """Google client that raises a hard error from process_layout."""
    client = MagicMock()
    client.process_layout = AsyncMock(side_effect=exc)
    return client


def _mock_page(page_id: str = "page-42") -> MagicMock:
    page = MagicMock()
    page.page_id = page_id
    page.status = "layout_detection"
    page.output_image_uri = "s3://bucket/page.tiff"
    return page


def _disabled_worker_state() -> GoogleWorkerState:
    return GoogleWorkerState(enabled=False, config=None, client=None)


# ── SECTION 1: Disagreement forces Google path ────────────────────────────────


class TestDisagreementForcesGoogle:
    """IEP2A and IEP2B disagree → Google must be called."""

    @pytest.mark.asyncio
    async def test_disagreement_calls_google(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client()

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=b"fake_page_bytes",
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )

        client.process_layout.assert_awaited_once()
        assert result.layout_decision_source == "google_document_ai"

    @pytest.mark.asyncio
    async def test_disagreement_passes_image_bytes_to_google(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client()
        test_bytes = b"real_page_image_content"

        await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=test_bytes,
            mime_type="image/tiff",
            material_type="newspaper",
            image_uri="s3://bucket/page.tiff",
        )

        call_kwargs = client.process_layout.call_args.kwargs
        assert call_kwargs["image_bytes"] == test_bytes
        assert call_kwargs["mime_type"] == "image/tiff"
        assert call_kwargs["material_type"] == "newspaper"

    @pytest.mark.asyncio
    async def test_agreement_skips_google(self) -> None:
        """When IEP2A and IEP2B agree, Google must NOT be called."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client()

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )

        client.process_layout.assert_not_awaited()
        assert result.layout_decision_source == "local_agreement"


# ── SECTION 2: Google path — output correctness ───────────────────────────────


class TestGoogleOutputCorrectness:
    """When Google is called, final_layout_result must match Google's regions exactly."""

    @pytest.mark.asyncio
    async def test_final_layout_regions_match_google(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client(_GOOGLE_REGIONS)

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=b"page_bytes",
            mime_type="image/png",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        assert result.final_layout_result == _GOOGLE_REGIONS
        assert len(result.final_layout_result) == 3

    @pytest.mark.asyncio
    async def test_google_region_types_preserved(self) -> None:
        google_regions = [
            _region("r0", RegionType.title),
            _region("r1", RegionType.text_block),
            _region("r2", RegionType.image),
            _region("r3", RegionType.table),
        ]
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client(google_regions)

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        types = [r.type for r in result.final_layout_result]
        assert RegionType.title in types
        assert RegionType.text_block in types
        assert RegionType.image in types
        assert RegionType.table in types

    @pytest.mark.asyncio
    async def test_google_region_bboxes_preserved(self) -> None:
        google_regions = [
            _region("r0", RegionType.text_block, 10, 20, 400, 60),
        ]
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client(google_regions)

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        r = result.final_layout_result[0]
        assert r.bbox.x_min == pytest.approx(10.0)
        assert r.bbox.y_min == pytest.approx(20.0)
        assert r.bbox.x_max == pytest.approx(400.0)
        assert r.bbox.y_max == pytest.approx(60.0)


# ── SECTION 3: Audit metadata correctness ────────────────────────────────────


class TestAuditMetadata:
    """Google audit metadata in google_document_ai_result must be present and correct."""

    @pytest.mark.asyncio
    async def test_google_audit_dict_has_required_keys(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
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

        audit = result.google_document_ai_result
        assert audit is not None
        assert audit["attempted"] is True
        assert audit["success"] is True
        assert audit["hard_failure"] is False
        assert "region_count" in audit
        assert "page_width" in audit
        assert "page_height" in audit

    @pytest.mark.asyncio
    async def test_google_response_time_ms_set(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
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

    @pytest.mark.asyncio
    async def test_processing_time_gte_google_response_time(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
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

        assert result.processing_time_ms >= result.google_response_time_ms  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_result_is_json_serialisable(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
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

        json_str = result.model_dump_json()
        restored = LayoutAdjudicationResult.model_validate_json(json_str)
        assert restored.layout_decision_source == "google_document_ai"
        assert len(restored.final_layout_result) == len(_GOOGLE_REGIONS)

    @pytest.mark.asyncio
    async def test_local_agreement_audit_fields_null(self) -> None:
        """On local agreement path, Google audit fields must be None."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        assert result.google_document_ai_result is None
        assert result.google_response_time_ms is None


# ── SECTION 4: Failure case A — wrong processor / permanent API error ──────────


class TestFailureCasePermanentError:
    """
    Step 5 — Failure behavior:
    Wrong processor_id → Google permanent error → local_fallback_unverified.
    Pipeline must not crash; routing continues to 'accepted'.
    """

    @pytest.mark.asyncio
    async def test_permanent_error_uses_local_fallback(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client_hard_fail(
            RuntimeError("Processor not found (HTTP 404): wrong-processor-id")
        )

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=b"img",
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.status == "done"
        assert result.error is None  # gate-level error does not propagate as result.error

    @pytest.mark.asyncio
    async def test_permanent_error_does_not_crash(self) -> None:
        """The pipeline must return a result, not raise, on Google hard failure."""
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client_hard_fail(
            Exception("Authentication failed (HTTP 401)")
        )

        # Must not raise
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        assert isinstance(result, LayoutAdjudicationResult)

    @pytest.mark.asyncio
    async def test_permanent_error_uses_iep2a_as_fallback(self) -> None:
        """On Google failure, final_layout_result falls back to IEP2A regions."""
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client_hard_fail(Exception("wrong processor id"))

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        # IEP2A preferred over IEP2B for local fallback
        assert result.final_layout_result == iep2a.regions
        assert result.google_document_ai_result is not None
        assert result.google_document_ai_result["hard_failure"] is True

    @pytest.mark.asyncio
    async def test_timeout_uses_local_fallback(self) -> None:
        """Timeout → local_fallback_unverified."""
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client_hard_fail(TimeoutError("google layout timeout"))

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.google_response_time_ms is not None
        assert result.google_document_ai_result is not None
        assert "timeout" in result.google_document_ai_result["error"].lower()


# ── SECTION 5: Failure case B — Google disabled (google_client=None) ──────────


class TestFailureCaseGoogleDisabled:
    """
    Step 5 — Failure behavior:
    GOOGLE_ENABLED=false (google_client=None) → google_skipped decision.
    """

    @pytest.mark.asyncio
    async def test_no_google_client_skips_google(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.fallback_used is False  # Google was never attempted
        assert result.google_response_time_ms is None
        assert result.google_document_ai_result is not None
        assert result.google_document_ai_result["attempted"] is False

    @pytest.mark.asyncio
    async def test_no_google_client_does_not_crash(self) -> None:
        result = await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=None,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )
        assert isinstance(result, LayoutAdjudicationResult)
        assert result.status == "done"
        assert result.final_layout_result == []


# ── SECTION 6: Prometheus counter labels ──────────────────────────────────────


class TestPrometheusCounterLabels:
    """
    Step 6 — Metric validation:
    GOOGLE_LAYOUT_ADJUDICATION_DECISIONS increments for each decision path.
    """

    def _get_counter_value(self, source: str) -> float:
        """Read current value of the adjudication decisions counter for a given source label."""
        from shared.metrics import GOOGLE_LAYOUT_ADJUDICATION_DECISIONS

        try:
            return GOOGLE_LAYOUT_ADJUDICATION_DECISIONS.labels(source=source)._value.get()
        except Exception:
            return 0.0

    @pytest.mark.asyncio
    async def test_local_agreement_increments_counter(self) -> None:
        before = self._get_counter_value("local_agreement")
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")

        await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        after = self._get_counter_value("local_agreement")
        assert after > before

    @pytest.mark.asyncio
    async def test_google_document_ai_increments_counter(self) -> None:
        before = self._get_counter_value("google_document_ai")
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client()

        await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        after = self._get_counter_value("google_document_ai")
        assert after > before

    @pytest.mark.asyncio
    async def test_local_fallback_unverified_increments_counter(self) -> None:
        before = self._get_counter_value("local_fallback_unverified")
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client_hard_fail(Exception("API failure"))

        await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        after = self._get_counter_value("local_fallback_unverified")
        assert after > before

    @pytest.mark.asyncio
    async def test_google_skipped_increments_counter(self) -> None:
        """When google_client=None and disagreement, google_skipped fires."""
        before = self._get_counter_value("google_skipped")
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")

        await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        after = self._get_counter_value("google_skipped")
        assert after > before


# ── SECTION 7: Both IEP2 detectors failed + Google success ────────────────────


class TestBothIEP2FailedGoogleSuccess:
    """When both IEP2A=None and IEP2B=None, Google provides the layout result."""

    @pytest.mark.asyncio
    async def test_both_none_with_google_returns_google_result(self) -> None:
        client = _mock_google_client(_GOOGLE_REGIONS)

        result = await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=None,
            google_client=client,
            image_bytes=b"img",
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        assert result.layout_decision_source == "google_document_ai"
        assert result.final_layout_result == _GOOGLE_REGIONS
        assert result.iep2a_region_count == 0
        assert result.iep2b_region_count is None
        client.process_layout.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_both_none_with_google_success_status_done(self) -> None:
        client = _mock_google_client()

        result = await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=None,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
        )

        assert result.status == "done"
        assert result.error is None


# ── SECTION 8: complete_layout_detection() — full stack integration ───────────


class TestCompleteLayoutDetectionIntegration:
    """
    End-to-end through complete_layout_detection(): gate + routing + DB mocks.
    Verifies routing decision is always 'accepted' and audit data is persisted.
    """

    @pytest.mark.asyncio
    async def test_google_path_routes_to_accepted(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        google_client = _mock_google_client(_GOOGLE_REGIONS)
        session = MagicMock()
        page = _mock_page()

        with (
            patch(
                "services.eep_worker.app.layout_step.get_google_worker_state",
                return_value=_disabled_worker_state(),
            ),
            patch(
                "services.eep_worker.app.layout_step.advance_page_state",
                return_value=True,
            ),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client,
            )

        assert result.routing.next_state == "accepted"
        assert result.routing.acceptance_decision == "accepted"
        assert result.adjudication.layout_decision_source == "google_document_ai"

    @pytest.mark.asyncio
    async def test_failure_path_also_routes_to_accepted(self) -> None:
        """Google hard failure → local_fallback_unverified → still accepted."""
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        google_client = _mock_google_client_hard_fail(Exception("wrong processor"))
        session = MagicMock()
        page = _mock_page()

        with (
            patch(
                "services.eep_worker.app.layout_step.get_google_worker_state",
                return_value=_disabled_worker_state(),
            ),
            patch(
                "services.eep_worker.app.layout_step.advance_page_state",
                return_value=True,
            ),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client,
            )

        assert result.routing.next_state == "accepted"
        assert result.adjudication.layout_decision_source == "local_fallback_unverified"

    @pytest.mark.asyncio
    async def test_google_acceptance_reason_message(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        google_client = _mock_google_client(_GOOGLE_REGIONS)
        session = MagicMock()
        page = _mock_page()

        with (
            patch(
                "services.eep_worker.app.layout_step.get_google_worker_state",
                return_value=_disabled_worker_state(),
            ),
            patch(
                "services.eep_worker.app.layout_step.advance_page_state",
                return_value=True,
            ),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client,
            )

        assert "google" in result.routing.acceptance_reason.lower()
        assert result.routing.review_reason is None

    @pytest.mark.asyncio
    async def test_adjudication_written_to_page_row(self) -> None:
        """session.query().filter().update() must be called with layout_consensus_result."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = MagicMock()
        page = _mock_page()

        # Wire query chain so update() can be called
        session.query.return_value.filter.return_value.update.return_value = None

        with (
            patch(
                "services.eep_worker.app.layout_step.get_google_worker_state",
                return_value=_disabled_worker_state(),
            ),
            patch(
                "services.eep_worker.app.layout_step.advance_page_state",
                return_value=True,
            ),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        session.query.return_value.filter.return_value.update.assert_called_once()
        update_arg = session.query.return_value.filter.return_value.update.call_args[0][0]
        assert "layout_consensus_result" in update_arg

    @pytest.mark.asyncio
    async def test_cas_miss_raises_layout_transition_error(self) -> None:
        """advance_page_state returning False must raise LayoutTransitionError."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = MagicMock()
        page = _mock_page()

        with (
            patch(
                "services.eep_worker.app.layout_step.get_google_worker_state",
                return_value=_disabled_worker_state(),
            ),
            patch(
                "services.eep_worker.app.layout_step.advance_page_state",
                return_value=False,  # CAS miss
            ),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            with pytest.raises(LayoutTransitionError):
                await complete_layout_detection(
                    session=session,
                    page=page,
                    lineage_id="lin-1",
                    material_type="book",
                    image_uri="s3://bucket/page.tiff",
                    iep2a_result=iep2a,
                    iep2b_result=iep2b,
                    google_client=None,
                )


# ── SECTION 9: Strict config forces disagreement → Google path ────────────────


class TestForcedDisagreementViaConfig:
    """
    Step 3 — Force adjudication path:
    Using a strict LayoutGateConfig (min_match_ratio=1.1) forces disagreement
    even on well-matched regions, proving the Google path is invoked.
    """

    @pytest.mark.asyncio
    async def test_strict_config_forces_google_on_matched_regions(self) -> None:
        """min_match_ratio=1.1 is impossible → disagreement guaranteed → Google called."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        client = _mock_google_client(_GOOGLE_REGIONS)
        strict_cfg = LayoutGateConfig(min_match_ratio=1.1)

        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=client,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/p.tiff",
            config=strict_cfg,
        )

        client.process_layout.assert_awaited_once()
        assert result.layout_decision_source == "google_document_ai"
        assert result.agreed is False
        assert result.final_layout_result == _GOOGLE_REGIONS
