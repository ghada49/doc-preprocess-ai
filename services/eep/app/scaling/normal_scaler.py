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
    Start normal-processing infrastructure.

    Starts (in order):
      1. RunPod GPU pods (iep0, iep1a, iep1b) — if RUNPOD_API_KEY is set
      2. eep-worker — with RunPod IEP URLs injected into a new task def revision
      3. CPU IEP services (iep1e, iep2a, iep2b) → 1
      4. Remaining worker services (eep-recovery, shadow-worker) → WORKER_DESIRED_COUNT

    Does NOT start iep1d, retraining-worker, dataset-builder, prometheus, grafana.

    Individual failures are logged but do not abort remaining calls.
    """
    import boto3  # noqa: PLC0415 — lazy import so tests can patch easily

    cluster = os.environ.get("ECS_CLUSTER", "")
    worker_desired = int(os.environ.get("WORKER_DESIRED_COUNT", "2"))
    region = os.environ.get("AWS_REGION", "us-east-1")
    runpod_api_key = os.environ.get("RUNPOD_API_KEY", "")

    ecs_client = boto3.client("ecs", region_name=region)

    if not cluster:
        logger.warning("normal_scaler: ECS_CLUSTER not set — ECS service updates skipped")
        return

    # 1. Create RunPod GPU pods and get their URLs ─────────────────────────────
    iep0_url = iep1a_url = iep1b_url = ""
    if runpod_api_key:
        try:
            iep0_url, iep1a_url, iep1b_url = _create_runpod_pods(runpod_api_key, region)
        except Exception as exc:  # noqa: BLE001
            logger.error("normal_scaler: RunPod pod creation failed: %s", exc)
    else:
        logger.warning("normal_scaler: RUNPOD_API_KEY not set — GPU pods not created")

    # 2. Start eep-worker with RunPod IEP URLs baked into task def ────────────
    eep_worker_task_def_arn: str | None = None
    if iep0_url:
        try:
            eep_worker_task_def_arn = _register_eep_worker_with_runpod_urls(
                ecs_client, iep0_url, iep1a_url, iep1b_url
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("normal_scaler: eep-worker task def update failed: %s", exc)

    _update_service(
        ecs_client,
        cluster,
        "libraryai-eep-worker",
        worker_desired,
        task_def_arn=eep_worker_task_def_arn,
        force_new=eep_worker_task_def_arn is not None,
    )

    # 3. CPU IEP services ──────────────────────────────────────────────────────
    for svc in _CPU_IEP_SERVICES:
        _update_service(ecs_client, cluster, svc, 1)

    # 4. Remaining worker services ─────────────────────────────────────────────
    for svc in ("libraryai-eep-recovery", "libraryai-shadow-worker"):
        _update_service(ecs_client, cluster, svc, worker_desired)


def _create_runpod_pods(api_key: str, region: str) -> tuple[str, str, str]:
    """Create RunPod pods for iep0, iep1a, iep1b. Returns (iep0_url, iep1a_url, iep1b_url)."""
    import boto3  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    _IEP_PODS = [
        ("libraryai-iep0",  "gma51/libraryai-iep0:latest",  8006),
        ("libraryai-iep1a", "gma51/libraryai-iep1a:latest", 8001),
        ("libraryai-iep1b", "gma51/libraryai-iep1b:latest", 8002),
    ]
    pod_ids: dict[str, str] = {}

    for name, image, port in _IEP_PODS:
        mutation = (
            'mutation { podFindAndDeployOnDemand(input: {'
            f' name: "{name}", imageName: "{image}",'
            ' gpuTypeId: "NVIDIA GeForce RTX 3080", cloudType: COMMUNITY,'
            f' containerDiskInGb: 20, minMemoryInGb: 8, minVcpuCount: 2,'
            f' ports: "{port}/http", startJupyter: false, startSsh: false'
            '}) { id } }'
        )
        resp = httpx.post(
            f"https://api.runpod.io/graphql?api_key={api_key}",
            json={"query": mutation},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"RunPod error creating {name}: {data['errors']}")
        pod_ids[name] = data["data"]["podFindAndDeployOnDemand"]["id"]
        logger.info("normal_scaler: RunPod pod created for %s → id=%s", name, pod_ids[name])

    iep0_url  = f"https://{pod_ids['libraryai-iep0']}-8006.proxy.runpod.net"
    iep1a_url = f"https://{pod_ids['libraryai-iep1a']}-8001.proxy.runpod.net"
    iep1b_url = f"https://{pod_ids['libraryai-iep1b']}-8002.proxy.runpod.net"

    # Persist pod IDs so scale-down workflow can terminate them
    try:
        import json  # noqa: PLC0415
        bucket = os.environ.get("S3_BUCKET_NAME", "libraryai2")
        s3 = boto3.client("s3", region_name=region)
        s3.put_object(
            Bucket=bucket,
            Key="ops/runpod-pods.json",
            Body=json.dumps({
                "iep0":  pod_ids["libraryai-iep0"],
                "iep1a": pod_ids["libraryai-iep1a"],
                "iep1b": pod_ids["libraryai-iep1b"],
            }).encode(),
        )
        logger.info("normal_scaler: RunPod pod IDs saved to s3://%s/ops/runpod-pods.json", bucket)
    except Exception as exc:  # noqa: BLE001
        logger.warning("normal_scaler: could not save pod IDs to S3: %s", exc)

    return iep0_url, iep1a_url, iep1b_url


# Fields returned by describe_task_definition that must be stripped before re-registering.
_TASK_DEF_READONLY_FIELDS = frozenset({
    "taskDefinitionArn", "revision", "status", "requiresAttributes",
    "compatibilities", "registeredAt", "registeredBy", "deregisteredAt",
})


def _register_eep_worker_with_runpod_urls(
    ecs_client,
    iep0_url: str,
    iep1a_url: str,
    iep1b_url: str,
) -> str:
    """
    Read the current eep-worker task definition, patch IEP URL env vars with
    RunPod URLs, register a new revision, and return its ARN.
    """
    resp = ecs_client.describe_task_definition(taskDefinition="libraryai-eep-worker")
    task_def: dict = {k: v for k, v in resp["taskDefinition"].items()
                      if k not in _TASK_DEF_READONLY_FIELDS}

    url_overrides = {"IEP0_URL": iep0_url, "IEP1A_URL": iep1a_url, "IEP1B_URL": iep1b_url}
    for container in task_def.get("containerDefinitions", []):
        container["environment"] = [
            {"name": e["name"], "value": url_overrides.get(e["name"], e["value"])}
            for e in container.get("environment", [])
        ]

    new_rev = ecs_client.register_task_definition(**task_def)
    arn: str = new_rev["taskDefinition"]["taskDefinitionArn"]
    logger.info("normal_scaler: registered eep-worker task def %s with RunPod URLs", arn)
    return arn


def _update_service(
    ecs_client,
    cluster: str,
    service: str,
    desired: int,
    *,
    task_def_arn: str | None = None,
    force_new: bool = False,
) -> None:
    """Call ecs:UpdateService, logging success or failure. Never raises."""
    assert service not in _EXCLUDED_SERVICES, (
        f"BUG: _update_service called for excluded service {service!r}"
    )
    kwargs: dict = {"cluster": cluster, "service": service, "desiredCount": desired}
    if task_def_arn:
        kwargs["taskDefinition"] = task_def_arn
    if force_new:
        kwargs["forceNewDeployment"] = True
    try:
        ecs_client.update_service(**kwargs)
        logger.info("normal_scaler: %s desired → %d", service, desired)
    except Exception as exc:  # noqa: BLE001
        logger.error("normal_scaler: failed to update %s: %s", service, exc)
