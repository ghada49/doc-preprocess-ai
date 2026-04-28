"""
services/eep/app/scaling/normal_scaler.py
------------------------------------------
On-demand normal-processing scale-up for LibraryAI.

Triggered after a job or page is durably committed and enqueued when
PROCESSING_START_MODE=immediate. Protected by a Redis SET NX lock so
concurrent job arrivals do not produce duplicate ECS/ASG calls.

PROCESSING_START_MODE (env var):
  immediate        — (default) call maybe_trigger_scale_up after every durable
                     enqueue. Lock ensures only one scale-up attempt runs at a
                     time. ECS update_service is idempotent so re-calling while
                     already running is safe.
  scheduled_window — do nothing on arrival; a cron workflow (scheduled-window.yml)
                     triggers scale-up only during the configured time window and
                     only when processable work exists.

Services started by normal scale-up (spec §5):
  GPU ASG → GPU_ASG_DESIRED instances
  libraryai-iep0, iep1a, iep1b        (GPU inference)
  libraryai-iep1e, iep2a, iep2b       (CPU inference)
  libraryai-eep-worker, eep-recovery, shadow-worker

Services intentionally NOT started (spec §6):
  libraryai-iep1d          — CPU/Fargate, on-demand rescue only (iep1d_scaler.py)
  libraryai-retraining-worker, dataset-builder  — offline/batch only
  libraryai-prometheus, grafana                 — on-demand observability only
  artifact cleanup                              — separate maintenance workflow

Redis lock:
  Key : libraryai:normal_scale:lock
  TTL : NORMAL_SCALE_LOCK_TTL_SECONDS (default 600 s)
  NX  : only the first concurrent caller acquires the lock and calls AWS.
        Subsequent callers within TTL skip the ECS calls (idempotent anyway).
        Lock is released immediately after all update_service/ASG calls complete
        (not after services are healthy) so the TTL is just an expiry guard.

Required env vars (set in eep-task-def.json):
  PROCESSING_START_MODE   — immediate | scheduled_window  (default: immediate)
  ECS_CLUSTER             — ECS cluster name
  GPU_ASG_NAME            — Auto Scaling Group name for GPU instances
  GPU_ASG_DESIRED         — desired GPU instance count  (default: 1)
  WORKER_DESIRED_COUNT    — desired count for eep-worker/recovery/shadow  (default: 2)
  AWS_REGION              — AWS region  (default: us-east-1)

IAM requirement:
  The EEP task role (libraryai-task-role) must have:
    autoscaling:SetDesiredCapacity on the GPU ASG
    ecs:UpdateService on all normal-processing services
  This mirrors the permissions already granted to the github-actions-deploy role.
"""

from __future__ import annotations

import logging
import os
import uuid

import redis as redis_lib

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────────

_SCALE_LOCK_KEY = "libraryai:normal_scale:lock"
_SCALE_LOCK_TTL_S = int(os.environ.get("NORMAL_SCALE_LOCK_TTL_SECONDS", "600"))

# Normal-processing services — must match spec §5 exactly.
# Do NOT add iep1d, retraining-worker, dataset-builder, prometheus, or grafana here.
_GPU_SERVICES: list[str] = [
    "libraryai-iep0",
    "libraryai-iep1a",
    "libraryai-iep1b",
]
_CPU_IEP_SERVICES: list[str] = [
    "libraryai-iep1e",
    "libraryai-iep2a",
    "libraryai-iep2b",
]
_WORKER_SERVICES: list[str] = [
    "libraryai-eep-worker",
    "libraryai-eep-recovery",
    "libraryai-shadow-worker",
]

# All services that normal scale-up touches (for testing/documentation).
NORMAL_SCALE_UP_SERVICES: list[str] = _GPU_SERVICES + _CPU_IEP_SERVICES + _WORKER_SERVICES

# Services that must NEVER be started by normal scale-up.
_EXCLUDED_SERVICES: frozenset[str] = frozenset(
    {
        "libraryai-iep1d",
        "libraryai-retraining-worker",
        "libraryai-dataset-builder",
        "libraryai-prometheus",
        "libraryai-grafana",
    }
)


# ── public API ─────────────────────────────────────────────────────────────────


def get_processing_start_mode() -> str:
    """Return PROCESSING_START_MODE env var, lower-cased. Default: 'immediate'."""
    return os.environ.get("PROCESSING_START_MODE", "immediate").strip().lower()


def maybe_trigger_scale_up(r: redis_lib.Redis) -> None:
    """
    Attempt to trigger normal-processing scale-up.

    Call this synchronously (e.g. via FastAPI BackgroundTasks) after a job or
    page is durably committed to the DB and enqueued to Redis.

    Behaviour:
    - PROCESSING_START_MODE != immediate → no-op.
    - Lock already held (another request is scaling) → no-op (idempotent).
    - Lock acquired → call AWS ASG + ECS update_service for all normal services.
    - Any individual service failure is logged but does not abort the rest.
    - Lock released after all API calls complete.

    This function is intentionally synchronous so it can be called from
    FastAPI BackgroundTasks (which runs sync functions in a thread pool).
    """
    mode = get_processing_start_mode()
    if mode != "immediate":
        logger.debug("normal_scaler: mode=%r — scale-up not triggered on arrival", mode)
        return

    lock_value = str(uuid.uuid4())
    acquired = r.set(_SCALE_LOCK_KEY, lock_value, nx=True, ex=_SCALE_LOCK_TTL_S)
    if not acquired:
        logger.debug(
            "normal_scaler: lock %r already held — duplicate trigger suppressed",
            _SCALE_LOCK_KEY,
        )
        return

    logger.info(
        "normal_scaler: acquired lock (ttl=%ds), initiating normal-processing scale-up",
        _SCALE_LOCK_TTL_S,
    )
    try:
        _do_scale_up()
    except Exception as exc:  # noqa: BLE001
        logger.error("normal_scaler: scale-up failed unexpectedly: %s", exc)
    finally:
        # Release only if we still own the lock (guard against TTL expiry race).
        try:
            current = r.get(_SCALE_LOCK_KEY)
            if current == lock_value:
                r.delete(_SCALE_LOCK_KEY)
                logger.debug("normal_scaler: lock released")
        except Exception:  # noqa: BLE001
            pass


# ── internals ─────────────────────────────────────────────────────────────────


def _do_scale_up() -> None:
    """
    Call AWS ASG + ECS APIs to start normal-processing infrastructure.

    Starts (in order):
      1. GPU ASG → GPU_ASG_DESIRED instances
      2. GPU IEP services (iep0, iep1a, iep1b) → 1
      3. CPU IEP services (iep1e, iep2a, iep2b) → 1
      4. Worker services (eep-worker, eep-recovery, shadow-worker) → WORKER_DESIRED_COUNT

    Does NOT start iep1d, retraining-worker, dataset-builder, prometheus, grafana.

    Individual failures are logged but do not abort remaining calls.
    ECS update_service with the same desiredCount is idempotent.
    """
    import boto3  # noqa: PLC0415 — lazy import so tests can patch easily

    cluster = os.environ.get("ECS_CLUSTER", "")
    asg_name = os.environ.get("GPU_ASG_NAME", "")
    gpu_desired = int(os.environ.get("GPU_ASG_DESIRED", "1"))
    worker_desired = int(os.environ.get("WORKER_DESIRED_COUNT", "2"))
    region = os.environ.get("AWS_REGION", "us-east-1")

    asg_client = boto3.client("autoscaling", region_name=region)
    ecs_client = boto3.client("ecs", region_name=region)

    # 1. Scale GPU ASG ─────────────────────────────────────────────────────────
    if asg_name:
        try:
            asg_client.set_desired_capacity(
                AutoScalingGroupName=asg_name,
                DesiredCapacity=gpu_desired,
                HonorCooldown=False,
            )
            logger.info(
                "normal_scaler: GPU ASG %r desired → %d", asg_name, gpu_desired
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("normal_scaler: GPU ASG scale failed: %s", exc)
    else:
        logger.warning(
            "normal_scaler: GPU_ASG_NAME not set — GPU ASG scale skipped"
        )

    if not cluster:
        logger.warning(
            "normal_scaler: ECS_CLUSTER not set — ECS service updates skipped"
        )
        return

    # 2. GPU IEP services ──────────────────────────────────────────────────────
    for svc in _GPU_SERVICES:
        _update_service(ecs_client, cluster, svc, 1)

    # 3. CPU IEP services ──────────────────────────────────────────────────────
    for svc in _CPU_IEP_SERVICES:
        _update_service(ecs_client, cluster, svc, 1)

    # 4. Worker services ───────────────────────────────────────────────────────
    for svc in _WORKER_SERVICES:
        _update_service(ecs_client, cluster, svc, worker_desired)


def _update_service(ecs_client, cluster: str, service: str, desired: int) -> None:
    """Call ecs:UpdateService, logging success or failure. Never raises."""
    # Safety guard — should never be reached given the constants above.
    assert service not in _EXCLUDED_SERVICES, (
        f"BUG: _update_service called for excluded service {service!r}"
    )
    try:
        ecs_client.update_service(
            cluster=cluster,
            service=service,
            desiredCount=desired,
        )
        logger.info("normal_scaler: %s desired → %d", service, desired)
    except Exception as exc:  # noqa: BLE001
        logger.error("normal_scaler: failed to update %s: %s", service, exc)
