# AI Pipeline


## Pipeline Summary

End-to-end behavior is driven by **job** → **per-page Redis tasks** → **`eep_worker`** (`services/eep_worker/app/worker_loop.py`), with **PostgreSQL** as authoritative page state and **S3-compatible storage** for artifacts (`shared/io/storage.py`). Inference is split across **HTTP microservices** (IEP0–IEP2, IEP1E) plus **library-local deterministic normalization** (**IEP1C**, `shared/normalization/normalize.py`).

| # | Step | Status | Notes |
|---|------|--------|-------|
| 1 | Page preprocessing | **Implemented** | Downsample, optional **IEP0** material classification (`worker_loop`), parallel **IEP1A/IEP1B** geometry (`geometry_invocation`), **IEP1C** warp/deskew/crop (`normalization_step`), artifact validation gates (`artifact_validation`, `normalization_step`). |
| 2 | Crop / deskew / split detection | **Implemented** | Geometry comes from **IEP1A** (segmentation-style) and **IEP1B** (pose/keypoints); selection and split routing via `services/eep/app/gates/geometry_selection.py` and `split_step.py`. |
| 3 | Rectification rescue path | **Implemented** | **IEP1D** (UVDoc) and multi-pass flows in `rescue_step.py`; policy toggle `rectification_policy` via `services/eep/app/policy_loader.py` → `PreprocessingGateConfig`. |
| 4 | Layout detection | **Implemented** | **IEP2A** and **IEP2B** HTTP inference (`worker_loop` `_run_layout`), consensus + adjudication in `services/eep/app/gates/layout_gate.py`; **`layout_with_ocr`** mode skips local detectors and uses **Google Document AI** (`worker_loop` ~3241–3245; `document_ai.py`). |
| 5 | Acceptance gates | **Implemented** | Geometry selection, artifact validation, layout consensus/adjudication, PTIFF QA routes (`services/eep/app/correction/ptiff_qa.py`); outcomes recorded on `job_pages` (`acceptance_decision`, `review_reasons`, `quality_summary`). |
| 6 | Human correction | **Implemented** | Routers under `services/eep/app/correction/` (apply, queue, reject, send-to-review) and workspace/QA UI contracts (`workspace_schema.py`); corrections persist into lineage/correction structures consumed downstream. |
| 7 | Retraining data export | **Implemented** | **`dataset_builder`** (`services/dataset_builder/app/main.py`) builds corrected-export datasets and registry entries when run (Compose profile `dataset-build` in `docker-compose.yml`). |

## Pipeline Stages

| # | Stage | Main service / module | Purpose | Inputs | Outputs | Status | Evidence |
|---|-------|------------------------|---------|--------|---------|--------|----------|
| 1 | Upload / job creation | `services/eep/app/jobs/create.py`, `uploads.py` | Create job and pages; enqueue **Redis** page tasks | Client upload / presign flow | DB rows + queued tasks | **Implemented** | `create.py` (DB then Redis order), `queue.py` |
| 2 | Queue execution | `services/eep_worker/app/worker_loop.py` | Claim tasks, run state-specific pipeline | `PageTask` JSON on Redis | State transitions, artifacts | **Implemented** | `process_page_task`, `_run_preprocessing`, `_run_layout`, `_run_semantic_norm` |
| 3 | Downsample / intake | `downsample_step.py`, `intake.py` | Prepare proxies; decode OTIFF | Storage URIs | Images / hashes | **Implemented** | Worker imports and tests under `tests/test_worker_loop_*` |
| 4 | Material classification | `services/iep0/` | **YOLOv8-cls** material type | Image URIs | Class + confidence | **Implemented** (optional **mock**) | `iep0/app/inference.py`; worker `_invoke_iep0_*` in `worker_loop.py` |
| 5 | Geometry (IEP1A/B) | `services/iep1a/`, `services/iep1b/` | Page geometry, split cues | Image bytes/URLs | `GeometryResponse` | **Implemented** | Ultralytics + OpenCV usage `iep1a/app/inference.py`; `geometry_invocation.py` |
| 6 | Deterministic normalization (IEP1C) | `shared/normalization/normalize.py` | Deskew, crop, perspective warp from geometry | ndarray + geometry | Normalized image + metrics | **Implemented** | Docstring “IEP1C”; `normalization_step.py` |
| 7 | Geometry / artifact gates | `gates/geometry_selection.py`, `gates/artifact_validation.py` | Route to review vs continue | Responses + config | Routes, `quality_gate_log` | **Implemented** | `PreprocessingGateConfig` defaults; `policy_loader.load_gate_config` |
| 8 | Rectification (IEP1D) | `services/iep1d/` | Warped-page rectification | Image | Rectified artifact | **Implemented** | `rescue_step.py`, `iep1d_scaler.py` |
| 9 | Semantic norm (IEP1E) | `services/iep1e/` | Orientation + spread reading order signal | Page URIs | `SemanticNormResponse` | **Implemented** (default **mock** in Compose) | `iep1e/app/main.py`; `_call_iep1e` in `worker_loop.py` |
| 10 | Layout (IEP2A / IEP2B) | `services/iep2a/`, `services/iep2b/` | Region/layout detection | Image | `LayoutDetectResponse` | **Implemented** (build **optional** real/stub image) | Dockerfiles + `iep2a/app/detect.py`; worker timeouts `_run_layout` |
| 11 | Layout adjudication + Google | `gates/layout_gate.py`, `google/document_ai.py` | Consensus; fallback to **Google Document AI** | Local detector outputs / image bytes | `LayoutAdjudicationResult` | **Implemented** | `evaluate_layout_adjudication`; tests `test_p3_2_*`, `test_google_*` |
| 12 | PTIFF QA gate | `correction/ptiff_qa.py` | Human approval before layout | Admin/user actions | Page state release | **Implemented** | Routers mounted in `main.py` |
| 13 | Human correction queue | `correction/queue.py`, `correction/apply.py` | Correct and apply edits | API payloads | Updated lineage/pages | **Implemented** | `main.py` router includes |
| 14 | Lineage | `db/lineage.py`, `lineage_api.py` | Trace artifacts & invocations | Processing steps | `page_lineage` records | **Implemented** | `models.py` `PageLineage`, API router |
| 15 | Dataset export | `dataset_builder/app/main.py` | Build training datasets from corrections | DB + S3 | Manifest / registry | **Implemented** (batch job) | `main.py` modes; Compose `dataset-builder` |
| 16 | Retraining trigger → job | `retraining_webhook.py`, `retraining_worker/app/task.py` | Webhook → triggers → worker | Alertmanager / manual | `RetrainingJob` rows | **Partial** | Webhook **implemented**; training/eval **stub** unless `LIBRARYAI_RETRAINING_TRAIN=live` etc. |
| 17 | Shadow evaluation | `shadow_worker/app/main.py`, `worker_loop.py` | Secondary model evaluation queue | Redis shadow queue | `ShadowEvaluation` rows | **Implemented** (pipeline) | Shadow worker; `_maybe_enqueue_shadow_task`; metrics `SHADOW_*` in `shared/metrics.py` |
| 18 | Model promotion | `promotion_api.py` | Admin promote/rollback **IEP1A/B** staging | REST + `gate_results` | Stage transitions | **Implemented** | `POST /v1/models/promote`, `_check_gates` |
| 19 | Offline evaluation trigger | `models_api.py` | Admin triggers evaluation record | REST | `RetrainingTrigger` (`manual_evaluation`) | **Implemented** | **`POST /v1/models/evaluate`** creates **`RetrainingTrigger`**; **`retraining_worker`** must run to execute **`_run_manual_evaluation()`** |

## IEP and EEP Mapping

| Component | Role in pipeline | Independence / value | Evidence | Status |
|-----------|------------------|----------------------|----------|--------|
| **EEP** | REST API: jobs, uploads, policy, lineage, correction, retraining hooks, artifacts | Central orchestration and **policy store** for preprocessing thresholds; does not run CV inference | `services/eep/app/main.py` | **Implemented** |
| **EEP Worker** | Async consumer: Redis **→** DB updates **→** HTTP calls to IEPs | Isolates long-running CV from API latency | `services/eep_worker/app/worker_loop.py` | **Implemented** |
| **IEP0** | Material-type classifier (**YOLOv8-cls**, mock if missing weights) | Routes policy thresholds by material | `services/iep0/app/inference.py` | **Implemented** (mock optional) |
| **IEP1A** | Geometry **segmentation** (YOLOv8-seg + OpenCV post-processing) | Competing geometry hypothesis for gate | `services/iep1a/app/inference.py` | **Implemented** |
| **IEP1B** | Geometry **pose** (YOLOv8-pose) | Second geometry family for consensus / tie-break | `services/iep1b/app/inference.py` | **Implemented** |
| **IEP1C** | **Library** normalization (not a separate container): affine deskew, crop, split metadata | Deterministic application of geometry | `shared/normalization/normalize.py` | **Implemented** |
| **IEP1D** | UVDoc-style rectification (**optional** real weights) | Rescue path for difficult warps | `services/iep1d/app/` | **Implemented** (weights **optional**) |
| **IEP1E** | Semantic normalization: PaddleOCR-backed orientation / reading-order **signal** | Ordering for splits and downstream ZIP export | `services/iep1e/app/main.py` | **Implemented** (Compose defaults **mock**) |
| **IEP2A** | Layout: Detectron2 **or** Paddle **PP-DocLayoutV2** (`IEP2A_LAYOUT_BACKEND`) | First layout detector | `services/iep2a/app/detect.py`, `backends/` | **Implemented** (real model **optional** build) |
| **IEP2B** | Layout: DocLayout-YOLO | Second detector for consensus | `services/iep2b/app/` | **Implemented** (real model **optional** build) |

## Why the Pipeline Is Non-Trivial

- **Multi-stage processing:** Worker composes **IEP0 → geometry → IEP1C → gates → optional IEP1D rescue → optional IEP1E → IEP2 → adjudication** (`worker_loop.py`, `split_step.py`, `normalization_step.py`, `layout_step.py`).
- **Orchestration vs inference:** **EEP** owns API and DB policy; **eep_worker** owns execution and **HTTP** fan-out to IEPs (`worker_loop.py`).
- **Redis-backed async execution:** Custom queue with processing lists, retries, dead-letter (`services/eep/app/queue.py`, `MAX_TASK_RETRIES = 3`).
- **Postgres page state machine:** `advance_page_state` transitions (`services/eep/app/db/page_state.py`) drive what the worker runs next.
- **S3 artifact model:** Artifacts addressed as URIs; boto3 backend (`shared/io/storage.py`).
- **Independent IEP services:** Separate Docker images and ports in `docker-compose.yml` (IEP0, IEP1A/B/D/E, IEP2A/B).
- **Gates with explicit thresholds:** `PreprocessingGateConfig` numeric defaults (e.g. `split_confidence_threshold: 0.75`) in `geometry_selection.py`; layout consensus defaults in `LayoutGateConfig` (`match_iou_threshold: 0.5`, `min_match_ratio: 0.7`, …) in `layout_gate.py`; policy YAML overrides preprocessing via `policy_loader.py`.
- **Human-in-the-loop:** PTIFF QA and correction APIs (`correction/ptiff_qa.py`, `correction/apply.py`, etc.).
- **Lineage:** `PageLineage` and related APIs (`db/lineage.py`, `lineage_api.py`).
- **Retraining path (partial):** Dataset builder export (`dataset_builder/app/main.py`); retraining worker with **stub** default and **optional live** training (`retraining_worker/app/task.py`, `live_train.py`).
- **Shadow path:** Redis shadow queue + `shadow_worker` finalizes evaluations (`shadow_worker/app/main.py`).
- **Observability:** Prometheus metrics for geometry, layout, Google adjudication, IEP timers (`shared/metrics.py`); MLflow server in `docker-compose.yml` for experiment tracking when training runs.

## AI + Classical CV Combination

The repository shows a **hybrid design**: **learned** models (YOLO-family classifiers/segmentation/pose for IEP0–IEP1B, and **DocLayout-YOLO** / **Paddle**-based layout backends) produce structured predictions, while **`shared/normalization`** applies **deterministic** geometry (deskew, perspective warp, quality scalars) from `normalize_single_page` (`shared/normalization/normalize.py`, using `deskew` / `perspective` / `quality` helpers). **Where evidenced**, **gates** (`geometry_selection`, `artifact_validation`, `layout_gate`) combine **scalar metrics**, **model confidences**, and **consensus checks** before accepting or routing to review—see gate modules under `services/eep/app/gates/`. Layout consensus thresholds use **in-code** `LayoutGateConfig` defaults.

## Acceptance Gates and Human Review

- **Storage of decisions:** `job_pages.acceptance_decision`, `review_reasons`, `quality_summary`, `status` (`services/eep/app/db/models.py`).
- **Preprocessing:** `run_geometry_selection` and `run_artifact_validation` (invoked from worker steps) write **quality gate logs** via `log_gate` (`normalization_step.py`, `split_step.py`). **Thresholds** come from **`PreprocessingGateConfig`**, loaded through **`load_gate_config(session)`** from the active **`policy_versions`** row (`policy_loader.py`). Default numeric examples include **artifact_validation_threshold** by material (`geometry_selection.py` `_DEFAULT_ARTIFACT_VALIDATION_THRESHOLDS`).
- **Layout:** `evaluate_layout_adjudication` may accept **local consensus**, call **Google Document AI**, or fall back (`layout_gate.py`); metrics **`GOOGLE_LAYOUT_ADJUDICATION_DECISIONS`**, **`EEP_LAYOUT_CONSENSUS_CONFIDENCE`** (`shared/metrics.py`).
- **Routing to review:** Split and normalization outcomes set **`review_reason`** strings (e.g. rectification policy) in `split_step.py` / `rescue_step.py` / `normalization_step.py`.
- **PTIFF QA:** Pages can enter **`ptiff_qa_pending`** when `ptiff_qa_mode == "manual"` (`worker_loop.py`), gated by **`ptiff_qa.py`** before continuing to layout.
- **Corrections as training evidence:** **`dataset_builder`** reads human correction data from lineage and emits training artifacts (`dataset_builder/app/main.py`).

## Retraining and Candidate Model Flow

| Aspect | Status | Evidence |
|--------|--------|----------|
| Alertmanager → webhook | **Implemented** | `services/eep/app/retraining_webhook.py` (`POST /v1/retraining/webhook`, `X-Webhook-Secret`) |
| Trigger recording | **Implemented** | `RetrainingTrigger` rows, cooldown logic in webhook |
| Worker picks triggers | **Implemented** | `retraining_worker/app/main.py` poll loop, `execute_retraining_task` (`task.py`) |
| Default training + eval | **Stubbed** | `_stub_mlflow_train`, `_stub_gate_results` in `task.py` |
| Live training | **Optional** | `LIBRARYAI_RETRAINING_TRAIN=live`, `live_train.py` subprocess to `training/scripts/train_iep*.py` |
| Live golden eval | **Optional** | `LIBRARYAI_RETRAINING_GOLDEN_EVAL=live` documented in `task.py` |
| MLflow | **Configured** | `docker-compose.yml` **mlflow** service; `live_train.py` parses stdout for `LIBRARYAI_MLFLOW_RUN_ID` |
| Staging **model_versions** + **gate_results** | **Implemented** | `task.py` writes `ModelVersion` stage **staging** |
| Promotion / rollback | **Implemented** (manual admin) | `promotion_api.py` — **gate check** reads `gate_results`; **`force`** skips gates |
| MLflow stage transition on promote | **Implemented** (graceful degradation) | **`promotion_api._mlflow_transition()`** calls **`MlflowClient.transition_model_version_stage`** when **`MLFLOW_TRACKING_URI`** is set and a registered version exists for **`mlflow_run_id`**; returns **`skipped_*`** with warning otherwise — **never blocks DB promotion**. Older inline comments may still read “not wired”; behavior is in code. |
| `POST /v1/models/evaluate` | **Implemented** | `models_api.py`|

**Conclusion:** End-to-end **automated** retraining from alert → trained weights → production **without** human steps is **not** claimed; **stub** paths and **optional live** paths are explicit in `task.py` and `.env.example`.

## Edge Cases and Fallbacks

| Mechanism | Role | Evidence |
|-----------|------|----------|
| Task retries + dead letter | Redis `fail_task`, `MAX_TASK_RETRIES` | `queue.py` |
| Worker task timeout / watchdog | Long-running task handling | `watchdog.py`, `worker_loop` |
| Circuit breakers | Skip repeated failing IEP calls | `circuit_breaker.py`, worker config |
| IEP0 unavailable | Fallback material type | `_invoke_iep0_*` in `worker_loop.py` |
| IEP2 timeouts | `asyncio.wait_for`, circuit breaker record | `_run_layout` in `worker_loop.py` |
| **layout_with_ocr** | Skips local IEP2 calls | `worker_loop.py` (sets `iep2a_result = iep2b_result = None`) |
| Google failure | Local fallback / audit dict | `layout_gate.py`, `document_ai.py` |
| Recovery | Reconcile orphaned Redis tasks | `services/eep_recovery/app/reconciler.py` |
| Shadow retries | Shadow worker reconcile loop | `shadow_worker/app/main.py` |

## ML / Pipeline Observability

| Signal | Status | Evidence |
|--------|--------|----------|
| Prometheus metrics (per-IEP histograms/counters) | **Implemented** | `shared/metrics.py` (`IEP1A_*`, `IEP2` implied via EEP layout metrics, `GOOGLE_*`, shadow metrics) |
| Service `/metrics` middleware | **Implemented** | `configure_observability` (e.g. `iep1e/app/main.py`) |
| Dashboard / admin APIs | **Implemented** | `admin/dashboard.py`, `models_api.py` (evaluation GET) |
| **MLflow** UI (local) | **Configured** | `docker-compose.yml` **mlflow** on port **5000** |


## What Is Not Implemented or Not Claimed

- **Dedicated IEP3 OCR microservice** (e.g. Tesseract-only service): **not found** under `services/`.
- **In-repo OCR output** for all modes: full-document OCR is **not** implemented as a native stage; **`layout_with_ocr`** relies on **Google Document AI** (`document_ai.py`).
- **Automatic model promotion** without admin/API gates: **not claimed**; promotion is **`POST /v1/models/promote`** with **`gate_results`** check (`promotion_api.py`).
- **Policy-driven layout thresholds from DB:** **partial** — `LayoutGateConfig` defaults used in code; `policy_loader.py` does not export loaded layout config (per its module docstring).
- **`POST /v1/models/evaluate` → worker:** **Implemented** — queues **`RetrainingTrigger`** (`trigger_type='manual_evaluation'`) in **`models_api.py`**; **`retraining_worker`** poll loop must be running for work to execute (operational dependency, not a code TODO).
- **`shadow_recovery` / `retraining_recovery`:** present as **code folders** but **not** in `docker-compose.yml` or ECS task list (see `docs/02_ARCHITECTURE.md`).
- **Default retraining train/eval:** **stubbed** unless env enables **live** modes (`task.py`).
- **Golden / benchmark dataset in repo:** not audited as present; training scripts referenced from `live_train.py` live under `training/scripts/` when enabled—not claimed as shipped datasets.
