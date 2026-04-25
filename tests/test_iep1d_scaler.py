"""
tests/test_iep1d_scaler.py
---------------------------
Tests for services/eep_worker/app/iep1d_scaler.py

Covers:
  1.  disabled mode → ensure_iep1d_ready() raises Iep1dUnavailableError immediately
  2.  noop mode, /ready 200 → returns without AWS calls
  3.  noop mode, /ready timeout → raises Iep1dUnavailableError
  4.  ecs mode, lock acquired → calls _ecs_scale_to_one + _poll_ready; releases lock
  5.  ecs mode, lock not acquired → skips _ecs_scale_to_one, calls _poll_ready only
  6.  ecs mode, _ecs_scale_to_one timeout → raises Iep1dUnavailableError, lock deleted
  7.  ecs mode, /ready timeout after stable → raises Iep1dUnavailableError
  8.  ecs mode, no Redis client → raises Iep1dUnavailableError (no ECS call)
  9.  build_iep1d_scaler(None) → DISABLED mode regardless of IEP1D_SCALER_MODE env
  10. build_iep1d_scaler(redis) with IEP1D_SCALER_MODE=ecs → ECS mode
  11. build_iep1d_scaler unknown mode → defaults to DISABLED
  12. maybe_scale_down_iep1d → no-op (does not raise)
  13. get_iep1d_status disabled mode → mode=disabled, ready=False
  14. get_iep1d_status ecs mode → returns running/desired counts from boto3
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.eep_worker.app.iep1d_scaler import (
    Iep1dScaler,
    Iep1dScalerConfig,
    Iep1dScalerMode,
    Iep1dUnavailableError,
    build_iep1d_scaler,
)

# ── helpers ────────────────────────────────────────────────────────────────────


def _cfg(
    mode: Iep1dScalerMode = Iep1dScalerMode.DISABLED,
    ready_url: str = "http://iep1d:8003/ready",
    scale_timeout: float = 2.0,
    ready_timeout: float = 2.0,
) -> Iep1dScalerConfig:
    return Iep1dScalerConfig(
        mode=mode,
        service_name="libraryai-iep1d",
        cluster_name="test-cluster",
        ready_url=ready_url,
        scale_timeout_seconds=scale_timeout,
        ready_timeout_seconds=ready_timeout,
        scale_down_after_idle_seconds=300.0,
    )


def _redis(lock_acquired: bool = True) -> MagicMock:
    r = MagicMock()
    r.set = MagicMock(return_value=True if lock_acquired else None)
    r.delete = MagicMock()
    return r


def _http_resp(status: int) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    return m


def _async_http_client(status: int) -> tuple[MagicMock, MagicMock]:
    """Return (context-manager mock, inner client mock) that returns given status."""
    inner = AsyncMock()
    inner.get = AsyncMock(return_value=_http_resp(status))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, inner


# ── 1: disabled raises immediately ────────────────────────────────────────────


async def test_disabled_raises_immediately() -> None:
    scaler = Iep1dScaler(_cfg(Iep1dScalerMode.DISABLED), redis_client=None)
    with pytest.raises(Iep1dUnavailableError, match="disabled"):
        await scaler.ensure_iep1d_ready()


# ── 2: noop, /ready 200 ───────────────────────────────────────────────────────


async def test_noop_ready_ok() -> None:
    scaler = Iep1dScaler(_cfg(Iep1dScalerMode.NOOP), redis_client=None)
    ctx, inner = _async_http_client(200)
    with patch("services.eep_worker.app.iep1d_scaler.httpx.AsyncClient", return_value=ctx):
        await scaler.ensure_iep1d_ready()
    inner.get.assert_awaited_once()


# ── 3: noop, /ready timeout ───────────────────────────────────────────────────


async def test_noop_ready_timeout_raises() -> None:
    scaler = Iep1dScaler(
        _cfg(Iep1dScalerMode.NOOP, ready_timeout=0.01),
        redis_client=None,
    )
    ctx, _ = _async_http_client(503)
    with (
        patch("services.eep_worker.app.iep1d_scaler.httpx.AsyncClient", return_value=ctx),
        patch("asyncio.sleep", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(Iep1dUnavailableError, match="/ready"):
            await scaler.ensure_iep1d_ready()


# ── 4: ecs, lock acquired → scale + ready ─────────────────────────────────────


async def test_ecs_lock_acquired_calls_scale_and_ready() -> None:
    r = _redis(lock_acquired=True)
    scaler = Iep1dScaler(_cfg(Iep1dScalerMode.ECS), redis_client=r)

    scale_mock = AsyncMock()
    ready_mock = AsyncMock()
    with (
        patch.object(scaler, "_ecs_scale_to_one", scale_mock),
        patch.object(scaler, "_poll_ready", ready_mock),
    ):
        await scaler.ensure_iep1d_ready()

    scale_mock.assert_awaited_once()
    ready_mock.assert_awaited_once()
    # Lock must be released after ECS call
    r.delete.assert_called_once()


# ── 5: ecs, lock not acquired → skip scale, poll ready ────────────────────────


async def test_ecs_lock_not_acquired_skips_scale() -> None:
    r = _redis(lock_acquired=False)
    scaler = Iep1dScaler(_cfg(Iep1dScalerMode.ECS), redis_client=r)

    scale_mock = AsyncMock()
    ready_mock = AsyncMock()
    with (
        patch.object(scaler, "_ecs_scale_to_one", scale_mock),
        patch.object(scaler, "_poll_ready", ready_mock),
    ):
        await scaler.ensure_iep1d_ready()

    scale_mock.assert_not_awaited()
    ready_mock.assert_awaited_once()
    r.delete.assert_not_called()


# ── 6: ecs, ECS stable timeout → raises, lock deleted ─────────────────────────


async def test_ecs_scale_timeout_raises_and_releases_lock() -> None:
    r = _redis(lock_acquired=True)
    scaler = Iep1dScaler(
        _cfg(Iep1dScalerMode.ECS, scale_timeout=0.01, ready_timeout=5.0),
        redis_client=r,
    )

    mock_boto_client = MagicMock()
    mock_boto_client.update_service = MagicMock()
    # runningCount always 0 → never stabilises
    mock_boto_client.describe_services = MagicMock(
        return_value={"services": [{"runningCount": 0, "desiredCount": 1, "pendingCount": 1}]}
    )

    with (
        patch("boto3.client", return_value=mock_boto_client),
        patch("asyncio.sleep", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(Iep1dUnavailableError, match="runningCount"):
            await scaler.ensure_iep1d_ready()

    r.delete.assert_called()


# ── 7: ecs, /ready timeout after ECS stable ───────────────────────────────────


async def test_ecs_ready_timeout_after_stable_raises() -> None:
    r = _redis(lock_acquired=True)
    scaler = Iep1dScaler(
        _cfg(Iep1dScalerMode.ECS, scale_timeout=5.0, ready_timeout=0.01),
        redis_client=r,
    )

    mock_boto_client = MagicMock()
    mock_boto_client.update_service = MagicMock()
    mock_boto_client.describe_services = MagicMock(
        return_value={"services": [{"runningCount": 1, "desiredCount": 1, "pendingCount": 0}]}
    )

    ctx, _ = _async_http_client(503)
    with (
        patch("boto3.client", return_value=mock_boto_client),
        patch("services.eep_worker.app.iep1d_scaler.httpx.AsyncClient", return_value=ctx),
        patch("asyncio.sleep", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(Iep1dUnavailableError, match="/ready"):
            await scaler.ensure_iep1d_ready()


# ── 8: ecs, no Redis client → raises without ECS call ─────────────────────────


async def test_ecs_no_redis_raises_immediately() -> None:
    scaler = Iep1dScaler(
        _cfg(Iep1dScalerMode.ECS, scale_timeout=5.0, ready_timeout=5.0),
        redis_client=None,
    )
    with pytest.raises(Iep1dUnavailableError, match="Redis"):
        await scaler.ensure_iep1d_ready()


# ── 9: build with no redis → DISABLED regardless of env ───────────────────────


def test_build_no_redis_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IEP1D_SCALER_MODE", "ecs")
    scaler = build_iep1d_scaler(redis_client=None)
    assert scaler._cfg.mode == Iep1dScalerMode.DISABLED


# ── 10: build with redis + IEP1D_SCALER_MODE=ecs ──────────────────────────────


def test_build_ecs_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IEP1D_SCALER_MODE", "ecs")
    monkeypatch.setenv("IEP1D_SERVICE_NAME", "libraryai-iep1d")
    monkeypatch.setenv("ECS_CLUSTER", "my-cluster")
    scaler = build_iep1d_scaler(redis_client=_redis())
    assert scaler._cfg.mode == Iep1dScalerMode.ECS
    assert scaler._cfg.cluster_name == "my-cluster"


# ── 11: build unknown mode → DISABLED ─────────────────────────────────────────


def test_build_unknown_mode_defaults_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IEP1D_SCALER_MODE", "bogus")
    scaler = build_iep1d_scaler(redis_client=_redis())
    assert scaler._cfg.mode == Iep1dScalerMode.DISABLED


# ── 12: maybe_scale_down is no-op ─────────────────────────────────────────────


async def test_maybe_scale_down_is_noop() -> None:
    scaler = Iep1dScaler(_cfg(Iep1dScalerMode.DISABLED), redis_client=None)
    await scaler.maybe_scale_down_iep1d()  # must not raise


# ── 13: get_iep1d_status disabled ─────────────────────────────────────────────


async def test_get_status_disabled() -> None:
    scaler = Iep1dScaler(_cfg(Iep1dScalerMode.DISABLED), redis_client=None)
    ctx, _ = _async_http_client(503)
    with patch("services.eep_worker.app.iep1d_scaler.httpx.AsyncClient", return_value=ctx):
        status = await scaler.get_iep1d_status()
    assert status["mode"] == "disabled"
    assert status["ready"] is False
    assert status["desired_count"] == 0


# ── 14: get_iep1d_status ecs ──────────────────────────────────────────────────


async def test_get_status_ecs_returns_counts() -> None:
    scaler = Iep1dScaler(_cfg(Iep1dScalerMode.ECS), redis_client=_redis())
    mock_boto_client = MagicMock()
    mock_boto_client.describe_services = MagicMock(
        return_value={"services": [{"runningCount": 1, "desiredCount": 1}]}
    )
    ctx, _ = _async_http_client(200)
    with (
        patch("boto3.client", return_value=mock_boto_client),
        patch("services.eep_worker.app.iep1d_scaler.httpx.AsyncClient", return_value=ctx),
    ):
        status = await scaler.get_iep1d_status()
    assert status["mode"] == "ecs"
    assert status["desired_count"] == 1
    assert status["running_count"] == 1
    assert status["ready"] is True
