"""
tests/test_p3_1_adjudication_schemas.py
----------------------------------------
P3.1 — Focused schema tests for LayoutAdjudicationRequest and
LayoutAdjudicationResult.

Validates construction, field defaults, validation constraints, and
round-trip serialization.  Does not test any adjudication logic.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from shared.schemas.layout import (
    LayoutAdjudicationRequest,
    LayoutAdjudicationResult,
    LayoutConfSummary,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox

# ── Fixtures ───────────────────────────────────────────────────────────────────


def _bbox() -> BoundingBox:
    return BoundingBox(x_min=0.0, x_max=100.0, y_min=0.0, y_max=50.0)


def _region(rid: str = "r1") -> Region:
    return Region(id=rid, type=RegionType.text_block, bbox=_bbox(), confidence=0.9)


def _conf_summary() -> LayoutConfSummary:
    return LayoutConfSummary(mean_conf=0.85, low_conf_frac=0.1)


def _iep2a_response() -> LayoutDetectResponse:
    return LayoutDetectResponse(
        region_schema_version="v1",
        regions=[_region("r1"), _region("r2")],
        layout_conf_summary=_conf_summary(),
        region_type_histogram={"text_block": 2},
        column_structure=None,
        model_version="paddle-v1",
        detector_type="paddleocr_pp_doclayout_v2",
        processing_time_ms=120.0,
        warnings=[],
    )


def _iep2b_response() -> LayoutDetectResponse:
    return LayoutDetectResponse(
        region_schema_version="v1",
        regions=[_region("r1"), _region("r2")],
        layout_conf_summary=_conf_summary(),
        region_type_histogram={"text_block": 2},
        column_structure=None,
        model_version="yolo-v1",
        detector_type="doclayout_yolo",
        processing_time_ms=95.0,
        warnings=[],
    )


# ── LayoutAdjudicationRequest ──────────────────────────────────────────────────


class TestLayoutAdjudicationRequest:
    def _minimal(self, reason: str = "local_disagreement") -> dict[str, Any]:
        return {
            "job_id": "job-123",
            "page_number": 1,
            "image_uri": "s3://bucket/page1.tiff",
            "material_type": "book",
            "reason": reason,
        }

    def test_minimal_construction(self) -> None:
        req = LayoutAdjudicationRequest(**self._minimal())
        assert req.job_id == "job-123"
        assert req.page_number == 1
        assert req.iep2a_result is None
        assert req.iep2b_result is None

    def test_with_both_results(self) -> None:
        req = LayoutAdjudicationRequest(
            **self._minimal(),
            iep2a_result=_iep2a_response(),
            iep2b_result=_iep2b_response(),
        )
        assert req.iep2a_result is not None
        assert req.iep2b_result is not None
        assert req.iep2a_result.detector_type == "paddleocr_pp_doclayout_v2"

    def test_iep2b_none_when_unavailable(self) -> None:
        req = LayoutAdjudicationRequest(
            **self._minimal(reason="iep2b_failed"),
            iep2a_result=_iep2a_response(),
        )
        assert req.reason == "iep2b_failed"
        assert req.iep2b_result is None

    def test_all_valid_reasons(self) -> None:
        for reason in ("local_disagreement", "iep2a_failed", "iep2b_failed", "both_failed"):
            req = LayoutAdjudicationRequest(**self._minimal(reason=reason))
            assert req.reason == reason

    def test_invalid_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LayoutAdjudicationRequest(**self._minimal(reason="not_a_valid_reason"))

    def test_page_number_ge1(self) -> None:
        with pytest.raises(ValidationError):
            LayoutAdjudicationRequest(**{**self._minimal(), "page_number": 0})

    def test_all_material_types(self) -> None:
        for mt in ("book", "newspaper", "archival_document"):
            req = LayoutAdjudicationRequest(**{**self._minimal(), "material_type": mt})
            assert req.material_type == mt

    def test_invalid_material_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LayoutAdjudicationRequest(**{**self._minimal(), "material_type": "magazine"})

    def test_serialization_round_trip(self) -> None:
        req = LayoutAdjudicationRequest(
            **self._minimal(),
            iep2a_result=_iep2a_response(),
        )
        data = req.model_dump()
        assert data["job_id"] == "job-123"
        assert data["reason"] == "local_disagreement"
        assert data["iep2a_result"] is not None
        assert data["iep2b_result"] is None

    def test_json_round_trip(self) -> None:
        req = LayoutAdjudicationRequest(**self._minimal())
        json_str = req.model_dump_json()
        restored = LayoutAdjudicationRequest.model_validate_json(json_str)
        assert restored.job_id == req.job_id
        assert restored.reason == req.reason


# ── LayoutAdjudicationResult ───────────────────────────────────────────────────


class TestLayoutAdjudicationResult:
    def _local_agreement_result(self) -> dict[str, Any]:
        """Represents the fast path: IEP2A + IEP2B agreed."""
        return {
            "agreed": True,
            "consensus_confidence": 0.87,
            "layout_decision_source": "local_agreement",
            "fallback_used": False,
            "iep2a_region_count": 2,
            "iep2b_region_count": 2,
            "matched_regions": 2,
            "mean_matched_iou": 0.82,
            "type_histogram_match": True,
            "iep2a_result": None,
            "iep2b_result": None,
            "google_document_ai_result": None,
            "final_layout_result": [_region("r1").model_dump(), _region("r2").model_dump()],
            "status": "done",
            "error": None,
            "processing_time_ms": 350.0,
            "google_response_time_ms": None,
        }

    def _google_fallback_result(self) -> dict[str, Any]:
        """Represents the Google adjudication path."""
        return {
            "agreed": False,
            "consensus_confidence": None,
            "layout_decision_source": "google_document_ai",
            "fallback_used": True,
            "iep2a_region_count": 2,
            "iep2b_region_count": 3,
            "matched_regions": None,
            "mean_matched_iou": None,
            "type_histogram_match": None,
            "iep2a_result": None,
            "iep2b_result": None,
            "google_document_ai_result": {"pages": [], "meta": "raw"},
            "final_layout_result": [_region("r1").model_dump()],
            "status": "done",
            "error": None,
            "processing_time_ms": 1200.0,
            "google_response_time_ms": 850.0,
        }

    def _all_failed_result(self) -> dict[str, Any]:
        """Represents the failure path: all methods failed."""
        return {
            "agreed": False,
            "consensus_confidence": None,
            "layout_decision_source": "none",
            "fallback_used": True,
            "iep2a_region_count": 0,
            "iep2b_region_count": None,
            "matched_regions": None,
            "mean_matched_iou": None,
            "type_histogram_match": None,
            "iep2a_result": None,
            "iep2b_result": None,
            "google_document_ai_result": None,
            "final_layout_result": [],
            "status": "failed",
            "error": "All layout detection methods failed",
            "processing_time_ms": 500.0,
            "google_response_time_ms": None,
        }

    # ── Local agreement path ───────────────────────────────────────────────────

    def test_local_agreement_construction(self) -> None:
        result = LayoutAdjudicationResult(**self._local_agreement_result())
        assert result.agreed is True
        assert result.consensus_confidence == pytest.approx(0.87)
        assert result.layout_decision_source == "local_agreement"
        assert result.fallback_used is False
        assert result.status == "done"
        assert len(result.final_layout_result) == 2

    def test_local_agreement_matched_fields_present(self) -> None:
        result = LayoutAdjudicationResult(**self._local_agreement_result())
        assert result.matched_regions == 2
        assert result.mean_matched_iou == pytest.approx(0.82)
        assert result.type_histogram_match is True

    # ── Google fallback path ───────────────────────────────────────────────────

    def test_google_fallback_construction(self) -> None:
        result = LayoutAdjudicationResult(**self._google_fallback_result())
        assert result.agreed is False
        assert result.consensus_confidence is None
        assert result.layout_decision_source == "google_document_ai"
        assert result.fallback_used is True
        assert result.google_response_time_ms == pytest.approx(850.0)
        assert result.google_document_ai_result == {"pages": [], "meta": "raw"}

    def test_google_fallback_matching_fields_null(self) -> None:
        result = LayoutAdjudicationResult(**self._google_fallback_result())
        assert result.matched_regions is None
        assert result.mean_matched_iou is None
        assert result.type_histogram_match is None

    # ── All-failed path ────────────────────────────────────────────────────────

    def test_all_failed_construction(self) -> None:
        result = LayoutAdjudicationResult(**self._all_failed_result())
        assert result.agreed is False
        assert result.status == "failed"
        assert result.error == "All layout detection methods failed"
        assert result.final_layout_result == []
        assert result.layout_decision_source == "none"

    def test_failed_with_no_error_message_allowed(self) -> None:
        data = {**self._all_failed_result(), "error": None}
        result = LayoutAdjudicationResult(**data)
        assert result.error is None

    # ── Field defaults ─────────────────────────────────────────────────────────

    def test_final_layout_result_defaults_to_empty_list(self) -> None:
        data = {k: v for k, v in self._all_failed_result().items() if k != "final_layout_result"}
        result = LayoutAdjudicationResult(**data)
        assert result.final_layout_result == []

    def test_iep2b_region_count_none_for_single_model(self) -> None:
        result = LayoutAdjudicationResult(**self._all_failed_result())
        assert result.iep2b_region_count is None

    # ── Constraint validation ──────────────────────────────────────────────────

    def test_invalid_status_rejected(self) -> None:
        data = {**self._local_agreement_result(), "status": "pending"}
        with pytest.raises(ValidationError):
            LayoutAdjudicationResult(**data)

    def test_invalid_decision_source_rejected(self) -> None:
        data = {**self._local_agreement_result(), "layout_decision_source": "iep2a_only"}
        with pytest.raises(ValidationError):
            LayoutAdjudicationResult(**data)

    def test_consensus_confidence_out_of_range_rejected(self) -> None:
        data = {**self._local_agreement_result(), "consensus_confidence": 1.5}
        with pytest.raises(ValidationError):
            LayoutAdjudicationResult(**data)

    def test_negative_processing_time_rejected(self) -> None:
        data = {**self._local_agreement_result(), "processing_time_ms": -1.0}
        with pytest.raises(ValidationError):
            LayoutAdjudicationResult(**data)

    def test_negative_region_count_rejected(self) -> None:
        data = {**self._local_agreement_result(), "iep2a_region_count": -1}
        with pytest.raises(ValidationError):
            LayoutAdjudicationResult(**data)

    def test_negative_google_response_time_rejected(self) -> None:
        data = {**self._google_fallback_result(), "google_response_time_ms": -5.0}
        with pytest.raises(ValidationError):
            LayoutAdjudicationResult(**data)

    # ── With full LayoutDetectResponse embedded ────────────────────────────────

    def test_with_embedded_iep2a_result(self) -> None:
        data = {**self._local_agreement_result(), "iep2a_result": _iep2a_response().model_dump()}
        result = LayoutAdjudicationResult(**data)
        assert result.iep2a_result is not None
        assert result.iep2a_result.detector_type == "paddleocr_pp_doclayout_v2"
        assert len(result.iep2a_result.regions) == 2

    def test_with_embedded_iep2b_result(self) -> None:
        data = {**self._local_agreement_result(), "iep2b_result": _iep2b_response().model_dump()}
        result = LayoutAdjudicationResult(**data)
        assert result.iep2b_result is not None
        assert result.iep2b_result.detector_type == "doclayout_yolo"

    # ── Serialization ──────────────────────────────────────────────────────────

    def test_model_dump_local_agreement(self) -> None:
        result = LayoutAdjudicationResult(**self._local_agreement_result())
        data = result.model_dump()
        assert data["agreed"] is True
        assert data["layout_decision_source"] == "local_agreement"
        assert data["google_document_ai_result"] is None
        assert isinstance(data["final_layout_result"], list)

    def test_model_dump_google_fallback(self) -> None:
        result = LayoutAdjudicationResult(**self._google_fallback_result())
        data = result.model_dump()
        assert data["fallback_used"] is True
        assert data["google_document_ai_result"] == {"pages": [], "meta": "raw"}
        assert data["google_response_time_ms"] == pytest.approx(850.0)

    def test_json_round_trip_local_agreement(self) -> None:
        result = LayoutAdjudicationResult(**self._local_agreement_result())
        json_str = result.model_dump_json()
        restored = LayoutAdjudicationResult.model_validate_json(json_str)
        assert restored.agreed == result.agreed
        assert restored.consensus_confidence == pytest.approx(result.consensus_confidence)
        assert len(restored.final_layout_result) == len(result.final_layout_result)

    def test_json_round_trip_google_fallback(self) -> None:
        result = LayoutAdjudicationResult(**self._google_fallback_result())
        json_str = result.model_dump_json()
        restored = LayoutAdjudicationResult.model_validate_json(json_str)
        assert restored.layout_decision_source == "google_document_ai"
        assert restored.google_document_ai_result == {"pages": [], "meta": "raw"}
