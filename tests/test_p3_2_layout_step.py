"""
tests/test_p3_2_layout_step.py
-------------------------------
Integration tests for complete_layout_detection() — the layout_step.py
orchestration layer that wires evaluate_layout_adjudication() into the
worker DB transition path.

Tests verify:
  1. Google client is sourced from get_google_worker_state() when not
     supplied explicitly (default _USE_WORKER_GOOGLE sentinel).
  2. Local agreement path: page state advances to "accepted".
  3. Google fallback path: page state still advances to "accepted".
  4. IEP2 never routes to review — always "accepted".
  5. layout_consensus_result is written to the page row.
  6. update_lineage_completion is called once with the correct decision.
  7. Explicit google_client=None suppresses Google (overrides worker state).

All DB calls are mocked; no real DB or Redis needed.
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from services.eep_worker.app.layout_step import (
    LayoutStepResult,
    complete_layout_detection,
    run_layout_adjudication_only,
)
from services.eep_worker.app.google_config import GoogleWorkerState
from shared.schemas.layout import (
    LayoutAdjudicationResult,
    LayoutConfSummary,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox

# ── Helpers ───────────────────────────────────────────────────────────────────


def _bbox(x_min: float = 0.0, y_min: float = 0.0, x_max: float = 100.0, y_max: float = 50.0) -> BoundingBox:
    return BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


def _region(rid: str, rtype: RegionType = RegionType.text_block) -> Region:
    return Region(id=rid, type=rtype, bbox=_bbox(), confidence=0.9)


def _conf_summary() -> LayoutConfSummary:
    return LayoutConfSummary(mean_conf=0.88, low_conf_frac=0.05)


def _detect_response(
    regions: list[Region],
    detector_type: Literal["detectron2", "doclayout_yolo", "paddleocr_pp_doclayout_v2"] = "paddleocr_pp_doclayout_v2",
) -> LayoutDetectResponse:
    histogram: dict[str, int] = {}
    for r in regions:
        histogram[r.type.value] = histogram.get(r.type.value, 0) + 1
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


def _make_page(page_id: str = "page-1", status: str = "layout_detection") -> MagicMock:
    page = MagicMock()
    page.page_id = page_id
    page.status = status
    page.output_image_uri = "s3://bucket/page.tiff"
    return page


def _make_session() -> MagicMock:
    session = MagicMock()
    # advance_page_state is synchronous; return True (CAS success)
    return session


def _make_google_client(regions: list[Region]) -> MagicMock:
    """Mock CallGoogleDocumentAI with process_layout returning given regions."""
    client = MagicMock()
    google_raw = {
        "elements": ["e1"],
        "page_width": 800,
        "page_height": 1100,
        "region_count": len(regions),
        "raw_response": object(),
    }
    client.process_layout = AsyncMock(return_value=google_raw)
    client._map_google_to_canonical = MagicMock(return_value=regions)
    return client


# ── Common setup patches ──────────────────────────────────────────────────────

_AGREE_A = [_region("r1", RegionType.text_block), _region("r2", RegionType.title)]
_AGREE_B = [_region("r1", RegionType.text_block), _region("r2", RegionType.title)]
_DISAGREE_A = [_region("r1", RegionType.text_block)]
_DISAGREE_B = [
    Region(id="r1", type=RegionType.image, bbox=_bbox(500, 500, 600, 600), confidence=0.8),
    Region(id="r2", type=RegionType.table, bbox=_bbox(700, 700, 800, 800), confidence=0.7),
]


# ── Test 1: Google client sourced from worker state by default ────────────────


class TestGoogleClientWiring:
    @pytest.mark.asyncio
    async def test_worker_state_client_used_when_not_supplied(self) -> None:
        """
        When google_client is not passed (default _USE_WORKER_GOOGLE sentinel),
        complete_layout_detection() must call get_google_worker_state().client.
        """
        google_client = _make_google_client([_region("r1")])
        mock_state = GoogleWorkerState(enabled=True, config=MagicMock(), client=google_client)

        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state", return_value=mock_state),
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                # google_client not supplied → uses worker state
            )

        # Google must have been called since IEP2A and IEP2B disagree
        google_client.process_layout.assert_awaited_once()
        assert result.adjudication.layout_decision_source == "google_document_ai"

    @pytest.mark.asyncio
    async def test_explicit_none_suppresses_google(self) -> None:
        """
        Passing google_client=None explicitly must suppress the Google call
        even when get_google_worker_state() would return an enabled client.
        """
        google_client = _make_google_client([_region("r1")])
        mock_state = GoogleWorkerState(enabled=True, config=MagicMock(), client=google_client)

        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state", return_value=mock_state),
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,  # explicit override
            )

        # Google must NOT have been called
        google_client.process_layout.assert_not_awaited()
        assert result.adjudication.layout_decision_source == "local_fallback_unverified"


# ── Test 2: IEP2 always routes to "accepted" ─────────────────────────────────


class TestIEP2AlwaysAccepted:
    @pytest.mark.asyncio
    async def test_local_agreement_routes_to_accepted(self) -> None:
        """Local agreement → advance_page_state called with next_state='accepted'."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True) as mock_advance,
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        assert result.routing.next_state == "accepted"
        assert result.routing.acceptance_decision == "accepted"
        assert result.routing.review_reason is None
        # advance_page_state(session, page_id, from_state, to_state, ...)
        # to_state is at positional index 3
        advance_call = mock_advance.call_args
        assert advance_call.args[3] == "accepted"

    @pytest.mark.asyncio
    async def test_google_fallback_routes_to_accepted(self) -> None:
        """Google fallback path also routes to 'accepted' (IEP2 never reviews)."""
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        google_client = _make_google_client([_region("r1"), _region("r2")])
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client,
            )

        assert result.routing.next_state == "accepted"
        assert result.adjudication.layout_decision_source == "google_document_ai"
        assert result.adjudication.status == "done"

    @pytest.mark.asyncio
    async def test_google_hard_fail_routes_to_accepted(self) -> None:
        """Google hard failure also routes to 'accepted' — never pending_human_correction."""
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()
        google_client_timeout = MagicMock()
        google_client_timeout.process_layout = AsyncMock(side_effect=TimeoutError("timeout"))

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client_timeout,
            )

        assert result.routing.next_state == "accepted"
        assert result.adjudication.layout_decision_source == "local_fallback_unverified"
        assert result.adjudication.status == "done"

    @pytest.mark.asyncio
    async def test_both_iep2_failed_routes_to_accepted(self) -> None:
        """Even when both IEP2A and IEP2B fail, IEP2 still routes to 'accepted'."""
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=None,
                iep2b_result=None,
                google_client=None,
            )

        assert result.routing.next_state == "accepted"
        assert result.adjudication.final_layout_result == []


# ── Test 3: LayoutTransitionError on CAS miss ─────────────────────────────────


class TestLayoutTransitionError:
    @pytest.mark.asyncio
    async def test_cas_miss_raises_layout_transition_error(self) -> None:
        """advance_page_state returns False → LayoutTransitionError raised."""
        from services.eep_worker.app.layout_step import LayoutTransitionError

        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=False),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            with pytest.raises(LayoutTransitionError):
                await complete_layout_detection(
                    session=session,
                    page=page,
                    lineage_id="lin-1",
                    material_type="book",
                    image_uri="s3://bucket/page.tiff",
                    iep2a_result=iep2a,
                    iep2b_result=iep2b,
                    google_client=None,
                )


# ── Test 4: Result type invariants ────────────────────────────────────────────


class TestResultType:
    @pytest.mark.asyncio
    async def test_returns_layout_step_result(self) -> None:
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        assert isinstance(result, LayoutStepResult)
        assert isinstance(result.adjudication, LayoutAdjudicationResult)

    @pytest.mark.asyncio
    async def test_update_lineage_completion_called_once(self) -> None:
        """update_lineage_completion must be called exactly once per task."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion") as mock_update,
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        assert mock_update.call_count == 1
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs.get("acceptance_decision") == "accepted"

    @pytest.mark.asyncio
    async def test_local_agreement_acceptance_reason(self) -> None:
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        assert "local agreement" in result.routing.acceptance_reason.lower()

    @pytest.mark.asyncio
    async def test_google_decision_acceptance_reason(self) -> None:
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        google_client = _make_google_client([_region("r1")])
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client,
            )

        assert "google" in result.routing.acceptance_reason.lower()


# ── Test 5: End-to-end pipeline integration — disagreement path ───────────────
#
# These tests drive the full pipeline path:
#   IEP2A/IEP2B disagree  →  evaluate_layout_adjudication  →  Google called
#   →  result persisted to DB  →  lineage updated
#
# DB calls are mocked; adjudication logic runs for real.


class TestEndToEndDisagreementPath:
    """
    Full pipeline: disagreement → Google → persisted google_document_ai result.

    Validates:
    - Google is invoked on IEP2A/IEP2B disagreement
    - layout_decision_source == "google_document_ai"
    - fallback_used == False when Google succeeds
    - final_layout_result non-empty when Google returns regions
    - persisted layout_consensus_result JSON has all required fields
    - lineage and DB state updated correctly
    """

    @pytest.mark.asyncio
    async def test_disagreement_google_success_persisted_json_shape(self) -> None:
        """
        Full path: IEP2A/IEP2B disagree → Google succeeds → adjudication
        result persisted to job_pages.layout_consensus_result.

        Verifies all required fields in the persisted JSON.
        """
        google_regions = [
            _region("r0", RegionType.text_block),
            _region("r1", RegionType.title),
            _region("r2", RegionType.text_block),
        ]
        google_client = _make_google_client(google_regions)
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        # Capture what gets written to the DB
        update_call_args: list = []

        def _capture_update(values, **kwargs):
            update_call_args.append(values)

        session.query.return_value.filter.return_value.update.side_effect = _capture_update

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-e2e",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                image_bytes=b"fake-image-bytes",
                mime_type="image/tiff",
                google_client=google_client,
            )

        # ── Core adjudication assertions ──
        adj = result.adjudication
        assert adj.layout_decision_source == "google_document_ai"
        assert adj.fallback_used is False          # Google succeeded — not a fallback
        assert adj.agreed is False
        assert adj.status == "done"
        assert len(adj.final_layout_result) == 3
        assert adj.google_response_time_ms is not None
        assert adj.google_response_time_ms >= 0.0
        assert adj.processing_time_ms >= adj.google_response_time_ms

        # ── Google was actually called ──
        google_client.process_layout.assert_awaited_once()

        # ── Routing always "accepted" for IEP2 ──
        assert result.routing.next_state == "accepted"
        assert result.routing.acceptance_decision == "accepted"
        assert "google" in result.routing.acceptance_reason.lower()

        # ── Persisted JSON shape ──
        assert update_call_args, "session.query().filter().update() was never called"
        persisted = update_call_args[0]
        assert "layout_consensus_result" in persisted
        layout_json = persisted["layout_consensus_result"]

        # Required top-level keys
        assert layout_json["layout_decision_source"] == "google_document_ai"
        assert layout_json["fallback_used"] is False
        assert layout_json["agreed"] is False
        assert layout_json["status"] == "done"
        assert len(layout_json["final_layout_result"]) == 3
        assert layout_json["google_document_ai_result"] is not None
        assert layout_json["google_response_time_ms"] is not None
        assert layout_json["processing_time_ms"] is not None
        # IEP2A and IEP2B results present
        assert layout_json["iep2a_result"] is not None
        assert layout_json["iep2b_result"] is not None
        # Region shape: each final region has id, type, bbox
        r0 = layout_json["final_layout_result"][0]
        assert "id" in r0
        assert "type" in r0
        assert "bbox" in r0
        assert set(r0["bbox"].keys()) == {"x_min", "y_min", "x_max", "y_max"}

    @pytest.mark.asyncio
    async def test_google_success_fallback_used_is_false(self) -> None:
        """Google succeeds → fallback_used=False in both adjudication and persisted JSON."""
        google_regions = [_region("r0"), _region("r1")]
        google_client = _make_google_client(google_regions)
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-2",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client,
            )

        assert result.adjudication.fallback_used is False
        # Also verify the serialized form
        serialized = result.adjudication.model_dump(mode="json")
        assert serialized["fallback_used"] is False

    @pytest.mark.asyncio
    async def test_google_hard_fail_fallback_used_is_true(self) -> None:
        """Google hard failure → fallback_used=True, local result used."""
        from unittest.mock import AsyncMock

        failing_client = MagicMock()
        failing_client.process_layout = AsyncMock(side_effect=TimeoutError("google timeout"))

        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-3",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=failing_client,
            )

        assert result.adjudication.fallback_used is True
        assert result.adjudication.layout_decision_source == "local_fallback_unverified"
        # local fallback still routes to accepted
        assert result.routing.next_state == "accepted"

    @pytest.mark.asyncio
    async def test_persisted_json_google_audit_fields_populated(self) -> None:
        """google_document_ai_result audit dict in persisted JSON has required audit keys."""
        google_regions = [_region("r0")]
        google_client = _make_google_client(google_regions)
        iep2a = _detect_response(_DISAGREE_A)
        iep2b = _detect_response(_DISAGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        captured: list = []
        session.query.return_value.filter.return_value.update.side_effect = (
            lambda v, **kw: captured.append(v)
        )

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state", return_value=True),
            patch("services.eep_worker.app.layout_step.update_lineage_completion"),
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            await complete_layout_detection(
                session=session,
                page=page,
                lineage_id="lin-4",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=google_client,
            )

        layout_json = captured[0]["layout_consensus_result"]
        audit = layout_json["google_document_ai_result"]
        assert audit is not None
        # Audit dict must carry these keys for frontend display
        assert audit["attempted"] is True
        assert audit["success"] is True
        assert audit["hard_failure"] is False
        assert "region_count" in audit
        assert "page_width" in audit
        assert "page_height" in audit


# ── Test 6: run_layout_adjudication_only ──────────────────────────────────────


class TestRunLayoutAdjudicationOnly:
    """
    Tests for run_layout_adjudication_only() — inline layout step that runs
    adjudication and persists results WITHOUT transitioning page state.

    Called when IEP1 routes to pending_human_correction in auto_continue +
    layout pipeline mode, so reviewers can see layout output immediately.
    """

    @pytest.mark.asyncio
    async def test_returns_adjudication_result(self) -> None:
        """run_layout_adjudication_only returns a LayoutAdjudicationResult."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await run_layout_adjudication_only(
                session=session,
                page=page,
                lineage_id=None,
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        assert isinstance(result, LayoutAdjudicationResult)

    @pytest.mark.asyncio
    async def test_does_not_call_advance_page_state(self) -> None:
        """run_layout_adjudication_only must never call advance_page_state."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
            patch("services.eep_worker.app.layout_step.advance_page_state") as mock_advance,
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            await run_layout_adjudication_only(
                session=session,
                page=page,
                lineage_id=None,
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        mock_advance.assert_not_called()

    @pytest.mark.asyncio
    async def test_persists_layout_consensus_result_on_page(self) -> None:
        """layout_consensus_result is written to job_pages via session.query().update()."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        captured: list = []
        session.query.return_value.filter.return_value.update.side_effect = (
            lambda v, **kw: captured.append(v)
        )

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            await run_layout_adjudication_only(
                session=session,
                page=page,
                lineage_id=None,
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        assert captured, "session.query().filter().update() was never called"
        assert "layout_consensus_result" in captured[0]

    @pytest.mark.asyncio
    async def test_updates_lineage_gate_results(self) -> None:
        """When lineage_id is provided, gate_results['layout_adjudication'] is set."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        mock_lineage = MagicMock()
        mock_lineage.gate_results = {}
        session.get.return_value = mock_lineage

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            await run_layout_adjudication_only(
                session=session,
                page=page,
                lineage_id="lin-inline-1",
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        assert "layout_adjudication" in mock_lineage.gate_results
        adj_json = mock_lineage.gate_results["layout_adjudication"]
        assert adj_json["status"] == "done"

    @pytest.mark.asyncio
    async def test_skips_lineage_update_when_lineage_id_is_none(self) -> None:
        """When lineage_id is None, session.get is never called for PageLineage."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            await run_layout_adjudication_only(
                session=session,
                page=page,
                lineage_id=None,
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_agreement_result_agreed_true(self) -> None:
        """Local agreement produces agreed=True in the returned result."""
        iep2a = _detect_response(_AGREE_A)
        iep2b = _detect_response(_AGREE_B, detector_type="doclayout_yolo")
        session = _make_session()
        page = _make_page()

        with (
            patch("services.eep_worker.app.layout_step.get_google_worker_state") as mock_gs,
        ):
            mock_gs.return_value = GoogleWorkerState(enabled=False, config=None, client=None)
            result = await run_layout_adjudication_only(
                session=session,
                page=page,
                lineage_id=None,
                material_type="book",
                image_uri="s3://bucket/page.tiff",
                iep2a_result=iep2a,
                iep2b_result=iep2b,
                google_client=None,
            )

        assert result.agreed is True
        assert result.layout_decision_source == "local_agreement"
        assert result.status == "done"
