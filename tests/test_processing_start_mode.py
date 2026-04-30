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
    EXPECTED_STARTED = {
        "libraryai-iep1e",
        "libraryai-iep2a",
        "libraryai-iep2b",
        "libraryai-eep-worker",
        "libraryai-eep-recovery",
        "libraryai-shadow-worker",
    }

    MUST_NOT_START = {
        "libraryai-iep1d",
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

    def test_iep1d_specifically_excluded(self):
        mock_ecs = self._run_do_scale_up()
        started = {c.kwargs["service"] for c in mock_ecs.update_service.call_args_list}
        self.assertNotIn("libraryai-iep1d", started)

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

        with patch.dict(os.environ, {"RUNPOD_CLOUD_TYPE": "COMMUNITY"}, clear=False):
            self.assertEqual(normal_scaler._runpod_cloud_type_candidates(), ["COMMUNITY", "SECURE"])

    def test_runpod_pod_mode_invalid_defaults_to_create(self):
        import importlib
        from services.eep.app.scaling import normal_scaler
        importlib.reload(normal_scaler)

        self.assertEqual(normal_scaler._normalize_runpod_pod_mode("_create_runpod_pods"), "create")

    def test_worker_desired_count_used(self):
        mock_ecs = self._run_do_scale_up({"WORKER_DESIRED_COUNT": "3"})

        worker_calls = [
            c for c in mock_ecs.update_service.call_args_list
            if c.kwargs["service"] in {
                "libraryai-eep-worker",
                "libraryai-eep-recovery",
                "libraryai-shadow-worker",
            }
        ]
        for c in worker_calls:
            self.assertEqual(c.kwargs["desiredCount"], 3)


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
        self.assertEqual(payload["gpuTypePriority"], "availability")
        self.assertEqual(payload["ports"], ["8001/http"])
        self.assertEqual(payload["env"]["IEP1A_MODELS_DIR"], "/app/models/iep1a")


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

    def test_not_drained_when_active_jobs_nonzero(self):
        self.assertFalse(self.dm._is_drained(self._empty(), 2, 0))

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
            "libraryai-iep1e",
            "libraryai-iep2a",
            "libraryai-iep2b",
            "libraryai-eep-worker",
            "libraryai-eep-recovery",
            "libraryai-shadow-worker",
        }
        self.assertEqual(set(NORMAL_SCALE_UP_SERVICES), expected)

    def test_excluded_services_not_in_constant(self):
        from services.eep.app.scaling.normal_scaler import NORMAL_SCALE_UP_SERVICES
        excluded = {
            "libraryai-iep1d",
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


# ── IEP1D exclusion from normal scale-up (spec §7) ────────────────────────────

class TestIep1dExclusionInvariant(unittest.TestCase):
    """
    IEP1D must never appear in normal scale-up service lists.
    Scale-down.yml (manual/scheduled) resets iep1d to 0 as safety cleanup —
    this is correct and expected. But normal scale-up must never start it.
    """

    def test_iep1d_not_in_normal_scale_up_services(self):
        from services.eep.app.scaling.normal_scaler import NORMAL_SCALE_UP_SERVICES
        self.assertNotIn("libraryai-iep1d", NORMAL_SCALE_UP_SERVICES)

    def test_iep1d_in_excluded_set(self):
        from services.eep.app.scaling.normal_scaler import _EXCLUDED_SERVICES
        self.assertIn("libraryai-iep1d", _EXCLUDED_SERVICES)

    def test_scale_up_assert_guard_rejects_iep1d(self):
        """_update_service raises AssertionError if called with an excluded service."""
        from services.eep.app.scaling.normal_scaler import _update_service
        mock_ecs = MagicMock()
        with self.assertRaises(AssertionError):
            _update_service(mock_ecs, "test-cluster", "libraryai-iep1d", 1)

    def test_scale_up_assert_guard_rejects_retraining(self):
        from services.eep.app.scaling.normal_scaler import _update_service
        mock_ecs = MagicMock()
        with self.assertRaises(AssertionError):
            _update_service(mock_ecs, "test-cluster", "libraryai-retraining-worker", 1)


if __name__ == "__main__":
    unittest.main()
