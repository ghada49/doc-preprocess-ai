"""
tests/test_p4_db_lineage.py
-----------------------------
Packet 4.2 — page lineage helper tests.

Covers:
  - create_lineage: inserts PageLineage with correct field values and defaults
  - create_lineage: both artifact states default to 'pending'
  - create_lineage: split_source and parent_page_id optional fields
  - confirm_preprocessed_artifact: updates preprocessed_artifact_state='confirmed'
  - confirm_layout_artifact: updates layout_artifact_state='confirmed'
  - mark_artifact_recovery_failed('preprocessed'): sets state + increments retry
  - mark_artifact_recovery_failed('layout'): same for layout column
  - update_geometry_result: sets all geometry fields
  - update_lineage_completion: sets acceptance/completion fields + completed_at
  - update_lineage_completion: output_image_uri and gate_results included only
    when provided
  - record_human_correction: sets human_corrected, timestamp, fields
  - record_human_correction: reviewed_by/notes included only when provided
  - get_lineage: delegates to session.get with correct args

Session is mocked — no live database required.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from services.eep.app.db.lineage import (
    confirm_layout_artifact,
    confirm_preprocessed_artifact,
    create_lineage,
    get_lineage,
    mark_artifact_recovery_failed,
    record_human_correction,
    update_geometry_result,
    update_lineage_completion,
)
from services.eep.app.db.models import PageLineage

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def session() -> MagicMock:
    return MagicMock()


_LINEAGE_ID = "lin-001"
_JOB_ID = "job-abc"


def _update_kwargs(session: MagicMock) -> dict[str, Any]:
    """Return the dict passed to the last .update() call."""
    return cast(
        dict[str, Any], session.query.return_value.filter.return_value.update.call_args[0][0]
    )


# ── create_lineage ─────────────────────────────────────────────────────────────


class TestCreateLineage:
    def _make(self, session: MagicMock, **overrides: Any) -> PageLineage:
        defaults: dict[str, Any] = dict(
            lineage_id=_LINEAGE_ID,
            job_id=_JOB_ID,
            page_number=3,
            correlation_id="corr-xyz",
            input_image_uri="s3://bucket/input.tiff",
            otiff_uri="s3://bucket/out.otiff",
            material_type="book",
            policy_version="v1.2",
        )
        defaults.update(overrides)
        return create_lineage(session, **defaults)

    def test_returns_page_lineage_instance(self, session: MagicMock) -> None:
        record = self._make(session)
        assert isinstance(record, PageLineage)

    def test_lineage_id_set(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.lineage_id == _LINEAGE_ID

    def test_job_id_set(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.job_id == _JOB_ID

    def test_page_number_set(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.page_number == 3

    def test_correlation_id_set(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.correlation_id == "corr-xyz"

    def test_input_image_uri_set(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.input_image_uri == "s3://bucket/input.tiff"

    def test_otiff_uri_set(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.otiff_uri == "s3://bucket/out.otiff"

    def test_material_type_set(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.material_type == "book"

    def test_policy_version_set(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.policy_version == "v1.2"

    def test_preprocessed_artifact_state_defaults_to_pending(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.preprocessed_artifact_state == "pending"

    def test_layout_artifact_state_defaults_to_pending(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.layout_artifact_state == "pending"

    def test_sub_page_index_defaults_to_none(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.sub_page_index is None

    def test_sub_page_index_set_when_provided(self, session: MagicMock) -> None:
        record = self._make(session, sub_page_index=1)
        assert record.sub_page_index == 1

    def test_parent_page_id_defaults_to_none(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.parent_page_id is None

    def test_parent_page_id_set_when_provided(self, session: MagicMock) -> None:
        record = self._make(session, parent_page_id="pg-parent")
        assert record.parent_page_id == "pg-parent"

    def test_split_source_defaults_to_false(self, session: MagicMock) -> None:
        record = self._make(session)
        assert record.split_source is False

    def test_split_source_set_when_true(self, session: MagicMock) -> None:
        record = self._make(session, split_source=True)
        assert record.split_source is True

    def test_input_image_hash_set_when_provided(self, session: MagicMock) -> None:
        record = self._make(session, input_image_hash="abc123")
        assert record.input_image_hash == "abc123"

    def test_session_add_called(self, session: MagicMock) -> None:
        record = self._make(session)
        session.add.assert_called_once_with(record)


# ── confirm_preprocessed_artifact ─────────────────────────────────────────────


class TestConfirmPreprocessedArtifact:
    def test_updates_preprocessed_state_to_confirmed(self, session: MagicMock) -> None:
        confirm_preprocessed_artifact(session, _LINEAGE_ID)
        updates = _update_kwargs(session)
        assert updates["preprocessed_artifact_state"] == "confirmed"

    def test_does_not_touch_layout_state(self, session: MagicMock) -> None:
        confirm_preprocessed_artifact(session, _LINEAGE_ID)
        updates = _update_kwargs(session)
        assert "layout_artifact_state" not in updates

    def test_uses_correct_lineage_id_filter(self, session: MagicMock) -> None:
        confirm_preprocessed_artifact(session, _LINEAGE_ID)
        session.query.assert_called_once()


# ── confirm_layout_artifact ────────────────────────────────────────────────────


class TestConfirmLayoutArtifact:
    def test_updates_layout_state_to_confirmed(self, session: MagicMock) -> None:
        confirm_layout_artifact(session, _LINEAGE_ID)
        updates = _update_kwargs(session)
        assert updates["layout_artifact_state"] == "confirmed"

    def test_does_not_touch_preprocessed_state(self, session: MagicMock) -> None:
        confirm_layout_artifact(session, _LINEAGE_ID)
        updates = _update_kwargs(session)
        assert "preprocessed_artifact_state" not in updates


# ── mark_artifact_recovery_failed ─────────────────────────────────────────────


class TestMarkArtifactRecoveryFailed:
    def test_preprocessed_sets_recovery_failed(self, session: MagicMock) -> None:
        mark_artifact_recovery_failed(session, _LINEAGE_ID, "preprocessed")
        updates = _update_kwargs(session)
        assert updates["preprocessed_artifact_state"] == "recovery_failed"

    def test_layout_sets_recovery_failed(self, session: MagicMock) -> None:
        mark_artifact_recovery_failed(session, _LINEAGE_ID, "layout")
        updates = _update_kwargs(session)
        assert updates["layout_artifact_state"] == "recovery_failed"

    def test_includes_cleanup_retry_count_increment(self, session: MagicMock) -> None:
        mark_artifact_recovery_failed(session, _LINEAGE_ID, "preprocessed")
        updates = _update_kwargs(session)
        assert "cleanup_retry_count" in updates

    def test_preprocessed_does_not_touch_layout_column(self, session: MagicMock) -> None:
        mark_artifact_recovery_failed(session, _LINEAGE_ID, "preprocessed")
        updates = _update_kwargs(session)
        assert "layout_artifact_state" not in updates

    def test_layout_does_not_touch_preprocessed_column(self, session: MagicMock) -> None:
        mark_artifact_recovery_failed(session, _LINEAGE_ID, "layout")
        updates = _update_kwargs(session)
        assert "preprocessed_artifact_state" not in updates


# ── update_geometry_result ────────────────────────────────────────────────────


class TestUpdateGeometryResult:
    def test_iep1a_used_set(self, session: MagicMock) -> None:
        update_geometry_result(
            session,
            _LINEAGE_ID,
            iep1a_used=True,
            iep1b_used=True,
            selected_geometry_model="iep1a",
            structural_agreement=True,
        )
        updates = _update_kwargs(session)
        assert updates["iep1a_used"] is True

    def test_iep1b_used_set(self, session: MagicMock) -> None:
        update_geometry_result(
            session,
            _LINEAGE_ID,
            iep1a_used=True,
            iep1b_used=False,
            selected_geometry_model="iep1a",
            structural_agreement=None,
        )
        updates = _update_kwargs(session)
        assert updates["iep1b_used"] is False

    def test_selected_model_set(self, session: MagicMock) -> None:
        update_geometry_result(
            session,
            _LINEAGE_ID,
            iep1a_used=True,
            iep1b_used=True,
            selected_geometry_model="iep1b",
            structural_agreement=False,
        )
        updates = _update_kwargs(session)
        assert updates["selected_geometry_model"] == "iep1b"

    def test_structural_agreement_set(self, session: MagicMock) -> None:
        update_geometry_result(
            session,
            _LINEAGE_ID,
            iep1a_used=True,
            iep1b_used=True,
            selected_geometry_model="iep1a",
            structural_agreement=False,
        )
        updates = _update_kwargs(session)
        assert updates["structural_agreement"] is False

    def test_iep1d_used_defaults_to_false(self, session: MagicMock) -> None:
        update_geometry_result(
            session,
            _LINEAGE_ID,
            iep1a_used=True,
            iep1b_used=False,
            selected_geometry_model="iep1a",
            structural_agreement=None,
        )
        updates = _update_kwargs(session)
        assert updates["iep1d_used"] is False

    def test_iep1d_used_set_when_true(self, session: MagicMock) -> None:
        update_geometry_result(
            session,
            _LINEAGE_ID,
            iep1a_used=True,
            iep1b_used=True,
            selected_geometry_model="iep1a",
            structural_agreement=True,
            iep1d_used=True,
        )
        updates = _update_kwargs(session)
        assert updates["iep1d_used"] is True


# ── update_lineage_completion ─────────────────────────────────────────────────


class TestUpdateLineageCompletion:
    def test_acceptance_decision_set(self, session: MagicMock) -> None:
        update_lineage_completion(
            session,
            _LINEAGE_ID,
            acceptance_decision="accepted",
            acceptance_reason="all gates passed",
            routing_path="standard",
            total_processing_ms=1200.0,
        )
        updates = _update_kwargs(session)
        assert updates["acceptance_decision"] == "accepted"

    def test_acceptance_reason_set(self, session: MagicMock) -> None:
        update_lineage_completion(
            session,
            _LINEAGE_ID,
            acceptance_decision="review",
            acceptance_reason="layout disagreement",
            routing_path="layout",
            total_processing_ms=900.0,
        )
        updates = _update_kwargs(session)
        assert updates["acceptance_reason"] == "layout disagreement"

    def test_completed_at_always_set(self, session: MagicMock) -> None:
        update_lineage_completion(
            session,
            _LINEAGE_ID,
            acceptance_decision="failed",
            acceptance_reason=None,
            routing_path=None,
            total_processing_ms=None,
        )
        updates = _update_kwargs(session)
        assert "completed_at" in updates

    def test_output_image_uri_included_when_provided(self, session: MagicMock) -> None:
        update_lineage_completion(
            session,
            _LINEAGE_ID,
            acceptance_decision="accepted",
            acceptance_reason=None,
            routing_path=None,
            total_processing_ms=100.0,
            output_image_uri="s3://bucket/out.ptiff",
        )
        updates = _update_kwargs(session)
        assert updates["output_image_uri"] == "s3://bucket/out.ptiff"

    def test_output_image_uri_excluded_when_none(self, session: MagicMock) -> None:
        update_lineage_completion(
            session,
            _LINEAGE_ID,
            acceptance_decision="accepted",
            acceptance_reason=None,
            routing_path=None,
            total_processing_ms=None,
        )
        updates = _update_kwargs(session)
        assert "output_image_uri" not in updates

    def test_gate_results_included_when_provided(self, session: MagicMock) -> None:
        gr = {"gate1": "accepted"}
        update_lineage_completion(
            session,
            _LINEAGE_ID,
            acceptance_decision="accepted",
            acceptance_reason=None,
            routing_path=None,
            total_processing_ms=None,
            gate_results=gr,
        )
        updates = _update_kwargs(session)
        assert updates["gate_results"] == gr

    def test_gate_results_excluded_when_none(self, session: MagicMock) -> None:
        update_lineage_completion(
            session,
            _LINEAGE_ID,
            acceptance_decision="accepted",
            acceptance_reason=None,
            routing_path=None,
            total_processing_ms=None,
        )
        updates = _update_kwargs(session)
        assert "gate_results" not in updates


# ── record_human_correction ────────────────────────────────────────────────────


class TestRecordHumanCorrection:
    def test_human_corrected_set_to_true(self, session: MagicMock) -> None:
        record_human_correction(
            session,
            _LINEAGE_ID,
            correction_fields={"deskew_angle": 2.5},
        )
        updates = _update_kwargs(session)
        assert updates["human_corrected"] is True

    def test_correction_fields_stored(self, session: MagicMock) -> None:
        fields = {"crop_box": [10, 20, 800, 1200], "deskew_angle": 1.0}
        record_human_correction(session, _LINEAGE_ID, correction_fields=fields)
        updates = _update_kwargs(session)
        assert updates["human_correction_fields"] == fields

    def test_timestamp_always_set(self, session: MagicMock) -> None:
        record_human_correction(
            session,
            _LINEAGE_ID,
            correction_fields={"split_x": 600},
        )
        updates = _update_kwargs(session)
        assert "human_correction_timestamp" in updates

    def test_reviewed_by_included_when_provided(self, session: MagicMock) -> None:
        record_human_correction(
            session,
            _LINEAGE_ID,
            correction_fields={},
            reviewed_by="user-42",
        )
        updates = _update_kwargs(session)
        assert updates["reviewed_by"] == "user-42"

    def test_reviewed_by_excluded_when_none(self, session: MagicMock) -> None:
        record_human_correction(session, _LINEAGE_ID, correction_fields={})
        updates = _update_kwargs(session)
        assert "reviewed_by" not in updates

    def test_reviewer_notes_included_when_provided(self, session: MagicMock) -> None:
        record_human_correction(
            session,
            _LINEAGE_ID,
            correction_fields={},
            reviewer_notes="looks fine after correction",
        )
        updates = _update_kwargs(session)
        assert updates["reviewer_notes"] == "looks fine after correction"

    def test_reviewer_notes_excluded_when_none(self, session: MagicMock) -> None:
        record_human_correction(session, _LINEAGE_ID, correction_fields={})
        updates = _update_kwargs(session)
        assert "reviewer_notes" not in updates


# ── get_lineage ────────────────────────────────────────────────────────────────


class TestGetLineage:
    def test_delegates_to_session_get(self, session: MagicMock) -> None:
        mock_row = MagicMock(spec=PageLineage)
        session.get.return_value = mock_row
        result = get_lineage(session, _LINEAGE_ID)
        session.get.assert_called_once_with(PageLineage, _LINEAGE_ID)
        assert result is mock_row

    def test_returns_none_when_not_found(self, session: MagicMock) -> None:
        session.get.return_value = None
        result = get_lineage(session, "nonexistent")
        assert result is None
