"""
tests/test_p7_lineage.py
--------------------------
Packet 7.5 contract tests for GET /v1/lineage/{job_id}/{page_number}.

Tests cover:
  - 401 when no bearer token supplied
  - 401 when invalid bearer token supplied
  - 403 when a non-admin caller accesses the endpoint
  - 404 when no lineage records exist for the given (job_id, page_number)
  - 200 with correct top-level schema fields
  - lineage record contains all page_lineage fields
  - service_invocations are embedded within each lineage record
  - quality_gates list is populated correctly
  - unsplit page: single lineage record, sub_page_index None
  - split page: two lineage records with sub_page_index 0 and 1
  - empty service_invocations list when no invocations exist
  - empty quality_gates list when no gate records exist
  - invocations are scoped per lineage_id (not cross-contaminated)

Uses a mini FastAPI app with only the lineage router so that real auth is
tested for 401/403 cases.  Auth-bypassed cases use dependency_overrides.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, create_access_token, require_admin
from services.eep.app.db.session import get_session
from services.eep.app.lineage_api import router

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TS = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
_JOB_ID = "job-001"
_PAGE = 3


# ---------------------------------------------------------------------------
# Helpers — mock ORM objects
# ---------------------------------------------------------------------------


def _make_lineage(
    lineage_id: str = "lin-001",
    job_id: str = _JOB_ID,
    page_number: int = _PAGE,
    sub_page_index: int | None = None,
) -> MagicMock:
    row = MagicMock()
    row.lineage_id = lineage_id
    row.job_id = job_id
    row.page_number = page_number
    row.sub_page_index = sub_page_index
    row.correlation_id = "corr-001"
    row.input_image_uri = "s3://bucket/input.tiff"
    row.input_image_hash = "abc123"
    row.otiff_uri = "s3://bucket/otiff.tiff"
    row.reference_ptiff_uri = None
    row.ptiff_ssim = None
    row.iep1a_used = True
    row.iep1b_used = True
    row.selected_geometry_model = "iep1a"
    row.structural_agreement = True
    row.iep1d_used = False
    row.material_type = "book"
    row.routing_path = "preprocessing_only"
    row.policy_version = "v1.0"
    row.acceptance_decision = "accepted"
    row.acceptance_reason = "all gates passed"
    row.gate_results = {"geometry": "pass"}
    row.total_processing_ms = 1234.5
    row.shadow_eval_id = None
    row.cleanup_retry_count = 0
    row.preprocessed_artifact_state = "confirmed"
    row.layout_artifact_state = "confirmed"
    row.output_image_uri = "s3://bucket/output.tiff"
    row.parent_page_id = None
    row.split_source = False
    row.human_corrected = False
    row.human_correction_timestamp = None
    row.human_correction_fields = None
    row.reviewed_by = None
    row.reviewed_at = None
    row.reviewer_notes = None
    row.created_at = _TS
    row.completed_at = _TS
    return row


def _make_invocation(
    inv_id: int = 1,
    lineage_id: str = "lin-001",
    service_name: str = "iep1a",
) -> MagicMock:
    inv = MagicMock()
    inv.id = inv_id
    inv.lineage_id = lineage_id
    inv.service_name = service_name
    inv.service_version = "1.0"
    inv.model_version = "model-v1"
    inv.model_source = "local"
    inv.invoked_at = _TS
    inv.completed_at = _TS
    inv.processing_time_ms = 200.0
    inv.status = "success"
    inv.error_message = None
    inv.metrics = {"score": 0.9}
    inv.config_snapshot = None
    return inv


def _make_gate(
    gate_id: str = "gate-001",
    job_id: str = _JOB_ID,
    page_number: int = _PAGE,
) -> MagicMock:
    g = MagicMock()
    g.gate_id = gate_id
    g.job_id = job_id
    g.page_number = page_number
    g.gate_type = "geometry_selection"
    g.iep1a_geometry = {"width": 100}
    g.iep1b_geometry = {"width": 100}
    g.structural_agreement = True
    g.selected_model = "iep1a"
    g.selection_reason = "agreement"
    g.sanity_check_results = None
    g.split_confidence = None
    g.tta_variance = None
    g.artifact_validation_score = None
    g.route_decision = "accepted"
    g.review_reason = None
    g.processing_time_ms = 50.0
    g.created_at = _TS
    return g


def _make_session(
    lineage_rows: list[Any],
    invocation_rows: list[Any],
    gate_rows: list[Any],
) -> MagicMock:
    """
    Build a mock Session whose query chain returns the given rows.

    Query call order in the endpoint:
      1. PageLineage  → lineage_rows
      2. ServiceInvocation → invocation_rows
      3. QualityGateLog   → gate_rows
    """
    session = MagicMock(spec=Session)

    call_count = [0]
    results = [lineage_rows, invocation_rows, gate_rows]

    def _query_side_effect(*args, **kwargs):
        chain = MagicMock()
        chain.filter.return_value = chain
        chain.order_by.return_value = chain
        chain.in_.return_value = chain
        idx = call_count[0]
        call_count[0] += 1
        chain.all.return_value = results[idx] if idx < len(results) else []
        return chain

    session.query.side_effect = _query_side_effect
    return session


def _bearer(user_id: str, role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mini_app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture()
def inject_admin(mini_app: FastAPI):
    """Inject a mock session and an admin user; yield TestClient."""

    def _setup(
        lineage_rows: list[Any],
        invocation_rows: list[Any],
        gate_rows: list[Any],
    ) -> TestClient:
        mini_app.dependency_overrides[get_session] = lambda: _make_session(
            lineage_rows, invocation_rows, gate_rows
        )
        mini_app.dependency_overrides[require_admin] = lambda: CurrentUser(
            user_id="admin-001", role="admin"
        )
        return TestClient(mini_app, raise_server_exceptions=False)

    yield _setup
    mini_app.dependency_overrides.pop(get_session, None)
    mini_app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


class TestLineageAuth:
    def _bare_client(self, mini_app: FastAPI) -> TestClient:
        mini_app.dependency_overrides[get_session] = lambda: _make_session([], [], [])
        return TestClient(mini_app, raise_server_exceptions=False)

    def _cleanup(self, mini_app: FastAPI) -> None:
        mini_app.dependency_overrides.pop(get_session, None)

    def test_401_no_token(self, mini_app: FastAPI) -> None:
        client = self._bare_client(mini_app)
        r = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}")
        assert r.status_code == 401
        self._cleanup(mini_app)

    def test_401_invalid_token(self, mini_app: FastAPI) -> None:
        client = self._bare_client(mini_app)
        r = client.get(
            f"/v1/lineage/{_JOB_ID}/{_PAGE}",
            headers={"Authorization": "Bearer garbage"},
        )
        assert r.status_code == 401
        self._cleanup(mini_app)

    def test_403_non_admin(self, mini_app: FastAPI) -> None:
        client = self._bare_client(mini_app)
        r = client.get(
            f"/v1/lineage/{_JOB_ID}/{_PAGE}",
            headers=_bearer("user-001", role="user"),
        )
        assert r.status_code == 403
        self._cleanup(mini_app)


# ---------------------------------------------------------------------------
# 404 — no lineage
# ---------------------------------------------------------------------------


class TestLineage404:
    def test_404_when_no_lineage_rows(self, inject_admin: Any) -> None:
        client = inject_admin([], [], [])
        r = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}")
        assert r.status_code == 404

    def test_404_detail_mentions_job_and_page(self, inject_admin: Any) -> None:
        client = inject_admin([], [], [])
        r = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}")
        detail = r.json()["detail"]
        assert _JOB_ID in detail
        assert str(_PAGE) in detail


# ---------------------------------------------------------------------------
# 200 — top-level schema
# ---------------------------------------------------------------------------


class TestLineageSchema:
    def test_200_top_level_fields(self, inject_admin: Any) -> None:
        """Response must have exactly: job_id, page_number, lineage, quality_gates."""
        lin = _make_lineage()
        client = inject_admin([lin], [], [])
        r = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}")
        assert r.status_code == 200
        data = r.json()
        assert set(data.keys()) == {"job_id", "page_number", "lineage", "quality_gates"}

    def test_200_job_id_and_page_echoed(self, inject_admin: Any) -> None:
        lin = _make_lineage()
        client = inject_admin([lin], [], [])
        data = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()
        assert data["job_id"] == _JOB_ID
        assert data["page_number"] == _PAGE


# ---------------------------------------------------------------------------
# lineage record fields
# ---------------------------------------------------------------------------


class TestLineageRecordFields:
    def test_lineage_record_has_all_fields(self, inject_admin: Any) -> None:
        """All page_lineage column names must appear in the lineage record."""
        lin = _make_lineage()
        client = inject_admin([lin], [], [])
        item = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["lineage"][0]
        expected_fields = {
            "lineage_id", "job_id", "page_number", "sub_page_index",
            "correlation_id", "input_image_uri", "input_image_hash", "otiff_uri",
            "reference_ptiff_uri", "ptiff_ssim", "iep1a_used", "iep1b_used",
            "selected_geometry_model", "structural_agreement", "iep1d_used",
            "material_type", "routing_path", "policy_version",
            "acceptance_decision", "acceptance_reason", "gate_results",
            "total_processing_ms", "shadow_eval_id", "cleanup_retry_count",
            "preprocessed_artifact_state", "layout_artifact_state",
            "output_image_uri", "parent_page_id", "split_source",
            "human_corrected", "human_correction_timestamp",
            "human_correction_fields", "reviewed_by", "reviewed_at",
            "reviewer_notes", "created_at", "completed_at",
            "service_invocations",
        }
        assert set(item.keys()) == expected_fields

    def test_lineage_record_values(self, inject_admin: Any) -> None:
        lin = _make_lineage(lineage_id="lin-abc")
        client = inject_admin([lin], [], [])
        item = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["lineage"][0]
        assert item["lineage_id"] == "lin-abc"
        assert item["iep1a_used"] is True
        assert item["acceptance_decision"] == "accepted"
        assert item["preprocessed_artifact_state"] == "confirmed"
        assert item["sub_page_index"] is None

    def test_human_corrected_fields(self, inject_admin: Any) -> None:
        lin = _make_lineage()
        lin.human_corrected = True
        lin.human_correction_timestamp = _TS
        lin.human_correction_fields = {"deskew_angle": 1.5}
        lin.reviewed_by = "admin-001"
        client = inject_admin([lin], [], [])
        item = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["lineage"][0]
        assert item["human_corrected"] is True
        assert item["human_correction_fields"] == {"deskew_angle": 1.5}
        assert item["reviewed_by"] == "admin-001"


# ---------------------------------------------------------------------------
# service_invocations embedding
# ---------------------------------------------------------------------------


class TestServiceInvocations:
    def test_empty_invocations_when_none(self, inject_admin: Any) -> None:
        lin = _make_lineage()
        client = inject_admin([lin], [], [])
        item = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["lineage"][0]
        assert item["service_invocations"] == []

    def test_invocations_embedded_in_lineage_record(self, inject_admin: Any) -> None:
        lin = _make_lineage(lineage_id="lin-001")
        inv = _make_invocation(inv_id=42, lineage_id="lin-001", service_name="iep1a")
        client = inject_admin([lin], [inv], [])
        item = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["lineage"][0]
        assert len(item["service_invocations"]) == 1
        assert item["service_invocations"][0]["id"] == 42
        assert item["service_invocations"][0]["service_name"] == "iep1a"
        assert item["service_invocations"][0]["status"] == "success"

    def test_invocation_fields_complete(self, inject_admin: Any) -> None:
        lin = _make_lineage()
        inv = _make_invocation()
        client = inject_admin([lin], [inv], [])
        si = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["lineage"][0][
            "service_invocations"
        ][0]
        expected = {
            "id", "lineage_id", "service_name", "service_version",
            "model_version", "model_source", "invoked_at", "completed_at",
            "processing_time_ms", "status", "error_message", "metrics",
            "config_snapshot",
        }
        assert set(si.keys()) == expected

    def test_invocations_scoped_to_correct_lineage_id(self, inject_admin: Any) -> None:
        """
        Invocations are grouped by lineage_id.  When two lineage rows exist,
        each row's service_invocations must only contain invocations whose
        lineage_id matches that row.
        """
        lin0 = _make_lineage(lineage_id="lin-0", sub_page_index=0)
        lin1 = _make_lineage(lineage_id="lin-1", sub_page_index=1)
        inv0 = _make_invocation(inv_id=10, lineage_id="lin-0", service_name="iep1a")
        inv1 = _make_invocation(inv_id=20, lineage_id="lin-1", service_name="iep1b")
        client = inject_admin([lin0, lin1], [inv0, inv1], [])
        lineage = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["lineage"]
        assert len(lineage) == 2
        ids_0 = [i["id"] for i in lineage[0]["service_invocations"]]
        ids_1 = [i["id"] for i in lineage[1]["service_invocations"]]
        assert ids_0 == [10]
        assert ids_1 == [20]


# ---------------------------------------------------------------------------
# quality_gates
# ---------------------------------------------------------------------------


class TestQualityGates:
    def test_empty_quality_gates(self, inject_admin: Any) -> None:
        lin = _make_lineage()
        client = inject_admin([lin], [], [])
        data = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()
        assert data["quality_gates"] == []

    def test_quality_gate_fields_complete(self, inject_admin: Any) -> None:
        lin = _make_lineage()
        gate = _make_gate()
        client = inject_admin([lin], [], [gate])
        g = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["quality_gates"][0]
        expected = {
            "gate_id", "job_id", "page_number", "gate_type",
            "iep1a_geometry", "iep1b_geometry", "structural_agreement",
            "selected_model", "selection_reason", "sanity_check_results",
            "split_confidence", "tta_variance", "artifact_validation_score",
            "route_decision", "review_reason", "processing_time_ms", "created_at",
        }
        assert set(g.keys()) == expected

    def test_quality_gate_values(self, inject_admin: Any) -> None:
        lin = _make_lineage()
        gate = _make_gate(gate_id="gate-xyz")
        client = inject_admin([lin], [], [gate])
        g = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["quality_gates"][0]
        assert g["gate_id"] == "gate-xyz"
        assert g["route_decision"] == "accepted"
        assert g["structural_agreement"] is True

    def test_multiple_gates_returned(self, inject_admin: Any) -> None:
        lin = _make_lineage()
        g1 = _make_gate(gate_id="gate-001")
        g2 = _make_gate(gate_id="gate-002")
        g2.gate_type = "artifact_validation"
        client = inject_admin([lin], [], [g1, g2])
        gates = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["quality_gates"]
        assert len(gates) == 2
        assert gates[0]["gate_id"] == "gate-001"
        assert gates[1]["gate_id"] == "gate-002"


# ---------------------------------------------------------------------------
# Split page — two lineage records
# ---------------------------------------------------------------------------


class TestSplitPage:
    def test_split_page_returns_two_lineage_records(self, inject_admin: Any) -> None:
        lin0 = _make_lineage(lineage_id="lin-0", sub_page_index=0)
        lin1 = _make_lineage(lineage_id="lin-1", sub_page_index=1)
        lin0.split_source = True
        lin1.split_source = True
        client = inject_admin([lin0, lin1], [], [])
        lineage = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["lineage"]
        assert len(lineage) == 2

    def test_split_page_sub_page_indices(self, inject_admin: Any) -> None:
        lin0 = _make_lineage(lineage_id="lin-0", sub_page_index=0)
        lin1 = _make_lineage(lineage_id="lin-1", sub_page_index=1)
        client = inject_admin([lin0, lin1], [], [])
        lineage = client.get(f"/v1/lineage/{_JOB_ID}/{_PAGE}").json()["lineage"]
        indices = [rec["sub_page_index"] for rec in lineage]
        assert 0 in indices
        assert 1 in indices
