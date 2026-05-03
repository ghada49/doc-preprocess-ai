# MLOps and observability


## MLOps lifecycle summary

The repo implements a **feedback-oriented** lifecycle with **manual gates** for promotion and **stub defaults** for training/evaluation unless env vars enable live runs.

| Lifecycle stage | Purpose | Evidence | Status |
|-----------------|---------|----------|--------|
| Accepted correction collection | Trusted rows for export (**`human_corrected`**, **`acceptance_decision = 'accepted'`**) | `services/dataset_builder/app/main.py` (`_fetch_corrected_rows`) | **Implemented** |
| Dataset builder | Export training-ready images + label lines + **`data.yaml`** / manifest | `services/dataset_builder/app/main.py`; `tests/test_dataset_builder.py` | **Implemented** |
| Candidate training | Stub subprocess **`training/scripts/train_iep*.py`** when **`LIBRARYAI_RETRAINING_TRAIN=live`** | `services/retraining_worker/app/live_train.py`; `services/retraining_worker/app/task.py` | **Partial** (stub default, live optional) |
| Experiment tracking | MLflow server + **`mlflow`** dependency; training stdout **`LIBRARYAI_MLFLOW_RUN_ID`** parsed in live mode | `docker-compose.yml` (**mlflow**); `pyproject.toml` (**mlflow**); `services/retraining_worker/app/live_train.py`; `k8s/ecs/mlflow-task-def.json` | **Configured / partial** (promotion→MLflow stage transition **implemented** in **`promotion_api._mlflow_transition()`** with graceful skip when MLflow unavailable) |
| Evaluation / thresholds | **`gate_results`** JSON on **`model_versions`**; **`_check_gates`** on promote; golden script + **`LIBRARYAI_RETRAINING_GOLDEN_EVAL=live`** | `services/eep/app/promotion_api.py`; `services/retraining_worker/app/task.py`; `training/scripts/evaluate_golden_dataset.py`; `tests/test_p9_golden_dataset.py` | **Partial** (stub eval default; live optional) |
| Manual promotion / rollback | **`POST /v1/models/promote`**, **`POST /v1/models/rollback`**; **`model_promotion_audit`** | `services/eep/app/promotion_api.py`; `services/eep/app/db/models.py` | **Implemented** (not automatic) |
| Model reload / version reporting | Redis **`libraryai:model_reload:{service}`** on promote (`promotion_api.py`); **`GET /model-info`** on **IEP1a/IEP1b** only | `services/eep/app/promotion_api.py`; `services/iep1a/app/inference.py`; `services/iep1b/app/inference.py`; `tests/test_model_info.py` | **Partial** (TODO in **`get_model_info`** for DB **`version_tag`**) |
| Observability | Structured logs; Prometheus **`/metrics`**; Compose Prometheus/Grafana/Alertmanager; ECS **awslogs**; optional ECS observability workflows | `shared/logging_config.py`; `shared/metrics.py`; `monitoring/prometheus/prometheus.yml`; `docker-compose.yml`; `k8s/ecs/eep-task-def.json`; `.github/workflows/observability-up.yml` | **Implemented locally / optional in cloud** |

---

## Experiment tracking

| Feature | Evidence | Status | Notes |
|---------|----------|--------|-------|
| MLflow server (local) | `docker-compose.yml` service **`mlflow`**; **`services/mlflow/Dockerfile`** | **Configured** | Backend URI via **`MLFLOW_BACKEND_STORE_URI`** secret on ECS (`k8s/ecs/mlflow-task-def.json`) |
| MLflow ECS task | `k8s/ecs/mlflow-task-def.json`; **`deploy.yml`** registers image | **Configured** | |
| Metrics logging (training) | **`parse_train_script_stdout`** reads **`LIBRARYAI_MLFLOW_RUN_ID=`** | **Partial** | Only when **`live_train`** runs |
| Parameter logging | Training scripts emit stdout markers — not audited line-by-line here | **Partial** | See **`training/scripts/`** |
| Artifact logging | **`MLFLOW_ARTIFACT_ROOT`** `s3://libraryai2/mlflow-artifacts` in **`mlflow-task-def.json`** | **Configured** | |
| Candidate **`model_versions`** rows | **`execute_retraining_task`** creates staging rows with **`gate_results`** | **Implemented** | Stub gates possible (`services/retraining_worker/app/task.py`) |
| Threshold checks | **`promotion_api._check_gates`** | **Implemented** | **`force=true`** bypass documented |

---

## Model evaluation and promotion

| Capability | Purpose | Evidence | Status |
|------------|---------|----------|--------|
| Golden evaluation script | Offline evaluation CLI for IEP0/IEP1A/IEP1B | `training/scripts/evaluate_golden_dataset.py`; `tests/test_p9_golden_dataset.py` | **Implemented** (CLI + tests) |
| Candidate vs production comparison | **`gate_results`** structure includes geometry IoU, split precision, etc.; admin UI describes offline shadow vs prod comparison | `services/retraining_worker/app/task.py` (gate shape); `frontend/src/app/admin/model-lifecycle/page.tsx` | **Partial** (UI + stored metrics; live shadow inference not claimed for dashboards) |
| Manual promotion | Admin **`POST /v1/models/promote`** (**iep1a** / **iep1b** only); gates unless **`force`** | `services/eep/app/promotion_api.py`; `tests/test_p8_promotion_api.py` | **Implemented** |
| Rollback | **`POST /v1/models/rollback`** | `services/eep/app/promotion_api.py` | **Implemented** |
| Promotion audit | **`model_promotion_audit`** table | `services/eep/app/db/models.py`; `promotion_api.py` | **Implemented** |
| Trigger offline evaluation | **`GET /v1/models/evaluation`**, **`POST /v1/models/evaluate`** | `services/eep/app/models_api.py` | **Implemented** (dedicated API tests **not found** under **`tests/`** name match) |
| Shadow worker | Finalizes **`ShadowEvaluation`** rows from Redis shadow queues | `services/shadow_worker/app/main.py`; `tests/test_p9_shadow_worker.py` | **Implemented** |

**Automatic promotion:** **Not implemented** — promotion requires admin API call and passes **`_check_gates`** unless **`force=true`**.

---

## Model reload and version reporting

**`GET /model-info`** exists on **IEP1a** and **IEP1b** only (`services/iep1a/app/main.py`, `services/iep1b/app/main.py`). **`get_model_info()`** returns **`service`**, **`mock_mode`**, **`models_dir`**, **`loaded_models`** (per-material files), **`reload_count`**, **`last_reload_at`**, **`reloaded_since_startup`**, **`version_tag`** (often **`None`** until Redis reload message carries tag — see TODO comments in **`services/iep1a/app/inference.py`** and **`services/iep1b/app/inference.py`**).

| Service | Endpoint / mechanism | Fields reported | Evidence | Status |
|---------|---------------------|-----------------|----------|--------|
| **iep1a** | **`GET /model-info`** | **`service`**, **`mock_mode`**, **`models_dir`**, **`loaded_models`**, **`reload_count`**, **`last_reload_at`**, **`reloaded_since_startup`**, **`version_tag`** | `services/iep1a/app/inference.py` (`get_model_info`) | **Implemented** |
| **iep1b** | **`GET /model-info`** | Same shape | `services/iep1b/app/inference.py` | **Implemented** |
| **Other IEPs** | — | No **`/model-info`** route found in quick audit | — | **Not found** (use **`/health`**, **`/ready`**, **`/metrics`** via **`configure_observability`**) |
| **EEP** | **`GET /v1/status`** | **`status`**, **`service`** (not model weights) | `services/eep/app/main.py` | **Implemented** |

---

## Observability stack

| Tool / signal | Purpose | Evidence | Status |
|---------------|---------|----------|--------|
| **CloudWatch Logs** | ECS task stdout → **`awslogs`** groups e.g. **`/ecs/libraryai-eep`** | `k8s/ecs/eep-task-def.json` **`logConfiguration`** | **Configured** (templates + deploy) |
| **Prometheus** | Scrapes service **`/metrics`** on Compose network ports | `monitoring/prometheus/prometheus.yml`; `docker-compose.yml` | **Implemented** (local stack) |
| **Grafana** | Dashboards from **`monitoring/grafana/`** | `docker-compose.yml`; provisioning under **`monitoring/grafana/provisioning`** | **Implemented** (local) |
| **Alertmanager** | Routes alerts | `monitoring/alertmanager/alertmanager.yml`; `docker-compose.yml` | **Implemented** (local) |
| **`prometheus.ecs.yml`** | ECS Service Connect scrape targets | `monitoring/prometheus/prometheus.ecs.yml` | **Configured** (ECS-oriented file) |
| **Service `/metrics`** | Prometheus text via **`shared.metrics`** | `shared/middleware.py`; `tests/test_p9_prometheus_metrics.py` | **Implemented** |
| **Health / readiness** | **`/health`**, **`/ready`** | `shared/middleware.py` | **Implemented** |
| **Observability ECS workflows** | Build/start/stop Prometheus & Grafana on ECS | `.github/workflows/observability-up.yml`; `.github/workflows/observability-down.yml`; `k8s/ecs/prometheus-task-def.json`; `k8s/ecs/grafana-task-def.json` | **Optional** (manual **`workflow_dispatch`**) |

---

## ML-specific signals

Signals below appear in **schemas, DB JSONB, logs, or metrics** — not all are scraped into Grafana by default.

| Signal | Meaning | Where captured | Evidence | Status |
|--------|---------|----------------|----------|--------|
| Gate results / thresholds | Preprocessing decisions | **`page_lineage.gate_results`**, **`quality_gate_log`** | `services/eep/app/db/models.py` | **Implemented** |
| **`acceptance_decision`** | Page-level acceptance | **`job_pages`**, **`page_lineage`** | `services/eep/app/db/models.py` | **Implemented** |
| **`review_reasons`** | Why human review | **`job_pages.review_reasons`** | `models.py`; correction modules | **Implemented** |
| **`human_corrected`** | Correction applied | **`page_lineage`** | `models.py` | **Implemented** |
| Model **version_tag** / **stage** | Registry | **`model_versions`** | `models.py` | **Implemented** |
| **`geometry_iou`** etc. | Offline evaluation gates | **`model_versions.gate_results`** | `services/retraining_worker/app/task.py`; `promotion_api.py` | **Implemented** |
| Routing path | Pipeline branch | **`page_lineage.routing_path`**, **`job_pages.routing_path`** | `models.py` | **Implemented** |
| Latency / timing | Duration | **`service_invocations.processing_time_ms`**, **`total_processing_ms`** | `models.py` | **Implemented** |
| Retry / dead letter | Queue depth | Redis **`libraryai:page_tasks:dead_letter`** | `shared/schemas/queue.py`; `services/eep/app/admin/*.py` (queue status) | **Implemented** |
| Prometheus counters | Ops metrics | **`shared/metrics.py`** | `tests/test_p9_prometheus_metrics.py` | **Implemented** |

---

## Operational lifecycle automation

Scaling is **workflow-driven** and application-triggered (**`normal_scaler`**), not Kubernetes **KEDA**/HPA manifests (none found).

| Workflow / script | Purpose | Trigger | Evidence | Status |
|-------------------|---------|---------|----------|--------|
| **`ci.yml`** | Unit/integration tests + migration tests | Push / PR / **`workflow_call`** | `.github/workflows/ci.yml` | **Implemented** |
| **`deploy.yml`** | Build ECR, migrate, deploy ECS, bootstrap admin, **e2e** curls | Push **`main`** / **`workflow_dispatch`** | `.github/workflows/deploy.yml` | **Implemented** |
| **`scale-up.yml`** | Start processing window | Cron **`0 22 * * *`** / dispatch | `.github/workflows/scale-up.yml` | **Implemented** |
| **`scale-down.yml`** | Drain + scale down | Cron **`0 8 * * *`** / dispatch | `.github/workflows/scale-down.yml` | **Implemented** |
| **`scale-down-auto.yml`** | Idle drain + dispatch scale-down | Cron **`*/15 * * * *`** | `.github/workflows/scale-down-auto.yml` | **Implemented** |
| **`scheduled-window.yml`** | Scheduled processing mode gate | Cron / dispatch | `.github/workflows/scheduled-window.yml` | **Implemented** |
| **`observability-up.yml`** / **`observability-down.yml`** | ECS Prometheus/Grafana | **`workflow_dispatch`** | `.github/workflows/observability-up.yml` | **Optional** |
| **Drain monitor** | Assert drained before scale-down | ECS task **`drain_monitor.py`** | `k8s/ecs/drain-monitor-task-def.json`; scale workflows | **Configured** |

**Retraining-specific GitHub workflow:** **Not found** as a dedicated workflow — retraining runs via **ECS `retraining-worker`**, **`POST /v1/retraining/trigger`**, and DB triggers (`services/eep/app/retraining_api.py`).

---

## Failure visibility and debugging

| Failure / risk | Visibility mechanism | Evidence | Status |
|----------------|---------------------|----------|--------|
| Structured application logs | JSON lines to stdout | `shared/logging_config.py` | **Implemented** |
| Task retries exhausted | Dead-letter list length | `shared/schemas/queue.py`; admin queue endpoints | **Implemented** |
| Page / job failure states | **`job_pages.status`**, **`jobs.status`** | `services/eep/app/db/models.py` | **Implemented** |
| Recovery reconciliation | **`eep-recovery`** worker | `services/eep_recovery/`; `docker-compose.yml` | **Implemented** |
| ECS container health | **`healthCheck`** in task defs (e.g. curl **`/health`**) | `k8s/ecs/eep-task-def.json` | **Configured** |
| Deploy smoke | **`e2e-tests`** job curls **`/health`**, **`/v1/status`**, auth, optional job create | `.github/workflows/deploy.yml` | **Implemented** |

---

## What is not implemented or not claimed

| Not claimed | Reason / evidence |
|-------------|-------------------|
| Automatic production promotion | **`promotion_api`** requires gates unless **`force`** |
| Always-on ECS Grafana/Prometheus | **`observability-up.yml`** is manual; **`normal_scaler`** excludes prometheus/grafana services |
| Kubernetes **KEDA** / **HPA** queue autoscaling | No such manifests; ECS + GitHub Actions instead |
| **`GET /model-info`** on every service | Only **iep1a** / **iep1b** (`grep` / `main.py` routes) |
| Promotion-time MLflow registry transition | **`promotion_api._mlflow_transition()`** executes when configured; **`ModelVersionRecord.mlflow_transition_result`** records **`executed`** / **`skipped_*`** — not a stub |
| Golden evaluation in CI by default | **`evaluate_golden_dataset.py`** exists; CI does not invoke it (`ci.yml`) |

---

## Rubric alignment

This document supports:

- **M1 Automated lifecycle pipeline** — deploy/CI workflows, retraining worker poll, dataset builder
- **M2 Experiment tracking and thresholds** — MLflow wiring, **`gate_results`**, **`_check_gates`**
- **M3 Monitoring and ML-specific signals** — Prometheus stack, **`/metrics`**, DB + lineage fields
- **M4 Documentation completeness** — evidence paths throughout
- **D3 Evidence shown** — references **Compose**, **ECS JSON**, **workflows**, **source files**

For deployment specifics, see **`docs/05_DEPLOYMENT.md`** and **`docs/06_CLOUD_INFRASTRUCTURE.md`**.
