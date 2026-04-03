"""
tests/test_p4_db_quality_gate.py
----------------------------------
Packet 4.2 — quality gate log helper tests.

Covers:
  - VALID_GATE_TYPES contains exactly the 5 spec-defined values
  - VALID_ROUTE_DECISIONS contains exactly the 4 spec-defined values
  - VALID_REVIEW_REASONS contains all 6 recognised reason strings (2 legacy + 4 adjudication)
  - log_gate creates a QualityGateLog with correct required fields
  - log_gate sets all optional fields when provided
  - log_gate leaves optional fields as None when not provided
  - log_gate raises ValueError for unknown gate_type
  - log_gate raises ValueError for unknown route_decision
  - All 5 gate_type values accepted without error
  - All 4 route_decision values accepted without error
  - session.add() is called with the created record

Session is mocked — no live database required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from services.eep.app.db.models import QualityGateLog
from services.eep.app.db.quality_gate import (
    VALID_GATE_TYPES,
    VALID_REVIEW_REASONS,
    VALID_ROUTE_DECISIONS,
    log_gate,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def session() -> MagicMock:
    return MagicMock()


def _log(session: MagicMock, **overrides: Any) -> QualityGateLog:
    """Create a gate log with sensible defaults."""
    defaults: dict[str, Any] = dict(
        gate_id="gate-001",
        job_id="job-abc",
        page_number=1,
        gate_type="geometry_selection",
        route_decision="accepted",
    )
    defaults.update(overrides)
    return log_gate(session, **defaults)


# ── Constants ──────────────────────────────────────────────────────────────────


class TestValidGateTypes:
    def test_contains_geometry_selection(self) -> None:
        assert "geometry_selection" in VALID_GATE_TYPES

    def test_contains_geometry_selection_post_rectification(self) -> None:
        assert "geometry_selection_post_rectification" in VALID_GATE_TYPES

    def test_contains_artifact_validation(self) -> None:
        assert "artifact_validation" in VALID_GATE_TYPES

    def test_contains_artifact_validation_final(self) -> None:
        assert "artifact_validation_final" in VALID_GATE_TYPES

    def test_contains_layout(self) -> None:
        assert "layout" in VALID_GATE_TYPES

    def test_exactly_five_values(self) -> None:
        assert len(VALID_GATE_TYPES) == 5

    def test_is_frozenset(self) -> None:
        assert isinstance(VALID_GATE_TYPES, frozenset)


class TestValidRouteDecisions:
    def test_contains_accepted(self) -> None:
        assert "accepted" in VALID_ROUTE_DECISIONS

    def test_contains_rectification(self) -> None:
        assert "rectification" in VALID_ROUTE_DECISIONS

    def test_contains_pending_human_correction(self) -> None:
        assert "pending_human_correction" in VALID_ROUTE_DECISIONS

    def test_contains_review(self) -> None:
        assert "review" in VALID_ROUTE_DECISIONS

    def test_exactly_four_values(self) -> None:
        assert len(VALID_ROUTE_DECISIONS) == 4

    def test_is_frozenset(self) -> None:
        assert isinstance(VALID_ROUTE_DECISIONS, frozenset)


class TestValidReviewReasons:
    # ── Legacy local-consensus reasons ─────────────────────────────────────
    def test_contains_layout_consensus_failed(self) -> None:
        assert "layout_consensus_failed" in VALID_REVIEW_REASONS

    def test_contains_layout_consensus_low_confidence(self) -> None:
        assert "layout_consensus_low_confidence" in VALID_REVIEW_REASONS

    # ── Adjudication reasons (P3.3 / P4.1) ────────────────────────────────
    def test_contains_layout_adjudication_google_failed(self) -> None:
        assert "layout_adjudication_google_failed" in VALID_REVIEW_REASONS

    def test_contains_layout_adjudication_google_implausible(self) -> None:
        assert "layout_adjudication_google_implausible" in VALID_REVIEW_REASONS

    def test_contains_layout_adjudication_failed(self) -> None:
        assert "layout_adjudication_failed" in VALID_REVIEW_REASONS

    def test_contains_layout_single_model_mode(self) -> None:
        assert "layout_single_model_mode" in VALID_REVIEW_REASONS

    def test_exactly_six_values(self) -> None:
        assert len(VALID_REVIEW_REASONS) == 6

    def test_is_frozenset(self) -> None:
        assert isinstance(VALID_REVIEW_REASONS, frozenset)


# ── log_gate — required fields ────────────────────────────────────────────────


class TestLogGateRequiredFields:
    def test_returns_quality_gate_log_instance(self, session: MagicMock) -> None:
        record = _log(session)
        assert isinstance(record, QualityGateLog)

    def test_gate_id_set(self, session: MagicMock) -> None:
        record = _log(session, gate_id="gate-xyz")
        assert record.gate_id == "gate-xyz"

    def test_job_id_set(self, session: MagicMock) -> None:
        record = _log(session, job_id="job-123")
        assert record.job_id == "job-123"

    def test_page_number_set(self, session: MagicMock) -> None:
        record = _log(session, page_number=7)
        assert record.page_number == 7

    def test_gate_type_set(self, session: MagicMock) -> None:
        record = _log(session, gate_type="artifact_validation")
        assert record.gate_type == "artifact_validation"

    def test_route_decision_set(self, session: MagicMock) -> None:
        record = _log(session, route_decision="rectification")
        assert record.route_decision == "rectification"

    def test_session_add_called(self, session: MagicMock) -> None:
        record = _log(session)
        session.add.assert_called_once_with(record)


# ── log_gate — optional fields ────────────────────────────────────────────────


class TestLogGateOptionalFields:
    def test_optional_fields_default_to_none(self, session: MagicMock) -> None:
        record = _log(session)
        assert record.iep1a_geometry is None
        assert record.iep1b_geometry is None
        assert record.structural_agreement is None
        assert record.selected_model is None
        assert record.selection_reason is None
        assert record.sanity_check_results is None
        assert record.split_confidence is None
        assert record.tta_variance is None
        assert record.artifact_validation_score is None
        assert record.review_reason is None
        assert record.processing_time_ms is None

    def test_iep1a_geometry_set(self, session: MagicMock) -> None:
        geom = {"keypoints": [[1, 2], [3, 4]]}
        record = _log(session, iep1a_geometry=geom)
        assert record.iep1a_geometry == geom

    def test_iep1b_geometry_set(self, session: MagicMock) -> None:
        geom = {"keypoints": [[5, 6], [7, 8]]}
        record = _log(session, iep1b_geometry=geom)
        assert record.iep1b_geometry == geom

    def test_structural_agreement_set(self, session: MagicMock) -> None:
        record = _log(session, structural_agreement=True)
        assert record.structural_agreement is True

    def test_structural_agreement_false(self, session: MagicMock) -> None:
        record = _log(session, structural_agreement=False)
        assert record.structural_agreement is False

    def test_selected_model_set(self, session: MagicMock) -> None:
        record = _log(session, selected_model="iep1b")
        assert record.selected_model == "iep1b"

    def test_selection_reason_set(self, session: MagicMock) -> None:
        record = _log(session, selection_reason="iep1a timed out")
        assert record.selection_reason == "iep1a timed out"

    def test_sanity_check_results_set(self, session: MagicMock) -> None:
        scr = {"split_confidence": "pass", "tta_variance": "pass"}
        record = _log(session, sanity_check_results=scr)
        assert record.sanity_check_results == scr

    def test_split_confidence_set(self, session: MagicMock) -> None:
        sc = {"score": 0.12, "threshold": 0.35}
        record = _log(session, split_confidence=sc)
        assert record.split_confidence == sc

    def test_tta_variance_set(self, session: MagicMock) -> None:
        tv = {"keypoints": 0.003}
        record = _log(session, tta_variance=tv)
        assert record.tta_variance == tv

    def test_artifact_validation_score_set(self, session: MagicMock) -> None:
        record = _log(session, artifact_validation_score=0.87)
        assert record.artifact_validation_score == pytest.approx(0.87)

    def test_review_reason_set(self, session: MagicMock) -> None:
        record = _log(session, route_decision="review", review_reason="layout disagreement")
        assert record.review_reason == "layout disagreement"

    def test_processing_time_ms_set(self, session: MagicMock) -> None:
        record = _log(session, processing_time_ms=234.5)
        assert record.processing_time_ms == pytest.approx(234.5)


# ── log_gate — validation ─────────────────────────────────────────────────────


class TestLogGateValidation:
    def test_invalid_gate_type_raises_value_error(self, session: MagicMock) -> None:
        with pytest.raises(ValueError, match="Invalid gate_type"):
            _log(session, gate_type="unknown_gate")

    def test_invalid_route_decision_raises_value_error(self, session: MagicMock) -> None:
        with pytest.raises(ValueError, match="Invalid route_decision"):
            _log(session, route_decision="skipped")

    def test_invalid_gate_type_error_message_names_value(self, session: MagicMock) -> None:
        with pytest.raises(ValueError, match="bad_gate"):
            _log(session, gate_type="bad_gate")

    def test_invalid_route_decision_error_message_names_value(self, session: MagicMock) -> None:
        with pytest.raises(ValueError, match="bad_decision"):
            _log(session, route_decision="bad_decision")

    def test_no_session_add_on_invalid_gate_type(self, session: MagicMock) -> None:
        with pytest.raises(ValueError):
            _log(session, gate_type="wrong")
        session.add.assert_not_called()

    def test_no_session_add_on_invalid_route_decision(self, session: MagicMock) -> None:
        with pytest.raises(ValueError):
            _log(session, route_decision="wrong")
        session.add.assert_not_called()


# ── log_gate — all valid values smoke test ────────────────────────────────────


class TestLogGateAllValidValues:
    @pytest.mark.parametrize("gate_type", sorted(VALID_GATE_TYPES))
    def test_all_gate_types_accepted(self, session: MagicMock, gate_type: str) -> None:
        _log(session, gate_type=gate_type)
        session.add.assert_called()

    @pytest.mark.parametrize("route_decision", sorted(VALID_ROUTE_DECISIONS))
    def test_all_route_decisions_accepted(self, session: MagicMock, route_decision: str) -> None:
        _log(session, route_decision=route_decision)
        session.add.assert_called()
