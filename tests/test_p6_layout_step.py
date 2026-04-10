"""
tests/test_p6_layout_step.py
----------------------------
Focused worker-orchestration tests for the real IEP2 accepted path.

These tests cover the smallest concrete module that now owns the
``layout_detection`` -> ``accepted`` transition and persistence flow.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.eep.app.db.models import JobPage
from services.eep_worker.app.layout_step import (
    LayoutStepResult,
    LayoutTransitionError,
    complete_layout_detection,
)
from shared.schemas.layout import LayoutConfSummary, LayoutDetectResponse, Region, RegionType
from shared.schemas.ucf import BoundingBox

DetectorType = Literal["detectron2", "doclayout_yolo", "paddleocr_pp_doclayout_v2"]


def _page() -> JobPage:
    return JobPage(
        page_id="page-123",
        job_id="job-123",
        page_number=7,
        sub_page_index=None,
        status="layout_detection",
        input_image_uri="s3://bucket/page-123.tiff",
        output_image_uri="s3://bucket/page-123.ptiff",
        ptiff_qa_approved=False,
    )


def _page_output_uri(page: JobPage) -> str:
    assert page.output_image_uri is not None
    return page.output_image_uri


def _bbox(
    x_min: float = 0.0,
    y_min: float = 0.0,
    x_max: float = 100.0,
    y_max: float = 50.0,
) -> BoundingBox:
    return BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


def _region(
    rid: str,
    rtype: RegionType = RegionType.text_block,
    *,
    bbox: BoundingBox | None = None,
) -> Region:
    return Region(
        id=rid,
        type=rtype,
        bbox=bbox or _bbox(),
        confidence=0.9,
    )


def _conf_summary() -> LayoutConfSummary:
    return LayoutConfSummary(mean_conf=0.85, low_conf_frac=0.1)


def _detect_response(
    regions: list[Region],
    detector_type: DetectorType = "paddleocr_pp_doclayout_v2",
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
        detector_type=detector_type,
        processing_time_ms=100.0,
        warnings=[],
    )


def _agreeing_results() -> tuple[LayoutDetectResponse, LayoutDetectResponse]:
    regions = [_region("r1"), _region("r2", RegionType.title)]
    return (
        _detect_response(regions),
        _detect_response(regions, detector_type="doclayout_yolo"),
    )


def _disagreeing_results() -> tuple[LayoutDetectResponse, LayoutDetectResponse]:
    iep2a = [
        _region("r1", bbox=_bbox(0, 0, 100, 50)),
        _region("r2", RegionType.title, bbox=_bbox(0, 60, 100, 120)),
    ]
    iep2b = [
        _region("r1", bbox=_bbox(500, 0, 600, 50)),
        _region("r2", RegionType.title, bbox=_bbox(500, 60, 600, 120)),
    ]
    return (
        _detect_response(iep2a),
        _detect_response(iep2b, detector_type="doclayout_yolo"),
    )


def _mock_google_timeout() -> MagicMock:
    client = MagicMock()
    client.process_layout = AsyncMock(side_effect=TimeoutError("google layout timeout"))
    return client


def _page_update_dict(session: MagicMock) -> dict[str, Any]:
    call_args = session.query.return_value.filter.return_value.update.call_args
    assert call_args is not None
    return cast(dict[str, Any], call_args.args[0])


class TestCompleteLayoutDetection:
    def test_local_agreement_transitions_and_persists(self) -> None:
        session = MagicMock()
        page = _page()
        iep2a_result, iep2b_result = _agreeing_results()

        with (
            patch(
                "services.eep_worker.app.layout_step.advance_page_state", return_value=True
            ) as mock_advance,
            patch("services.eep_worker.app.layout_step.update_lineage_completion") as mock_lineage,
        ):
            result = asyncio.run(
                complete_layout_detection(
                    session=session,
                    page=page,
                    lineage_id="lin-123",
                    material_type="archival_document",
                    image_uri=_page_output_uri(page),
                    iep2a_result=iep2a_result,
                    iep2b_result=iep2b_result,
                    google_client=None,
                )
            )

        assert isinstance(result, LayoutStepResult)
        assert result.adjudication.layout_decision_source == "local_agreement"
        mock_advance.assert_called_once_with(
            session,
            page.page_id,
            "layout_detection",
            "accepted",
            acceptance_decision="accepted",
            routing_path="layout_adjudication",
            processing_time_ms=result.adjudication.processing_time_ms,
        )
        updates = _page_update_dict(session)
        assert updates["review_reasons"] is None
        assert updates["layout_consensus_result"]["layout_decision_source"] == "local_agreement"

        mock_lineage.assert_called_once()
        kwargs = mock_lineage.call_args.kwargs
        assert kwargs["acceptance_decision"] == "accepted"
        assert kwargs["acceptance_reason"] == "layout accepted from local agreement"
        assert kwargs["gate_results"]["layout_adjudication"]["layout_decision_source"] == (
            "local_agreement"
        )

    def test_google_timeout_uses_local_fallback_and_still_accepts(self) -> None:
        session = MagicMock()
        page = _page()
        iep2a_result, iep2b_result = _disagreeing_results()

        with (
            patch(
                "services.eep_worker.app.layout_step.advance_page_state", return_value=True
            ) as mock_advance,
            patch("services.eep_worker.app.layout_step.update_lineage_completion") as mock_lineage,
            patch(
                "services.eep_worker.app.layout_step.get_google_worker_state",
                return_value=SimpleNamespace(client=_mock_google_timeout()),
            ),
        ):
            result = asyncio.run(
                complete_layout_detection(
                    session=session,
                    page=page,
                    lineage_id="lin-123",
                    material_type="archival_document",
                    image_uri=_page_output_uri(page),
                    iep2a_result=iep2a_result,
                    iep2b_result=iep2b_result,
                )
            )

        assert result.adjudication.layout_decision_source == "local_fallback_unverified"
        assert result.adjudication.final_layout_result == iep2a_result.regions
        mock_advance.assert_called_once()
        assert mock_advance.call_args.kwargs["acceptance_decision"] == "accepted"
        assert mock_advance.call_args.kwargs["routing_path"] == "layout_adjudication"

        updates = _page_update_dict(session)
        assert updates["layout_consensus_result"]["layout_decision_source"] == (
            "local_fallback_unverified"
        )

        kwargs = mock_lineage.call_args.kwargs
        assert kwargs["acceptance_reason"] == (
            "layout accepted from unverified local fallback after Google hard failure"
        )
        assert kwargs["gate_results"]["layout_adjudication"]["layout_decision_source"] == (
            "local_fallback_unverified"
        )

    def test_cas_miss_raises_before_persisting(self) -> None:
        session = MagicMock()
        page = _page()
        iep2a_result, iep2b_result = _agreeing_results()

        with (
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=False),
            patch("services.eep_worker.app.layout_step.update_lineage_completion") as mock_lineage,
        ):
            with pytest.raises(LayoutTransitionError, match="Could not advance page_id"):
                asyncio.run(
                    complete_layout_detection(
                        session=session,
                        page=page,
                        lineage_id="lin-123",
                        material_type="archival_document",
                        image_uri=_page_output_uri(page),
                        iep2a_result=iep2a_result,
                        iep2b_result=iep2b_result,
                        google_client=None,
                    )
                )

        session.query.assert_not_called()
        mock_lineage.assert_not_called()

    def test_output_layout_uri_marks_layout_artifact_confirmed(self) -> None:
        session = MagicMock()
        page = _page()
        iep2a_result, iep2b_result = _agreeing_results()

        with (
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
            patch("services.eep_worker.app.layout_step.confirm_layout_artifact") as mock_confirm,
        ):
            asyncio.run(
                complete_layout_detection(
                    session=session,
                    page=page,
                    lineage_id="lin-123",
                    material_type="archival_document",
                    image_uri=_page_output_uri(page),
                    iep2a_result=iep2a_result,
                    iep2b_result=iep2b_result,
                    google_client=None,
                    output_layout_uri="s3://bucket/page-123.layout.json",
                )
            )

        updates = _page_update_dict(session)
        assert updates["output_layout_uri"] == "s3://bucket/page-123.layout.json"
        mock_confirm.assert_called_once_with(session, "lin-123")
