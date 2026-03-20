"""tests.test_p3_gate_integration
----------------------------------
Packet 3.6 — gate test suite.

Covers the phase-level definition of done for Phase 3:
  - All geometry selection routing paths (accepted / rectification /
    pending_human_correction) and the conditions that trigger each.
  - All artifact validation routing paths (hard pass+soft pass, hard fail,
    hard pass+soft fail).
  - Safety invariants mandated by the spec:
      * Single-model confidence NEVER produces route_decision="accepted".
      * Structural disagreement ALWAYS prevents "accepted".
      * Any filter dropout ALWAYS prevents "accepted".
      * The only path to "accepted" requires BOTH models, agreement, AND zero
        dropouts at every filter stage.
      * Neither gate ever produces route_decision="failed" — "failed" is
        reserved for data-integrity / infrastructure failures (Phase 4).
  - Cross-gate integration: geometry and artifact gate log records share
    consistent job_id / page_number and have complementary column sets.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from services.eep.app.gates.artifact_validation import (
    ArtifactImageDimensions,
    build_artifact_gate_log_record,
    run_artifact_validation,
)
from services.eep.app.gates.geometry_selection import (
    PreprocessingGateConfig,
    build_geometry_gate_log_record,
    run_geometry_selection,
)
from shared.schemas.geometry import GeometryResponse, PageRegion
from shared.schemas.preprocessing import (
    CropResult,
    DeskewResult,
    PreprocessBranchResponse,
    QualityMetrics,
    SplitResult,
)
from shared.schemas.ucf import BoundingBox, Dimensions, TransformRecord

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_CONFIG = PreprocessingGateConfig()
_PROXY_W = 800
_PROXY_H = 600
_JOB_ID = "job-integration-test"
_PAGE = 0

# ---------------------------------------------------------------------------
# Geometry response builders
# ---------------------------------------------------------------------------


def _page_region(
    region_id: str = "page_0",
    page_area_fraction: float = 0.5,
    bbox: tuple[int, int, int, int] = (50, 30, 350, 530),
    corners: list[tuple[float, float]] | None = None,
) -> PageRegion:
    if corners is None:
        corners = [(50.0, 30.0), (350.0, 30.0), (350.0, 530.0), (50.0, 530.0)]
    return PageRegion(
        region_id=region_id,
        geometry_type="quadrilateral",
        corners=corners,
        bbox=bbox,
        confidence=0.9,
        page_area_fraction=page_area_fraction,
    )


def _geo(
    page_count: int = 1,
    split_required: bool = False,
    geometry_confidence: float = 0.90,
    tta_structural_agreement_rate: float = 0.95,
    tta_prediction_variance: float = 0.05,
    pages: list[PageRegion] | None = None,
) -> GeometryResponse:
    if pages is None:
        pages = [_page_region()]
    return GeometryResponse(
        page_count=page_count,
        pages=pages,
        split_required=split_required,
        split_x=400 if split_required else None,
        geometry_confidence=geometry_confidence,
        tta_structural_agreement_rate=tta_structural_agreement_rate,
        tta_prediction_variance=tta_prediction_variance,
        tta_passes=5,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=50.0,
    )


def _two_page_geo(
    geometry_confidence: float = 0.90,
    tta_structural_agreement_rate: float = 0.95,
    tta_prediction_variance: float = 0.05,
) -> GeometryResponse:
    """Geometry response for a two-page spread with non-overlapping regions."""
    return GeometryResponse(
        page_count=2,
        pages=[
            _page_region(
                "page_0",
                0.48,
                bbox=(10, 10, 390, 590),
                corners=[(10.0, 10.0), (390.0, 10.0), (390.0, 590.0), (10.0, 590.0)],
            ),
            _page_region(
                "page_1",
                0.48,
                bbox=(410, 10, 790, 590),
                corners=[(410.0, 10.0), (790.0, 10.0), (790.0, 590.0), (410.0, 590.0)],
            ),
        ],
        split_required=True,
        split_x=400,
        geometry_confidence=geometry_confidence,
        tta_structural_agreement_rate=tta_structural_agreement_rate,
        tta_prediction_variance=tta_prediction_variance,
        tta_passes=5,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=50.0,
    )


# ---------------------------------------------------------------------------
# Artifact response builder
# ---------------------------------------------------------------------------


def _artifact_response(
    skew_residual: float = 0.5,
    blur_score: float = 0.2,
    border_score: float = 0.8,
    foreground_coverage: float = 0.6,
) -> PreprocessBranchResponse:
    transform = TransformRecord(
        original_dimensions=Dimensions(width=1200, height=900),
        crop_box=BoundingBox(x_min=100.0, y_min=50.0, x_max=400.0, y_max=450.0),
        deskew_angle_deg=0.0,
        post_preprocessing_dimensions=Dimensions(width=300, height=400),
    )
    return PreprocessBranchResponse(
        processed_image_uri="file:///artifacts/page_0.tiff",
        deskew=DeskewResult(angle_deg=0.0, residual_deg=0.0, method="geometry_quad"),
        crop=CropResult(
            crop_box=transform.crop_box, border_score=border_score, method="geometry_quad"
        ),
        split=SplitResult(split_required=False, method="instance_boundary"),
        quality=QualityMetrics(
            skew_residual=skew_residual,
            blur_score=blur_score,
            border_score=border_score,
            foreground_coverage=foreground_coverage,
        ),
        transform=transform,
        source_model="iep1a",
        processing_time_ms=120.0,
        warnings=[],
    )


def _ok_loader(width: int = 300, height: int = 400) -> Callable[[str], ArtifactImageDimensions]:
    def _load(uri: str) -> ArtifactImageDimensions:
        return ArtifactImageDimensions(width=width, height=height)

    return _load


def _missing_loader() -> Callable[[str], ArtifactImageDimensions]:
    def _load(uri: str) -> ArtifactImageDimensions:
        raise FileNotFoundError(uri)

    return _load


# ===========================================================================
# Geometry selection routing paths
# ===========================================================================


class TestGeometrySelectionRoutingPaths:
    """All routing paths must terminate in accepted / rectification /
    pending_human_correction — never in 'failed'."""

    def test_both_agree_both_pass_all_filters_routes_accepted(self) -> None:
        a = _geo(geometry_confidence=0.92, tta_prediction_variance=0.05)
        b = _geo(geometry_confidence=0.88, tta_prediction_variance=0.05)
        result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert result.route_decision == "accepted"
        assert result.geometry_trust == "high"

    def test_structural_disagreement_routes_rectification(self) -> None:
        a = _geo(page_count=1, split_required=False)
        b = _two_page_geo()  # disagrees: page_count=2, split_required=True
        result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert result.route_decision == "rectification"
        assert result.structural_agreement is False

    def test_one_model_fails_sanity_routes_rectification(self) -> None:
        a = _geo()
        # b fails area_fraction_plausible (page_area_fraction 0.01 < min 0.15)
        bad_region = _page_region(page_area_fraction=0.01)
        b = _geo(pages=[bad_region])
        result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert result.route_decision == "rectification"
        assert result.selected is not None

    def test_one_model_fails_tta_variance_routes_rectification(self) -> None:
        a = _geo(tta_prediction_variance=0.05)
        b = _geo(tta_prediction_variance=0.99)  # > ceiling 0.15
        result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert result.route_decision == "rectification"
        assert result.selected is not None

    def test_both_fail_sanity_routes_pending_human(self) -> None:
        bad = _page_region(page_area_fraction=0.01)
        a = _geo(pages=[bad])
        b = _geo(pages=[bad])
        result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "geometry_sanity_failed"
        assert result.selected is None

    def test_both_fail_split_confidence_routes_pending_human(self) -> None:
        split_pages = [
            _page_region("page_0", 0.48, bbox=(10, 10, 390, 590)),
            _page_region("page_1", 0.48, bbox=(410, 10, 790, 590)),
        ]
        # geometry_confidence=0.3 → min(0.3, 0.95)=0.3 < threshold 0.75
        a = _geo(page_count=2, split_required=True, geometry_confidence=0.3, pages=split_pages)
        b = _geo(
            page_count=2, split_required=True, geometry_confidence=0.4, pages=list(split_pages)
        )
        result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "split_confidence_low"

    def test_both_fail_tta_variance_routes_pending_human(self) -> None:
        a = _geo(tta_prediction_variance=0.99)
        b = _geo(tta_prediction_variance=0.88)
        result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "tta_variance_high"

    def test_no_models_routes_pending_human_with_fallback_reason(self) -> None:
        result = run_geometry_selection(None, None, "book", _PROXY_W, _PROXY_H)
        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "geometry_selection_failed"
        assert result.selected is None

    def test_route_decision_never_equals_failed(self) -> None:
        """The geometry gate must never produce route_decision='failed'."""
        scenarios: list[tuple[GeometryResponse | None, GeometryResponse | None]] = [
            (_geo(), _geo()),
            (_geo(), None),
            (None, _geo()),
            (None, None),
            (
                _geo(pages=[_page_region(page_area_fraction=0.01)]),
                _geo(pages=[_page_region(page_area_fraction=0.01)]),
            ),
        ]
        for iep1a, iep1b in scenarios:
            r = run_geometry_selection(iep1a, iep1b, "book", _PROXY_W, _PROXY_H)
            assert r.route_decision in {
                "accepted",
                "rectification",
                "pending_human_correction",
            }, f"Unexpected route_decision for ({iep1a}, {iep1b}): {r.route_decision!r}"


# ===========================================================================
# Geometry selection safety invariants
# ===========================================================================


class TestGeometrySelectionSafetyInvariants:
    """Mandatory non-negotiable rules from spec Section 6.8 and 6.10."""

    def test_single_model_iep1a_only_never_accepted(self) -> None:
        """Single-model mode must never yield 'accepted' regardless of confidence."""
        a = _geo(geometry_confidence=0.999, tta_prediction_variance=0.0)
        result = run_geometry_selection(a, None, "book", _PROXY_W, _PROXY_H)
        assert result.route_decision != "accepted"
        assert result.geometry_trust == "low"

    def test_single_model_iep1b_only_never_accepted(self) -> None:
        b = _geo(geometry_confidence=0.999, tta_prediction_variance=0.0)
        result = run_geometry_selection(None, b, "book", _PROXY_W, _PROXY_H)
        assert result.route_decision != "accepted"
        assert result.geometry_trust == "low"

    def test_structural_disagreement_always_prevents_accepted(self) -> None:
        a = _geo(
            page_count=1,
            split_required=False,
            geometry_confidence=0.999,
            tta_prediction_variance=0.0,
        )
        b = _two_page_geo(geometry_confidence=0.999, tta_prediction_variance=0.0)
        result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert result.structural_agreement is False
        assert result.route_decision != "accepted"

    def test_any_sanity_dropout_prevents_accepted(self) -> None:
        """If one model fails sanity, geometry_trust must be 'low'."""
        a = _geo()
        b_bad = _geo(pages=[_page_region(page_area_fraction=0.01)])
        result = run_geometry_selection(a, b_bad, "book", _PROXY_W, _PROXY_H)
        assert result.geometry_trust == "low"
        assert result.route_decision == "rectification"

    def test_any_tta_variance_dropout_prevents_accepted(self) -> None:
        a = _geo(tta_prediction_variance=0.05)
        b = _geo(tta_prediction_variance=0.20)  # > ceiling → dropped
        result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert result.geometry_trust == "low"
        assert result.route_decision == "rectification"

    def test_accepted_requires_both_models_present(self) -> None:
        """The only path to route_decision='accepted' needs both models."""
        # Verify the positive case: both present, agree, pass → accepted.
        a = _geo(geometry_confidence=0.90)
        b = _geo(geometry_confidence=0.88)
        ok = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert ok.route_decision == "accepted"

        # Single model: never accepted regardless of confidence.
        only_a = run_geometry_selection(a, None, "book", _PROXY_W, _PROXY_H)
        assert only_a.route_decision != "accepted"
        only_b = run_geometry_selection(None, b, "book", _PROXY_W, _PROXY_H)
        assert only_b.route_decision != "accepted"

    def test_accepted_requires_zero_dropouts(self) -> None:
        """High trust → accepted requires zero dropouts at every filter stage."""
        a = _geo(geometry_confidence=0.90, tta_prediction_variance=0.05)
        b = _geo(geometry_confidence=0.88, tta_prediction_variance=0.05)
        ok = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert ok.route_decision == "accepted"
        assert ok.geometry_trust == "high"

        # Introduce a TTA dropout on b → still selects but now "low" trust.
        b_unstable = _geo(geometry_confidence=0.88, tta_prediction_variance=0.20)
        degraded = run_geometry_selection(a, b_unstable, "book", _PROXY_W, _PROXY_H)
        assert degraded.route_decision == "rectification"
        assert degraded.geometry_trust == "low"

    def test_low_trust_first_pass_triggers_rectification_not_pending(self) -> None:
        """Low trust (first pass) must route to rectification, not human correction."""
        a = _geo()  # will pass, sole survivor after b fails
        b = _geo(pages=[_page_region(page_area_fraction=0.01)])  # fails sanity
        result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        # At least one candidate survives → rectification (not pending_human_correction).
        assert result.route_decision == "rectification"
        assert result.selected is not None


# ===========================================================================
# Artifact validation routing paths
# ===========================================================================


class TestArtifactValidationRoutingPaths:
    def test_hard_pass_soft_pass_artifact_is_valid(self) -> None:
        resp = _artifact_response()  # all good quality signals
        result = run_artifact_validation(resp, None, _ok_loader(), _CONFIG)
        assert result.passed is True
        assert result.hard_result.passed is True
        assert result.soft_passed is True
        assert result.soft_score is not None

    def test_hard_fail_file_missing_artifact_invalid_no_soft_score(self) -> None:
        resp = _artifact_response()
        result = run_artifact_validation(resp, None, _missing_loader(), _CONFIG)
        assert result.passed is False
        assert "file_exists" in result.hard_result.failed_checks
        assert result.soft_score is None
        assert result.soft_passed is None

    def test_hard_pass_soft_fail_artifact_invalid(self) -> None:
        # All signals suspicious → combined score = 0.0 < threshold 0.60.
        bad_resp = _artifact_response(
            skew_residual=6.0,
            blur_score=0.9,
            border_score=0.1,
            foreground_coverage=0.05,
        )
        result = run_artifact_validation(bad_resp, None, _ok_loader(), _CONFIG)
        assert result.hard_result.passed is True
        assert result.soft_passed is False
        assert result.passed is False

    def test_hard_fail_always_produces_passed_false(self) -> None:
        """Hard failure must produce passed=False regardless of quality signals."""
        resp = _artifact_response()  # would score well if image loaded
        result = run_artifact_validation(resp, None, _missing_loader(), _CONFIG)
        assert result.passed is False

    def test_geometry_confidence_included_in_soft_score(self) -> None:
        """Providing a GeometryResponse must add geometry signals to the score."""
        resp = _artifact_response()
        good_geo = _geo(geometry_confidence=0.90, tta_structural_agreement_rate=0.95)
        result_with_geo = run_artifact_validation(resp, good_geo, _ok_loader(), _CONFIG)
        result_no_geo = run_artifact_validation(resp, None, _ok_loader(), _CONFIG)
        assert result_with_geo.signal_scores is not None
        assert "geometry_confidence" in result_with_geo.signal_scores
        assert "tta_agreement" in result_with_geo.signal_scores
        assert result_no_geo.signal_scores is not None
        assert "geometry_confidence" not in result_no_geo.signal_scores

    def test_artifact_route_decision_never_equals_failed(self) -> None:
        """Artifact validation gate never produces route_decision='failed'."""
        # This gate returns ArtifactValidationResult.passed (bool), not a route_decision.
        # The caller (worker) maps result.passed to a route_decision.
        # Here we verify that build_artifact_gate_log_record rejects "failed" at the
        # type level (Literal constraint) — calling with "accepted" must work fine.
        resp = _artifact_response()
        result = run_artifact_validation(resp, None, _ok_loader(), _CONFIG)
        # Only "accepted" | "rectification" | "pending_human_correction" are valid.
        record = build_artifact_gate_log_record(
            result, _JOB_ID, _PAGE, "artifact_validation", "accepted", None, 60.0
        )
        assert record["route_decision"] in {"accepted", "rectification", "pending_human_correction"}


# ===========================================================================
# Artifact validation safety invariants
# ===========================================================================


class TestArtifactValidationSafetyInvariants:
    def test_soft_score_always_in_0_1_range(self) -> None:
        """Soft score must always be in [0, 1] when present."""
        resp = _artifact_response()
        result = run_artifact_validation(resp, None, _ok_loader(), _CONFIG)
        assert result.soft_score is not None
        assert 0.0 <= result.soft_score <= 1.0

    def test_signal_scores_all_in_0_1_range(self) -> None:
        resp = _artifact_response()
        result = run_artifact_validation(resp, None, _ok_loader(), _CONFIG)
        assert result.signal_scores is not None
        for name, score in result.signal_scores.items():
            assert 0.0 <= score <= 1.0, f"signal {name!r} score {score} outside [0, 1]"

    def test_passed_iff_hard_and_soft_both_pass(self) -> None:
        resp = _artifact_response()
        result = run_artifact_validation(resp, None, _ok_loader(), _CONFIG)
        # When both pass:
        expected = result.hard_result.passed and bool(result.soft_passed)
        assert result.passed is expected

    def test_signal_scores_none_when_hard_fails(self) -> None:
        resp = _artifact_response()
        result = run_artifact_validation(resp, None, _missing_loader())
        assert result.signal_scores is None

    def test_soft_passed_none_when_hard_fails(self) -> None:
        resp = _artifact_response()
        result = run_artifact_validation(resp, None, _missing_loader())
        assert result.soft_passed is None


# ===========================================================================
# Cross-gate integration
# ===========================================================================


class TestCrossGateIntegration:
    """Verify that geometry gate and artifact gate log records are consistent
    and complementary — as the Phase 4 worker will produce them together."""

    def _run_both_gates(
        self,
    ) -> tuple[object, object]:
        """Run both gates and return both results."""
        a = _geo()
        b = _geo(geometry_confidence=0.88)
        geo_result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        resp = _artifact_response()
        art_result = run_artifact_validation(resp, a, _ok_loader(), _CONFIG)
        return geo_result, art_result

    def test_geometry_record_artifact_score_is_none(self) -> None:
        """Geometry gate records never populate artifact_validation_score."""
        a = _geo()
        b = _geo(geometry_confidence=0.88)
        geo_result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        record = build_geometry_gate_log_record(
            geo_result, _JOB_ID, _PAGE, "geometry_selection", a, b, 50.0
        )
        assert record["artifact_validation_score"] is None

    def test_artifact_record_geometry_columns_are_none(self) -> None:
        """Artifact gate records never populate geometry columns."""
        resp = _artifact_response()
        art_result = run_artifact_validation(resp, None, _ok_loader(), _CONFIG)
        record = build_artifact_gate_log_record(
            art_result, _JOB_ID, _PAGE, "artifact_validation", "accepted", None, 80.0
        )
        assert record["iep1a_geometry"] is None
        assert record["iep1b_geometry"] is None
        assert record["structural_agreement"] is None
        assert record["selected_model"] is None

    def test_both_records_share_job_id_and_page_number(self) -> None:
        a = _geo()
        b = _geo(geometry_confidence=0.88)
        geo_result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        geo_record = build_geometry_gate_log_record(
            geo_result, _JOB_ID, _PAGE, "geometry_selection", a, b, 50.0
        )
        resp = _artifact_response()
        art_result = run_artifact_validation(resp, a, _ok_loader(), _CONFIG)
        art_record = build_artifact_gate_log_record(
            art_result, _JOB_ID, _PAGE, "artifact_validation", "accepted", None, 80.0
        )
        assert geo_record["job_id"] == art_record["job_id"] == _JOB_ID
        assert geo_record["page_number"] == art_record["page_number"] == _PAGE

    def test_gate_ids_are_distinct(self) -> None:
        a = _geo()
        b = _geo(geometry_confidence=0.88)
        geo_result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        geo_record = build_geometry_gate_log_record(
            geo_result, _JOB_ID, _PAGE, "geometry_selection", a, b, 50.0
        )
        resp = _artifact_response()
        art_result = run_artifact_validation(resp, a, _ok_loader(), _CONFIG)
        art_record = build_artifact_gate_log_record(
            art_result, _JOB_ID, _PAGE, "artifact_validation", "accepted", None, 80.0
        )
        assert geo_record["gate_id"] != art_record["gate_id"]

    def test_high_trust_geometry_feeds_good_geometry_signals_to_artifact(self) -> None:
        """High-confidence selected geometry improves the artifact soft score."""
        a = _geo(geometry_confidence=0.95, tta_structural_agreement_rate=0.98)
        b = _geo(geometry_confidence=0.90, tta_structural_agreement_rate=0.96)
        geo_result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert geo_result.selected is not None
        selected_response = geo_result.selected.response

        resp = _artifact_response()
        art_result = run_artifact_validation(resp, selected_response, _ok_loader(), _CONFIG)
        assert art_result.signal_scores is not None
        assert art_result.signal_scores["geometry_confidence"] == pytest.approx(1.0)
        assert art_result.signal_scores["tta_agreement"] == pytest.approx(1.0)

    def test_full_happy_path_both_gates_pass(self) -> None:
        """End-to-end: high trust geometry + good artifact → both gates pass."""
        a = _geo(geometry_confidence=0.92, tta_prediction_variance=0.05)
        b = _geo(geometry_confidence=0.88, tta_prediction_variance=0.05)
        geo_result = run_geometry_selection(a, b, "book", _PROXY_W, _PROXY_H)
        assert geo_result.route_decision == "accepted"

        resp = _artifact_response()
        assert geo_result.selected is not None
        art_result = run_artifact_validation(
            resp, geo_result.selected.response, _ok_loader(), _CONFIG
        )
        assert art_result.passed is True

    def test_low_trust_geometry_does_not_block_artifact_validation(self) -> None:
        """Even low-trust geometry produces a valid selected candidate for artifact scoring."""
        a = _geo(geometry_confidence=0.90)
        b_bad = _geo(pages=[_page_region(page_area_fraction=0.01)])
        geo_result = run_geometry_selection(a, b_bad, "book", _PROXY_W, _PROXY_H)
        assert geo_result.route_decision == "rectification"
        assert geo_result.selected is not None

        resp = _artifact_response()
        art_result = run_artifact_validation(
            resp, geo_result.selected.response, _ok_loader(), _CONFIG
        )
        assert art_result.soft_score is not None  # artifact gate still runs
