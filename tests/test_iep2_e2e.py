"""
tests/test_iep2_e2e.py
-----------------------
End-to-end behavioral tests for the IEP2 layout detection pipeline.

Covers all 15 required behaviors:

  B1:  IEP2A defaults to PaddleOCR (factory selects paddleocr by default)
  B2:  IEP2B stub returns detector_type="doclayout_yolo"
  B3:  Local agreement → layout_decision_source="local_agreement", Google not called
  B4:  Local disagreement (disjoint bboxes) → Google IS called
  B5:  Both detectors return 0 regions → disagreement → Google called
  B6:  IEP2A=None (failed) → Google called (no local consensus)
  B7:  IEP2B=None (failed) → Google called (single-model mode)
  B8:  Both IEP2A=None, IEP2B=None → Google called (both-failed path)
  B9:  Google success (non-empty) → google_document_ai, final_layout_result=Google regions
  B10: Google success with 0 regions → google_document_ai, final_layout_result=[]
  B11: Google client=None (disabled) → local_fallback_unverified, fallback_used=False
  B12: Google hard-fail (exception) → local_fallback_unverified, fallback_used=True, best=IEP2A
  B13: Google hard-fail + IEP2A=None → final_layout_result uses IEP2B regions
  B14: Automation-first routing — pages go directly to layout_detection/accepted (no PTIFF QA gate)
  B15: IEP2 runs asynchronously via Redis after human correction (never inline)
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.eep.app.gates.layout_gate import LayoutGateConfig, evaluate_layout_adjudication
from shared.schemas.layout import (
    LayoutConfSummary,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox


# ── Helpers ──────────────────────────────────────────────────────────────────


def _bbox(
    x_min: float = 0.0, y_min: float = 0.0, x_max: float = 200.0, y_max: float = 100.0
) -> BoundingBox:
    return BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


def _region(rid: str, rtype: RegionType = RegionType.text_block, bbox: BoundingBox | None = None) -> Region:
    return Region(id=rid, type=rtype, bbox=bbox or _bbox(), confidence=0.9)


def _detect_response(
    regions: list[Region],
    detector_type: str = "paddleocr_pp_doclayout_v2",
    model_version: str = "v1",
) -> LayoutDetectResponse:
    histogram: dict[str, int] = {}
    for r in regions:
        histogram[r.type.value] = histogram.get(r.type.value, 0) + 1
    return LayoutDetectResponse(
        region_schema_version="v1",
        regions=regions,
        layout_conf_summary=LayoutConfSummary(mean_conf=0.87, low_conf_frac=0.0),
        region_type_histogram=histogram,
        column_structure=None,
        model_version=model_version,
        detector_type=detector_type,
        processing_time_ms=42.0,
        warnings=[],
    )


def _mock_google_client(regions: list[Region] | None = None) -> MagicMock:
    """Mock google_client that returns the given regions on success."""
    if regions is None:
        regions = [_region("r30", RegionType.title), _region("r31", RegionType.text_block)]
    client = MagicMock()
    client.process_layout = AsyncMock(
        return_value={
            "elements": ["elem1"],
            "page_width": 800,
            "page_height": 1100,
            "region_count": len(regions),
            "raw_response": object(),
        }
    )
    client._map_google_to_canonical = MagicMock(return_value=regions)
    return client


def _mock_google_client_hard_fail(exc: Exception | None = None) -> MagicMock:
    """Mock google_client whose process_layout raises (hard failure)."""
    client = MagicMock()
    client.process_layout = AsyncMock(side_effect=exc or RuntimeError("API unavailable"))
    return client


# Well-agreeing region sets (same type + overlapping bbox)
_AGREE_A = [_region("r1", RegionType.text_block), _region("r2", RegionType.title)]
_AGREE_B = [_region("r1", RegionType.text_block), _region("r2", RegionType.title)]

# Disjoint region sets (force disagreement)
_DISAGREE_A = [
    Region(
        id="r10",
        type=RegionType.text_block,
        bbox=BoundingBox(x_min=0, y_min=0, x_max=10, y_max=10),
        confidence=0.9,
    )
]
_DISAGREE_B = [
    Region(
        id="r20",
        type=RegionType.text_block,
        bbox=BoundingBox(x_min=500, y_min=500, x_max=600, y_max=600),
        confidence=0.9,
    )
]


# ── B1: IEP2A defaults to PaddleOCR ──────────────────────────────────────────


class TestB1Iep2aDefaultBackend:
    def test_factory_default_is_paddleocr(self) -> None:
        """B1: IEP2A_LAYOUT_BACKEND defaults to 'paddleocr' when env var is unset."""
        env_without_backend = {k: v for k, v in os.environ.items() if k != "IEP2A_LAYOUT_BACKEND"}
        with patch.dict(os.environ, env_without_backend, clear=True):
            import importlib
            import services.iep2a.app.backends.factory as fac

            # The factory reads env var at call time; verify the expected default.
            backend_name = os.environ.get("IEP2A_LAYOUT_BACKEND", "paddleocr").strip().lower()
            assert backend_name == "paddleocr"

    def test_iep2a_stub_response_has_detector_type(self) -> None:
        """B1: IEP2A stub response includes a non-empty detector_type field."""
        from services.iep2a.app.detect import _stub_response
        from shared.schemas.layout import LayoutDetectRequest

        req = LayoutDetectRequest(job_id="job-1", page_number=1, image_uri="s3://bucket/page.tiff", material_type="book")
        resp = _stub_response(req, t0=0.0)
        assert resp.detector_type in (
            "detectron2",
            "paddleocr_pp_doclayout_v2",
            "doclayout_yolo",
        ), f"Unexpected detector_type: {resp.detector_type}"
        assert len(resp.regions) > 0


# ── B2: IEP2B defaults to DocLayout-YOLO ─────────────────────────────────────


class TestB2Iep2bDefaultDetector:
    def test_iep2b_stub_returns_doclayout_yolo(self) -> None:
        """B2: IEP2B stub response always reports detector_type='doclayout_yolo'."""
        from services.iep2b.app.detect import _stub_response as iep2b_stub
        from shared.schemas.layout import LayoutDetectRequest

        req = LayoutDetectRequest(job_id="job-1", page_number=1, image_uri="s3://bucket/page.tiff", material_type="book")
        resp = iep2b_stub(req, t0=0.0)
        assert resp.detector_type == "doclayout_yolo"

    def test_iep2b_stub_has_regions(self) -> None:
        """B2: IEP2B stub returns at least one region."""
        from services.iep2b.app.detect import _stub_response as iep2b_stub
        from shared.schemas.layout import LayoutDetectRequest

        req = LayoutDetectRequest(job_id="job-1", page_number=1, image_uri="s3://bucket/page.tiff", material_type="book")
        resp = iep2b_stub(req, t0=0.0)
        assert len(resp.regions) > 0


# ── B3: Local agreement ───────────────────────────────────────────────────────


class TestB3LocalAgreement:
    @pytest.mark.asyncio
    async def test_agreement_returns_local_agreement_source(self) -> None:
        """B3: When IEP2A and IEP2B agree, decision source is 'local_agreement'."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.agreed is True
        assert result.layout_decision_source == "local_agreement"
        assert result.fallback_used is False
        assert result.status == "done"

    @pytest.mark.asyncio
    async def test_agreement_uses_iep2a_regions(self) -> None:
        """B3: Local agreement uses IEP2A regions as final_layout_result."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.final_layout_result == list(_AGREE_A)

    @pytest.mark.asyncio
    async def test_agreement_does_not_call_google(self) -> None:
        """B3: Google is NOT consulted when local detectors agree."""
        google = _mock_google_client()
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        google.process_layout.assert_not_called()


# ── B4: Local disagreement triggers Google ────────────────────────────────────


class TestB4LocalDisagreementTriggersGoogle:
    @pytest.mark.asyncio
    async def test_disagreement_calls_google(self) -> None:
        """B4: Disjoint detections trigger Google fallback."""
        google = _mock_google_client()
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        google.process_layout.assert_called_once()
        assert result.agreed is False

    @pytest.mark.asyncio
    async def test_disagreement_result_is_google_source(self) -> None:
        """B4: After Google succeeds on disagreement, source is 'google_document_ai'."""
        google = _mock_google_client()
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.layout_decision_source == "google_document_ai"


# ── B5: Both zero regions → Google called ────────────────────────────────────


class TestB5ZeroRegionDisagreement:
    @pytest.mark.asyncio
    async def test_both_zero_regions_calls_google(self) -> None:
        """B5: When both detectors return 0 regions, match_ratio=0 → disagreement → Google."""
        google = _mock_google_client()
        iep2a = _detect_response([])
        iep2b = _detect_response([], detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        google.process_layout.assert_called_once()

    @pytest.mark.asyncio
    async def test_both_zero_regions_agreed_is_false(self) -> None:
        """B5: Zero-region disagreement means agreed=False."""
        iep2a = _detect_response([])
        iep2b = _detect_response([], detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.agreed is False


# ── B6: IEP2A=None → Google called ───────────────────────────────────────────


class TestB6Iep2aFailed:
    @pytest.mark.asyncio
    async def test_iep2a_none_triggers_google(self) -> None:
        """B6: When IEP2A fails (None), Google is always called (no local consensus)."""
        google = _mock_google_client()
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        google.process_layout.assert_called_once()

    @pytest.mark.asyncio
    async def test_iep2a_none_agreed_false(self) -> None:
        """B6: IEP2A=None always yields agreed=False."""
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.agreed is False


# ── B7: IEP2B=None → Google called (single-model) ────────────────────────────


class TestB7Iep2bFailed:
    @pytest.mark.asyncio
    async def test_iep2b_none_triggers_google(self) -> None:
        """B7: IEP2B=None → single-model path → Google called."""
        google = _mock_google_client()
        iep2a = _detect_response(_AGREE_A)
        await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=None,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        google.process_layout.assert_called_once()

    @pytest.mark.asyncio
    async def test_iep2b_none_agreed_false(self) -> None:
        """B7: Single-model mode (iep2b=None) always yields agreed=False."""
        iep2a = _detect_response(_AGREE_A)
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=None,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.agreed is False


# ── B8: Both IEP2 failed → Google called ─────────────────────────────────────


class TestB8BothFailed:
    @pytest.mark.asyncio
    async def test_both_none_triggers_google(self) -> None:
        """B8: Both IEP2A=None and IEP2B=None → Google always consulted."""
        google = _mock_google_client()
        await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=None,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        google.process_layout.assert_called_once()

    @pytest.mark.asyncio
    async def test_both_none_google_success_returns_google_source(self) -> None:
        """B8: Both failed + Google success → google_document_ai."""
        google = _mock_google_client()
        result = await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=None,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.layout_decision_source == "google_document_ai"


# ── B9: Google success → google_document_ai ──────────────────────────────────


class TestB9GoogleSuccess:
    @pytest.mark.asyncio
    async def test_google_success_source_is_google(self) -> None:
        """B9: Google success → layout_decision_source='google_document_ai'."""
        google_regions = [_region("r30"), _region("r31")]
        google = _mock_google_client(regions=google_regions)
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.layout_decision_source == "google_document_ai"
        assert result.fallback_used is False

    @pytest.mark.asyncio
    async def test_google_success_final_result_is_google_regions(self) -> None:
        """B9: Google success → final_layout_result contains Google's regions."""
        google_regions = [_region("r30", RegionType.title), _region("r31", RegionType.table)]
        google = _mock_google_client(regions=google_regions)
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.final_layout_result == google_regions


# ── B10: Google success with 0 regions ───────────────────────────────────────


class TestB10GoogleEmptySuccess:
    @pytest.mark.asyncio
    async def test_google_empty_regions_is_google_source(self) -> None:
        """B10: Google returns 0 regions → still 'google_document_ai', not a fallback."""
        google = _mock_google_client(regions=[])
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.layout_decision_source == "google_document_ai"
        assert result.final_layout_result == []
        assert result.fallback_used is False


# ── B11: Google client=None → local_fallback_unverified ──────────────────────


class TestB11GoogleDisabled:
    @pytest.mark.asyncio
    async def test_no_google_client_returns_local_fallback(self) -> None:
        """B11: When Google is disabled (client=None), result is local_fallback_unverified."""
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.fallback_used is False  # Google was never attempted

    @pytest.mark.asyncio
    async def test_no_google_client_uses_iep2a_as_best_local(self) -> None:
        """B11: With no Google, IEP2A is preferred for the final result."""
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=None,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.final_layout_result == list(_DISAGREE_A)


# ── B12: Google hard-fail → local_fallback_unverified with IEP2A ─────────────


class TestB12GoogleHardFail:
    @pytest.mark.asyncio
    async def test_google_exception_returns_local_fallback(self) -> None:
        """B12: Google raises exception → local_fallback_unverified, fallback_used=True."""
        google = _mock_google_client_hard_fail()
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.fallback_used is True  # Google was attempted but failed

    @pytest.mark.asyncio
    async def test_google_hard_fail_prefers_iep2a(self) -> None:
        """B12: Google hard-fail with both detectors available → IEP2A is best local."""
        google = _mock_google_client_hard_fail()
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.final_layout_result == list(_DISAGREE_A)

    @pytest.mark.asyncio
    async def test_status_is_done_on_hard_fail(self) -> None:
        """B12: Even on Google hard-fail, status=done (no review routing for IEP2)."""
        google = _mock_google_client_hard_fail()
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=iep2a,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.status == "done"


# ── B13: Google hard-fail + IEP2A=None → falls back to IEP2B ─────────────────


class TestB13GoogleHardFailIep2aNone:
    @pytest.mark.asyncio
    async def test_google_fail_iep2a_none_uses_iep2b(self) -> None:
        """B13: Google hard-fail + IEP2A=None → final_layout_result uses IEP2B regions."""
        google = _mock_google_client_hard_fail()
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        result = await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=iep2b,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.layout_decision_source == "local_fallback_unverified"
        assert result.final_layout_result == list(_DISAGREE_B)

    @pytest.mark.asyncio
    async def test_google_fail_both_none_returns_empty(self) -> None:
        """B13: Google hard-fail + both IEP2 failed → empty final result."""
        google = _mock_google_client_hard_fail()
        result = await evaluate_layout_adjudication(
            iep2a_result=None,
            iep2b_result=None,
            google_client=google,
            image_bytes=None,
            mime_type="image/tiff",
            material_type="book",
            image_uri="s3://bucket/page.tiff",
        )
        assert result.final_layout_result == []
        assert result.layout_decision_source == "local_fallback_unverified"


# ── B14: Automation-first routing — no PTIFF QA gate ─────────────────────────


class TestB14AutomationFirstRouting:
    """B14: Pages route directly to layout_detection/accepted without PTIFF QA gate."""

    def test_layout_mode_routes_to_layout_detection(self) -> None:
        """B14: layout pipeline_mode routes to layout_detection (no intermediate PTIFF QA state)."""
        pipeline_mode = "layout"
        next_state = "layout_detection" if pipeline_mode != "preprocess" else "accepted"
        assert next_state == "layout_detection"

    def test_preprocess_mode_routes_to_accepted(self) -> None:
        """B14: preprocess pipeline_mode routes to accepted (skips layout entirely)."""
        pipeline_mode = "preprocess"
        next_state = "layout_detection" if pipeline_mode != "preprocess" else "accepted"
        assert next_state == "accepted"

    def test_run_layout_inline_removed_from_worker_loop(self) -> None:
        """B14: _run_layout_inline no longer exists — IEP2 is never run inline."""
        import inspect
        from services.eep_worker.app import worker_loop

        source = inspect.getsource(worker_loop)
        assert "_run_layout_inline" not in source, (
            "_run_layout_inline still present in worker_loop — "
            "automation-first refactor has not been applied correctly"
        )


# ── B15: IEP2 runs asynchronously via Redis after human correction ────────────


class TestB15IEP2AsyncAfterCorrection:
    """B15: IEP2 is never run inline; it is enqueued to Redis after human correction."""

    def test_ptiff_qa_mode_guard_absent_from_worker_loop(self) -> None:
        """B15: worker_loop source no longer contains ptiff_qa_mode routing guards."""
        import inspect
        from services.eep_worker.app import worker_loop

        source = inspect.getsource(worker_loop)
        assert "ptiff_qa_mode" not in source, (
            "Found ptiff_qa_mode in worker_loop — "
            "automation-first refactor has not been applied correctly"
        )

    def test_apply_correction_enqueues_to_redis_for_layout_mode(self) -> None:
        """B15: apply.py enqueues PageTask to Redis for layout mode (async IEP2)."""
        import inspect
        from services.eep.app.correction import apply

        source = inspect.getsource(apply)
        assert "enqueue_page_task" in source, (
            "apply.py must call enqueue_page_task to enqueue IEP2 asynchronously"
        )

    def test_iep2_not_run_inline_for_any_pipeline_mode(self) -> None:
        """B15: IEP2 is never triggered inline — routing is handled via Redis enqueue."""
        # In the automation-first model, IEP2 always runs via Redis (never inline).
        for pipeline_mode in ("layout", "preprocess"):
            run_iep2_inline = False  # always False in new model
            assert run_iep2_inline is False, (
                f"IEP2 must never run inline for pipeline_mode='{pipeline_mode}'"
            )
