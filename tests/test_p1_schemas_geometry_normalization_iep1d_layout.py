"""
tests/test_p1_schemas_geometry_normalization_iep1d_layout.py
-------------------------------------------------------------
Packet 1.2 validator tests for:
  - shared.schemas.geometry     (GeometryRequest, PageRegion, GeometryResponse)
  - shared.schemas.normalization (NormalizeRequest)
  - shared.schemas.iep1d        (RectifyRequest, RectifyResponse)
  - shared.schemas.layout       (RegionType, Region, LayoutConfSummary,
                                  ColumnStructure, LayoutDetectRequest,
                                  LayoutDetectResponse, LayoutConsensusResult)

Definition of done: all service request/response models validate correctly.
"""

import pytest
from pydantic import ValidationError

from shared.schemas.geometry import GeometryRequest, GeometryResponse, PageRegion
from shared.schemas.iep1d import RectifyRequest, RectifyResponse
from shared.schemas.layout import (
    ColumnStructure,
    LayoutConfSummary,
    LayoutConsensusResult,
    LayoutDetectRequest,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.normalization import NormalizeRequest
from shared.schemas.ucf import BoundingBox

# ── Helpers ────────────────────────────────────────────────────────────────────


def _page_region(
    region_id: str = "page_0",
    geometry_type: str = "quadrilateral",
    confidence: float = 0.92,
    page_area_fraction: float = 0.75,
    corners: list[tuple[float, float]] | None = None,
    bbox: tuple[int, int, int, int] | None = (0, 0, 1200, 1600),
) -> PageRegion:
    if corners is None and geometry_type == "quadrilateral":
        corners = [(10.0, 10.0), (1190.0, 10.0), (1190.0, 1590.0), (10.0, 1590.0)]
    return PageRegion(
        region_id=region_id,
        geometry_type=geometry_type,  # type: ignore[arg-type]
        corners=corners,
        bbox=bbox,
        confidence=confidence,
        page_area_fraction=page_area_fraction,
    )


def _geometry_response(page_count: int = 1) -> GeometryResponse:
    pages = [_page_region(f"page_{i}") for i in range(page_count)]
    return GeometryResponse(
        page_count=page_count,
        pages=pages,
        split_required=page_count == 2,
        split_x=600 if page_count == 2 else None,
        geometry_confidence=0.91,
        tta_structural_agreement_rate=0.95,
        tta_prediction_variance=0.02,
        tta_passes=5,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=120.5,
    )


def _layout_bbox() -> BoundingBox:
    return BoundingBox(x_min=50.0, y_min=100.0, x_max=500.0, y_max=300.0)


def _region(region_id: str = "r1", rtype: RegionType = RegionType.text_block) -> Region:
    return Region(
        id=region_id,
        type=rtype,
        bbox=_layout_bbox(),
        confidence=0.88,
    )


def _layout_response(detector: str = "detectron2") -> LayoutDetectResponse:
    return LayoutDetectResponse.model_validate(
        {
            "region_schema_version": "v1",
            "regions": [
                {
                    "id": "r1",
                    "type": "text_block",
                    "bbox": {"x_min": 50, "y_min": 100, "x_max": 500, "y_max": 300},
                    "confidence": 0.88,
                }
            ],
            "layout_conf_summary": {"mean_conf": 0.88, "low_conf_frac": 0.0},
            "region_type_histogram": {"text_block": 1},
            "column_structure": None,
            "model_version": "v1.0.0",
            "detector_type": detector,
            "processing_time_ms": 200.0,
            "warnings": [],
        }
    )


# ── GeometryRequest ────────────────────────────────────────────────────────────


class TestGeometryRequest:
    def test_valid(self) -> None:
        r = GeometryRequest(
            job_id="j1",
            page_number=1,
            image_uri="s3://bucket/proxy/1.jpg",
            material_type="book",
        )
        assert r.material_type == "book"

    def test_page_number_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeometryRequest(
                job_id="j1",
                page_number=0,
                image_uri="s3://bucket/proxy/1.jpg",
                material_type="newspaper",
            )

    def test_invalid_material_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeometryRequest.model_validate(
                {
                    "job_id": "j1",
                    "page_number": 1,
                    "image_uri": "s3://bucket/proxy/1.jpg",
                    "material_type": "scroll",
                }
            )

    def test_all_material_types_valid(self) -> None:
        for mt in ["book", "newspaper", "archival_document", "microfilm"]:
            r = GeometryRequest.model_validate(
                {"job_id": "j1", "page_number": 1, "image_uri": "s3://x", "material_type": mt}
            )
            assert r.material_type == mt


# ── PageRegion ─────────────────────────────────────────────────────────────────


class TestPageRegion:
    def test_valid_quadrilateral(self) -> None:
        r = _page_region()
        assert r.geometry_type == "quadrilateral"
        assert len(r.corners) == 4  # type: ignore[arg-type]

    def test_valid_bbox_geometry(self) -> None:
        r = _page_region(geometry_type="bbox", corners=None)
        assert r.geometry_type == "bbox"
        assert r.corners is None

    def test_valid_mask_ref(self) -> None:
        r = _page_region(geometry_type="mask_ref", corners=None)
        assert r.geometry_type == "mask_ref"

    def test_quadrilateral_missing_corners_rejected(self) -> None:
        # Bypass helper (which auto-fills corners) to hit the model validator directly
        with pytest.raises(ValidationError):
            PageRegion(
                region_id="page_0",
                geometry_type="quadrilateral",
                corners=None,
                bbox=(0, 0, 1200, 1600),
                confidence=0.9,
                page_area_fraction=0.75,
            )

    def test_quadrilateral_wrong_corner_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _page_region(
                geometry_type="quadrilateral",
                corners=[(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)],  # only 3
            )

    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _page_region(confidence=1.01)

    def test_confidence_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _page_region(confidence=-0.1)

    def test_page_area_fraction_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _page_region(page_area_fraction=1.01)

    def test_invalid_geometry_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PageRegion.model_validate(
                {
                    "region_id": "page_0",
                    "geometry_type": "polygon",
                    "corners": None,
                    "bbox": (0, 0, 100, 200),
                    "confidence": 0.9,
                    "page_area_fraction": 0.8,
                }
            )


# ── GeometryResponse ───────────────────────────────────────────────────────────


class TestGeometryResponse:
    def test_valid_single_page(self) -> None:
        r = _geometry_response(page_count=1)
        assert r.page_count == 1
        assert len(r.pages) == 1
        assert not r.split_required
        assert r.split_x is None

    def test_valid_two_page_spread(self) -> None:
        r = _geometry_response(page_count=2)
        assert r.page_count == 2
        assert len(r.pages) == 2
        assert r.split_required
        assert r.split_x == 600

    def test_pages_count_mismatch_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeometryResponse(
                page_count=2,
                pages=[_page_region()],  # only 1 page for page_count=2
                split_required=True,
                split_x=600,
                geometry_confidence=0.9,
                tta_structural_agreement_rate=0.95,
                tta_prediction_variance=0.02,
                tta_passes=5,
                uncertainty_flags=[],
                warnings=[],
                processing_time_ms=100.0,
            )

    def test_page_count_above_two_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeometryResponse(
                page_count=3,
                pages=[_page_region(), _page_region("page_1"), _page_region("page_2")],
                split_required=True,
                split_x=None,
                geometry_confidence=0.9,
                tta_structural_agreement_rate=0.95,
                tta_prediction_variance=0.02,
                tta_passes=5,
                uncertainty_flags=[],
                warnings=[],
                processing_time_ms=100.0,
            )

    def test_negative_split_x_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeometryResponse(
                page_count=2,
                pages=[_page_region(), _page_region("page_1")],
                split_required=True,
                split_x=-1,
                geometry_confidence=0.9,
                tta_structural_agreement_rate=0.95,
                tta_prediction_variance=0.02,
                tta_passes=5,
                uncertainty_flags=[],
                warnings=[],
                processing_time_ms=100.0,
            )

    def test_tta_passes_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            data = _geometry_response().model_dump()
            data["tta_passes"] = 0
            GeometryResponse(**data)

    def test_tta_prediction_variance_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            data = _geometry_response().model_dump()
            data["tta_prediction_variance"] = -0.01
            GeometryResponse(**data)

    def test_geometry_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            data = _geometry_response().model_dump()
            data["geometry_confidence"] = 1.1
            GeometryResponse(**data)

    def test_negative_processing_time_rejected(self) -> None:
        with pytest.raises(ValidationError):
            data = _geometry_response().model_dump()
            data["processing_time_ms"] = -1.0
            GeometryResponse(**data)


# ── NormalizeRequest ───────────────────────────────────────────────────────────


class TestNormalizeRequest:
    def test_valid_iep1a(self) -> None:
        req = NormalizeRequest(
            job_id="j1",
            page_number=1,
            image_uri="s3://bucket/otiff/1.tiff",
            material_type="book",
            selected_geometry=_geometry_response(),
            source_model="iep1a",
        )
        assert req.source_model == "iep1a"

    def test_valid_iep1b(self) -> None:
        req = NormalizeRequest(
            job_id="j1",
            page_number=2,
            image_uri="s3://bucket/otiff/2.tiff",
            material_type="newspaper",
            selected_geometry=_geometry_response(),
            source_model="iep1b",
        )
        assert req.source_model == "iep1b"

    def test_invalid_source_model_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NormalizeRequest.model_validate(
                {
                    "job_id": "j1",
                    "page_number": 1,
                    "image_uri": "s3://x",
                    "material_type": "book",
                    "selected_geometry": _geometry_response().model_dump(),
                    "source_model": "iep1c",
                }
            )

    def test_page_number_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NormalizeRequest(
                job_id="j1",
                page_number=0,
                image_uri="s3://x",
                material_type="book",
                selected_geometry=_geometry_response(),
                source_model="iep1a",
            )


# ── RectifyRequest ─────────────────────────────────────────────────────────────


class TestRectifyRequest:
    def test_valid(self) -> None:
        r = RectifyRequest(
            job_id="j1",
            page_number=3,
            image_uri="s3://bucket/normalized/3.tiff",
            material_type="archival_document",
        )
        assert r.material_type == "archival_document"

    def test_page_number_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RectifyRequest(
                job_id="j1",
                page_number=0,
                image_uri="s3://x",
                material_type="book",
            )

    def test_invalid_material_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RectifyRequest.model_validate(
                {"job_id": "j1", "page_number": 1, "image_uri": "s3://x", "material_type": "doc"}
            )


# ── RectifyResponse ────────────────────────────────────────────────────────────


class TestRectifyResponse:
    def test_valid(self) -> None:
        r = RectifyResponse(
            rectified_image_uri="s3://bucket/rectified/1.tiff",
            rectification_confidence=0.91,
            skew_residual_before=2.3,
            skew_residual_after=0.1,
            border_score_before=0.6,
            border_score_after=0.92,
            processing_time_ms=310.0,
            warnings=[],
        )
        assert r.rectification_confidence == 0.91

    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RectifyResponse(
                rectified_image_uri="s3://x",
                rectification_confidence=1.01,
                skew_residual_before=1.0,
                skew_residual_after=0.1,
                border_score_before=0.7,
                border_score_after=0.9,
                processing_time_ms=100.0,
                warnings=[],
            )

    def test_negative_skew_before_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RectifyResponse(
                rectified_image_uri="s3://x",
                rectification_confidence=0.9,
                skew_residual_before=-0.1,
                skew_residual_after=0.0,
                border_score_before=0.7,
                border_score_after=0.9,
                processing_time_ms=100.0,
                warnings=[],
            )

    def test_border_score_after_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RectifyResponse(
                rectified_image_uri="s3://x",
                rectification_confidence=0.9,
                skew_residual_before=1.0,
                skew_residual_after=0.1,
                border_score_before=0.7,
                border_score_after=1.5,
                processing_time_ms=100.0,
                warnings=[],
            )


# ── RegionType ─────────────────────────────────────────────────────────────────


class TestRegionType:
    def test_all_values(self) -> None:
        expected = {"text_block", "title", "table", "image", "caption"}
        actual = {rt.value for rt in RegionType}
        assert actual == expected

    def test_str_subclass(self) -> None:
        assert isinstance(RegionType.text_block, str)
        assert RegionType.title == "title"


# ── Region ─────────────────────────────────────────────────────────────────────


class TestRegion:
    def test_valid(self) -> None:
        r = _region()
        assert r.id == "r1"
        assert r.type == RegionType.text_block

    def test_valid_ids(self) -> None:
        for rid in ["r1", "r2", "r10", "r999"]:
            r = Region(id=rid, type=RegionType.title, bbox=_layout_bbox(), confidence=0.8)
            assert r.id == rid

    def test_invalid_id_no_prefix_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Region(id="1", type=RegionType.text_block, bbox=_layout_bbox(), confidence=0.8)

    def test_invalid_id_wrong_format_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Region(id="region_1", type=RegionType.text_block, bbox=_layout_bbox(), confidence=0.8)

    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Region(id="r1", type=RegionType.text_block, bbox=_layout_bbox(), confidence=1.1)

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Region.model_validate(
                {
                    "id": "r1",
                    "type": "advertisement",
                    "bbox": {"x_min": 0, "y_min": 0, "x_max": 100, "y_max": 50},
                    "confidence": 0.8,
                }
            )


# ── LayoutConfSummary ──────────────────────────────────────────────────────────


class TestLayoutConfSummary:
    def test_valid(self) -> None:
        s = LayoutConfSummary(mean_conf=0.85, low_conf_frac=0.1)
        assert s.mean_conf == 0.85

    def test_mean_conf_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LayoutConfSummary(mean_conf=1.1, low_conf_frac=0.0)

    def test_low_conf_frac_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LayoutConfSummary(mean_conf=0.8, low_conf_frac=-0.01)


# ── ColumnStructure ────────────────────────────────────────────────────────────


class TestColumnStructure:
    def test_single_column(self) -> None:
        cs = ColumnStructure(column_count=1, column_boundaries=[])
        assert cs.column_count == 1
        assert cs.column_boundaries == []

    def test_two_columns(self) -> None:
        cs = ColumnStructure(column_count=2, column_boundaries=[0.5])
        assert cs.column_boundaries == [0.5]

    def test_three_columns(self) -> None:
        cs = ColumnStructure(column_count=3, column_boundaries=[0.33, 0.66])
        assert len(cs.column_boundaries) == 2

    def test_wrong_boundary_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStructure(column_count=2, column_boundaries=[0.3, 0.6])  # needs 1, got 2

    def test_boundary_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStructure(column_count=2, column_boundaries=[1.1])

    def test_boundary_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStructure(column_count=2, column_boundaries=[-0.1])

    def test_boundaries_not_sorted_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStructure(column_count=3, column_boundaries=[0.6, 0.3])

    def test_boundaries_equal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStructure(column_count=3, column_boundaries=[0.5, 0.5])

    def test_column_count_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStructure(column_count=0, column_boundaries=[])


# ── LayoutDetectRequest ────────────────────────────────────────────────────────


class TestLayoutDetectRequest:
    def test_valid(self) -> None:
        r = LayoutDetectRequest(
            job_id="j1",
            page_number=5,
            image_uri="s3://bucket/ptiff/5.tiff",
            material_type="newspaper",
        )
        assert r.page_number == 5

    def test_page_number_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LayoutDetectRequest(
                job_id="j1", page_number=0, image_uri="s3://x", material_type="book"
            )


# ── LayoutDetectResponse ───────────────────────────────────────────────────────


class TestLayoutDetectResponse:
    def test_valid_detectron2(self) -> None:
        r = _layout_response("detectron2")
        assert r.detector_type == "detectron2"
        assert r.region_schema_version == "v1"

    def test_valid_doclayout_yolo(self) -> None:
        r = _layout_response("doclayout_yolo")
        assert r.detector_type == "doclayout_yolo"

    def test_invalid_detector_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _layout_response("yolov8")

    def test_negative_histogram_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LayoutDetectResponse.model_validate(
                {
                    "region_schema_version": "v1",
                    "regions": [],
                    "layout_conf_summary": {"mean_conf": 0.0, "low_conf_frac": 0.0},
                    "region_type_histogram": {"text_block": -1},
                    "column_structure": None,
                    "model_version": "v1.0.0",
                    "detector_type": "detectron2",
                    "processing_time_ms": 100.0,
                    "warnings": [],
                }
            )

    def test_with_column_structure(self) -> None:
        r = LayoutDetectResponse.model_validate(
            {
                "region_schema_version": "v1",
                "regions": [],
                "layout_conf_summary": {"mean_conf": 0.85, "low_conf_frac": 0.05},
                "region_type_histogram": {},
                "column_structure": {"column_count": 2, "column_boundaries": [0.5]},
                "model_version": "v1.0.0",
                "detector_type": "doclayout_yolo",
                "processing_time_ms": 150.0,
                "warnings": [],
            }
        )
        assert r.column_structure is not None
        assert r.column_structure.column_count == 2

    def test_negative_processing_time_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LayoutDetectResponse.model_validate(
                {
                    "region_schema_version": "v1",
                    "regions": [],
                    "layout_conf_summary": {"mean_conf": 0.8, "low_conf_frac": 0.0},
                    "region_type_histogram": {},
                    "column_structure": None,
                    "model_version": "v1.0.0",
                    "detector_type": "detectron2",
                    "processing_time_ms": -1.0,
                    "warnings": [],
                }
            )


# ── LayoutConsensusResult ──────────────────────────────────────────────────────


class TestLayoutConsensusResult:
    def test_valid_agreed(self) -> None:
        r = LayoutConsensusResult(
            iep2a_region_count=5,
            iep2b_region_count=5,
            matched_regions=4,
            unmatched_iep2a=1,
            unmatched_iep2b=1,
            mean_matched_iou=0.78,
            type_histogram_match=True,
            agreed=True,
            consensus_confidence=0.82,
            single_model_mode=False,
        )
        assert r.agreed is True

    def test_valid_disagreed_single_model_fallback(self) -> None:
        r = LayoutConsensusResult(
            iep2a_region_count=5,
            iep2b_region_count=0,
            matched_regions=0,
            unmatched_iep2a=5,
            unmatched_iep2b=0,
            mean_matched_iou=0.0,
            type_histogram_match=False,
            agreed=False,
            consensus_confidence=0.0,
            single_model_mode=True,
        )
        assert r.agreed is False

    def test_roundtrip_serialization(self) -> None:
        r = LayoutConsensusResult(
            iep2a_region_count=3,
            iep2b_region_count=3,
            matched_regions=3,
            unmatched_iep2a=0,
            unmatched_iep2b=0,
            mean_matched_iou=0.85,
            type_histogram_match=True,
            agreed=True,
            consensus_confidence=0.87,
            single_model_mode=False,
        )
        dumped = r.model_dump()
        restored = LayoutConsensusResult(**dumped)
        assert restored.agreed == r.agreed
        assert restored.consensus_confidence == r.consensus_confidence
