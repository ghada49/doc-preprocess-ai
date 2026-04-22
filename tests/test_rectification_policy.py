"""
tests/test_rectification_policy.py
------------------------------------
Rectification policy routing tests.

Covers:
  TestRectificationPolicySplitStep (6 tests):
    1. disabled_direct_review — left child rescue_required → pending_human_correction, no rescue call
    2. disabled_direct_review — right child rescue_required → pending_human_correction, no rescue call
    3. disabled_direct_review — both children rescue_required → both pending, no rescue calls
    4. disabled_direct_review — accept_now children are unaffected (still accepted)
    5. conditional (default) — rescue_required triggers run_rescue_flow as before
    6. review_reason is exactly "rectification_policy_disabled" for skipped pages

  TestRectificationPolicyConfig (4 tests):
    1. PreprocessingGateConfig default is "conditional"
    2. parse_gate_config missing field → "conditional"
    3. parse_gate_config with "disabled_direct_review" → loaded
    4. parse_gate_config with unknown value → fallback to "conditional"
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from services.eep.app.gates.artifact_validation import (
    ArtifactHardCheckResult,
    ArtifactValidationResult,
)
from services.eep.app.gates.geometry_selection import PreprocessingGateConfig
from services.eep.app.policy_loader import parse_gate_config
from services.eep_worker.app.circuit_breaker import CircuitBreaker
from services.eep_worker.app.normalization_step import NormalizationOutcome
from services.eep_worker.app.rescue_step import RescueOutcome
from services.eep_worker.app.split_step import run_split_normalization
from shared.schemas.geometry import GeometryResponse, PageRegion
from shared.schemas.preprocessing import PreprocessBranchResponse


# ── Helpers ──────────────────────────────────────────────────────────────────────

_LEFT_URI = "s3://bucket/left.tiff"
_RIGHT_URI = "s3://bucket/right.tiff"
_LEFT_RESCUE_URI = "s3://bucket/left_rescue.tiff"
_RIGHT_RESCUE_URI = "s3://bucket/right_rescue.tiff"
_LEFT_PROXY_URI = "s3://bucket/left_rectified_proxy.png"
_RIGHT_PROXY_URI = "s3://bucket/right_rectified_proxy.png"
_IEP1D_ENDPOINT = "http://iep1d:8003/v1/rectify"
_IEP1A_ENDPOINT = "http://iep1a:8001/v1/geometry"
_IEP1B_ENDPOINT = "http://iep1b:8002/v1/geometry"


def _make_geometry() -> GeometryResponse:
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
        split_required=True,
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


def _make_norm_outcome(route: str) -> NormalizationOutcome:
    branch = MagicMock(spec=PreprocessBranchResponse)
    branch.processed_image_uri = _LEFT_URI
    return NormalizationOutcome(
        branch_response=branch,
        validation_result=_make_validation(passed=(route == "accept_now")),
        route=route,  # type: ignore[arg-type]
        duration_ms=50.0,
    )


def _make_rescue_outcome(route: str = "accept_now") -> RescueOutcome:
    branch = MagicMock(spec=PreprocessBranchResponse) if route == "accept_now" else None
    return RescueOutcome(
        route=route,  # type: ignore[arg-type]
        review_reason=None if route == "accept_now" else "rectification_failed",
        branch_response=branch,
        validation_result=_make_validation(passed=(route == "accept_now")) if branch else None,
        rectify_response=None,
        second_selection_result=None,
        duration_ms=100.0,
    )


def _make_cbs() -> tuple[CircuitBreaker, CircuitBreaker, CircuitBreaker]:
    return CircuitBreaker("iep1a"), CircuitBreaker("iep1b"), CircuitBreaker("iep1d")


async def _run_split(
    norm_side_effects: list[NormalizationOutcome],
    rescue_side_effects: list[RescueOutcome] | None = None,
    gate_config: PreprocessingGateConfig | None = None,
):
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
            full_res_image=np.full((400, 600, 3), 128, dtype=np.uint8),
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
            gate_config=gate_config,
        )
        result._mock_norm = mock_norm  # type: ignore[attr-defined]
        result._mock_rescue = mock_rescue  # type: ignore[attr-defined]
    return result


_DISABLED_CFG = PreprocessingGateConfig(rectification_policy="disabled_direct_review")
_CONDITIONAL_CFG = PreprocessingGateConfig(rectification_policy="conditional")


# ── TestRectificationPolicySplitStep ──────────────────────────────────────────────


class TestRectificationPolicySplitStep:
    @pytest.mark.asyncio
    async def test_disabled_policy_left_child_rescue_required_no_iep1d(self) -> None:
        """disabled_direct_review: left rescue_required → pending_human_correction, no rescue call."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("rescue_required"),
                _make_norm_outcome("accept_now"),
            ],
            gate_config=_DISABLED_CFG,
        )
        assert result.left.route == "pending_human_correction"
        assert result.left.used_rescue is False
        result._mock_rescue.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_policy_right_child_rescue_required_no_iep1d(self) -> None:
        """disabled_direct_review: right rescue_required → pending_human_correction, no rescue call."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("accept_now"),
                _make_norm_outcome("rescue_required"),
            ],
            gate_config=_DISABLED_CFG,
        )
        assert result.right.route == "pending_human_correction"
        assert result.right.used_rescue is False
        result._mock_rescue.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_policy_both_children_rescue_required_no_iep1d(self) -> None:
        """disabled_direct_review: both children rescue_required → both pending, no rescue calls."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("rescue_required"),
                _make_norm_outcome("rescue_required"),
            ],
            gate_config=_DISABLED_CFG,
        )
        assert result.left.route == "pending_human_correction"
        assert result.right.route == "pending_human_correction"
        assert result.left.used_rescue is False
        assert result.right.used_rescue is False
        result._mock_rescue.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_policy_accept_now_children_unaffected(self) -> None:
        """disabled_direct_review does not affect pages that pass the first pass."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("accept_now"),
                _make_norm_outcome("accept_now"),
            ],
            gate_config=_DISABLED_CFG,
        )
        assert result.left.route == "accept_now"
        assert result.right.route == "accept_now"

    @pytest.mark.asyncio
    async def test_conditional_policy_rescue_required_calls_iep1d(self) -> None:
        """conditional policy: rescue_required triggers run_rescue_flow as before."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("rescue_required"),
                _make_norm_outcome("accept_now"),
            ],
            rescue_side_effects=[_make_rescue_outcome("accept_now")],
            gate_config=_CONDITIONAL_CFG,
        )
        assert result.left.route == "accept_now"
        assert result.left.used_rescue is True
        result._mock_rescue.assert_called_once()

    @pytest.mark.asyncio
    async def test_disabled_policy_review_reason_is_rectification_policy_disabled(self) -> None:
        """review_reason for disabled-policy pages is exactly 'rectification_policy_disabled'."""
        result = await _run_split(
            norm_side_effects=[
                _make_norm_outcome("rescue_required"),
                _make_norm_outcome("rescue_required"),
            ],
            gate_config=_DISABLED_CFG,
        )
        assert result.left.review_reason == "rectification_policy_disabled"
        assert result.right.review_reason == "rectification_policy_disabled"


# ── TestRectificationPolicyConfig ────────────────────────────────────────────────


class TestRectificationPolicyConfig:
    def test_default_config_is_conditional(self) -> None:
        """PreprocessingGateConfig() defaults to 'conditional' — preserves current behavior."""
        cfg = PreprocessingGateConfig()
        assert cfg.rectification_policy == "conditional"

    def test_parse_gate_config_missing_field_defaults_to_conditional(self) -> None:
        """parse_gate_config with no rectification_policy key → 'conditional'."""
        cfg = parse_gate_config({"preprocessing": {}})
        assert cfg.rectification_policy == "conditional"

    def test_parse_gate_config_disabled_direct_review(self) -> None:
        cfg = parse_gate_config(
            {"preprocessing": {"rectification_policy": "disabled_direct_review"}}
        )
        assert cfg.rectification_policy == "disabled_direct_review"

    def test_parse_gate_config_unknown_value_falls_back_to_conditional(self) -> None:
        cfg = parse_gate_config({"preprocessing": {"rectification_policy": "typo_value"}})
        assert cfg.rectification_policy == "conditional"
