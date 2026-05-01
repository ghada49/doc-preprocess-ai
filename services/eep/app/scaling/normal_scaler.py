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
  libraryai-iep0, iep1a, iep1b           (GPU inference)
  libraryai-iep1d                         (CPU/Fargate rescue path — pre-warmed)
  libraryai-iep1e, iep2a-v2, iep2b       (CPU inference)
  libraryai-eep-worker, eep-recovery, shadow-worker

iep1d note:
  iep1d (rectification rescue path) was historically left at desired=0 and
  scaled on demand by ``services/eep_worker/app/iep1d_scaler.py``.  In
  practice the cold-start latency from desired=0 (Fargate task pull + model
  load) routinely exceeds the worker's IEP1D_READY_TIMEOUT_SECONDS, causing
  the first wave of pages that need rescue to be routed to
  ``pending_human_correction`` while iep1d is still warming up.  We now
  pre-warm iep1d alongside iep1e/iep2a/iep2b — the on-demand scaler still
  exists as a defence-in-depth fallback (its update_service call is
  idempotent when desired=1 already).

Services intentionally NOT started (spec §6):
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
    ecs:DescribeTaskDefinition and ecs:RegisterTaskDefinition for eep-worker URL injection
    iam:PassRole for ecsTaskExecutionRole and libraryai-task-role
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
# Do NOT add retraining-worker, dataset-builder, prometheus, or grafana here.
_GPU_SERVICES: list[str] = [
    "libraryai-iep0",
    "libraryai-iep1a",
    "libraryai-iep1b",
]
_CPU_IEP_SERVICES: list[str] = [
    "libraryai-iep1d",
    "libraryai-iep1e",
    "libraryai-iep2a-v2",
    "libraryai-iep2b",
]
_WORKER_SERVICES: list[str] = [
    "libraryai-eep-worker",
    "libraryai-eep-recovery",
    "libraryai-shadow-worker",
]

_SERVICE_CONNECT_CONFIGS: dict[str, dict] = {
    "libraryai-iep1e": {
        "enabled": True,
        "namespace": "libraryai.local",
        "services": [
            {
                "portName": "http",
                "discoveryName": "iep1e",
                "clientAliases": [{"port": 8007, "dnsName": "iep1e"}],
            }
        ],
    },
    "libraryai-iep2a-v2": {
        "enabled": True,
        "namespace": "libraryai.local",
        "services": [
            {
                "portName": "http",
                "discoveryName": "iep2a-v2",
                "clientAliases": [{"port": 8004, "dnsName": "iep2a-v2"}],
            }
        ],
    },
    "libraryai-iep2b": {
        "enabled": True,
        "namespace": "libraryai.local",
        "services": [
            {
                "portName": "http",
                "discoveryName": "iep2b",
                "clientAliases": [{"port": 8005, "dnsName": "iep2b"}],
            }
        ],
    },
    "libraryai-eep-worker": {
        "enabled": True,
        "namespace": "libraryai.local",
        "services": [
            {
                "portName": "health",
                "discoveryName": "eep-worker",
                "clientAliases": [{"port": 9100, "dnsName": "eep-worker"}],
            }
        ],
    },
    "libraryai-eep-recovery": {
        "enabled": True,
        "namespace": "libraryai.local",
        "services": [
            {
                "portName": "health",
                "discoveryName": "eep-recovery",
                "clientAliases": [{"port": 9101, "dnsName": "eep-recovery"}],
            }
        ],
    },
    "libraryai-shadow-worker": {
        "enabled": True,
        "namespace": "libraryai.local",
        "services": [
            {
                "portName": "health",
                "discoveryName": "shadow-worker",
                "clientAliases": [{"port": 9102, "dnsName": "shadow-worker"}],
            }
        ],
    },
}

# All services that normal scale-up touches (for testing/documentation).
NORMAL_SCALE_UP_SERVICES: list[str] = _GPU_SERVICES + _CPU_IEP_SERVICES + _WORKER_SERVICES

# Services that must NEVER be started by normal scale-up.
_EXCLUDED_SERVICES: frozenset[str] = frozenset(
    {
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
      1. RunPod GPU pods (iep0, iep1a, iep1b) — if RUNPOD_API_KEY is set (pods that stay
         unhealthy for RUNPOD_HEALTH_WAIT_SECONDS default 600s are terminated and recreated,
         up to RUNPOD_POD_REPLACE_MAX times each)
      2. eep-worker — with RunPod IEP URLs injected into a new task def revision
      3. CPU IEP services (iep1d, iep1e, iep2a, iep2b) → 1 each
      4. Remaining worker services (eep-recovery, shadow-worker) → WORKER_DESIRED_COUNT

    Does NOT start retraining-worker, dataset-builder, prometheus, grafana.

    Individual failures are logged but do not abort remaining calls.
    """
    import boto3  # noqa: PLC0415 — lazy import so tests can patch easily

    cluster = os.environ.get("ECS_CLUSTER", "")
    worker_desired = int(os.environ.get("WORKER_DESIRED_COUNT", "2"))
    region = os.environ.get("AWS_REGION", "us-east-1")
    runpod_api_key = os.environ.get("RUNPOD_API_KEY", "")
    runpod_pod_mode = _normalize_runpod_pod_mode(os.environ.get("RUNPOD_POD_MODE", "create"))
    created_runpod_pod_ids: list[str] = []

    ecs_client = boto3.client("ecs", region_name=region)

    if not cluster:
        logger.warning("normal_scaler: ECS_CLUSTER not set — ECS service updates skipped")
        return

    if _normal_processing_already_active(ecs_client, cluster):
        logger.info("normal_scaler: processing services already active — skipping scale-up")
        return

    # 1. Create RunPod GPU pods and get their URLs ─────────────────────────────
    iep0_url = iep1a_url = iep1b_url = ""
    if runpod_api_key:
        try:
            if runpod_pod_mode == "existing":
                iep0_url, iep1a_url, iep1b_url = _resume_existing_runpod_pods(
                    runpod_api_key,
                    region,
                )
            else:
                iep0_url, iep1a_url, iep1b_url = _create_runpod_pods(runpod_api_key, region)
                created_runpod_pod_ids = _runpod_pod_ids_from_urls(
                    iep0_url,
                    iep1a_url,
                    iep1b_url,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "normal_scaler: RunPod pod startup failed: %s — "
                "aborting scale-up to avoid starting workers with stale GPU URLs. "
                "Check RUNPOD_GPU_TYPE_ID / RUNPOD_CLOUD_TYPE vars or RunPod supply.",
                exc,
            )
            return
    else:
        logger.warning("normal_scaler: RUNPOD_API_KEY not set — GPU pods not created")

    if not iep0_url:
        logger.error(
            "normal_scaler: no RunPod URLs available — aborting scale-up. "
            "Workers would time out against stale GPU service URLs."
        )
        _cleanup_created_runpod_pods(runpod_api_key, created_runpod_pod_ids, "missing RunPod URLs")
        return

    # 2. Start eep-worker with RunPod IEP URLs baked into task def ────────────
    try:
        eep_worker_task_def_arn: str | None = _register_eep_worker_with_runpod_urls(
            ecs_client, iep0_url, iep1a_url, iep1b_url
        )

        if not _update_service(
            ecs_client,
            cluster,
            "libraryai-eep-worker",
            worker_desired,
            task_def_arn=eep_worker_task_def_arn,
            force_new=True,
        ):
            raise RuntimeError("failed to update libraryai-eep-worker")

    # 3. CPU IEP services ──────────────────────────────────────────────────────
        for svc in _CPU_IEP_SERVICES:
            if not _update_service(ecs_client, cluster, svc, 1, force_new=True):
                raise RuntimeError(f"failed to update {svc}")

    # 4. Remaining worker services ─────────────────────────────────────────────
        # eep-recovery mutates Redis queues and must run as a singleton.  The
        # reconciler also takes a Redis lock as a defense against mis-scaling.
        if not _update_service(ecs_client, cluster, "libraryai-eep-recovery", 1):
            raise RuntimeError("failed to update libraryai-eep-recovery")
        if not _update_service(ecs_client, cluster, "libraryai-shadow-worker", worker_desired):
            raise RuntimeError("failed to update libraryai-shadow-worker")
    except Exception as exc:  # noqa: BLE001
        logger.error("normal_scaler: AWS service startup failed: %s — rolling back scale-up.", exc)
        _rollback_aws_services(ecs_client, cluster)
        _cleanup_created_runpod_pods(runpod_api_key, created_runpod_pod_ids, "AWS startup failure")
        return


def _terminate_single_runpod_pod(api_key: str, pod_id: str) -> None:
    """Terminate one RunPod pod (best-effort). Used when recycling stuck pulls."""
    if not api_key or not pod_id:
        return
    import httpx  # noqa: PLC0415

    mutation = 'mutation { podTerminate(input: { podId: "' + pod_id + '" }) }'
    try:
        resp = httpx.post(
            f"https://api.runpod.io/graphql?api_key={api_key}",
            json={"query": mutation},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            logger.warning(
                "normal_scaler: RunPod terminate %s returned errors: %s",
                pod_id,
                data["errors"],
            )
            return
        logger.info("normal_scaler: terminated RunPod pod %s (recycle stuck image pull)", pod_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("normal_scaler: failed to terminate RunPod pod %s: %s", pod_id, exc)


def _runpod_pod_running_and_healthy(api_key: str, pod_id: str, health_url: str) -> bool:
    """True when GraphQL reports runtime (RUNNING) and GET /health succeeds."""
    import httpx  # noqa: PLC0415

    try:
        q = {
            "query": (
                'query { pod(input: { podId: "'
                + pod_id.replace('"', "")
                + '" }) { desiredStatus runtime { uptimeInSeconds } } }'
            ),
        }
        resp = httpx.post(
            f"https://api.runpod.io/graphql?api_key={api_key}",
            json=q,
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        pod = (data.get("data") or {}).get("pod") or {}
        if pod.get("runtime") is None:
            return False
        h = httpx.get(health_url, timeout=10.0)
        return h.status_code == 200
    except Exception:
        return False


def _wait_runpod_iep_ready_or_replace(
    api_key: str,
    pod_id: str,
    port: int,
    name: str,
    image: str,
    gpu_type_ids: list[str],
    cloud_types: list[str],
) -> str:
    """
    Poll until the pod is RUNNING and answers GET /health, or each
    RUNPOD_HEALTH_WAIT_SECONDS window expires — then terminate and create a replacement.
    Mitigates RunPod hosts stuck on Docker image fetch without failing the whole scale-up for 30+ minutes.
    """
    import time  # noqa: PLC0415

    wait_s = int(os.environ.get("RUNPOD_HEALTH_WAIT_SECONDS", "600"))
    poll_s = int(os.environ.get("RUNPOD_HEALTH_POLL_SECONDS", "15"))
    max_windows = max(1, int(os.environ.get("RUNPOD_POD_REPLACE_MAX", "3")))
    health_url = f"https://{pod_id}-{port}.proxy.runpod.net/health"

    for window in range(max_windows):
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if _runpod_pod_running_and_healthy(api_key, pod_id, health_url):
                logger.info("normal_scaler: RunPod %s pod %s is healthy.", name, pod_id)
                return pod_id
            remaining = deadline - time.monotonic()
            time.sleep(min(float(poll_s), max(0.5, remaining)))

        if window >= max_windows - 1:
            raise RuntimeError(
                f"RunPod {name} pod {pod_id} did not become healthy after "
                f"{max_windows} window(s) of {wait_s}s each"
            )

        logger.warning(
            "normal_scaler: %s pod %s not healthy within %ds — terminating and recreating "
            "(window %d/%d)",
            name,
            pod_id,
            wait_s,
            window + 1,
            max_windows,
        )
        _terminate_single_runpod_pod(api_key, pod_id)
        pod_id = _create_runpod_pod_with_fallback(
            api_key, name, image, port, gpu_type_ids, cloud_types
        )
        health_url = f"https://{pod_id}-{port}.proxy.runpod.net/health"

    raise RuntimeError(f"RunPod {name}: internal wait loop exited unexpectedly")


def _create_runpod_pods(api_key: str, region: str) -> tuple[str, str, str]:
    """Create RunPod pods for iep0, iep1a, iep1b. Returns (iep0_url, iep1a_url, iep1b_url)."""
    import boto3  # noqa: PLC0415

    gpu_type_ids = _runpod_gpu_type_candidates()
    cloud_types = _runpod_cloud_type_candidates()

    _IEP_PODS = [
        ("libraryai-iep0",  "gma51/libraryai-iep0:latest",  8006),
        ("libraryai-iep1a", "gma51/libraryai-iep1a:latest", 8001),
        ("libraryai-iep1b", "gma51/libraryai-iep1b:latest", 8002),
    ]
    logger.info(
        "normal_scaler: requesting RunPod pods gpu_candidates=%s cloud_candidates=%s",
        ",".join(gpu_type_ids),
        ",".join(cloud_types),
    )

    pod_ids: dict[str, str] = {}
    for name, image, port in _IEP_PODS:
        pod_ids[name] = _create_runpod_pod_with_fallback(
            api_key,
            name,
            image,
            port,
            gpu_type_ids,
            cloud_types,
        )

    for name, image, port in _IEP_PODS:
        pod_ids[name] = _wait_runpod_iep_ready_or_replace(
            api_key,
            pod_ids[name],
            port,
            name,
            image,
            gpu_type_ids,
            cloud_types,
        )

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


def _runpod_pod_ids_from_urls(*urls: str) -> list[str]:
    """Extract RunPod pod IDs from proxy URLs generated by this scaler."""
    pod_ids: list[str] = []
    for url in urls:
        if not url.startswith("https://"):
            continue
        host = url.removeprefix("https://").split("/", 1)[0]
        pod_id = host.split("-", 1)[0]
        if pod_id:
            pod_ids.append(pod_id)
    return pod_ids


def _cleanup_created_runpod_pods(api_key: str, pod_ids: list[str], reason: str) -> None:
    """Best-effort termination for pods created by a failed scale-up attempt."""
    if not api_key or not pod_ids:
        return

    import httpx  # noqa: PLC0415

    for pod_id in pod_ids:
        try:
            mutation = (
                'mutation { podTerminate(input: {'
                f'podId: "{pod_id}"'
                '}) }'
            )
            resp = httpx.post(
                f"https://api.runpod.io/graphql?api_key={api_key}",
                json={"query": mutation},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                logger.warning(
                    "normal_scaler: RunPod cleanup for pod %s returned errors: %s",
                    pod_id,
                    data["errors"],
                )
                continue
            logger.info("normal_scaler: terminated RunPod pod %s after %s", pod_id, reason)
        except Exception as exc:  # noqa: BLE001
            logger.warning("normal_scaler: failed to terminate RunPod pod %s: %s", pod_id, exc)


def _rollback_aws_services(ecs_client, cluster: str) -> None:
    """Best-effort reset for services touched before a failed scale-up completed."""
    services = (
        "libraryai-eep-worker",
        *_CPU_IEP_SERVICES,
        "libraryai-eep-recovery",
        "libraryai-shadow-worker",
    )
    for service in services:
        _update_service(ecs_client, cluster, service, 0)


# Services whose state is consulted by the active-check.  GPU IEPs (iep0/1a/1b)
# are deliberately excluded: their ECS placeholders always sit at desired=0;
# the real GPU work runs on RunPod pods created by ``_create_runpod_pods``.
_ACTIVE_CHECK_SERVICES: list[str] = _CPU_IEP_SERVICES + _WORKER_SERVICES


def _normal_processing_already_active(ecs_client, cluster: str) -> bool:
    """
    Return True only when *every* ECS-managed normal-processing service is
    currently up.  Returns False (i.e. allow scale-up) on any unknown / failed
    state so a partial cluster can self-heal on the next trigger.

    Why every service?  Previously this checked only ``libraryai-eep-worker``
    and skipped scale-up whenever that one service had ``desired > 0``.  When
    an operator (or the scheduled scale-down path) brings any of the CPU IEPs
    or worker singletons to 0 individually — or terminates the RunPod pods
    out-of-band — the cluster ends up half-up and unable to make progress, but
    the trigger silently returns "already active" because eep-worker is still
    running with stale env-baked IEP URLs.

    The fix: a service counts as "active" only if both ``desired >= 1`` and
    ``running >= 1``.  If any of the services in ``_ACTIVE_CHECK_SERVICES`` is
    below that bar we return False so ``_do_scale_up`` proceeds to (re)create
    RunPod pods, register a fresh eep-worker task definition with the new IEP
    URLs, and force-redeploy every affected service.

    GPU IEPs (iep0/iep1a/iep1b) intentionally aren't checked here: their ECS
    services are placeholders at desired=0 by design.  The freshness of their
    RunPod backing is captured indirectly — every full scale-up registers a
    new eep-worker task def with the new RunPod URLs, so refreshing any of the
    other services is sufficient to also refresh the GPU URLs.
    """
    if not _ACTIVE_CHECK_SERVICES:
        return False

    try:
        resp = ecs_client.describe_services(
            cluster=cluster,
            services=_ACTIVE_CHECK_SERVICES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "normal_scaler: could not describe normal-processing services: %s — "
            "treating as not-active and proceeding with scale-up",
            exc,
        )
        return False

    services_by_name: dict[str, dict] = {
        svc.get("serviceName", ""): svc for svc in resp.get("services") or []
    }
    inactive: list[str] = []
    for name in _ACTIVE_CHECK_SERVICES:
        svc = services_by_name.get(name)
        if svc is None:
            inactive.append(f"{name}=missing")
            continue
        desired = int(svc.get("desiredCount") or 0)
        running = int(svc.get("runningCount") or 0)
        if desired < 1 or running < 1:
            inactive.append(f"{name}(desired={desired},running={running})")

    if inactive:
        logger.info(
            "normal_scaler: scale-up needed — %d service(s) below active threshold: %s",
            len(inactive),
            ", ".join(inactive),
        )
        return False

    logger.info(
        "normal_scaler: all %d normal-processing services already active — skipping scale-up",
        len(_ACTIVE_CHECK_SERVICES),
    )
    return True


def _create_runpod_pod_with_fallback_gpu_only_unused(
    api_key: str,
    name: str,
    image: str,
    port: int,
    gpu_type_ids: list[str],
    cloud_type: str,
) -> str:
    """
    Create one RunPod pod, trying GPU candidates in priority order.

    Only advances to the next GPU type on SUPPLY_CONSTRAINT (capacity unavailable).
    Any other error (auth failure, bad image, network) is re-raised immediately.
    """
    last_supply_error: Exception | None = None
    for gpu_type_id in gpu_type_ids:
        logger.info(
            "normal_scaler: attempting RunPod pod %s gpu_type=%r cloud_type=%s",
            name,
            gpu_type_id,
            cloud_type,
        )
        try:
            pod_id = _create_runpod_pod(api_key, name, image, port, gpu_type_id, cloud_type)
            logger.info(
                "normal_scaler: RunPod pod %s created with gpu_type=%r id=%s",
                name,
                gpu_type_id,
                pod_id,
            )
            return pod_id
        except RuntimeError as exc:
            err_str = str(exc)
            if _is_runpod_supply_error(err_str):
                logger.warning(
                    "normal_scaler: SUPPLY_CONSTRAINT for %s gpu_type=%r cloud_type=%s — trying next",
                    name,
                    gpu_type_id,
                    cloud_type,
                )
                last_supply_error = exc
                continue
            # Non-supply error (auth, bad image, etc.) — fail immediately, do not try next GPU
            raise
    raise RuntimeError(
        f"All GPU candidates exhausted for {name} "
        f"(tried {gpu_type_ids!r}, cloud_type={cloud_type!r}): {last_supply_error}"
    )


def _create_runpod_pod_with_fallback(
    api_key: str,
    name: str,
    image: str,
    port: int,
    gpu_type_ids: list[str],
    cloud_types: list[str] | None = None,
    cloud_type: str | None = None,
) -> str:
    """Create one RunPod pod, trying GPU and cloud candidates in priority order."""
    if not gpu_type_ids:
        raise RuntimeError(f"No RunPod GPU candidates configured for {name}")
    if cloud_types is None:
        cloud_types = [_normalize_runpod_cloud_type(cloud_type or "COMMUNITY")]
    last_supply_error: Exception | None = None
    for cloud_type in cloud_types:
        logger.info(
            "normal_scaler: attempting RunPod REST pod %s gpu_types=%r cloud_type=%s",
            name,
            gpu_type_ids,
            cloud_type,
        )
        try:
            pod_id = _create_runpod_pod_rest(api_key, name, image, port, gpu_type_ids, cloud_type)
            logger.info(
                "normal_scaler: RunPod REST pod %s created cloud_type=%s id=%s",
                name,
                cloud_type,
                pod_id,
            )
            return pod_id
        except RuntimeError as exc:
            err_str = str(exc)
            if _is_runpod_supply_error(err_str):
                logger.warning(
                    "normal_scaler: SUPPLY_CONSTRAINT for %s cloud_type=%s; trying next cloud",
                    name,
                    cloud_type,
                )
                last_supply_error = exc
                continue
            raise
    raise RuntimeError(
        f"All GPU candidates exhausted for {name} "
        f"(tried {gpu_type_ids!r}, cloud_types={cloud_types!r}): {last_supply_error}"
    )


def _create_runpod_pod_rest(
    api_key: str,
    name: str,
    image: str,
    port: int,
    gpu_type_ids: list[str],
    cloud_type: str,
) -> str:
    """Create one RunPod pod through the REST API using the configured GPU order."""
    import httpx  # noqa: PLC0415

    payload = {
        "name": name,
        "imageName": image,
        "computeType": "GPU",
        "cloudType": cloud_type,
        "gpuCount": 1,
        "gpuTypeIds": gpu_type_ids,
        "gpuTypePriority": "custom",
        "interruptible": False,
        "containerDiskInGb": int(os.environ.get("RUNPOD_CONTAINER_DISK_GB", "50")),
        "minVCPUPerGPU": int(os.environ.get("RUNPOD_MIN_VCPU_PER_GPU", "2")),
        "minRAMPerGPU": int(os.environ.get("RUNPOD_MIN_RAM_PER_GPU", "8")),
        "ports": [f"{port}/http"],
        "env": _runpod_iep_env(name, port),
    }
    volume_gb = int(os.environ.get("RUNPOD_VOLUME_GB", "0"))
    if volume_gb > 0:
        payload["volumeInGb"] = volume_gb
        payload["volumeMountPath"] = os.environ.get("RUNPOD_VOLUME_MOUNT_PATH", "/workspace")

    resp = httpx.post(
        "https://rest.runpod.io/v1/pods",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30.0,
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = resp.text[:1000]
        raise RuntimeError(f"RunPod REST error creating {name}: HTTP {resp.status_code}: {body}") from exc

    data = resp.json()
    pod_id = data.get("id")
    if not pod_id:
        raise RuntimeError(f"RunPod REST response missing pod id for {name}: {data}")
    return str(pod_id)


def _is_runpod_supply_error(message: str) -> bool:
    normalized = message.lower()
    return (
        "supply_constraint" in normalized
        or "capacity" in normalized
        or "no longer any instances available" in normalized
        or "does not have the resources to deploy your pod" in normalized
        or "try a different machine" in normalized
    )


def _runpod_iep_env(name: str, port: int) -> dict[str, str]:
    env = {
        "PYTHONPATH": "/app",
        "AWS_REGION": os.environ.get("AWS_REGION", "eu-central-1"),
        "S3_REGION": os.environ.get("S3_REGION") or os.environ.get("AWS_REGION", "eu-central-1"),
        "S3_BUCKET_NAME": os.environ.get("S3_BUCKET_NAME", "libraryai2"),
        "HEALTH_PORT": str(port),
        "RUNPOD_TERMINATE_ON_IDLE": "true",
    }
    if name.endswith("iep0"):
        env["IEP0_MODEL_PATH"] = os.environ.get("IEP0_MODEL_PATH", "/app/models/iep0/classifier.pt")
    elif name.endswith("iep1a"):
        env["IEP1A_MODELS_DIR"] = os.environ.get("IEP1A_MODELS_DIR", "/app/models/iep1a")
    elif name.endswith("iep1b"):
        env["IEP1B_MODELS_DIR"] = os.environ.get("IEP1B_MODELS_DIR", "/app/models/iep1b")
    return env


def _create_runpod_pod(
    api_key: str,
    name: str,
    image: str,
    port: int,
    gpu_type_id: str,
    cloud_type: str,
) -> str:
    """Create one RunPod pod for a specific GPU type."""
    import httpx  # noqa: PLC0415

    mutation = (
        'mutation { podFindAndDeployOnDemand(input: {'
        f' name: "{name}", imageName: "{image}",'
        f' gpuTypeId: "{gpu_type_id}", cloudType: {cloud_type},'
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
        raise RuntimeError(data["errors"])
    pod_id = data["data"]["podFindAndDeployOnDemand"]["id"]
    logger.info(
        "normal_scaler: RunPod pod created for %s gpu_type=%s id=%s",
        name,
        gpu_type_id,
        pod_id,
    )
    return pod_id


def _create_runpod_pod_set(
    api_key: str,
    gpu_type_id: str,
    cloud_type: str,
    pods: list[tuple[str, str, int]],
) -> dict[str, str]:
    """Create the full GPU pod set for one RunPod GPU type."""
    import httpx  # noqa: PLC0415

    pod_ids: dict[str, str] = {}
    for name, image, port in pods:
        mutation = (
            'mutation { podFindAndDeployOnDemand(input: {'
            f' name: "{name}", imageName: "{image}",'
            f' gpuTypeId: "{gpu_type_id}", cloudType: {cloud_type},'
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

    return pod_ids


def _normalize_runpod_cloud_type(value: str) -> str:
    cloud_type = (value or "SECURE").strip().upper()
    if cloud_type == "SECURITY":
        return "SECURE"
    if cloud_type not in {"COMMUNITY", "SECURE"}:
        logger.warning(
            "normal_scaler: invalid RUNPOD_CLOUD_TYPE=%s, defaulting to SECURE",
            cloud_type,
        )
        return "SECURE"
    return cloud_type


def _runpod_cloud_type_candidates() -> list[str]:
    primary = _normalize_runpod_cloud_type(os.environ.get("RUNPOD_CLOUD_TYPE", "SECURE"))
    raw = os.environ.get("RUNPOD_CLOUD_TYPES", "").strip()
    values = [primary]
    if raw:
        values.extend(item.strip() for item in raw.split(","))
    else:
        values.append("SECURE" if primary == "COMMUNITY" else "COMMUNITY")

    candidates: list[str] = []
    for value in values:
        cloud_type = _normalize_runpod_cloud_type(value)
        if cloud_type not in candidates:
            candidates.append(cloud_type)
    return candidates


def _normalize_runpod_pod_mode(value: str) -> str:
    mode = (value or "create").strip().lower()
    if mode in {"create", "existing"}:
        return mode
    logger.warning("normal_scaler: invalid RUNPOD_POD_MODE=%r, defaulting to create", value)
    return "create"


def _runpod_gpu_type_candidates() -> list[str]:
    raw_values: list[str] = []
    primary = os.environ.get("RUNPOD_GPU_TYPE_ID", "").strip()
    fallback_list = os.environ.get("RUNPOD_GPU_TYPES", "").strip()
    if primary:
        raw_values.append(primary)
    if fallback_list:
        raw_values.extend(item.strip() for item in fallback_list.split(","))
    raw_values.extend(
        [
            # Tier 1 — preferred (lower cost / good availability)
            "NVIDIA RTX 4000 Ada Generation",
            "NVIDIA RTX A4000",
            "NVIDIA RTX 2000 Ada Generation",
            "NVIDIA RTX A5000",
            "NVIDIA RTX A4500",
            # Tier 2 — fallback (higher-end / less available)
            "NVIDIA L4",
            "NVIDIA A40",
            "NVIDIA RTX A6000",
        ]
    )

    aliases = {
        "A40": "NVIDIA A40",
        "RTX 3090": "NVIDIA GeForce RTX 3090",
        "3090": "NVIDIA GeForce RTX 3090",
        "RTX 4090": "NVIDIA GeForce RTX 4090",
        "4090": "NVIDIA GeForce RTX 4090",
        "RTX A5000": "NVIDIA RTX A5000",
        "A5000": "NVIDIA RTX A5000",
        "L4": "NVIDIA L4",
        "L40": "NVIDIA L40",
        "L40S": "NVIDIA L40S",
        "RTX A4000": "NVIDIA RTX A4000",
        "A4000": "NVIDIA RTX A4000",
        "RTX A4500": "NVIDIA RTX A4500",
        "A4500": "NVIDIA RTX A4500",
        "RTX A6000": "NVIDIA RTX A6000",
        "A6000": "NVIDIA RTX A6000",
        "RTX 4000 ADA": "NVIDIA RTX 4000 Ada Generation",
        "4000 ADA": "NVIDIA RTX 4000 Ada Generation",
        "RTX 4000": "NVIDIA RTX 4000 Ada Generation",
        "RTX 2000 ADA": "NVIDIA RTX 2000 Ada Generation",
        "2000 ADA": "NVIDIA RTX 2000 Ada Generation",
        "RTX 5000 ADA": "NVIDIA RTX 5000 Ada Generation",
        "5000 ADA": "NVIDIA RTX 5000 Ada Generation",
        "RTX 6000 ADA": "NVIDIA RTX 6000 Ada Generation",
        "6000 ADA": "NVIDIA RTX 6000 Ada Generation",
        "RTX 4080 SUPER": "NVIDIA GeForce RTX 4080 SUPER",
        "4080 SUPER": "NVIDIA GeForce RTX 4080 SUPER",
        "RTX 5090": "NVIDIA GeForce RTX 5090",
        "5090": "NVIDIA GeForce RTX 5090",
    }
    rest_supported_gpu_types = {
        "NVIDIA GeForce RTX 4090",
        "NVIDIA A40",
        "NVIDIA RTX A5000",
        "NVIDIA GeForce RTX 5090",
        "NVIDIA H100 80GB HBM3",
        "NVIDIA GeForce RTX 3090",
        "NVIDIA RTX A4500",
        "NVIDIA L40S",
        "NVIDIA H200",
        "NVIDIA L4",
        "NVIDIA RTX 6000 Ada Generation",
        "NVIDIA A100-SXM4-80GB",
        "NVIDIA RTX 4000 Ada Generation",
        "NVIDIA RTX A6000",
        "NVIDIA A100 80GB PCIe",
        "NVIDIA RTX 2000 Ada Generation",
        "NVIDIA RTX A4000",
        "NVIDIA RTX PRO 6000 Blackwell Server Edition",
        "NVIDIA H100 PCIe",
        "NVIDIA H100 NVL",
        "NVIDIA L40",
        "NVIDIA B200",
        "NVIDIA GeForce RTX 3080 Ti",
        "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
        "NVIDIA GeForce RTX 3080",
        "NVIDIA GeForce RTX 3070",
    }
    candidates: list[str] = []
    for value in raw_values:
        if not value:
            continue
        gpu_type = aliases.get(value, value)
        if gpu_type not in rest_supported_gpu_types:
            logger.warning(
                "normal_scaler: skipping unsupported RunPod REST gpu_type_id=%r",
                gpu_type,
            )
            continue
        if gpu_type not in candidates:
            candidates.append(gpu_type)
    return candidates


def _resume_existing_runpod_pods(api_key: str, region: str) -> tuple[str, str, str]:
    """Resume known RunPod pods and return their stable proxy URLs."""
    import boto3  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    pods = {
        "iep0": (os.environ.get("RUNPOD_IEP0_POD_ID", "").strip(), 8006),
        "iep1a": (os.environ.get("RUNPOD_IEP1A_POD_ID", "").strip(), 8001),
        "iep1b": (os.environ.get("RUNPOD_IEP1B_POD_ID", "").strip(), 8002),
    }
    missing = [name for name, (pod_id, _) in pods.items() if not pod_id]
    if missing:
        raise RuntimeError(f"RUNPOD_POD_MODE=existing but pod IDs are missing: {missing}")

    logger.info(
        "normal_scaler: resuming existing RunPod pods iep0=%s iep1a=%s iep1b=%s",
        pods["iep0"][0],
        pods["iep1a"][0],
        pods["iep1b"][0],
    )

    for name, (pod_id, _) in pods.items():
        mutation = (
            'mutation { podResume(input: {'
            f' podId: "{pod_id}", gpuCount: 1'
            '}) { id desiredStatus imageName } }'
        )
        resp = httpx.post(
            f"https://api.runpod.io/graphql?api_key={api_key}",
            json={"query": mutation},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"RunPod error resuming {name}: {data['errors']}")
        status = data["data"]["podResume"].get("desiredStatus")
        logger.info("normal_scaler: RunPod pod resumed for %s id=%s status=%s", name, pod_id, status)

    try:
        import json  # noqa: PLC0415
        bucket = os.environ.get("S3_BUCKET_NAME", "libraryai2")
        s3 = boto3.client("s3", region_name=region)
        s3.put_object(
            Bucket=bucket,
            Key="ops/runpod-pods.json",
            Body=json.dumps({name: pod_id for name, (pod_id, _) in pods.items()}).encode(),
        )
        logger.info("normal_scaler: existing RunPod pod IDs saved to s3://%s/ops/runpod-pods.json", bucket)
    except Exception as exc:  # noqa: BLE001
        logger.warning("normal_scaler: could not save existing pod IDs to S3: %s", exc)

    return (
        f"https://{pods['iep0'][0]}-8006.proxy.runpod.net",
        f"https://{pods['iep1a'][0]}-8001.proxy.runpod.net",
        f"https://{pods['iep1b'][0]}-8002.proxy.runpod.net",
    )


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

    iep2a_url = os.environ.get("IEP2A_URL", "http://iep2a-v2:8004").strip()
    if not iep2a_url:
        iep2a_url = "http://iep2a-v2:8004"

    url_overrides = {
        "IEP0_URL": iep0_url,
        "IEP1A_URL": iep1a_url,
        "IEP1B_URL": iep1b_url,
        "IEP2A_URL": iep2a_url,
    }
    for container in task_def.get("containerDefinitions", []):
        env = []
        seen: set[str] = set()
        for entry in container.get("environment", []):
            name = entry["name"]
            seen.add(name)
            env.append({"name": name, "value": url_overrides.get(name, entry["value"])})
        for name, value in url_overrides.items():
            if name not in seen:
                env.append({"name": name, "value": value})
        container["environment"] = env

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
) -> bool:
    """Call ecs:UpdateService, logging success or failure."""
    assert service not in _EXCLUDED_SERVICES, (
        f"BUG: _update_service called for excluded service {service!r}"
    )
    kwargs: dict = {"cluster": cluster, "service": service, "desiredCount": desired}
    if task_def_arn:
        kwargs["taskDefinition"] = task_def_arn
    service_connect_config = _SERVICE_CONNECT_CONFIGS.get(service)
    if service_connect_config is not None:
        kwargs["serviceConnectConfiguration"] = service_connect_config
    if force_new:
        kwargs["forceNewDeployment"] = True
    try:
        ecs_client.update_service(**kwargs)
        logger.info("normal_scaler: %s desired → %d", service, desired)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("normal_scaler: failed to update %s: %s", service, exc)
        return False
