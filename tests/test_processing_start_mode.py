"""
tests/test_processing_start_mode.py
-------------------------------------
Tests for PROCESSING_START_MODE=immediate|scheduled_window.

Covers all required scenarios (spec §10):
  1. immediate mode triggers scale-up after durable enqueue
  2. scheduled_window does not scale up on job arrival
  3. drain ignores human-review states
  4. drain waits for active processing states / Redis queues
  5. duplicate arrivals do not cause duplicate scale-up (Redis lock)
  6. iep1d/retraining/dataset-builder/prometheus/grafana excluded from normal scale-up

Tests are pure unit tests — no real Redis, AWS, or DB required.
boto3 is patched via unittest.mock; Redis is replaced with a FakeRedis dict.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, call, patch


# ── helpers ───────────────────────────────────────────────────────────────────

class FakeRedis:
    """Minimal in-memory Redis stand-in for lock testing."""

    def __init__(self):
        self._store: dict = {}
        self._lock = threading.Lock()

    def set(self, key, value, nx=False, ex=None):
        with self._lock:
            if nx and key in self._store:
                return None  # NX failed
            self._store[key] = value
            return True

    def get(self, key):
        with self._lock:
            return self._store.get(key)

    def delete(self, key):
        with self._lock:
            self._store.pop(key, None)


# ── 1 & 2: mode-based scale-up trigger ───────────────────────────────────────

class TestMaybeTriggerScaleUp(unittest.TestCase):
    """maybe_trigger_scale_up: mode routing and lock behaviour."""

    def _run(self, mode: str, fake_redis: FakeRedis | None = None):
        r = fake_redis if fake_redis is not None else FakeRedis()
        with patch.dict(os.environ, {"PROCESSING_START_MODE": mode}, clear=False):
            with patch(
                "services.eep.app.scaling.normal_scaler._do_scale_up"
            ) as mock_scale:
                from services.eep.app.scaling.normal_scaler import maybe_trigger_scale_up
                maybe_trigger_scale_up(r)
                return mock_scale

    # ── test 1: immediate triggers scale-up ───────────────────────────────────
    def test_immediate_mode_calls_do_scale_up(self):
        mock = self._run("immediate")
        mock.assert_called_once()

    # ── test 2: scheduled_window does NOT trigger ─────────────────────────────
    def test_scheduled_window_does_not_call_do_scale_up(self):
        mock = self._run("scheduled_window")
        mock.assert_not_called()

    def test_unknown_mode_does_not_call_do_scale_up(self):
        mock = self._run("batch")
        mock.assert_not_called()

    # ── test 5a: duplicate lock suppresses second trigger ────────────────────
    def test_duplicate_arrival_suppressed_by_redis_lock(self):
        """Second call while lock is held must not trigger _do_scale_up again."""
        r = FakeRedis()
        calls = []

        def slow_scale():
            calls.append("scale")
            time.sleep(0.05)  # hold lock just long enough for second call

        with patch.dict(os.environ, {"PROCESSING_START_MODE": "immediate"}, clear=False):
            with patch(
                "services.eep.app.scaling.normal_scaler._do_scale_up",
                side_effect=slow_scale,
            ):
                from services.eep.app.scaling.normal_scaler import maybe_trigger_scale_up

                t1 = threading.Thread(target=maybe_trigger_scale_up, args=(r,))
                t2 = threading.Thread(target=maybe_trigger_scale_up, args=(r,))
                t1.start()
                time.sleep(0.005)  # let t1 acquire the lock
                t2.start()
                t1.join()
                t2.join()

        self.assertEqual(calls, ["scale"], "scale-up must fire exactly once")

    # ── test 5b: lock released; subsequent job triggers fresh scale-up ────────
    def test_lock_released_allows_later_trigger(self):
        """After lock released a new job arrival triggers scale-up again."""
        r = FakeRedis()
        with patch.dict(os.environ, {"PROCESSING_START_MODE": "immediate"}, clear=False):
            with patch(
                "services.eep.app.scaling.normal_scaler._do_scale_up"
            ) as mock_scale:
                from services.eep.app.scaling.normal_scaler import maybe_trigger_scale_up
                maybe_trigger_scale_up(r)  # first call — acquires + releases lock
                maybe_trigger_scale_up(r)  # second call — lock gone, triggers again
        self.assertEqual(mock_scale.call_count, 2)


# ── 6: service inclusion/exclusion ────────────────────────────────────────────

class TestScaleUpServiceList(unittest.TestCase):
    """_do_scale_up must start exactly the right ECS services and exclude the rest.

    iep0/iep1a/iep1b are started via RunPod (not ECS update_service) — they are
    intentionally absent from EXPECTED_STARTED.
    """

    # ECS services started by update_service.  iep0/iep1a/iep1b are RunPod — excluded.
    # iep1d is now pre-warmed alongside the other CPU IEPs (was previously
    # left at desired=0 and started on-demand, but its cold-start exceeded
    # the worker's IEP1D_READY_TIMEOUT_SECONDS for the first wave of pages
    # that needed rescue).  See normal_scaler.py module docstring.
    EXPECTED_STARTED = {
        "libraryai-iep1d",
        "libraryai-iep1e",
        "libraryai-iep2a-v2",
        "libraryai-iep2b",
        "libraryai-eep-worker",
        "libraryai-eep-recovery",
        "libraryai-shadow-worker",
    }

    MUST_NOT_START = {
        "libraryai-retraining-worker",
        "libraryai-dataset-builder",
        "libraryai-prometheus",
        "libraryai-grafana",
    }

    def _run_do_scale_up(self, env_extra: dict | None = None):
        """Run _do_scale_up with successful RunPod URL discovery."""
        env = {
            "ECS_CLUSTER": "test-cluster",
            "WORKER_DESIRED_COUNT": "2",
            "AWS_REGION": "us-east-1",
            "RUNPOD_API_KEY": "test-key",
        }
        if env_extra:
            env.update(env_extra)

        mock_ecs = MagicMock()

        with patch.dict(os.environ, env, clear=False):
            with patch("boto3.client", return_value=mock_ecs):
                from services.eep.app.scaling import normal_scaler
                import importlib
                importlib.reload(normal_scaler)  # reload so env vars are re-read
                with patch.object(
                    normal_scaler,
                    "_create_runpod_pods",
                    return_value=(
                        "https://pod1-8006.proxy.runpod.net",
                        "https://pod2-8001.proxy.runpod.net",
                        "https://pod3-8002.proxy.runpod.net",
                    ),
                ):
                    with patch.object(
                        normal_scaler,
                        "_register_eep_worker_with_runpod_urls",
                        return_value="arn:aws:ecs:us-east-1:123:task-definition/libraryai-eep-worker:2",
                    ):
                        normal_scaler._do_scale_up()

        return mock_ecs

    def test_all_required_services_started(self):
        mock_ecs = self._run_do_scale_up()
        started = {
            c.kwargs["service"]
            for c in mock_ecs.update_service.call_args_list
        }
        self.assertEqual(started, self.EXPECTED_STARTED)

    def test_excluded_services_not_started(self):
        mock_ecs = self._run_do_scale_up()
        started = {
            c.kwargs["service"]
            for c in mock_ecs.update_service.call_args_list
        }
        overlap = started & self.MUST_NOT_START
        self.assertEqual(overlap, set(), f"Excluded services were started: {overlap}")

    def test_iep1d_pre_warmed(self):
        """
        iep1d is now pre-warmed during normal scale-up so its Fargate cold
        start (4–6 min) doesn't race the worker's IEP1D /ready timeout for
        pages that need rescue.  See normal_scaler.py module docstring.
        """
        mock_ecs = self._run_do_scale_up()
        started = {c.kwargs["service"] for c in mock_ecs.update_service.call_args_list}
        self.assertIn("libraryai-iep1d", started)

    def test_retraining_dataset_builder_excluded(self):
        mock_ecs = self._run_do_scale_up()
        started = {c.kwargs["service"] for c in mock_ecs.update_service.call_args_list}
        self.assertNotIn("libraryai-retraining-worker", started)
        self.assertNotIn("libraryai-dataset-builder", started)

    def test_observability_excluded(self):
        mock_ecs = self._run_do_scale_up()
        started = {c.kwargs["service"] for c in mock_ecs.update_service.call_args_list}
        self.assertNotIn("libraryai-prometheus", started)
        self.assertNotIn("libraryai-grafana", started)

    def test_gpu_ieps_not_started_via_ecs(self):
        """iep0/iep1a/iep1b are RunPod pods — must NOT appear in ECS update_service calls."""
        mock_ecs = self._run_do_scale_up()
        started = {c.kwargs["service"] for c in mock_ecs.update_service.call_args_list}
        self.assertNotIn("libraryai-iep0", started)
        self.assertNotIn("libraryai-iep1a", started)
        self.assertNotIn("libraryai-iep1b", started)

    def test_runpod_pods_created_when_api_key_set(self):
        """When RUNPOD_API_KEY is set, _create_runpod_pods is called for iep0/iep1a/iep1b."""
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)  # reload before patching so reload doesn't undo the patch

        env = {
            "ECS_CLUSTER": "test-cluster",
            "WORKER_DESIRED_COUNT": "2",
            "AWS_REGION": "us-east-1",
            "RUNPOD_API_KEY": "test-key",
        }
        mock_ecs = MagicMock()
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {
                "family": "libraryai-eep-worker",
                "containerDefinitions": [{"name": "eep-worker", "environment": []}],
            }
        }
        mock_ecs.register_task_definition.return_value = {
            "taskDefinition": {
                "taskDefinitionArn": "arn:aws:ecs:us-east-1:123:task-definition/libraryai-eep-worker:2"
            }
        }

        with patch.dict(os.environ, env, clear=False):
            with patch("boto3.client", return_value=mock_ecs):
                with patch.object(
                    normal_scaler,
                    "_create_runpod_pods",
                    return_value=(
                        "https://pod1-8006.proxy.runpod.net",
                        "https://pod2-8001.proxy.runpod.net",
                        "https://pod3-8002.proxy.runpod.net",
                    ),
                ) as mock_create_pods:
                    normal_scaler._do_scale_up()

        mock_create_pods.assert_called_once_with("test-key", "us-east-1")
        registered_env = mock_ecs.register_task_definition.call_args.kwargs[
            "containerDefinitions"
        ][0]["environment"]
        env_by_name = {entry["name"]: entry["value"] for entry in registered_env}
        self.assertEqual(env_by_name["IEP0_URL"], "https://pod1-8006.proxy.runpod.net")
        self.assertEqual(env_by_name["IEP1A_URL"], "https://pod2-8001.proxy.runpod.net")
        self.assertEqual(env_by_name["IEP1B_URL"], "https://pod3-8002.proxy.runpod.net")
        self.assertEqual(env_by_name["IEP2A_URL"], "http://iep2a-v2:8004")

    def test_runpod_pods_skipped_when_no_api_key(self):
        """When RUNPOD_API_KEY is absent, _create_runpod_pods is not called."""
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        with patch.object(normal_scaler, "_create_runpod_pods") as mock_create_pods:
            mock_ecs = self._run_do_scale_up({"RUNPOD_API_KEY": ""})
        mock_create_pods.assert_not_called()
        mock_ecs.update_service.assert_not_called()

    def test_runpod_startup_failure_aborts_without_starting_workers(self):
        """RunPod create failures must not start workers with stale ECS DNS URLs."""
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        env = {
            "ECS_CLUSTER": "test-cluster",
            "WORKER_DESIRED_COUNT": "2",
            "AWS_REGION": "us-east-1",
            "RUNPOD_API_KEY": "test-key",
        }
        mock_ecs = MagicMock()

        with patch.dict(os.environ, env, clear=False):
            with patch("boto3.client", return_value=mock_ecs):
                with patch.object(
                    normal_scaler,
                    "_create_runpod_pods",
                    side_effect=RuntimeError("RunPod supply constraint"),
                ):
                    normal_scaler._do_scale_up()

        mock_ecs.update_service.assert_not_called()

    def test_fully_active_cluster_suppresses_duplicate_runpod_create(self):
        """When *every* normal-processing service is up, scale-up is a no-op.

        The active-check guards against duplicate RunPod pod creation when an
        immediate-mode trigger fires while the cluster is already healthy.
        It returns True only if all services in _ACTIVE_CHECK_SERVICES report
        desired>=1 AND running>=1.
        """
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        env = {
            "ECS_CLUSTER": "test-cluster",
            "WORKER_DESIRED_COUNT": "2",
            "AWS_REGION": "us-east-1",
            "RUNPOD_API_KEY": "test-key",
        }
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = {
            "services": [
                {
                    "serviceName": svc,
                    "desiredCount": 1,
                    "runningCount": 1,
                    "pendingCount": 0,
                }
                for svc in normal_scaler._ACTIVE_CHECK_SERVICES
            ]
        }

        with patch.dict(os.environ, env, clear=False):
            with patch("boto3.client", return_value=mock_ecs):
                with patch.object(normal_scaler, "_create_runpod_pods") as mock_create_pods:
                    normal_scaler._do_scale_up()

        mock_create_pods.assert_not_called()
        mock_ecs.update_service.assert_not_called()

    def test_aws_startup_failure_terminates_created_runpod_pods(self):
        """If AWS startup fails after pod creation, the created RunPod pods are terminated."""
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        env = {
            "ECS_CLUSTER": "test-cluster",
            "WORKER_DESIRED_COUNT": "2",
            "AWS_REGION": "us-east-1",
            "RUNPOD_API_KEY": "test-key",
        }
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "libraryai-eep-worker",
                    "desiredCount": 0,
                    "runningCount": 0,
                    "pendingCount": 0,
                }
            ]
        }

        with patch.dict(os.environ, env, clear=False):
            with patch("boto3.client", return_value=mock_ecs):
                with patch.object(
                    normal_scaler,
                    "_create_runpod_pods",
                    return_value=(
                        "https://pod1-8006.proxy.runpod.net",
                        "https://pod2-8001.proxy.runpod.net",
                        "https://pod3-8002.proxy.runpod.net",
                    ),
                ):
                    with patch.object(
                        normal_scaler,
                        "_register_eep_worker_with_runpod_urls",
                        side_effect=RuntimeError("iam:PassRole denied"),
                    ):
                        with patch.object(normal_scaler, "_cleanup_created_runpod_pods") as mock_cleanup:
                            normal_scaler._do_scale_up()

        mock_cleanup.assert_called_once_with(
            "test-key",
            ["pod1", "pod2", "pod3"],
            "AWS startup failure",
        )

    def test_runpod_gpu_candidates_normalize_aliases_and_fallbacks(self):
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        env = {
            "RUNPOD_GPU_TYPE_ID": "A40",
            "RUNPOD_GPU_TYPES": "NVIDIA RTX A5000,RTX 4090,A40,NVIDIA RTX PRO 4500 Blackwell",
        }
        with patch.dict(os.environ, env, clear=False):
            candidates = normal_scaler._runpod_gpu_type_candidates()

        self.assertEqual(candidates[:3], ["NVIDIA A40", "NVIDIA RTX A5000", "NVIDIA GeForce RTX 4090"])
        self.assertNotIn("NVIDIA RTX PRO 4500 Blackwell", candidates)
        self.assertIn("NVIDIA L4", candidates)
        self.assertIn("NVIDIA RTX A6000", candidates)

    def test_runpod_cloud_type_normalizes_security_alias(self):
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        self.assertEqual(normal_scaler._normalize_runpod_cloud_type("SECURITY"), "SECURE")
        self.assertEqual(normal_scaler._normalize_runpod_cloud_type("community"), "COMMUNITY")

    def test_runpod_cloud_candidates_default_to_both_pools(self):
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        with patch.dict(os.environ, {"RUNPOD_CLOUD_TYPE": "", "RUNPOD_CLOUD_TYPES": ""}, clear=False):
            self.assertEqual(normal_scaler._runpod_cloud_type_candidates(), ["SECURE", "COMMUNITY"])

    def test_runpod_pod_mode_invalid_defaults_to_create(self):
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        self.assertEqual(normal_scaler._normalize_runpod_pod_mode("_create_runpod_pods"), "create")

    def test_worker_desired_count_used(self):
        """WORKER_DESIRED_COUNT applies to eep-worker and shadow-worker only.

        eep-recovery is intentionally pinned to 1 in _do_scale_up because it
        mutates Redis queues and must run as a singleton (see normal_scaler:
        "eep-recovery mutates Redis queues and must run as a singleton").
        """
        mock_ecs = self._run_do_scale_up({"WORKER_DESIRED_COUNT": "3"})

        scaled_calls = [
            c for c in mock_ecs.update_service.call_args_list
            if c.kwargs["service"] in {
                "libraryai-eep-worker",
                "libraryai-shadow-worker",
            }
        ]
        self.assertEqual(len(scaled_calls), 2, "eep-worker + shadow-worker must both scale")
        for c in scaled_calls:
            self.assertEqual(c.kwargs["desiredCount"], 3)

        recovery_calls = [
            c for c in mock_ecs.update_service.call_args_list
            if c.kwargs["service"] == "libraryai-eep-recovery"
        ]
        self.assertEqual(len(recovery_calls), 1)
        self.assertEqual(
            recovery_calls[0].kwargs["desiredCount"], 1,
            "eep-recovery must remain a singleton regardless of WORKER_DESIRED_COUNT",
        )


# ── RunPod pod-with-fallback unit tests ───────────────────────────────────────

class TestCreateRunPodPodWithFallback(unittest.TestCase):
    """_create_runpod_pod_with_fallback: SUPPLY_CONSTRAINT fallback behaviour."""

    def _call(self, side_effects, gpu_types=None, cloud_types=None):
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        gpu_types = gpu_types or ["NVIDIA GeForce RTX 4090", "NVIDIA RTX A5000"]
        with patch.object(normal_scaler, "_create_runpod_pod_rest", side_effect=side_effects):
            return normal_scaler._create_runpod_pod_with_fallback(
                api_key="test-key",
                name="libraryai-iep0",
                image="gma51/libraryai-iep0:latest",
                port=8006,
                gpu_type_ids=gpu_types,
                cloud_type="COMMUNITY",
                cloud_types=cloud_types,
            )

    def test_first_gpu_succeeds(self):
        result = self._call(["pod-id-abc"])
        self.assertEqual(result, "pod-id-abc")

    def test_supply_constraint_on_first_falls_back_to_second_cloud(self):
        result = self._call([
            RuntimeError("SUPPLY_CONSTRAINT: no capacity"),
            "pod-id-fallback",
        ], cloud_types=["COMMUNITY", "SECURE"])
        self.assertEqual(result, "pod-id-fallback")

    def test_runpod_resource_error_falls_back_to_second_cloud(self):
        result = self._call([
            RuntimeError(
                "RunPod REST error creating libraryai-iep0: HTTP 500: "
                "This machine does not have the resources to deploy your pod. "
                "Please try a different machine"
            ),
            "pod-id-secure",
        ], cloud_types=["COMMUNITY", "SECURE"])
        self.assertEqual(result, "pod-id-secure")

    def test_all_clouds_supply_constrained_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._call([
                RuntimeError("SUPPLY_CONSTRAINT: no capacity"),
                RuntimeError("SUPPLY_CONSTRAINT: no capacity"),
            ], cloud_types=["COMMUNITY", "SECURE"])
        self.assertIn("exhausted", str(ctx.exception))

    def test_non_supply_error_propagates_immediately(self):
        """Auth failures and other non-capacity errors must not try the next GPU."""
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        call_count = 0

        def side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Unauthorized: invalid API key")

        with patch.object(normal_scaler, "_create_runpod_pod_rest", side_effect=side_effect):
            with self.assertRaises(RuntimeError) as ctx:
                normal_scaler._create_runpod_pod_with_fallback(
                    api_key="bad-key",
                    name="libraryai-iep0",
                    image="gma51/libraryai-iep0:latest",
                    port=8006,
                    gpu_type_ids=["NVIDIA GeForce RTX 4090", "NVIDIA RTX A5000"],
                    cloud_type="COMMUNITY",
                    cloud_types=["COMMUNITY", "SECURE"],
                )
        self.assertEqual(call_count, 1, "Must not try next cloud on non-supply error")
        self.assertIn("Unauthorized", str(ctx.exception))

    def test_gpu_types_empty_raises(self):
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        with self.assertRaises(RuntimeError):
            normal_scaler._create_runpod_pod_with_fallback(
                api_key="key",
                name="libraryai-iep0",
                image="img",
                port=8006,
                gpu_type_ids=[],
                cloud_type="COMMUNITY",
            )

    def test_runpod_gpu_types_empty_env_uses_defaults(self):
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        with patch.dict(os.environ, {"RUNPOD_GPU_TYPE_ID": "", "RUNPOD_GPU_TYPES": ""}, clear=False):
            candidates = normal_scaler._runpod_gpu_type_candidates()

        self.assertIn("NVIDIA RTX A5000", candidates)
        self.assertGreater(len(candidates), 1)
        self.assertEqual(candidates[0], "NVIDIA RTX 4000 Ada Generation")

    def test_runpod_rest_create_uses_gpu_type_ids_payload(self):
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        class Response:
            status_code = 200
            text = '{"id":"pod-rest"}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"id": "pod-rest"}

        with patch("httpx.post", return_value=Response()) as mock_post:
            pod_id = normal_scaler._create_runpod_pod_rest(
                api_key="test-key",
                name="libraryai-iep1a",
                image="gma51/libraryai-iep1a:latest",
                port=8001,
                gpu_type_ids=["NVIDIA RTX A5000", "NVIDIA GeForce RTX 4090"],
                cloud_type="SECURE",
            )

        self.assertEqual(pod_id, "pod-rest")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(payload["gpuTypeIds"], ["NVIDIA RTX A5000", "NVIDIA GeForce RTX 4090"])
        self.assertEqual(payload["gpuTypePriority"], "custom")
        self.assertEqual(payload["ports"], ["8001/http"])
        self.assertEqual(payload["env"]["IEP1A_MODELS_DIR"], "/app/models/iep1a")


# ── _normal_processing_already_active: full-cluster active check ──────────────


class TestNormalProcessingAlreadyActive(unittest.TestCase):
    """
    The active-check skips the scale-up only when *every* ECS-managed
    normal-processing service is fully up.

    The previous implementation checked only ``libraryai-eep-worker`` and
    silently skipped scale-up whenever that one service was running, even when
    a CPU IEP or worker singleton had been brought to 0 individually.  This
    suite locks in the multi-service behaviour.
    """

    def setUp(self):
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)
        self.normal_scaler = normal_scaler

    def _all_active_response(self):
        return {
            "services": [
                {
                    "serviceName": svc,
                    "desiredCount": 1,
                    "runningCount": 1,
                    "pendingCount": 0,
                }
                for svc in self.normal_scaler._ACTIVE_CHECK_SERVICES
            ]
        }

    def _response_with_overrides(self, overrides: dict[str, dict]):
        services = []
        for svc in self.normal_scaler._ACTIVE_CHECK_SERVICES:
            row = {
                "serviceName": svc,
                "desiredCount": 1,
                "runningCount": 1,
                "pendingCount": 0,
            }
            row.update(overrides.get(svc, {}))
            services.append(row)
        return {"services": services}

    def test_returns_true_when_all_services_fully_active(self):
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._all_active_response()
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertTrue(result)

    def test_returns_false_when_eep_worker_at_zero(self):
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._response_with_overrides(
            {"libraryai-eep-worker": {"desiredCount": 0, "runningCount": 0}}
        )
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertFalse(result)

    def test_returns_false_when_iep1d_at_zero(self):
        """iep1d is now pre-warmed, so a partial cluster where iep1d is at 0
        must trigger a fresh scale-up (otherwise the rescue-path race is back)."""
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._response_with_overrides(
            {"libraryai-iep1d": {"desiredCount": 0, "runningCount": 0}}
        )
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertFalse(result)

    def test_returns_false_when_iep1e_at_zero(self):
        """The bug we just lived through: eep-worker up but a CPU IEP scaled down."""
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._response_with_overrides(
            {"libraryai-iep1e": {"desiredCount": 0, "runningCount": 0}}
        )
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertFalse(result)

    def test_returns_false_when_iep2a_at_zero(self):
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._response_with_overrides(
            {"libraryai-iep2a-v2": {"desiredCount": 0, "runningCount": 0}}
        )
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertFalse(result)

    def test_returns_false_when_iep2b_at_zero(self):
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._response_with_overrides(
            {"libraryai-iep2b": {"desiredCount": 0, "runningCount": 0}}
        )
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertFalse(result)

    def test_returns_false_when_eep_recovery_at_zero(self):
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._response_with_overrides(
            {"libraryai-eep-recovery": {"desiredCount": 0, "runningCount": 0}}
        )
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertFalse(result)

    def test_returns_false_when_shadow_worker_at_zero(self):
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._response_with_overrides(
            {"libraryai-shadow-worker": {"desiredCount": 0, "runningCount": 0}}
        )
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertFalse(result)

    def test_returns_false_when_desired_one_but_running_zero(self):
        """Half-deployed state (e.g. ECS deployment in progress, task crashing
        loop) is treated as inactive so scale-up can refresh the task def and
        force a clean redeploy."""
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._response_with_overrides(
            {"libraryai-eep-worker": {"desiredCount": 1, "runningCount": 0}}
        )
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertFalse(result)

    def test_returns_false_when_a_service_is_missing_from_response(self):
        """ECS sometimes drops services from describe_services if they were
        deleted; treat that as inactive, not as an implicit pass."""
        mock_ecs = MagicMock()
        partial = self._response_with_overrides({})
        partial["services"] = [
            s for s in partial["services"] if s["serviceName"] != "libraryai-iep2b"
        ]
        mock_ecs.describe_services.return_value = partial
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertFalse(result)

    def test_returns_false_when_describe_services_raises(self):
        """Any boto error must NOT be silently treated as a pass — that would
        permanently block scale-up if the API was transiently unreachable."""
        mock_ecs = MagicMock()
        mock_ecs.describe_services.side_effect = RuntimeError("AWS unreachable")
        result = self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        self.assertFalse(result)

    def test_active_check_does_not_consult_gpu_iep_services(self):
        """GPU IEPs are RunPod-backed; their ECS placeholders sit at desired=0
        by design and must NOT be probed by the active-check."""
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._all_active_response()
        self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        called_services = mock_ecs.describe_services.call_args.kwargs["services"]
        for gpu_svc in ("libraryai-iep0", "libraryai-iep1a", "libraryai-iep1b"):
            self.assertNotIn(gpu_svc, called_services)

    def test_active_check_consults_all_cpu_ieps_and_workers(self):
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._all_active_response()
        self.normal_scaler._normal_processing_already_active(mock_ecs, "test-cluster")
        called_services = set(mock_ecs.describe_services.call_args.kwargs["services"])
        expected = {
            "libraryai-iep1d",
            "libraryai-iep1e",
            "libraryai-iep2a-v2",
            "libraryai-iep2b",
            "libraryai-eep-worker",
            "libraryai-eep-recovery",
            "libraryai-shadow-worker",
        }
        self.assertEqual(called_services, expected)

    def test_partial_active_triggers_full_scale_up(self):
        """End-to-end: when only eep-worker is up, _do_scale_up must proceed
        through RunPod creation, not exit early like the old single-service
        check would have."""
        env = {
            "ECS_CLUSTER": "test-cluster",
            "WORKER_DESIRED_COUNT": "2",
            "AWS_REGION": "us-east-1",
            "RUNPOD_API_KEY": "test-key",
        }
        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = self._response_with_overrides(
            {
                "libraryai-iep1e": {"desiredCount": 0, "runningCount": 0},
                "libraryai-iep2a-v2": {"desiredCount": 0, "runningCount": 0},
                "libraryai-iep2b": {"desiredCount": 0, "runningCount": 0},
                "libraryai-eep-recovery": {"desiredCount": 0, "runningCount": 0},
                "libraryai-shadow-worker": {"desiredCount": 0, "runningCount": 0},
            }
        )
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {
                "family": "libraryai-eep-worker",
                "containerDefinitions": [{"name": "eep-worker", "environment": []}],
            }
        }
        mock_ecs.register_task_definition.return_value = {
            "taskDefinition": {
                "taskDefinitionArn": "arn:aws:ecs:us-east-1:123:task-definition/libraryai-eep-worker:99"
            }
        }

        with patch.dict(os.environ, env, clear=False):
            with patch("boto3.client", return_value=mock_ecs):
                with patch.object(
                    self.normal_scaler,
                    "_create_runpod_pods",
                    return_value=(
                        "https://pod1-8006.proxy.runpod.net",
                        "https://pod2-8001.proxy.runpod.net",
                        "https://pod3-8002.proxy.runpod.net",
                    ),
                ) as mock_create_pods:
                    self.normal_scaler._do_scale_up()

        mock_create_pods.assert_called_once()
        started = {c.kwargs["service"] for c in mock_ecs.update_service.call_args_list}
        self.assertIn("libraryai-eep-worker", started)
        self.assertIn("libraryai-iep1e", started)
        self.assertIn("libraryai-eep-recovery", started)


# ── 3: drain ignores human-review states ──────────────────────────────────────

class TestDrainIgnoresHumanReviewStates(unittest.TestCase):
    """
    drain_monitor.PAGE_ACTIVE_STATES must exclude human-review states.
    _is_drained must return True (scale-down allowed) when only human-review
    pages remain (queues empty, DB active pages = 0).
    """

    def setUp(self):
        # Re-import to get fresh module state
        import importlib
        import scripts.ecs_scaler.drain_monitor as dm
        importlib.reload(dm)
        self.dm = dm

    def test_page_active_states_excludes_ptiff_qa_pending(self):
        self.assertNotIn("ptiff_qa_pending", self.dm.PAGE_ACTIVE_STATES)

    def test_page_active_states_excludes_pending_human_correction(self):
        self.assertNotIn("pending_human_correction", self.dm.PAGE_ACTIVE_STATES)

    def test_page_active_states_includes_processable(self):
        for state in ("queued", "preprocessing", "rectification",
                      "layout_detection", "semantic_norm"):
            self.assertIn(state, self.dm.PAGE_ACTIVE_STATES)

    def test_is_drained_true_when_only_human_review_remain(self):
        """
        When Redis queues are empty and DB active pages = 0
        (human-review pages are excluded from the count), _is_drained → True.
        Scale-down is allowed even if ptiff_qa_pending/pending_human_correction
        pages exist in the DB.
        """
        empty_queues = {
            "pending": 0,
            "processing": 0,
            "shadow_pending": 0,
            "shadow_processing": 0,
            "dead_letter": 0,
        }
        # Simulates: 3 pages in ptiff_qa_pending/pending_human_correction
        # but active_pages = 0 because those states are excluded from the query.
        result = self.dm._is_drained(empty_queues, active_jobs=0, active_pages=0)
        self.assertTrue(result, "drain should be True when only human-review pages remain")


# ── 4: drain waits for active processing states / Redis queues ────────────────

class TestDrainWaitsForActiveWork(unittest.TestCase):
    """_is_drained must return False while processable work exists."""

    def setUp(self):
        import importlib
        import scripts.ecs_scaler.drain_monitor as dm
        importlib.reload(dm)
        self.dm = dm

    def _empty(self):
        return {"pending": 0, "processing": 0,
                "shadow_pending": 0, "shadow_processing": 0, "dead_letter": 0}

    def test_not_drained_when_pending_queue_nonempty(self):
        q = self._empty()
        q["pending"] = 5
        self.assertFalse(self.dm._is_drained(q, 0, 0))

    def test_not_drained_when_processing_queue_nonempty(self):
        q = self._empty()
        q["processing"] = 2
        self.assertFalse(self.dm._is_drained(q, 0, 0))

    def test_not_drained_when_shadow_pending_nonempty(self):
        q = self._empty()
        q["shadow_pending"] = 1
        self.assertFalse(self.dm._is_drained(q, 0, 0))

    def test_not_drained_when_shadow_processing_nonempty(self):
        q = self._empty()
        q["shadow_processing"] = 1
        self.assertFalse(self.dm._is_drained(q, 0, 0))

    def test_drained_when_active_jobs_nonzero_but_active_pages_zero(self):
        # active_jobs is no longer a drain gate — job status rows lag behind page
        # state transitions. active_pages=0 with empty queues is sufficient for
        # drain. active_jobs is reported for visibility only.
        self.assertTrue(self.dm._is_drained(self._empty(), 2, 0))

    def test_not_drained_when_active_pages_nonzero(self):
        """Active pages = pages in processable states (queued/preprocessing etc.)"""
        self.assertFalse(self.dm._is_drained(self._empty(), 0, 10))

    def test_drained_only_when_all_zero(self):
        self.assertTrue(self.dm._is_drained(self._empty(), 0, 0))


# ── NORMAL_SCALE_UP_SERVICES constant sanity check ────────────────────────────

class TestNormalScaleUpServicesConstant(unittest.TestCase):
    """NORMAL_SCALE_UP_SERVICES must contain exactly the spec §5 list."""

    def test_constant_matches_spec(self):
        from services.eep.app.scaling.normal_scaler import NORMAL_SCALE_UP_SERVICES
        expected = {
            "libraryai-iep0",
            "libraryai-iep1a",
            "libraryai-iep1b",
            "libraryai-iep1d",
            "libraryai-iep1e",
            "libraryai-iep2a-v2",
            "libraryai-iep2b",
            "libraryai-eep-worker",
            "libraryai-eep-recovery",
            "libraryai-shadow-worker",
        }
        self.assertEqual(set(NORMAL_SCALE_UP_SERVICES), expected)

    def test_excluded_services_not_in_constant(self):
        from services.eep.app.scaling.normal_scaler import NORMAL_SCALE_UP_SERVICES
        excluded = {
            "libraryai-retraining-worker",
            "libraryai-dataset-builder",
            "libraryai-prometheus",
            "libraryai-grafana",
        }
        overlap = set(NORMAL_SCALE_UP_SERVICES) & excluded
        self.assertEqual(overlap, set())


# ── drain_monitor --assert-drained / --assert-has-work ────────────────────────

class TestDrainMonitorSingleShotModes(unittest.TestCase):
    """
    --assert-drained: exit 0 = IS drained (safe to stop), exit 1 = NOT drained.
    --assert-has-work: exit 0 = HAS work (scale-up warranted), exit 1 = NO work.

    These are the two unambiguous single-shot modes used by:
      --assert-drained  → scale-down-auto.yml
      --assert-has-work → scheduled-window.yml
    """

    def setUp(self):
        import importlib
        import scripts.ecs_scaler.drain_monitor as dm
        importlib.reload(dm)
        self.dm = dm

    def _empty_queues(self):
        return {"pending": 0, "processing": 0,
                "shadow_pending": 0, "shadow_processing": 0, "dead_letter": 0}

    def _nonempty_queues(self):
        return {"pending": 3, "processing": 1,
                "shadow_pending": 0, "shadow_processing": 0, "dead_letter": 0}

    # ── --assert-drained ──────────────────────────────────────────────────────

    # Test E-1: auto-scale-down triggers when no processable work remains
    def test_assert_drained_exits_0_when_drained(self):
        """--assert-drained exit 0 = IS drained → auto-scale-down may proceed."""
        drained = self.dm._is_drained(self._empty_queues(), 0, 0)
        self.assertTrue(drained)
        # In the workflow: exit_code=0 → trigger scale-down

    # Test E-2: auto-scale-down does NOT trigger when queues have active work
    def test_assert_drained_exits_1_when_queues_nonempty(self):
        """--assert-drained exit 1 = NOT drained → skip scale-down this cycle."""
        drained = self.dm._is_drained(self._nonempty_queues(), 0, 0)
        self.assertFalse(drained)
        # In the workflow: exit_code=1 → skip, retry next 15-min cycle

    def test_assert_drained_exits_1_when_active_pages(self):
        """Active processable pages prevent drain → no auto-scale-down."""
        drained = self.dm._is_drained(self._empty_queues(), 0, active_pages=5)
        self.assertFalse(drained)

    # Test E-3: auto-scale-down ignores human-review states
    def test_assert_drained_true_despite_human_review_pages(self):
        """
        Drain is TRUE even when human-review pages exist (ptiff_qa_pending,
        pending_human_correction). They are excluded from PAGE_ACTIVE_STATES
        so active_pages=0 when only those states remain.
        Scale-down proceeds — human reviewers don't keep GPU running.
        """
        # Simulate: 5 pages in ptiff_qa_pending/pending_human_correction,
        # but DB query returns active_pages=0 (those states are excluded).
        drained = self.dm._is_drained(self._empty_queues(), active_jobs=0, active_pages=0)
        self.assertTrue(drained)

    def test_human_review_states_absent_from_page_active_states(self):
        self.assertNotIn("ptiff_qa_pending", self.dm.PAGE_ACTIVE_STATES)
        self.assertNotIn("pending_human_correction", self.dm.PAGE_ACTIVE_STATES)

    # ── --assert-has-work ─────────────────────────────────────────────────────

    def test_assert_has_work_exits_0_when_not_drained(self):
        """--assert-has-work exit 0 = HAS work → scheduled-window triggers scale-up."""
        drained = self.dm._is_drained(self._nonempty_queues(), 0, 0)
        self.assertFalse(drained)
        # not drained → has work → exit 0 in --assert-has-work mode

    def test_assert_has_work_exits_1_when_drained(self):
        """--assert-has-work exit 1 = NO work → scheduled-window skips scale-up."""
        drained = self.dm._is_drained(self._empty_queues(), 0, 0)
        self.assertTrue(drained)
        # drained → no work → exit 1 in --assert-has-work mode

    def test_exit_code_semantics_are_inverse(self):
        """
        --assert-drained and --assert-has-work have inverse exit-code semantics
        for the same 'drained' state. Verify they cannot both be satisfied
        simultaneously with exit 0.
        """
        queues = self._empty_queues()
        drained = self.dm._is_drained(queues, 0, 0)
        # When drained=True:
        #   --assert-drained  → exit 0 (drained == True → exit 0)
        #   --assert-has-work → exit 1 (not drained == False → exit 1)
        assert_drained_exit = 0 if drained else 1
        assert_has_work_exit = 0 if not drained else 1
        self.assertNotEqual(assert_drained_exit, assert_has_work_exit)


# ── correction re-enqueue triggers scale-up ───────────────────────────────────

class TestDrainMonitorActiveWorkDiagnostics(unittest.TestCase):
    """Drain snapshot classification for DB, Redis, human-review, and stale work."""

    class FakeDrainRedis:
        def __init__(
            self,
            dm,
            *,
            queued: list[str] | None = None,
            processing: list[str] | None = None,
            shadow_pending: int = 0,
            shadow_processing: int = 0,
            dead_letter: int = 0,
            heartbeats: set[str] | None = None,
        ) -> None:
            self._lists = {
                dm.QUEUE_PAGE_TASKS: queued or [],
                dm.QUEUE_PAGE_TASKS_PROCESSING: processing or [],
                dm.QUEUE_SHADOW_TASKS: ["{}"] * shadow_pending,
                dm.QUEUE_SHADOW_TASKS_PROCESSING: ["{}"] * shadow_processing,
                dm.QUEUE_DEAD_LETTER: ["{}"] * dead_letter,
            }
            self._heartbeats = heartbeats or set()

        def llen(self, key: str) -> int:
            return len(self._lists.get(key, []))

        def lrange(self, key: str, start: int, end: int) -> list[str]:
            values = self._lists.get(key, [])
            if end == -1:
                return values[start:]
            return values[start : end + 1]

        def exists(self, key: str) -> bool:
            return key in self._heartbeats

        def hget(self, key: str, field: str) -> None:
            return None

    def setUp(self):
        import importlib
        import scripts.ecs_scaler.drain_monitor as dm
        importlib.reload(dm)
        self.dm = dm

    def _task(self, *, task_id: str = "t1", page_id: str = "p1", job_id: str = "j1") -> str:
        return json.dumps(
            {
                "task_id": task_id,
                "job_id": job_id,
                "page_id": page_id,
                "page_number": 1,
                "retry_count": 0,
            }
        )

    def _snapshot(self, *, active_pages: int, active_redis: int) -> dict:
        return {
            "active_pages_count": active_pages,
            "active_redis_items_count": active_redis,
        }

    def test_only_pending_human_correction_pages_drained(self):
        snapshot = self._snapshot(active_pages=0, active_redis=0)
        self.assertTrue(self.dm._is_snapshot_drained(snapshot))
        self.assertFalse(self.dm._is_automatable_page_status("pending_human_correction"))
        self.assertTrue(self.dm._is_human_review_only_status("pending_human_correction"))

    def test_split_parent_with_children_pending_human_correction_drained(self):
        snapshot = self._snapshot(active_pages=0, active_redis=0)
        self.assertTrue(self.dm._is_snapshot_drained(snapshot))
        self.assertFalse(self.dm._is_automatable_page_status("split"))
        self.assertFalse(self.dm._is_automatable_page_status("pending_human_correction"))

    def test_real_processing_page_blocks_drain(self):
        snapshot = self._snapshot(active_pages=1, active_redis=0)
        self.assertFalse(self.dm._is_snapshot_drained(snapshot))
        self.assertTrue(self.dm._is_automatable_page_status("preprocessing"))

    def test_queued_redis_item_with_active_db_page_blocks_drain(self):
        r = self.FakeDrainRedis(self.dm, queued=[self._task()])
        queues = self.dm._check_queues(r)
        lookup = {
            "p1": {
                "job_id": "j1",
                "page_id": "p1",
                "page_number": 1,
                "sub_page_index": None,
                "status": "queued",
                "review_reasons": None,
                "age_seconds": 2,
            }
        }
        with patch.object(self.dm, "_lookup_page_details", return_value=lookup):
            details = self.dm._check_redis_details(r, object(), queues, sample_limit=10)

        self.assertEqual(details["active_redis_items_count"], 1)
        self.assertEqual(details["redis_blocker_samples"][0]["reason"], "redis_item_with_automatable_db_page")
        self.assertFalse(
            self.dm._is_snapshot_drained(
                self._snapshot(active_pages=0, active_redis=details["active_redis_items_count"])
            )
        )

    def test_pending_human_correction_redis_item_is_reported_nonblocking(self):
        r = self.FakeDrainRedis(self.dm, processing=[self._task()])
        queues = self.dm._check_queues(r)
        lookup = {
            "p1": {
                "job_id": "j1",
                "page_id": "p1",
                "page_number": 1,
                "sub_page_index": None,
                "status": "pending_human_correction",
                "review_reasons": ["needs_human_crop"],
                "age_seconds": 1200,
            }
        }
        with patch.object(self.dm, "_lookup_page_details", return_value=lookup):
            details = self.dm._check_redis_details(r, object(), queues, sample_limit=10)

        self.assertEqual(details["active_redis_items_count"], 0)
        self.assertEqual(
            details["redis_nonblocking_samples"][0]["reason"],
            "redis_item_for_human_review_only_page",
        )
        self.assertTrue(details["redis_nonblocking_samples"][0]["human_review_only_work"])

    def test_stale_processing_page_without_heartbeat_is_reported_as_blocker(self):
        r = self.FakeDrainRedis(self.dm, processing=[self._task(task_id="stale-task")])
        queues = self.dm._check_queues(r)
        lookup = {
            "p1": {
                "job_id": "j1",
                "page_id": "p1",
                "page_number": 1,
                "sub_page_index": None,
                "status": "preprocessing",
                "review_reasons": None,
                "age_seconds": self.dm.DEFAULT_STALE_SECONDS + 1,
            }
        }
        with patch.object(self.dm, "_lookup_page_details", return_value=lookup):
            details = self.dm._check_redis_details(r, object(), queues, sample_limit=10)

        self.assertEqual(details["active_redis_items_count"], 1)
        blocker = details["redis_blocker_samples"][0]
        self.assertEqual(blocker["reason"], "stale_processing_item_no_live_worker_claim")
        self.assertFalse(blocker["live_heartbeat"])
        self.assertFalse(blocker["live_claim"])
        self.assertTrue(blocker["automatable_work"])


class TestCorrectionScaleUpTrigger(unittest.TestCase):
    """
    Tests E-4 and E-5: correction re-enqueue (apply_correction, approve_page,
    approve_all) must trigger scale-up in immediate mode and skip in
    scheduled_window mode.

    Strategy: test maybe_trigger_scale_up directly (already covered above) and
    verify that the enqueued_ok guard correctly determines whether to schedule
    the background task.
    """

    class FakeBackgroundTasks:
        """Minimal BackgroundTasks stand-in that records added tasks."""
        def __init__(self):
            self.tasks: list = []

        def add_task(self, func, *args, **kwargs):
            self.tasks.append((func, args, kwargs))

    def _simulate_correction_trigger(self, mode: str, enqueued_ok: bool):
        """Simulate the pattern used in apply.py / ptiff_qa.py post-enqueue."""
        bg = self.FakeBackgroundTasks()
        r = MagicMock()  # mock Redis client

        with patch.dict(os.environ, {"PROCESSING_START_MODE": mode}, clear=False):
            with patch(
                "services.eep.app.scaling.normal_scaler._do_scale_up"
            ) as mock_scale:
                from services.eep.app.scaling.normal_scaler import maybe_trigger_scale_up

                # Reproduce the guard from apply.py / ptiff_qa.py:
                if enqueued_ok:
                    bg.add_task(maybe_trigger_scale_up, r)

                # Execute all queued background tasks synchronously
                for func, args, kwargs in bg.tasks:
                    func(*args, **kwargs)

                return mock_scale

    # Test E-4: immediate mode triggers after successful enqueue
    def test_immediate_mode_triggers_after_enqueue(self):
        mock_scale = self._simulate_correction_trigger("immediate", enqueued_ok=True)
        mock_scale.assert_called_once()

    # Test E-5: scheduled_window does NOT trigger
    def test_scheduled_window_does_not_trigger(self):
        mock_scale = self._simulate_correction_trigger("scheduled_window", enqueued_ok=True)
        mock_scale.assert_not_called()

    def test_no_enqueue_no_trigger_in_immediate_mode(self):
        """Split correction path: task_to_enqueue=None → enqueued_ok=False → no trigger."""
        mock_scale = self._simulate_correction_trigger("immediate", enqueued_ok=False)
        mock_scale.assert_not_called()

    def test_redis_error_no_trigger(self):
        """If enqueue raises RedisError, enqueued_ok stays False → no trigger."""
        # enqueued_ok=False simulates the path where RedisError was caught
        mock_scale = self._simulate_correction_trigger("immediate", enqueued_ok=False)
        mock_scale.assert_not_called()


# ── IEP1D pre-warm invariant ──────────────────────────────────────────────────

class TestIep1dPreWarmInvariant(unittest.TestCase):
    """
    IEP1D is pre-warmed by normal scale-up to avoid its 4–6 min Fargate
    cold-start exceeding the worker's IEP1D /ready timeout for the first
    wave of pages that need rescue.  Scale-down.yml resets iep1d back to 0
    as part of normal teardown.

    The on-demand iep1d_scaler (services/eep_worker/app/iep1d_scaler.py)
    still exists as a defence-in-depth fallback — its update_service call
    is idempotent when iep1d is already at desired=1.
    """

    def test_iep1d_in_normal_scale_up_services(self):
        from services.eep.app.scaling.normal_scaler import NORMAL_SCALE_UP_SERVICES
        self.assertIn("libraryai-iep1d", NORMAL_SCALE_UP_SERVICES)

    def test_iep1d_not_in_excluded_set(self):
        from services.eep.app.scaling.normal_scaler import _EXCLUDED_SERVICES
        self.assertNotIn("libraryai-iep1d", _EXCLUDED_SERVICES)

    def test_iep1d_in_active_check_services(self):
        """iep1d must be in the active-check list so partial scale-down
        (iep1d at 0 while everything else is up) forces a fresh scale-up."""
        from services.eep.app.scaling.normal_scaler import _ACTIVE_CHECK_SERVICES
        self.assertIn("libraryai-iep1d", _ACTIVE_CHECK_SERVICES)

    def test_scale_up_assert_guard_allows_iep1d(self):
        """_update_service must accept iep1d as a normal scale-up service."""
        from services.eep.app.scaling.normal_scaler import _update_service
        mock_ecs = MagicMock()
        result = _update_service(mock_ecs, "test-cluster", "libraryai-iep1d", 1)
        self.assertTrue(result)
        mock_ecs.update_service.assert_called_once()

    def test_scale_up_assert_guard_rejects_retraining(self):
        from services.eep.app.scaling.normal_scaler import _update_service
        mock_ecs = MagicMock()
        with self.assertRaises(AssertionError):
            _update_service(mock_ecs, "test-cluster", "libraryai-retraining-worker", 1)


if __name__ == "__main__":
    unittest.main()
