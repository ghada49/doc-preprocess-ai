"""
services/eep_worker/app/iep1d_scaler.py
----------------------------------------
On-demand IEP1D scaler for the rescue-path GPU service.

IEP1D (UVDoc rectification) runs only when a page needs rescue. It stays at
ECS desired-count 0 during normal batch processing and is started on demand
when the first rescue request arrives.

Modes (IEP1D_SCALER_MODE env var):
  disabled  — never scale; ensure_iep1d_ready() raises Iep1dUnavailableError.
              Rescue pages route to pending_human_correction (iep1d_unavailable).
  noop      — assume IEP1D is already running (Docker Compose / local dev).
              Only poll /ready; no ECS calls.
  ecs       — call AWS ECS update-service desiredCount=1, wait for running,
              then poll /ready. Uses a Redis lock to avoid concurrent storms.

Concurrency safety (ecs mode):
  A Redis SET NX lock (key: libraryai:iep1d:scaling_lock) ensures only one
  worker calls ECS update-service. Others skip the ECS call and go straight to
  polling /ready. The lock is released as soon as update-service completes
  (before /ready polling) so no starvation occurs.

Scale-down:
  maybe_scale_down_iep1d() is a documented no-op. Scale-down is handled by
  .github/workflows/scale-down.yml at end of batch. Automatic mid-batch
  scale-down is future work.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
import uuid
from dataclasses import dataclass

import httpx
import redis as redis_lib

logger = logging.getLogger(__name__)

_SCALING_LOCK_KEY = "libraryai:iep1d:scaling_lock"
_SCALING_LOCK_TTL_BUFFER_S = 30
_ECS_POLL_INTERVAL_S = 10
_READY_POLL_INTERVAL_S = 5


class Iep1dScalerMode(enum.Enum):
    DISABLED = "disabled"
    NOOP = "noop"
    ECS = "ecs"


@dataclass(frozen=True)
class Iep1dScalerConfig:
    mode: Iep1dScalerMode
    service_name: str
    cluster_name: str
    ready_url: str
    scale_timeout_seconds: float
    ready_timeout_seconds: float
    # scale_down_after_idle_seconds is read for future use but not yet acted on
    scale_down_after_idle_seconds: float


class Iep1dUnavailableError(Exception):
    """
    IEP1D service could not be made ready.

    The caller should route the page to pending_human_correction with
    review_reason="iep1d_unavailable".
    """


class Iep1dScaler:
    """
    Manages on-demand startup and readiness of the libraryai-iep1d ECS service.

    Instantiate via build_iep1d_scaler(redis_client).
    """

    def __init__(
        self,
        config: Iep1dScalerConfig,
        redis_client: redis_lib.Redis | None,
    ) -> None:
        self._cfg = config
        self._redis = redis_client

    # ── public interface ───────────────────────────────────────────────────

    async def ensure_iep1d_ready(self) -> None:
        """
        Ensure IEP1D is running and /ready returns 200.

        Raises Iep1dUnavailableError if the service cannot be made ready
        within the configured timeouts.
        """
        mode = self._cfg.mode
        if mode == Iep1dScalerMode.DISABLED:
            raise Iep1dUnavailableError(
                "IEP1D_SCALER_MODE=disabled; rescue cannot proceed automatically. "
                "Scale libraryai-iep1d manually or set IEP1D_SCALER_MODE=ecs."
            )
        if mode == Iep1dScalerMode.NOOP:
            await self._poll_ready()
            return
        # ECS mode
        await self._ecs_ensure_ready()

    async def maybe_scale_down_iep1d(self) -> None:
        """
        Scale IEP1D down when idle.

        NOT YET IMPLEMENTED. Scale-down is currently handled by
        .github/workflows/scale-down.yml at the end of the batch window.
        Future work: query the DB for pages in "rectification" state; if none
        remain for IEP1D_SCALE_DOWN_AFTER_IDLE_SECONDS, set desiredCount=0.
        """

    async def get_iep1d_status(self) -> dict:
        """Return a status dict for the IEP1D service."""
        mode = self._cfg.mode
        ready = await self._check_ready_once()

        if mode == Iep1dScalerMode.DISABLED:
            return {"mode": "disabled", "desired_count": 0, "ready": False}

        if mode == Iep1dScalerMode.NOOP:
            return {"mode": "noop", "desired_count": None, "ready": ready}

        # ECS mode — query live state
        try:
            import boto3  # noqa: PLC0415

            client = boto3.client("ecs", region_name=_aws_region())
            resp = client.describe_services(
                cluster=self._cfg.cluster_name,
                services=[self._cfg.service_name],
            )
            svc = resp.get("services", [{}])[0]
            return {
                "mode": "ecs",
                "desired_count": svc.get("desiredCount", -1),
                "running_count": svc.get("runningCount", -1),
                "ready": ready,
            }
        except Exception as exc:
            logger.warning("iep1d_scaler: get_iep1d_status ECS describe failed: %s", exc)
            return {"mode": "ecs", "desired_count": -1, "ready": ready, "error": str(exc)}

    # ── ECS internals ──────────────────────────────────────────────────────

    async def _ecs_ensure_ready(self) -> None:
        lock_ttl = int(
            self._cfg.scale_timeout_seconds
            + self._cfg.ready_timeout_seconds
            + _SCALING_LOCK_TTL_BUFFER_S
        )
        lock_value = str(uuid.uuid4())

        if self._redis is None:
            raise Iep1dUnavailableError(
                "IEP1D_SCALER_MODE=ecs but no Redis client available; cannot acquire lock."
            )

        acquired = self._redis.set(_SCALING_LOCK_KEY, lock_value, nx=True, ex=lock_ttl)
        if acquired:
            # This worker is responsible for the ECS scale-up call.
            try:
                await self._ecs_scale_to_one()
            except Iep1dUnavailableError:
                raise
            except Exception as exc:
                raise Iep1dUnavailableError(
                    f"ECS scale-up for {self._cfg.service_name} failed: {exc}"
                ) from exc
            finally:
                # Release lock immediately after update-service call so other
                # workers can proceed to poll /ready without blocking.
                try:
                    self._redis.delete(_SCALING_LOCK_KEY)
                except Exception:  # noqa: BLE001
                    pass
        else:
            logger.debug(
                "iep1d_scaler: lock held by another worker — skipping ECS call, polling /ready"
            )

        # Both paths poll /ready (lock-free).
        await self._poll_ready()

    async def _ecs_scale_to_one(self) -> None:
        import boto3  # noqa: PLC0415

        client = boto3.client("ecs", region_name=_aws_region())
        client.update_service(
            cluster=self._cfg.cluster_name,
            service=self._cfg.service_name,
            desiredCount=1,
        )
        logger.info(
            "iep1d_scaler: ECS update_service desiredCount=1 cluster=%s service=%s",
            self._cfg.cluster_name,
            self._cfg.service_name,
        )

        deadline = time.monotonic() + self._cfg.scale_timeout_seconds
        while time.monotonic() < deadline:
            try:
                resp = client.describe_services(
                    cluster=self._cfg.cluster_name,
                    services=[self._cfg.service_name],
                )
                svc = resp.get("services", [{}])[0]
                running = svc.get("runningCount", 0)
                desired = svc.get("desiredCount", 0)
                if running >= 1 and desired >= 1:
                    logger.info(
                        "iep1d_scaler: ECS service running running=%d desired=%d",
                        running,
                        desired,
                    )
                    return
                logger.debug(
                    "iep1d_scaler: waiting for ECS running=%d pending=%d",
                    running,
                    svc.get("pendingCount", 0),
                )
            except Exception as exc:
                logger.warning("iep1d_scaler: describe_services error: %s", exc)
            await asyncio.sleep(_ECS_POLL_INTERVAL_S)

        raise Iep1dUnavailableError(
            f"ECS service {self._cfg.service_name!r} did not reach runningCount>=1 "
            f"within {self._cfg.scale_timeout_seconds:.0f}s"
        )

    # ── readiness polling ──────────────────────────────────────────────────

    async def _poll_ready(self) -> None:
        deadline = time.monotonic() + self._cfg.ready_timeout_seconds
        async with httpx.AsyncClient(timeout=10.0) as client:
            while time.monotonic() < deadline:
                try:
                    resp = await client.get(self._cfg.ready_url)
                    if resp.status_code == 200:
                        logger.info("iep1d_scaler: IEP1D /ready OK url=%s", self._cfg.ready_url)
                        return
                    logger.debug(
                        "iep1d_scaler: /ready returned %d — retrying", resp.status_code
                    )
                except httpx.RequestError as exc:
                    logger.debug("iep1d_scaler: /ready request error: %s", exc)
                await asyncio.sleep(_READY_POLL_INTERVAL_S)

        raise Iep1dUnavailableError(
            f"IEP1D /ready did not return 200 within {self._cfg.ready_timeout_seconds:.0f}s "
            f"(url={self._cfg.ready_url!r})"
        )

    async def _check_ready_once(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(self._cfg.ready_url)
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False


# ── factory ───────────────────────────────────────────────────────────────


def build_iep1d_scaler(redis_client: redis_lib.Redis | None) -> Iep1dScaler:
    """
    Build an Iep1dScaler from environment variables.

    When redis_client is None, always returns a DISABLED-mode scaler regardless
    of IEP1D_SCALER_MODE. This keeps tests and local runs that don't supply Redis
    from accidentally attempting ECS calls.
    """
    if redis_client is None:
        config = Iep1dScalerConfig(
            mode=Iep1dScalerMode.DISABLED,
            service_name=os.environ.get("IEP1D_SERVICE_NAME", "libraryai-iep1d"),
            cluster_name=os.environ.get("ECS_CLUSTER", ""),
            ready_url=os.environ.get("IEP1D_READY_URL", "http://iep1d:8003/ready"),
            scale_timeout_seconds=float(
                os.environ.get("IEP1D_SCALE_TIMEOUT_SECONDS", "600")
            ),
            ready_timeout_seconds=float(
                os.environ.get("IEP1D_READY_TIMEOUT_SECONDS", "300")
            ),
            scale_down_after_idle_seconds=float(
                os.environ.get("IEP1D_SCALE_DOWN_AFTER_IDLE_SECONDS", "300")
            ),
        )
        return Iep1dScaler(config, redis_client=None)

    mode_str = os.environ.get("IEP1D_SCALER_MODE", "disabled").strip().lower()
    try:
        mode = Iep1dScalerMode(mode_str)
    except ValueError:
        logger.warning(
            "iep1d_scaler: unknown IEP1D_SCALER_MODE=%r — defaulting to disabled",
            mode_str,
        )
        mode = Iep1dScalerMode.DISABLED

    config = Iep1dScalerConfig(
        mode=mode,
        service_name=os.environ.get("IEP1D_SERVICE_NAME", "libraryai-iep1d"),
        cluster_name=os.environ.get("ECS_CLUSTER", ""),
        ready_url=os.environ.get("IEP1D_READY_URL", "http://iep1d:8003/ready"),
        scale_timeout_seconds=float(
            os.environ.get("IEP1D_SCALE_TIMEOUT_SECONDS", "600")
        ),
        ready_timeout_seconds=float(
            os.environ.get("IEP1D_READY_TIMEOUT_SECONDS", "300")
        ),
        scale_down_after_idle_seconds=float(
            os.environ.get("IEP1D_SCALE_DOWN_AFTER_IDLE_SECONDS", "300")
        ),
    )
    return Iep1dScaler(config, redis_client)


def _aws_region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")
