"""
tests/test_p4_split_step.py
-----------------------------
Packet 4.6 — split normalization and PTIFF QA routing tests.

Covers:
  TestDecidePtiffQaRoute (5 tests):
    1. manual → ptiff_qa_pending
    2. auto_continue + preprocess → accepted, routing_path="preprocessing_only"
    3. auto_continue + layout → layout_detection
    4. Unknown ptiff_qa_mode → ValueError
    5. Unknown pipeline_mode with auto_continue → ValueError

  TestRunSplitNormalization (12 tests):
    Happy-path / both-pass:
      1. Both children pass first-pass → accept_now, used_rescue=False
      2. duration_ms >= 0.0 populated
      3. branch_response and validation_result populated for both accepted children

    One child needs rescue (left):
      4. Left fails first-pass → rescue triggered with is_split_child=True, page_index=0
      5. Left rescue succeeds → left route="accept_now", used_rescue=True
      6. Left rescue fails → left route="pending_human_correction", used_rescue=True

    One child needs rescue (right):
      7. Right fails first-pass → rescue triggered with is_split_child=True, page_index=1
      8. Right rescue succeeds → right route="accept_now", used_rescue=True

    Both fail:
      9.  Both children fail first-pass → both go through rescue
      10. Both rescues fail → both route="pending_human_correction"

    sub_page_index correctness:
      11. left.sub_page_index==0, right.sub_page_index==1 always

    Rescue independence:
      12. Left rescue failing does NOT affect right child processing
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from services.eep.app.gates.artifact_validation import (
    ArtifactHardCheckResult,
    ArtifactValidationResult,
)
from services.eep_worker.app.circuit_breaker import CircuitBreaker
from services.eep_worker.app.normalization_step import NormalizationOutcome
from services.eep_worker.app.rescue_step import RescueOutcome
from services.eep_worker.app.split_step import (
    SplitOutcome,
    decide_ptiff_qa_route,
    run_split_normalization,
)
from shared.schemas.geometry import GeometryResponse, PageRegion
from shared.schemas.preprocessing import PreprocessBranchResponse

# ── Constants ───────────────────────────────────────────────────────────────────

_LEFT_URI = "s3://bucket/left.tiff"
_RIGHT_URI = "s3://bucket/right.tiff"
_LEFT_RESCUE_URI = "s3://bucket/left_rescue.tiff"
_RIGHT_RESCUE_URI = "s3://bucket/right_rescue.tiff"
_LEFT_PROXY_URI = "s3://bucket/left_rectified_proxy.png"
_RIGHT_PROXY_URI = "s3://bucket/right_rectified_proxy.png"

_IEP1D_ENDPOINT = "http://iep1d:8003/v1/rectify"
_IEP1A_ENDPOINT = "http://iep1a:8001/v1/geometry"
_IEP1B_ENDPOINT = "http://iep1b:8002/v1/geometry"


# ── Test helpers ────────────────────────────────────────────────────────────────


def _make_geometry(split_required: bool = True) -> GeometryResponse:
    """Build a GeometryResponse with two pages (split scan)."""
    pages = [
        PageRegion(
            region_id=f"page_{i}",
            geometry_type="bbox",
            bbox=(10, 10, 90, 90),
            corners=None,
            confidence=0.92,
            page_area_fraction=0.80,
        )
        for i in range(2)
    ]
    return GeometryResponse(
        page_count=2,
        pages=pages,
        split_required=split_required,
        split_x=150,
        geometry_confidence=0.92,
        tta_structural_agreement_rate=0.95,
        tta_prediction_variance=0.01,
        tta_passes=3,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=80.0,
    )


def _make_validation(passed: bool = True) -> ArtifactValidationResult:
    hard = ArtifactHardCheckResult(
        passed=passed,
        failed_checks=[] if passed else ["dimensions_consistent"],
    )
    return ArtifactValidationResult(
        hard_result=hard,
        soft_score=0.85 if passed else None,
        signal_scores=None,
        soft_passed=passed or None,
        passed=passed,
    )


def _make_norm_outcome(route: str = "accept_now") -> NormalizationOutcome:
    branch = MagicMock(spec=PreprocessBranchResponse)
    branch.processed_image_uri = _LEFT_URI
    validation = _make_validation(passed=(route == "accept_now"))
    return NormalizationOutcome(
        branch_response=branch,
        validation_result=validation,
        route=route,  # type: ignore[arg-type]
        duration_ms=50.0,
    )


def _make_rescue_outcome(
    route: str = "accept_now",
    reason: str | None = None,
) -> RescueOutcome:
    branch = MagicMock(spec=PreprocessBranchResponse) if route == "accept_now" else None
    validation = _make_validation(passed=(route == "accept_now")) if route == "accept_now" else None
    return RescueOutcome(
        route=route,  # type: ignore[arg-type]
        review_reason=reason,
        branch_response=branch,
        validation_result=validation,
        rectify_response=None,
        second_selection_result=None,
        duration_ms=100.0,
    )


def _make_cbs() -> tuple[CircuitBreaker, CircuitBreaker, CircuitBreaker]:
    return (
        CircuitBreaker("iep1a"),
        CircuitBreaker("iep1b"),
        CircuitBreaker("iep1d"),
    )


def _make_image() -> np.ndarray:
    return np.full((400, 600, 3), 128, dtype=np.uint8)


async def _run_split(
    norm_side_effects: list[NormalizationOutcome],
    rescue_side_effects: list[RescueOutcome] | None = None,
    iep1d_execution_timeout_seconds: float | None = None,
) -> SplitOutcome:
    """
    Run run_split_normalization with mocked normalization and rescue flows.

    norm_side_effects: list of NormalizationOutcome (left then right).
    rescue_side_effects: optional list of RescueOutcome for rescue calls.
    """
    cb_a, cb_b, cb_d = _make_cbs()

    with (
        patch(
            "services.eep_worker.app.split_step.run_normalization_and_first_validation",
            side_effect=norm_side_effects,
        ) as mock_norm,
        patch(
            "services.eep_worker.app.split_step.run_rescue_flow",
            new_callable=AsyncMock,
            side_effect=rescue_side_effects or [],
        ) as mock_rescue,
    ):
        result = await run_split_normalization(
            full_res_image=_make_image(),
            selected_geometry=_make_geometry(),
            selected_model="iep1a",
            proxy_width=300,
            proxy_height=200,
            left_output_uri=_LEFT_URI,
            right_output_uri=_RIGHT_URI,
            left_rescue_output_uri=_LEFT_RESCUE_URI,
            right_rescue_output_uri=_RIGHT_RESCUE_URI,
            left_rectified_proxy_uri=_LEFT_PROXY_URI,
            right_rectified_proxy_uri=_RIGHT_PROXY_URI,
            storage=MagicMock(),
            image_loader=MagicMock(),
            job_id="job-123",
            page_number=1,
            lineage_id="lineage-abc",
            material_type="book",
            iep1d_endpoint=_IEP1D_ENDPOINT,
            iep1a_endpoint=_IEP1A_ENDPOINT,
            iep1b_endpoint=_IEP1B_ENDPOINT,
            iep1d_circuit_breaker=cb_d,
            iep1a_circuit_breaker=cb_a,
            iep1b_circuit_breaker=cb_b,
            backend=MagicMock(),
            session=MagicMock(),
            iep1d_execution_timeout_seconds=iep1d_execution_timeout_seconds,
        )
        # Make mocks accessible for assertion via result metadata trick
        result._mock_norm = mock_norm  # type: ignore[attr-defined]
        result._mock_rescue = mock_rescue  # type: ignore[attr-defined]
    return result


# ── TestDecidePtiffQaRoute ──────────────────────────────────────────────────────


class TestDecidePtiffQaRoute:
    def test_manual_mode(self) -> None:
        route = decide_ptiff_qa_route(pipeline_mode="layout", ptiff_qa_mode="manual")
        assert route.next_status == "ptiff_qa_pending"
        assert route.routing_path is None

    def test_auto_continue_preprocess(self) -> None:
        route = decide_ptiff_qa_route(pipeline_mode="preprocess", ptiff_qa_mode="auto_continue")
        assert route.next_status == "accepted"
        assert route.routing_path == "preprocessing_only"

    def test_auto_continue_layout(self) -> None:
        route = decide_ptiff_qa_route(pipeline_mode="layout", ptiff_qa_mode="auto_continue")
        assert route.next_status == "layout_detection"
        assert route.routing_path is None

    def test_unknown_ptiff_qa_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="ptiff_qa_mode"):
            decide_ptiff_qa_route(pipeline_mode="layout", ptiff_qa_mode="unknown_mode")

    def test_auto_continue_unknown_pipeline_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="pipeline_mode"):
            decide_ptiff_qa_route(pipeline_mode="bad_mode", ptiff_qa_mode="auto_continue")


# ── TestRunSplitNormalization ───────────────────────────────────────────────────


class TestRunSplitNormalization:
    @pytest.mark.asyncio
    async def test_both_pass_first_pass_accept_now(self) -> None:
        """Both children pass → both route='accept_now', used_rescue=False."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("accept_now"),
                _make_norm_outcome("accept_now"),
            ]
        )
        assert result.left.route == "accept_now"
        assert result.right.route == "accept_now"
        assert result.left.used_rescue is False
        assert result.right.used_rescue is False

    @pytest.mark.asyncio
    async def test_duration_ms_populated(self) -> None:
        """duration_ms is a non-negative float."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("accept_now"),
                _make_norm_outcome("accept_now"),
            ]
        )
        assert result.duration_ms >= 0.0

    @pytest.mark.asyncio
    async def test_branch_response_and_validation_populated_on_accept(self) -> None:
        """branch_response and validation_result are set for both accepted children."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("accept_now"),
                _make_norm_outcome("accept_now"),
            ]
        )
        assert result.left.branch_response is not None
        assert result.left.validation_result is not None
        assert result.right.branch_response is not None
        assert result.right.validation_result is not None

    @pytest.mark.asyncio
    async def test_left_fails_triggers_rescue_with_correct_args(self) -> None:
        """Left first-pass fails → rescue invoked with is_split_child=True, page_index=0."""
        cb_a, cb_b, cb_d = _make_cbs()

        with (
            patch(
                "services.eep_worker.app.split_step.run_normalization_and_first_validation",
                side_effect=[
                    _make_norm_outcome("rescue_required"),
                    _make_norm_outcome("accept_now"),
                ],
            ),
            patch(
                "services.eep_worker.app.split_step.run_rescue_flow",
                new_callable=AsyncMock,
                return_value=_make_rescue_outcome("accept_now"),
            ) as mock_rescue,
        ):
            await run_split_normalization(
                full_res_image=_make_image(),
                selected_geometry=_make_geometry(),
                selected_model="iep1a",
                proxy_width=300,
                proxy_height=200,
                left_output_uri=_LEFT_URI,
                right_output_uri=_RIGHT_URI,
                left_rescue_output_uri=_LEFT_RESCUE_URI,
                right_rescue_output_uri=_RIGHT_RESCUE_URI,
                left_rectified_proxy_uri=_LEFT_PROXY_URI,
                right_rectified_proxy_uri=_RIGHT_PROXY_URI,
                storage=MagicMock(),
                image_loader=MagicMock(),
                job_id="job-123",
                page_number=1,
                lineage_id="lineage-abc",
                material_type="book",
                iep1d_endpoint=_IEP1D_ENDPOINT,
                iep1a_endpoint=_IEP1A_ENDPOINT,
                iep1b_endpoint=_IEP1B_ENDPOINT,
                iep1d_circuit_breaker=cb_d,
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=MagicMock(),
                session=MagicMock(),
                iep1d_execution_timeout_seconds=240.0,
            )

        mock_rescue.assert_called_once()
        call_kwargs = mock_rescue.call_args.kwargs
        assert call_kwargs["is_split_child"] is True
        assert call_kwargs["page_index"] == 0
        assert call_kwargs["artifact_uri"] == _LEFT_URI
        assert call_kwargs["rescue_output_uri"] == _LEFT_RESCUE_URI
        assert call_kwargs["rectified_proxy_uri"] == _LEFT_PROXY_URI
        assert call_kwargs["iep1d_execution_timeout_seconds"] == 240.0

    @pytest.mark.asyncio
    async def test_left_rescue_succeeds(self) -> None:
        """Left rescue accepts → left route='accept_now', used_rescue=True."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("rescue_required"),
                _make_norm_outcome("accept_now"),
            ],
            rescue_side_effects=[_make_rescue_outcome("accept_now")],
        )
        assert result.left.route == "accept_now"
        assert result.left.used_rescue is True

    @pytest.mark.asyncio
    async def test_left_rescue_fails(self) -> None:
        """Left rescue fails → left route='pending_human_correction', used_rescue=True."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("rescue_required"),
                _make_norm_outcome("accept_now"),
            ],
            rescue_side_effects=[
                _make_rescue_outcome("pending_human_correction", "artifact_validation_failed")
            ],
        )
        assert result.left.route == "pending_human_correction"
        assert result.left.review_reason == "artifact_validation_failed"
        assert result.left.used_rescue is True

    @pytest.mark.asyncio
    async def test_right_fails_triggers_rescue_with_correct_args(self) -> None:
        """Right first-pass fails → rescue invoked with is_split_child=True, page_index=1."""
        cb_a, cb_b, cb_d = _make_cbs()

        with (
            patch(
                "services.eep_worker.app.split_step.run_normalization_and_first_validation",
                side_effect=[
                    _make_norm_outcome("accept_now"),
                    _make_norm_outcome("rescue_required"),
                ],
            ),
            patch(
                "services.eep_worker.app.split_step.run_rescue_flow",
                new_callable=AsyncMock,
                return_value=_make_rescue_outcome("accept_now"),
            ) as mock_rescue,
        ):
            await run_split_normalization(
                full_res_image=_make_image(),
                selected_geometry=_make_geometry(),
                selected_model="iep1a",
                proxy_width=300,
                proxy_height=200,
                left_output_uri=_LEFT_URI,
                right_output_uri=_RIGHT_URI,
                left_rescue_output_uri=_LEFT_RESCUE_URI,
                right_rescue_output_uri=_RIGHT_RESCUE_URI,
                left_rectified_proxy_uri=_LEFT_PROXY_URI,
                right_rectified_proxy_uri=_RIGHT_PROXY_URI,
                storage=MagicMock(),
                image_loader=MagicMock(),
                job_id="job-123",
                page_number=1,
                lineage_id="lineage-abc",
                material_type="book",
                iep1d_endpoint=_IEP1D_ENDPOINT,
                iep1a_endpoint=_IEP1A_ENDPOINT,
                iep1b_endpoint=_IEP1B_ENDPOINT,
                iep1d_circuit_breaker=cb_d,
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=MagicMock(),
                session=MagicMock(),
            )

        mock_rescue.assert_called_once()
        call_kwargs = mock_rescue.call_args.kwargs
        assert call_kwargs["is_split_child"] is True
        assert call_kwargs["page_index"] == 1
        assert call_kwargs["artifact_uri"] == _RIGHT_URI
        assert call_kwargs["rescue_output_uri"] == _RIGHT_RESCUE_URI
        assert call_kwargs["rectified_proxy_uri"] == _RIGHT_PROXY_URI

    @pytest.mark.asyncio
    async def test_right_rescue_succeeds(self) -> None:
        """Right rescue accepts → right route='accept_now', used_rescue=True."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("accept_now"),
                _make_norm_outcome("rescue_required"),
            ],
            rescue_side_effects=[_make_rescue_outcome("accept_now")],
        )
        assert result.right.route == "accept_now"
        assert result.right.used_rescue is True

    @pytest.mark.asyncio
    async def test_both_fail_both_go_through_rescue(self) -> None:
        """Both children fail first-pass → both invoke rescue → both get outcomes."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("rescue_required"),
                _make_norm_outcome("rescue_required"),
            ],
            rescue_side_effects=[
                _make_rescue_outcome("accept_now"),
                _make_rescue_outcome("accept_now"),
            ],
        )
        assert result.left.used_rescue is True
        assert result.right.used_rescue is True

    @pytest.mark.asyncio
    async def test_both_rescues_fail(self) -> None:
        """Both rescues fail → both route='pending_human_correction'."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("rescue_required"),
                _make_norm_outcome("rescue_required"),
            ],
            rescue_side_effects=[
                _make_rescue_outcome("pending_human_correction", "rectification_failed"),
                _make_rescue_outcome("pending_human_correction", "rectification_failed"),
            ],
        )
        assert result.left.route == "pending_human_correction"
        assert result.right.route == "pending_human_correction"

    @pytest.mark.asyncio
    async def test_sub_page_index_always_correct(self) -> None:
        """left.sub_page_index==0 and right.sub_page_index==1 regardless of routes."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("rescue_required"),
                _make_norm_outcome("accept_now"),
            ],
            rescue_side_effects=[
                _make_rescue_outcome("accept_now"),
            ],
        )
        assert result.left.sub_page_index == 0
        assert result.right.sub_page_index == 1

    @pytest.mark.asyncio
    async def test_left_rescue_failure_does_not_affect_right(self) -> None:
        """Left rescue failing does NOT prevent right child from being processed."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("rescue_required"),
                _make_norm_outcome("accept_now"),
            ],
            rescue_side_effects=[
                _make_rescue_outcome("pending_human_correction", "rectification_failed"),
            ],
        )
        # Left failed via rescue
        assert result.left.route == "pending_human_correction"
        assert result.left.used_rescue is True
        # Right succeeded via first-pass (no rescue)
        assert result.right.route == "accept_now"
        assert result.right.used_rescue is False
