"""
tests/test_p5_layout_sub_page.py
----------------------------------
Tests for sub_page_index passthrough in layout service calls (Bug 2 fix).

Verifies that _call_layout_service includes sub_page_index in the payload
sent to IEP2A/IEP2B, so split child pages (sub_page_index=0 and sub_page_index=1)
are distinguished correctly and cannot receive each other's cached results.

Tests:
  1. Unsplit page (sub_page_index=None) — sub_page_index absent from payload
  2. Child 0 (sub_page_index=0) — sub_page_index=0 present in payload
  3. Child 1 (sub_page_index=1) — sub_page_index=1 present in payload
  4. Circuit breaker open — returns None without calling backend
  5. BackendError — returns None, circuit breaker penalised
  6. Malformed response (ValidationError) — returns None, penalised
  7. LayoutDetectRequest schema accepts sub_page_index field
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest
from pydantic import ValidationError

from services.eep_worker.app.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)
from services.eep_worker.app.worker_loop import _call_layout_service
from shared.gpu.backend import BackendError, BackendErrorKind
from shared.schemas.layout import (
    LayoutDetectRequest,
    LayoutDetectResponse,
    LayoutConfSummary,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox

# ── Helpers ────────────────────────────────────────────────────────────────────

_JOB_ID = "job-sub-page-test"
_PAGE_NUMBER = 3
_IMAGE_URI = "local://layout/page3.tiff"
_MATERIAL_TYPE = "book"
_ENDPOINT = "http://iep2a:8004/v1/layout-detect"


def _make_circuit_breaker(state: CircuitState = CircuitState.CLOSED) -> CircuitBreaker:
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, reset_timeout_seconds=30))
    if state == CircuitState.OPEN:
        cb._state = CircuitState.OPEN
        cb._consecutive_failures = 3
    return cb


def _make_detect_response() -> dict:
    return {
        "region_schema_version": "v1",
        "regions": [],
        "layout_conf_summary": {"mean_conf": 0.9, "low_conf_frac": 0.05},
        "region_type_histogram": {},
        "column_structure": None,
        "model_version": "test-v1",
        "detector_type": "detectron2",
        "processing_time_ms": 100.0,
        "warnings": [],
    }


# ── Tests: payload construction ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unsplit_page_omits_sub_page_index_from_payload():
    """sub_page_index=None → sub_page_index absent from payload to IEP2."""
    backend = AsyncMock()
    backend.call = AsyncMock(return_value=_make_detect_response())
    cb = _make_circuit_breaker()

    await _call_layout_service(
        service_name="iep2a",
        endpoint=_ENDPOINT,
        job_id=_JOB_ID,
        page_number=_PAGE_NUMBER,
        sub_page_index=None,
        image_uri=_IMAGE_URI,
        material_type=_MATERIAL_TYPE,
        backend=backend,
        circuit_breaker=cb,
    )

    backend.call.assert_called_once()
    _, payload = backend.call.call_args.args
    assert "sub_page_index" not in payload
    assert payload["job_id"] == _JOB_ID
    assert payload["page_number"] == _PAGE_NUMBER
    assert payload["image_uri"] == _IMAGE_URI


@pytest.mark.asyncio
async def test_child_0_includes_sub_page_index_0_in_payload():
    """sub_page_index=0 (left child) → sub_page_index=0 included in payload."""
    backend = AsyncMock()
    backend.call = AsyncMock(return_value=_make_detect_response())
    cb = _make_circuit_breaker()

    await _call_layout_service(
        service_name="iep2a",
        endpoint=_ENDPOINT,
        job_id=_JOB_ID,
        page_number=_PAGE_NUMBER,
        sub_page_index=0,
        image_uri=_IMAGE_URI,
        material_type=_MATERIAL_TYPE,
        backend=backend,
        circuit_breaker=cb,
    )

    backend.call.assert_called_once()
    _, payload = backend.call.call_args.args
    assert payload["sub_page_index"] == 0
    assert payload["page_number"] == _PAGE_NUMBER
    assert payload["image_uri"] == _IMAGE_URI


@pytest.mark.asyncio
async def test_child_1_includes_sub_page_index_1_in_payload():
    """sub_page_index=1 (right child) → sub_page_index=1 included in payload."""
    backend = AsyncMock()
    backend.call = AsyncMock(return_value=_make_detect_response())
    cb = _make_circuit_breaker()

    await _call_layout_service(
        service_name="iep2b",
        endpoint="http://iep2b:8005/v1/layout-detect",
        job_id=_JOB_ID,
        page_number=_PAGE_NUMBER,
        sub_page_index=1,
        image_uri=_IMAGE_URI,
        material_type=_MATERIAL_TYPE,
        backend=backend,
        circuit_breaker=cb,
    )

    backend.call.assert_called_once()
    _, payload = backend.call.call_args.args
    assert payload["sub_page_index"] == 1
    assert payload["page_number"] == _PAGE_NUMBER


@pytest.mark.asyncio
async def test_child_0_and_child_1_payloads_differ_only_in_sub_page_index():
    """
    When two children of the same page are processed, their payloads contain
    the correct distinct sub_page_index values.  This is the core regression
    test: if sub_page_index were omitted, IEP2 services caching by
    (job_id, page_number) would return child-0's result for child-1.
    """
    backend_a = AsyncMock()
    backend_a.call = AsyncMock(return_value=_make_detect_response())
    backend_b = AsyncMock()
    backend_b.call = AsyncMock(return_value=_make_detect_response())

    cb_a = _make_circuit_breaker()
    cb_b = _make_circuit_breaker()

    # Two separate calls simulating processing child 0 then child 1
    await _call_layout_service(
        service_name="iep2a",
        endpoint=_ENDPOINT,
        job_id=_JOB_ID,
        page_number=_PAGE_NUMBER,
        sub_page_index=0,
        image_uri="local://corrected/page3_0.tiff",
        material_type=_MATERIAL_TYPE,
        backend=backend_a,
        circuit_breaker=cb_a,
    )
    await _call_layout_service(
        service_name="iep2a",
        endpoint=_ENDPOINT,
        job_id=_JOB_ID,
        page_number=_PAGE_NUMBER,
        sub_page_index=1,
        image_uri="local://corrected/page3_1.tiff",
        material_type=_MATERIAL_TYPE,
        backend=backend_b,
        circuit_breaker=cb_b,
    )

    _, payload_child0 = backend_a.call.call_args.args
    _, payload_child1 = backend_b.call.call_args.args

    assert payload_child0["sub_page_index"] == 0
    assert payload_child1["sub_page_index"] == 1
    assert payload_child0["image_uri"] != payload_child1["image_uri"]
    # Everything else is the same
    assert payload_child0["job_id"] == payload_child1["job_id"]
    assert payload_child0["page_number"] == payload_child1["page_number"]


# ── Tests: circuit breaker and error handling ──────────────────────────────────


@pytest.mark.asyncio
async def test_open_circuit_breaker_returns_none_without_backend_call():
    backend = AsyncMock()
    backend.call = AsyncMock()
    cb = _make_circuit_breaker(state=CircuitState.OPEN)

    result = await _call_layout_service(
        service_name="iep2a",
        endpoint=_ENDPOINT,
        job_id=_JOB_ID,
        page_number=_PAGE_NUMBER,
        sub_page_index=0,
        image_uri=_IMAGE_URI,
        material_type=_MATERIAL_TYPE,
        backend=backend,
        circuit_breaker=cb,
    )

    assert result is None
    backend.call.assert_not_called()


@pytest.mark.asyncio
async def test_backend_error_returns_none_and_penalises_circuit_breaker():
    backend = AsyncMock()
    backend.call = AsyncMock(
        side_effect=BackendError(BackendErrorKind.SERVICE_ERROR, "service down")
    )
    cb = _make_circuit_breaker()
    initial_failures = cb._consecutive_failures

    result = await _call_layout_service(
        service_name="iep2a",
        endpoint=_ENDPOINT,
        job_id=_JOB_ID,
        page_number=_PAGE_NUMBER,
        sub_page_index=1,
        image_uri=_IMAGE_URI,
        material_type=_MATERIAL_TYPE,
        backend=backend,
        circuit_breaker=cb,
    )

    assert result is None
    assert cb._consecutive_failures > initial_failures


@pytest.mark.asyncio
async def test_malformed_response_returns_none():
    """ValidationError from malformed IEP2 response → None returned."""
    backend = AsyncMock()
    backend.call = AsyncMock(return_value={"not_a_valid": "response"})
    cb = _make_circuit_breaker()

    result = await _call_layout_service(
        service_name="iep2b",
        endpoint=_ENDPOINT,
        job_id=_JOB_ID,
        page_number=_PAGE_NUMBER,
        sub_page_index=0,
        image_uri=_IMAGE_URI,
        material_type=_MATERIAL_TYPE,
        backend=backend,
        circuit_breaker=cb,
    )

    assert result is None


# ── Tests: LayoutDetectRequest schema ─────────────────────────────────────────


def test_layout_detect_request_accepts_sub_page_index_none():
    req = LayoutDetectRequest(
        job_id="job-1",
        page_number=1,
        image_uri="local://page1.tiff",
        material_type="book",
    )
    assert req.sub_page_index is None


def test_layout_detect_request_accepts_sub_page_index_0():
    req = LayoutDetectRequest(
        job_id="job-1",
        page_number=1,
        sub_page_index=0,
        image_uri="local://page1_0.tiff",
        material_type="book",
    )
    assert req.sub_page_index == 0


def test_layout_detect_request_accepts_sub_page_index_1():
    req = LayoutDetectRequest(
        job_id="job-1",
        page_number=1,
        sub_page_index=1,
        image_uri="local://page1_1.tiff",
        material_type="book",
    )
    assert req.sub_page_index == 1


def test_layout_detect_request_serializes_sub_page_index():
    """sub_page_index is included in the serialized payload when set."""
    req = LayoutDetectRequest(
        job_id="job-1",
        page_number=2,
        sub_page_index=0,
        image_uri="local://page2_0.tiff",
        material_type="newspaper",
    )
    data = req.model_dump()
    assert data["sub_page_index"] == 0


def test_layout_detect_request_sub_page_index_defaults_to_none():
    """Existing callers without sub_page_index are not broken."""
    req = LayoutDetectRequest(
        job_id="job-1",
        page_number=5,
        image_uri="local://page5.tiff",
        material_type="archival_document",
    )
    data = req.model_dump()
    assert data["sub_page_index"] is None
