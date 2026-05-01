"""
tests/test_p9_policy_loader.py
--------------------------------
Packet 9.2 — Policy loading and threshold wiring tests.

Covers:
  - No policy in DB → spec defaults returned
  - Policy with a subset of fields → loaded fields override defaults; rest remain defaults
  - Policy with all spec Section 8.4 preprocessing fields → all loaded
  - Spec YAML aliases: quality_blur_score_max → blur_score_bad_min
                        quality_border_score_min → border_score_bad_max
  - aspect_ratio_bounds loaded and merged
  - Partial aspect_ratio_bounds → provided keys overridden, others default
  - Soft-signal fields loadable by exact field name
  - Malformed YAML in DB → graceful fallback to defaults (no crash)
  - Non-dict top-level YAML → graceful fallback
  - Invalid (non-numeric) field value → field falls back to default
  - parse_gate_config (pure function) works without a DB session
  - layout section present in YAML does not crash load_gate_config
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml

from services.eep.app.gates.geometry_selection import (
    _DEFAULT_AREA_FRACTION_BOUNDS,
    PreprocessingGateConfig,
    _DEFAULT_ASPECT_RATIO_BOUNDS,
)
from services.eep.app.policy_loader import load_gate_config, parse_gate_config

# ── Helpers ───────────────────────────────────────────────────────────────────

_DEFAULTS = PreprocessingGateConfig()


def _mock_db_with_yaml(config_yaml: str | None) -> MagicMock:
    """Return a mock Session whose query chain returns a PolicyVersion stub."""
    db = MagicMock()
    if config_yaml is None:
        db.query.return_value.order_by.return_value.first.return_value = None
    else:
        pv = MagicMock()
        pv.config_yaml = config_yaml
        db.query.return_value.order_by.return_value.first.return_value = pv
    return db


# ── parse_gate_config (pure function) ─────────────────────────────────────────


class TestParseGateConfig:
    def test_empty_dict_returns_defaults(self):
        cfg = parse_gate_config({})
        assert cfg == _DEFAULTS

    def test_empty_preprocessing_section_returns_defaults(self):
        cfg = parse_gate_config({"preprocessing": {}})
        assert cfg == _DEFAULTS

    def test_none_preprocessing_section_returns_defaults(self):
        cfg = parse_gate_config({"preprocessing": None})
        assert cfg == _DEFAULTS

    def test_direct_field_split_confidence_threshold(self):
        cfg = parse_gate_config({"preprocessing": {"split_confidence_threshold": 0.80}})
        assert cfg.split_confidence_threshold == pytest.approx(0.80)
        # all other fields remain default
        assert cfg.tta_variance_ceiling == _DEFAULTS.tta_variance_ceiling

    def test_direct_field_tta_variance_ceiling(self):
        cfg = parse_gate_config({"preprocessing": {"tta_variance_ceiling": 0.20}})
        assert cfg.tta_variance_ceiling == pytest.approx(0.20)

    def test_direct_field_page_area_preference_threshold(self):
        cfg = parse_gate_config({"preprocessing": {"page_area_preference_threshold": 0.25}})
        assert cfg.page_area_preference_threshold == pytest.approx(0.25)

    def test_direct_field_geometry_sanity_min(self):
        cfg = parse_gate_config({"preprocessing": {"geometry_sanity_area_min_fraction": 0.10}})
        assert cfg.geometry_sanity_area_min_fraction == pytest.approx(0.10)

    def test_direct_field_geometry_sanity_max(self):
        cfg = parse_gate_config({"preprocessing": {"geometry_sanity_area_max_fraction": 0.95}})
        assert cfg.geometry_sanity_area_max_fraction == pytest.approx(0.95)

    def test_area_fraction_bounds_full_override(self):
        cfg = parse_gate_config({
            "preprocessing": {
                "area_fraction_bounds": {
                    "book": [0.14, 0.98],
                    "newspaper": [0.08, 1.0],
                    "microfilm": [0.12, 0.99],
                }
            }
        })
        assert cfg.area_fraction_bounds["book"] == (0.14, 0.98)
        assert cfg.area_fraction_bounds["newspaper"] == (0.08, 1.0)
        assert cfg.area_fraction_bounds["microfilm"] == (0.12, 0.99)

    def test_area_fraction_bounds_partial_override_merges_with_defaults(self):
        cfg = parse_gate_config({
            "preprocessing": {
                "area_fraction_bounds": {
                    "newspaper": [0.08, 1.0],
                }
            }
        })
        assert cfg.area_fraction_bounds["newspaper"] == (0.08, 1.0)
        assert cfg.area_fraction_bounds["book"] == _DEFAULT_AREA_FRACTION_BOUNDS["book"]

    def test_direct_field_artifact_validation_threshold(self):
        cfg = parse_gate_config({"preprocessing": {"artifact_validation_threshold": 0.70}})
        assert cfg.artifact_validation_threshold == pytest.approx(0.70)

    # ── Spec YAML aliases ──────────────────────────────────────────────────────

    def test_alias_quality_blur_score_max_maps_to_blur_score_bad_min(self):
        cfg = parse_gate_config({"preprocessing": {"quality_blur_score_max": 0.65}})
        assert cfg.blur_score_bad_min == pytest.approx(0.65)

    def test_alias_quality_border_score_min_maps_to_border_score_bad_max(self):
        cfg = parse_gate_config({"preprocessing": {"quality_border_score_min": 0.25}})
        assert cfg.border_score_bad_max == pytest.approx(0.25)

    def test_direct_blur_score_bad_min_takes_precedence_over_alias_when_alias_absent(self):
        # When only the direct field name is provided (not the alias)
        cfg = parse_gate_config({"preprocessing": {"blur_score_bad_min": 0.65}})
        assert cfg.blur_score_bad_min == pytest.approx(0.65)

    def test_alias_takes_precedence_when_both_alias_and_direct_present(self):
        # quality_blur_score_max is evaluated first (or() short-circuit)
        cfg = parse_gate_config({
            "preprocessing": {
                "quality_blur_score_max": 0.60,
                "blur_score_bad_min": 0.75,
            }
        })
        assert cfg.blur_score_bad_min == pytest.approx(0.60)

    # ── aspect_ratio_bounds ────────────────────────────────────────────────────

    def test_aspect_ratio_bounds_full_override(self):
        cfg = parse_gate_config({
            "preprocessing": {
                "aspect_ratio_bounds": {
                    "book": [0.4, 3.0],
                    "newspaper": [0.2, 6.0],
                    "archival_document": [0.4, 4.0],
                }
            }
        })
        assert cfg.aspect_ratio_bounds["book"] == (0.4, 3.0)
        assert cfg.aspect_ratio_bounds["newspaper"] == (0.2, 6.0)
        assert cfg.aspect_ratio_bounds["archival_document"] == (0.4, 4.0)

    def test_aspect_ratio_bounds_partial_override_merges_with_defaults(self):
        cfg = parse_gate_config({
            "preprocessing": {
                "aspect_ratio_bounds": {
                    "book": [0.4, 3.0],
                    # newspaper and archival_document absent
                }
            }
        })
        assert cfg.aspect_ratio_bounds["book"] == (0.4, 3.0)
        assert cfg.aspect_ratio_bounds["newspaper"] == _DEFAULT_ASPECT_RATIO_BOUNDS["newspaper"]
        assert cfg.aspect_ratio_bounds["archival_document"] == _DEFAULT_ASPECT_RATIO_BOUNDS["archival_document"]

    def test_aspect_ratio_bounds_absent_returns_defaults(self):
        cfg = parse_gate_config({"preprocessing": {}})
        assert cfg.aspect_ratio_bounds == dict(_DEFAULT_ASPECT_RATIO_BOUNDS)

    def test_aspect_ratio_bounds_not_a_dict_falls_back_to_defaults(self):
        cfg = parse_gate_config({"preprocessing": {"aspect_ratio_bounds": "invalid"}})
        assert cfg.aspect_ratio_bounds == dict(_DEFAULT_ASPECT_RATIO_BOUNDS)

    def test_aspect_ratio_bounds_malformed_entry_skipped(self):
        cfg = parse_gate_config({
            "preprocessing": {
                "aspect_ratio_bounds": {
                    "book": "not_a_list",         # malformed — skip
                    "newspaper": [0.2, 6.0],      # valid
                }
            }
        })
        assert cfg.aspect_ratio_bounds["book"] == _DEFAULT_ASPECT_RATIO_BOUNDS["book"]
        assert cfg.aspect_ratio_bounds["newspaper"] == (0.2, 6.0)

    # ── Soft-signal fields ─────────────────────────────────────────────────────

    def test_soft_signal_skew_residual_good_max(self):
        cfg = parse_gate_config({"preprocessing": {"skew_residual_good_max": 0.5}})
        assert cfg.skew_residual_good_max == pytest.approx(0.5)

    def test_soft_signal_weight_blur_score(self):
        cfg = parse_gate_config({"preprocessing": {"weight_blur_score": 2.0}})
        assert cfg.weight_blur_score == pytest.approx(2.0)

    def test_soft_signal_tta_agreement_bad_max(self):
        cfg = parse_gate_config({"preprocessing": {"tta_agreement_bad_max": 0.6}})
        assert cfg.tta_agreement_bad_max == pytest.approx(0.6)

    # ── Robustness ─────────────────────────────────────────────────────────────

    def test_non_numeric_field_falls_back_to_default(self):
        cfg = parse_gate_config({"preprocessing": {"split_confidence_threshold": "bad_value"}})
        assert cfg.split_confidence_threshold == _DEFAULTS.split_confidence_threshold

    def test_none_field_falls_back_to_default(self):
        cfg = parse_gate_config({"preprocessing": {"artifact_validation_threshold": None}})
        assert cfg.artifact_validation_threshold == _DEFAULTS.artifact_validation_threshold

    def test_full_spec_section_8_4_yaml(self):
        """All spec Section 8.4 preprocessing fields set; verify all loaded correctly."""
        full_yaml_dict = {
            "preprocessing": {
                "split_confidence_threshold": 0.80,
                "tta_variance_ceiling": 0.12,
                "page_area_preference_threshold": 0.28,
                "geometry_sanity_area_min_fraction": 0.12,
                "geometry_sanity_area_max_fraction": 0.97,
                "artifact_validation_threshold": 0.65,
                "quality_blur_score_max": 0.68,
                "quality_border_score_min": 0.28,
                "aspect_ratio_bounds": {
                    "book": [0.45, 2.8],
                    "newspaper": [0.28, 5.5],
                    "archival_document": [0.45, 3.2],
                },
            }
        }
        cfg = parse_gate_config(full_yaml_dict)
        assert cfg.split_confidence_threshold == pytest.approx(0.80)
        assert cfg.tta_variance_ceiling == pytest.approx(0.12)
        assert cfg.page_area_preference_threshold == pytest.approx(0.28)
        assert cfg.geometry_sanity_area_min_fraction == pytest.approx(0.12)
        assert cfg.geometry_sanity_area_max_fraction == pytest.approx(0.97)
        assert cfg.artifact_validation_threshold == pytest.approx(0.65)
        assert cfg.blur_score_bad_min == pytest.approx(0.68)
        assert cfg.border_score_bad_max == pytest.approx(0.28)
        assert cfg.aspect_ratio_bounds["book"] == (0.45, 2.8)

    def test_layout_section_present_does_not_crash(self):
        """layout section in YAML is silently ignored (Phase 6 not yet implemented)."""
        cfg = parse_gate_config({
            "preprocessing": {"split_confidence_threshold": 0.80},
            "layout": {"min_consensus_confidence": 0.7},
        })
        assert cfg.split_confidence_threshold == pytest.approx(0.80)

    # ── rectification_policy toggle ────────────────────────────────────────────

    def test_rectification_policy_absent_defaults_to_conditional(self):
        """Missing rectification_policy → 'conditional' (preserves current behavior)."""
        cfg = parse_gate_config({"preprocessing": {}})
        assert cfg.rectification_policy == "conditional"

    def test_rectification_policy_conditional_loaded(self):
        cfg = parse_gate_config({"preprocessing": {"rectification_policy": "conditional"}})
        assert cfg.rectification_policy == "conditional"

    def test_rectification_policy_disabled_direct_review_loaded(self):
        cfg = parse_gate_config(
            {"preprocessing": {"rectification_policy": "disabled_direct_review"}}
        )
        assert cfg.rectification_policy == "disabled_direct_review"

    def test_rectification_policy_unknown_value_falls_back_to_conditional(self):
        cfg = parse_gate_config({"preprocessing": {"rectification_policy": "unknown_value"}})
        assert cfg.rectification_policy == "conditional"

    def test_rectification_policy_none_falls_back_to_conditional(self):
        cfg = parse_gate_config({"preprocessing": {"rectification_policy": None}})
        assert cfg.rectification_policy == "conditional"


# ── load_gate_config (DB-backed) ──────────────────────────────────────────────


class TestLoadGateConfig:
    def test_no_policy_returns_spec_defaults(self):
        db = _mock_db_with_yaml(None)
        cfg = load_gate_config(db)
        assert cfg == _DEFAULTS

    def test_empty_preprocessing_section_returns_defaults(self):
        db = _mock_db_with_yaml("preprocessing: {}\n")
        cfg = load_gate_config(db)
        assert cfg == _DEFAULTS

    def test_loads_split_confidence_from_policy(self):
        policy_yaml = "preprocessing:\n  split_confidence_threshold: 0.85\n"
        db = _mock_db_with_yaml(policy_yaml)
        cfg = load_gate_config(db)
        assert cfg.split_confidence_threshold == pytest.approx(0.85)

    def test_loads_all_direct_fields(self):
        policy_yaml = yaml.dump({
            "preprocessing": {
                "split_confidence_threshold": 0.82,
                "tta_variance_ceiling": 0.18,
                "page_area_preference_threshold": 0.35,
                "geometry_sanity_area_min_fraction": 0.13,
                "geometry_sanity_area_max_fraction": 0.96,
                "artifact_validation_threshold": 0.62,
            }
        })
        db = _mock_db_with_yaml(policy_yaml)
        cfg = load_gate_config(db)
        assert cfg.split_confidence_threshold == pytest.approx(0.82)
        assert cfg.tta_variance_ceiling == pytest.approx(0.18)
        assert cfg.page_area_preference_threshold == pytest.approx(0.35)
        assert cfg.geometry_sanity_area_min_fraction == pytest.approx(0.13)
        assert cfg.geometry_sanity_area_max_fraction == pytest.approx(0.96)
        assert cfg.artifact_validation_threshold == pytest.approx(0.62)

    def test_loads_spec_aliases(self):
        policy_yaml = yaml.dump({
            "preprocessing": {
                "quality_blur_score_max": 0.72,
                "quality_border_score_min": 0.32,
            }
        })
        db = _mock_db_with_yaml(policy_yaml)
        cfg = load_gate_config(db)
        assert cfg.blur_score_bad_min == pytest.approx(0.72)
        assert cfg.border_score_bad_max == pytest.approx(0.32)

    def test_loads_aspect_ratio_bounds(self):
        policy_yaml = yaml.dump({
            "preprocessing": {
                "aspect_ratio_bounds": {
                    "book": [0.4, 3.0],
                    "newspaper": [0.25, 5.5],
                    "archival_document": [0.45, 3.5],
                }
            }
        })
        db = _mock_db_with_yaml(policy_yaml)
        cfg = load_gate_config(db)
        assert cfg.aspect_ratio_bounds["book"] == (0.4, 3.0)
        assert cfg.aspect_ratio_bounds["newspaper"] == (0.25, 5.5)
        assert cfg.aspect_ratio_bounds["archival_document"] == (0.45, 3.5)

    def test_malformed_yaml_falls_back_to_defaults(self):
        db = _mock_db_with_yaml("preprocessing: [\ninvalid yaml {{\n")
        cfg = load_gate_config(db)
        assert cfg == _DEFAULTS

    def test_non_dict_top_level_yaml_falls_back_to_defaults(self):
        db = _mock_db_with_yaml("- item1\n- item2\n")
        cfg = load_gate_config(db)
        assert cfg == _DEFAULTS

    def test_policy_with_layout_section_does_not_crash(self):
        policy_yaml = yaml.dump({
            "preprocessing": {"split_confidence_threshold": 0.78},
            "layout": {"min_consensus_confidence": 0.65},
        })
        db = _mock_db_with_yaml(policy_yaml)
        cfg = load_gate_config(db)
        assert cfg.split_confidence_threshold == pytest.approx(0.78)

    def test_unrecognised_preprocessing_keys_ignored(self):
        """Unknown keys in preprocessing section must not crash."""
        policy_yaml = yaml.dump({
            "preprocessing": {
                "split_confidence_threshold": 0.77,
                "future_unrecognised_key": 99.9,
            }
        })
        db = _mock_db_with_yaml(policy_yaml)
        cfg = load_gate_config(db)
        assert cfg.split_confidence_threshold == pytest.approx(0.77)

    def test_loads_rectification_policy_disabled_direct_review(self):
        """load_gate_config correctly propagates disabled_direct_review from YAML."""
        policy_yaml = yaml.dump(
            {"preprocessing": {"rectification_policy": "disabled_direct_review"}}
        )
        db = _mock_db_with_yaml(policy_yaml)
        cfg = load_gate_config(db)
        assert cfg.rectification_policy == "disabled_direct_review"

    def test_policy_without_rectification_policy_defaults_to_conditional(self):
        """Existing policies that lack rectification_policy retain current behavior."""
        policy_yaml = yaml.dump(
            {"preprocessing": {"split_confidence_threshold": 0.75}}
        )
        db = _mock_db_with_yaml(policy_yaml)
        cfg = load_gate_config(db)
        assert cfg.rectification_policy == "conditional"


# ── build_gate_config wiring (task.py entry point) ────────────────────────────


class TestBuildGateConfig:
    """
    Verify that build_gate_config (task.py) correctly wires load_gate_config
    from the policy store and produces a config passable to all step functions.
    """

    def test_build_gate_config_delegates_to_load_gate_config(self):
        """build_gate_config must return a PreprocessingGateConfig loaded from policy."""
        from services.eep_worker.app.task import build_gate_config

        policy_yaml = "preprocessing:\n  split_confidence_threshold: 0.88\n"
        db = _mock_db_with_yaml(policy_yaml)
        cfg = build_gate_config(db)
        assert isinstance(cfg, PreprocessingGateConfig)
        assert cfg.split_confidence_threshold == pytest.approx(0.88)

    def test_build_gate_config_returns_defaults_when_no_policy(self):
        from services.eep_worker.app.task import build_gate_config

        db = _mock_db_with_yaml(None)
        cfg = build_gate_config(db)
        assert cfg == _DEFAULTS

    def test_build_gate_config_result_is_passable_to_invoke_geometry_services(self):
        """
        Verify the returned config satisfies the gate_config type accepted by
        invoke_geometry_services (PreprocessingGateConfig).
        """
        import inspect
        from services.eep_worker.app.task import build_gate_config
        from services.eep_worker.app.geometry_invocation import invoke_geometry_services

        sig = inspect.signature(invoke_geometry_services)
        param = sig.parameters["gate_config"]
        # Annotation includes PreprocessingGateConfig — just verify the function accepts it
        assert "gate_config" in sig.parameters

        db = _mock_db_with_yaml(None)
        cfg = build_gate_config(db)
        assert isinstance(cfg, PreprocessingGateConfig)

    def test_build_gate_config_result_is_passable_to_normalization_step(self):
        import inspect
        from services.eep_worker.app.task import build_gate_config
        from services.eep_worker.app.normalization_step import run_normalization_and_first_validation

        assert "gate_config" in inspect.signature(run_normalization_and_first_validation).parameters
        cfg = build_gate_config(_mock_db_with_yaml(None))
        assert isinstance(cfg, PreprocessingGateConfig)

    def test_build_gate_config_result_is_passable_to_rescue_step(self):
        import inspect
        from services.eep_worker.app.task import build_gate_config
        from services.eep_worker.app.rescue_step import run_rescue_flow

        assert "gate_config" in inspect.signature(run_rescue_flow).parameters
        cfg = build_gate_config(_mock_db_with_yaml(None))
        assert isinstance(cfg, PreprocessingGateConfig)

    def test_build_gate_config_result_is_passable_to_split_step(self):
        import inspect
        from services.eep_worker.app.task import build_gate_config
        from services.eep_worker.app.split_step import run_split_normalization

        assert "gate_config" in inspect.signature(run_split_normalization).parameters
        cfg = build_gate_config(_mock_db_with_yaml(None))
        assert isinstance(cfg, PreprocessingGateConfig)

    def test_all_four_steps_accept_gate_config_keyword(self):
        """
        Confirm every preprocessing step function accepts gate_config as a
        keyword argument — this is the contract that makes the wiring possible.
        """
        import inspect
        from services.eep_worker.app.geometry_invocation import invoke_geometry_services
        from services.eep_worker.app.normalization_step import run_normalization_and_first_validation
        from services.eep_worker.app.rescue_step import run_rescue_flow
        from services.eep_worker.app.split_step import run_split_normalization

        for fn in [
            invoke_geometry_services,
            run_normalization_and_first_validation,
            run_rescue_flow,
            run_split_normalization,
        ]:
            params = inspect.signature(fn).parameters
            assert "gate_config" in params, (
                f"{fn.__name__} must accept gate_config keyword argument"
            )
