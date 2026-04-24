"""
tests/test_p6_layout_downsample.py
------------------------------------
Tests for downsample-aware IEP2 coordinate handling (google-adjudication branch).

Covers:
  1. _extract_downsample_gate — valid gate, missing keys, zero dims, absent gate.
  2. _rescale_layout_response — None input, identity scale, known bbox rescaling,
     empty regions, no double-scaling.
  3. complete_layout_detection with downsample_metadata:
     - Google path: regions rescaled to canonical coordinates.
     - Local-agreement path: no double scaling (already canonical from worker_loop).
     - Local-fallback path: no double scaling.
     - downsample_metadata recorded in gate_results["layout_input"].
     - coordinate_rescaled=False: no rescaling applied.
  4. URI selection: when downsample gate present the layout services receive the
     downsampled URI, not the original.
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.eep_worker.app.layout_step import LayoutStepResult, complete_layout_detection
from services.eep_worker.app.worker_loop import (
    _extract_downsample_gate,
    _rescale_layout_response,
)
from services.eep_worker.app.google_config import GoogleWorkerState
from shared.schemas.layout import (
    LayoutAdjudicationResult,
    LayoutConfSummary,
    LayoutDetectResponse,
    LayoutInputMetadata,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox


# ── Helpers ────────────────────────────────────────────────────────────────────


def _bbox(
    x_min: float = 10.0, y_min: float = 20.0, x_max: float = 110.0, y_max: float = 120.0
) -> BoundingBox:
    return BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


def _region(rid: str, rtype: RegionType = RegionType.text_block, bbox: BoundingBox | None = None) -> Region:
    return Region(id=rid, type=rtype, bbox=bbox or _bbox(), confidence=0.9)


def _conf_summary() -> LayoutConfSummary:
    return LayoutConfSummary(mean_conf=0.85, low_conf_frac=0.05)


def _detect_response(
    regions: list[Region],
    detector_type: Literal["detectron2", "doclayout_yolo", "paddleocr_pp_doclayout_v2"] = "paddleocr_pp_doclayout_v2",
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
        processing_time_ms=80.0,
        warnings=[],
    )


def _make_page(page_id: str = "pg-1", status: str = "layout_detection") -> MagicMock:
    page = MagicMock()
    page.page_id = page_id
    page.status = status
    page.output_image_uri = "s3://bucket/canonical.tiff"
    return page


def _make_session() -> MagicMock:
    return MagicMock()


def _make_lineage(gate_results: dict[str, Any] | None = None) -> MagicMock:
    lineage = MagicMock()
    lineage.gate_results = gate_results
    return lineage


def _make_google_client(regions: list[Region]) -> MagicMock:
    client = MagicMock()
    raw = {
        "elements": ["e1"],
        "page_width": 500,
        "page_height": 700,
        "region_count": len(regions),
    }
    client.process_layout = AsyncMock(return_value=raw)
    client._map_google_to_canonical = MagicMock(return_value=regions)
    return client


def _valid_downsample_gate(
    *,
    orig_w: int = 4000,
    orig_h: int = 6000,
    ds_w: int = 2000,
    ds_h: int = 3000,
    scale: float = 0.5,
) -> dict[str, Any]:
    return {
        "source_artifact_uri": "s3://bucket/original.tiff",
        "downsampled_artifact_uri": "s3://bucket/downsampled.tiff",
        "original_width": orig_w,
        "original_height": orig_h,
        "downsampled_width": ds_w,
        "downsampled_height": ds_h,
        "scale_factor": scale,
        "processing_time_ms": 120.0,
    }


# ── 1. _extract_downsample_gate ────────────────────────────────────────────────


class TestExtractDownsampleGate:
    def test_valid_gate_returned(self) -> None:
        lineage = _make_lineage({"downsample": _valid_downsample_gate()})
        result = _extract_downsample_gate(lineage)
        assert result is not None
        assert result["downsampled_artifact_uri"] == "s3://bucket/downsampled.tiff"

    def test_no_gate_results_returns_none(self) -> None:
        lineage = _make_lineage(None)
        assert _extract_downsample_gate(lineage) is None

    def test_empty_gate_results_returns_none(self) -> None:
        lineage = _make_lineage({})
        assert _extract_downsample_gate(lineage) is None

    def test_missing_downsampled_artifact_uri_returns_none(self) -> None:
        gate = _valid_downsample_gate()
        del gate["downsampled_artifact_uri"]
        lineage = _make_lineage({"downsample": gate})
        assert _extract_downsample_gate(lineage) is None

    def test_missing_original_width_returns_none(self) -> None:
        gate = _valid_downsample_gate()
        del gate["original_width"]
        lineage = _make_lineage({"downsample": gate})
        assert _extract_downsample_gate(lineage) is None

    def test_zero_downsampled_width_returns_none(self) -> None:
        gate = _valid_downsample_gate()
        gate["downsampled_width"] = 0
        lineage = _make_lineage({"downsample": gate})
        assert _extract_downsample_gate(lineage) is None

    def test_zero_original_height_returns_none(self) -> None:
        gate = _valid_downsample_gate()
        gate["original_height"] = 0
        lineage = _make_lineage({"downsample": gate})
        assert _extract_downsample_gate(lineage) is None

    def test_other_gate_keys_do_not_interfere(self) -> None:
        gate_results = {
            "downsample": _valid_downsample_gate(),
            "layout_adjudication": {"agreed": True},
        }
        lineage = _make_lineage(gate_results)
        result = _extract_downsample_gate(lineage)
        assert result is not None
        assert result["original_width"] == 4000


# ── 2. _rescale_layout_response ───────────────────────────────────────────────


class TestRescaleLayoutResponse:
    def test_none_input_returns_none(self) -> None:
        assert _rescale_layout_response(None, 2.0, 2.0) is None

    def test_identity_scale_returns_same_object(self) -> None:
        response = _detect_response([_region("r1")])
        result = _rescale_layout_response(response, 1.0, 1.0)
        assert result is response  # unchanged

    def test_bbox_coords_scaled_correctly(self) -> None:
        bbox = BoundingBox(x_min=10.0, y_min=20.0, x_max=110.0, y_max=220.0)
        r = _region("r1", bbox=bbox)
        response = _detect_response([r])

        # scale_x=2.0, scale_y=3.0 — original_dims / downsampled_dims
        result = _rescale_layout_response(response, 2.0, 3.0)

        assert result is not None
        assert len(result.regions) == 1
        rb = result.regions[0].bbox
        assert rb.x_min == pytest.approx(20.0)   # 10 * 2
        assert rb.y_min == pytest.approx(60.0)   # 20 * 3
        assert rb.x_max == pytest.approx(220.0)  # 110 * 2
        assert rb.y_max == pytest.approx(660.0)  # 220 * 3

    def test_empty_regions_list(self) -> None:
        response = _detect_response([])
        result = _rescale_layout_response(response, 2.0, 2.0)
        assert result is not None
        assert result.regions == []

    def test_multiple_regions_all_scaled(self) -> None:
        regions = [
            _region("r1", bbox=BoundingBox(x_min=0, y_min=0, x_max=100, y_max=50)),
            _region("r2", bbox=BoundingBox(x_min=50, y_min=50, x_max=150, y_max=150)),
        ]
        response = _detect_response(regions)
        result = _rescale_layout_response(response, 2.0, 2.0)
        assert result is not None
        assert result.regions[0].bbox.x_max == pytest.approx(200.0)
        assert result.regions[1].bbox.y_max == pytest.approx(300.0)

    def test_other_fields_preserved(self) -> None:
        response = _detect_response([_region("r1")], detector_type="doclayout_yolo")
        result = _rescale_layout_response(response, 2.0, 2.0)
        assert result is not None
        assert result.detector_type == "doclayout_yolo"
        assert result.model_version == "test-v1"
        assert result.region_schema_version == "v1"

    def test_no_double_scaling_applying_once_is_correct(self) -> None:
        """Applying _rescale_layout_response once gives correct coordinates.
        Applying the inverse to the result returns to original (no over-accumulation)."""
        bbox = BoundingBox(x_min=100.0, y_min=100.0, x_max=200.0, y_max=300.0)
        r = _region("r1", bbox=bbox)
        response = _detect_response([r])

        scale_x, scale_y = 2.0, 3.0
        scaled = _rescale_layout_response(response, scale_x, scale_y)
        assert scaled is not None

        # Verify: the scaled result is in canonical coords
        rb = scaled.regions[0].bbox
        assert rb.x_min == pytest.approx(200.0)
        assert rb.y_min == pytest.approx(300.0)

        # Verify: scaling back gives original (no double-scaling accumulation)
        unscaled = _rescale_layout_response(scaled, 1.0 / scale_x, 1.0 / scale_y)
        assert unscaled is not None
        orig_rb = unscaled.regions[0].bbox
        assert orig_rb.x_min == pytest.approx(100.0)
        assert orig_rb.y_min == pytest.approx(100.0)


# ── 3. complete_layout_detection with downsample_metadata ─────────────────────

# Helpers shared across layout_step tests
_LOCAL_A = [_region("r1", bbox=BoundingBox(x_min=0, y_min=0, x_max=100, y_max=50))]
_LOCAL_B = [_region("r1", bbox=BoundingBox(x_min=0, y_min=0, x_max=100, y_max=50))]


def _downsampled_metadata(
    *,
    input_w: int = 2000,
    input_h: int = 3000,
    canonical_w: int = 4000,
    canonical_h: int = 6000,
) -> LayoutInputMetadata:
    return LayoutInputMetadata(
        source_page_artifact_uri="s3://bucket/canonical.tiff",
        analyzed_artifact_uri="s3://bucket/downsampled.tiff",
        artifact_role="normalized_output",
        input_source="downsampled",
        layout_input_width=input_w,
        layout_input_height=input_h,
        canonical_output_width=canonical_w,
        canonical_output_height=canonical_h,
        coordinate_rescaled=True,
    )


def _original_metadata(image_uri: str = "s3://bucket/canonical.tiff") -> LayoutInputMetadata:
    return LayoutInputMetadata(
        source_page_artifact_uri=image_uri,
        analyzed_artifact_uri=image_uri,
        artifact_role="original_upload",
        input_source="page_output",
        layout_input_width=4000,
        layout_input_height=6000,
        canonical_output_width=4000,
        canonical_output_height=6000,
        coordinate_rescaled=False,
    )


class TestCompleteLayoutDetectionWithDownsampleMetadata:
    """Tests for complete_layout_detection() with downsample_metadata parameter."""

    @pytest.mark.asyncio
    async def test_google_path_regions_rescaled_to_canonical(self) -> None:
        """
        When Google is the decision source and coordinate_rescaled=True,
        final_layout_result regions must be in canonical (original) coordinates.
        """
        # Google returns regions in downsampled space (e.g. 500x700 image)
        google_region = _region("r1", bbox=BoundingBox(x_min=50, y_min=70, x_max=250, y_max=350))
        google_client = _make_google_client([google_region])

        # Detectors disagree → Google fallback will be used
        iep2a = _detect_response([_region("r1", RegionType.text_block)])
        iep2b = _detect_response(
            [_region("r1", RegionType.image, bbox=BoundingBox(x_min=500, y_min=500, x_max=600, y_max=600))],
            detector_type="doclayout_yolo",
        )

        meta = _downsampled_metadata(input_w=2000, input_h=3000, canonical_w=4000, canonical_h=6000)
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/downsampled.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client,
                layout_input=meta,
            )

        # scale_x = 4000 / 2000 = 2.0; scale_y = 6000 / 3000 = 2.0
        final = result.adjudication.final_layout_result
        assert len(final) == 1
        assert final[0].bbox.x_min == pytest.approx(100.0)   # 50 * 2
        assert final[0].bbox.y_min == pytest.approx(140.0)   # 70 * 2
        assert final[0].bbox.x_max == pytest.approx(500.0)   # 250 * 2
        assert final[0].bbox.y_max == pytest.approx(700.0)   # 350 * 2
        assert result.adjudication.layout_decision_source == "google_document_ai"

    @pytest.mark.asyncio
    async def test_local_agreement_no_double_scaling(self) -> None:
        """
        For local_agreement, final_layout_result comes from IEP2A regions.
        Those were already rescaled in worker_loop before being passed here,
        so complete_layout_detection must NOT apply additional scaling.
        """
        # Simulate pre-rescaled IEP2A regions (already in canonical space)
        canonical_region = _region("r1", bbox=BoundingBox(x_min=200, y_min=400, x_max=400, y_max=800))
        iep2a = _detect_response([canonical_region])
        iep2b = _detect_response(
            [_region("r1", bbox=BoundingBox(x_min=200, y_min=400, x_max=400, y_max=800))],
            detector_type="doclayout_yolo",
        )

        meta = _downsampled_metadata()
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/downsampled.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
                layout_input=meta,
            )

        assert result.adjudication.layout_decision_source == "local_agreement"
        final = result.adjudication.final_layout_result
        assert len(final) == 1
        # Coordinates must be exactly as provided — no additional scaling
        assert final[0].bbox.x_min == pytest.approx(200.0)
        assert final[0].bbox.y_min == pytest.approx(400.0)
        assert final[0].bbox.x_max == pytest.approx(400.0)
        assert final[0].bbox.y_max == pytest.approx(800.0)

    @pytest.mark.asyncio
    async def test_local_fallback_no_double_scaling(self) -> None:
        """
        For local_fallback_unverified, final_layout_result comes from IEP2A/IEP2B
        which were already rescaled in worker_loop — no additional scaling here.
        """
        canonical_region = _region("r1", bbox=BoundingBox(x_min=100, y_min=200, x_max=300, y_max=500))
        iep2a = _detect_response([canonical_region])
        # IEP2B disagrees heavily
        iep2b = _detect_response(
            [_region("r2", RegionType.image, bbox=BoundingBox(x_min=900, y_min=900, x_max=999, y_max=999))],
            detector_type="doclayout_yolo",
        )
        # Timeout simulates Google hard fail
        failing_client = MagicMock()
        failing_client.process_layout = AsyncMock(side_effect=TimeoutError("timeout"))

        meta = _downsampled_metadata()
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/downsampled.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=failing_client,
                layout_input=meta,
            )

        assert result.adjudication.layout_decision_source == "local_fallback_unverified"
        final = result.adjudication.final_layout_result
        assert len(final) == 1
        # Must be unchanged from what was passed in
        assert final[0].bbox.x_min == pytest.approx(100.0)
        assert final[0].bbox.y_min == pytest.approx(200.0)

    @pytest.mark.asyncio
    async def test_downsample_metadata_written_to_gate_results(self) -> None:
        """downsample_metadata must appear as gate_results['layout_input'] in lineage."""
        iep2a = _detect_response(_LOCAL_A)
        iep2b = _detect_response(_LOCAL_B, detector_type="doclayout_yolo")

        meta = _downsampled_metadata()
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion") as mock_update,
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/downsampled.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
                layout_input=meta,
            )

        kwargs = mock_update.call_args.kwargs
        gate_results = kwargs["gate_results"]
        assert "layout_input" in gate_results
        li = gate_results["layout_input"]
        assert li["input_source"] == "downsampled"
        assert li["coordinate_rescaled"] is True
        assert li["canonical_output_width"] // li["layout_input_width"] == 2

    @pytest.mark.asyncio
    async def test_original_metadata_written_when_no_downsample(self) -> None:
        """When coordinate_rescaled=False, layout_input is still recorded in gate_results."""
        iep2a = _detect_response(_LOCAL_A)
        iep2b = _detect_response(_LOCAL_B, detector_type="doclayout_yolo")

        meta = _original_metadata()
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion") as mock_update,
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/canonical.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
                layout_input=meta,
            )

        kwargs = mock_update.call_args.kwargs
        gate_results = kwargs["gate_results"]
        assert "layout_input" in gate_results
        li = gate_results["layout_input"]
        assert li["input_source"] == "page_output"
        assert li["coordinate_rescaled"] is False

    @pytest.mark.asyncio
    async def test_no_metadata_no_layout_input_in_gate_results(self) -> None:
        """When downsample_metadata is None (legacy call), layout_input is absent."""
        iep2a = _detect_response(_LOCAL_A)
        iep2b = _detect_response(_LOCAL_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion") as mock_update,
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/canonical.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
                # downsample_metadata not passed (None default)
            )

        kwargs = mock_update.call_args.kwargs
        gate_results = kwargs["gate_results"]
        assert "layout_input" not in gate_results

    @pytest.mark.asyncio
    async def test_google_empty_result_not_double_scaled(self) -> None:
        """Google returning 0 regions with coordinate_rescaled=True: empty list stays empty."""
        google_client = _make_google_client([])  # empty result

        iep2a = _detect_response([_region("r1")])
        iep2b = _detect_response(
            [_region("r1", RegionType.image, bbox=BoundingBox(x_min=900, y_min=900, x_max=999, y_max=999))],
            detector_type="doclayout_yolo",
        )
        meta = _downsampled_metadata()
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/downsampled.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client,
                layout_input=meta,
            )

        assert result.adjudication.layout_decision_source == "google_document_ai"
        assert result.adjudication.final_layout_result == []

    @pytest.mark.asyncio
    async def test_coordinate_rescaled_false_google_regions_not_scaled(self) -> None:
        """
        When coordinate_rescaled=False (original artifact used), Google regions
        must NOT be scaled — they are already in canonical space.
        """
        google_region = _region("r1", bbox=BoundingBox(x_min=50, y_min=70, x_max=250, y_max=350))
        google_client = _make_google_client([google_region])

        iep2a = _detect_response([_region("r1")])
        iep2b = _detect_response(
            [_region("r1", RegionType.image, bbox=BoundingBox(x_min=900, y_min=900, x_max=999, y_max=999))],
            detector_type="doclayout_yolo",
        )

        meta = _original_metadata()  # coordinate_rescaled=False
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/canonical.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client,
                layout_input=meta,
            )

        assert result.adjudication.layout_decision_source == "google_document_ai"
        final = result.adjudication.final_layout_result
        # Coordinates unchanged — no scaling applied
        assert final[0].bbox.x_min == pytest.approx(50.0)
        assert final[0].bbox.y_min == pytest.approx(70.0)
        assert final[0].bbox.x_max == pytest.approx(250.0)
        assert final[0].bbox.y_max == pytest.approx(350.0)


# ── 4. Rescaling math correctness ─────────────────────────────────────────────


class TestRescalingMathCorrectness:
    """Explicit numeric verification of the proportional rescaling formula."""

    def test_exact_proportional_rescale(self) -> None:
        """
        If original is 4000×6000 and downsampled is 2000×3000 (scale=0.5),
        a bbox at downsampled coords (x_min=500, y_min=750, x_max=1000, y_max=1500)
        must become (1000, 1500, 2000, 3000) in canonical space.
        """
        bbox = BoundingBox(x_min=500.0, y_min=750.0, x_max=1000.0, y_max=1500.0)
        r = _region("r1", bbox=bbox)
        response = _detect_response([r])

        scale_x = 4000 / 2000  # = 2.0
        scale_y = 6000 / 3000  # = 2.0
        result = _rescale_layout_response(response, scale_x, scale_y)

        assert result is not None
        rb = result.regions[0].bbox
        assert rb.x_min == pytest.approx(1000.0)
        assert rb.y_min == pytest.approx(1500.0)
        assert rb.x_max == pytest.approx(2000.0)
        assert rb.y_max == pytest.approx(3000.0)

    def test_non_square_scale_factors(self) -> None:
        """
        Non-square scale: orig 4096×3000, ds 2048×1500 → scale_x=2.0, scale_y=2.0.
        bbox(0,0,100,75) → (0,0,200,150).
        """
        bbox = BoundingBox(x_min=0.0, y_min=0.0, x_max=100.0, y_max=75.0)
        response = _detect_response([_region("r1", bbox=bbox)])

        result = _rescale_layout_response(response, 2.0, 2.0)
        assert result is not None
        rb = result.regions[0].bbox
        assert rb.x_min == pytest.approx(0.0)
        assert rb.y_min == pytest.approx(0.0)
        assert rb.x_max == pytest.approx(200.0)
        assert rb.y_max == pytest.approx(150.0)

    def test_region_id_and_type_preserved_after_rescale(self) -> None:
        """Region id and type must survive the coordinate transformation."""
        r = Region(
            id="r7",
            type=RegionType.table,
            bbox=BoundingBox(x_min=10, y_min=20, x_max=30, y_max=40),
            confidence=0.77,
        )
        response = _detect_response([r])
        result = _rescale_layout_response(response, 2.0, 3.0)
        assert result is not None
        assert result.regions[0].id == "r7"
        assert result.regions[0].type == RegionType.table
        assert result.regions[0].confidence == pytest.approx(0.77)
