# Tradeoffs and Design Decisions


---

## Summary of major decisions

| Decision | Chosen approach | Alternative considered | Why this choice appears in the repo | Main tradeoff | Evidence |
|----------|-----------------|------------------------|--------------------------------------|---------------|----------|
| Container orchestration for cloud | ECS task definitions + GitHub Actions deploy (**Fargate** for most services; **EC2** launch type for **iep0/iep1a/iep1b** JSON); optional **`k8s/*.yaml`** Kubernetes manifests (manual / generic cluster — **not** CI-automated) | Full Kubernetes / EKS / Helm | Templates and workflows target **ECS** + OIDC; **Helm/Kustomize** not present; **`k8s/*.yaml`** supplies plain K8s YAML without Chart/Kustomize | Less Kubernetes-native autoscaling (e.g. KEDA/HPA) on **ECS** path unless extended | `k8s/ecs/*.json`, `k8s/*.yaml`, `.github/workflows/deploy.yml` |
| Local development | **Docker Compose** multi-service stack | Cloud-only dev | One-command parity with service boundaries and MinIO | Heavier local resource use | `docker-compose.yml` |
| Layout / quality routing | **Conservative** acceptance: geometry/artifact gates route uncertain pages to **rectification** or **`pending_human_correction`**; “accepted” is constrained | Always accept high-confidence single-model output | Tests and gate code require agreement / filters for **`accepted`** | More human and IEP steps on edge cases | `services/eep/app/gates/artifact_validation.py`, `tests/test_p3_gate_integration.py` |
| Human review entry (layout stage) | **User-initiated** “send to review” from **`layout_detection`**; docstring states pipeline does not auto-route there during layout detection (except other paths such as preprocessing failures) | Auto-queue all ambiguous layout pages for review | Explicit operator control for correction queue | Missed review unless gates/user send pages | `services/eep/app/correction/send_to_review.py` |
| Retraining dataset hygiene | **Corrected-export** pulls rows with **`human_corrected = TRUE`**; registry mode prefers **approved** datasets | Export all pages regardless of correction status | SQL filter and registry approval flag | Slower dataset growth until enough verified corrections | `services/dataset_builder/app/main.py`, `services/retraining_worker/app/dataset_registry.py` |
| Artifact storage | **S3 / S3-compatible** URIs for artifacts; backends selected by scheme | Large blobs in Postgres | Explicit backend abstraction and MinIO for local S3 API | Object-store ops + failure handling | `shared/io/storage.py`, `docker-compose.yml` (**minio**) |
| Job execution model | **Redis** page-task queues + **EEP worker** async pipeline | Synchronous per-request processing | Job create enqueues **PageTask**s; worker claims/retry | Queue consistency, recovery, and ops surface area | `services/eep/app/jobs/create.py`, `shared/schemas/queue.py`, `services/eep_worker/app/worker_loop.py` |
| Service decomposition | **EEP** API + **eep_worker** + **IEP** services over HTTP | Monolith | Separate Dockerfiles/Compose services and IEP URLs | More network boundaries and health checks | `docker-compose.yml`, `services/iep*/` |
| Scale processing | **GitHub Actions** schedules + **`normal_scaler`** ECS/RunPod orchestration | Built-in queue-depth ECS autoscaling policies in-repo | Workflows and scaler code are explicit; no KEDA/HPA manifests found | Not the same as metric-driven autoscaling out of the box | `.github/workflows/scale-up.yml`, `services/eep/app/scaling/normal_scaler.py` |
| When compute spins up for queued work | **`PROCESSING_START_MODE=immediate`** (enqueue-triggered scale-up) vs **`scheduled_window`** (only during cron windows — **batch / cost-aware**) | Always-on workers so every job starts instantly | **`immediate`** suits **demos** and low-latency feedback; **`scheduled_window`** matches **batch digitization** where finishing **later** is acceptable | **`immediate`** can spend more on idle-adjacent bursts; **`scheduled_window`** batches work into **off-peak / planned** capacity | `services/eep/app/scaling/normal_scaler.py` (lines ~10–17, ~200–212); `.github/workflows/scheduled-window.yml` |
| Model promotion | **Gated** promote: **`gate_results`** must pass unless **`force=true`** (IEP1a/IEP1b only) | Always-on automatic production swap | `promotion_api` enforces offline gate JSON on promote | Slower promotion; needs evaluation data | `services/eep/app/promotion_api.py` |
| Shadow vs production comparison (admin) | **Offline** metrics: UI states gate comparison uses **offline evaluation**, **not live candidate inference on pages** | Run candidate model on live traffic for dashboards | Admin copy matches promotion’s reliance on stored evaluation artifacts | Shadow **worker** still maintains evaluation lifecycle (separate concern) | `frontend/src/app/admin/model-lifecycle/page.tsx`, `frontend/src/app/admin/observability/page.tsx`; `services/eep/app/promotion_api.py` |
| Observability in cloud | **Optional** ECS Prometheus/Grafana + **`observability-up/down.yml`**; core deploy leaves batch observability off **normal** scale-up | Always-on full metrics stack | `normal_scaler` excludes prometheus/grafana from automatic processing scale-up | Dashboards off unless started | `services/eep/app/scaling/normal_scaler.py` (`_EXCLUDED_SERVICES`), `.github/workflows/observability-up.yml` |

---

## Decision 1: ECS/Fargate-oriented deployment vs full Kubernetes/EKS

**Chosen (evidence):** The repo ships **Docker Compose** for local development (**`docker-compose.yml`**) and **AWS ECS-oriented** artifacts: JSON task definitions under **`k8s/ecs/`**, deployed via **`.github/workflows/deploy.yml`** (ECR build, `register-task-definition`, `update-service`, Service Connect). **Scale-up / scale-down / scheduled-window** workflows adjust ECS services and run drain-monitor tasks.

**Naming caveat:** The directory **`k8s/`** contains **`k8s/ecs/*.json`** (ECS task definitions) **and** **`k8s/*.yaml`** (Kubernetes **Deployment** / **Service** / **Ingress** / **Job** manifests — see **`k8s/README.md`**). No **`Chart.yaml`** or **Kustomize** bases were found.

**Alternative:** EKS or another Kubernetes host using the existing **`k8s/*.yaml`** set; controllers such as **KEDA** for queue-based scaling (not in-repo).

**Tradeoff:** ECS + documented workflows match the repo’s automation and teaching/demo needs; Kubernetes could offer richer autoscaling primitives but adds cluster operational overhead. **Plain** K8s YAML exists for portability **without** Helm/Kustomize in this workspace.

**Evidence:** `docker-compose.yml`; `k8s/ecs/*.json`; `k8s/*.yaml`; `.github/workflows/deploy.yml`; `.github/workflows/scale-up.yml`; `.github/workflows/scale-down.yml`; `.github/workflows/scheduled-window.yml`.

---

## Decision 2: Conservative automation vs full automation

**Chosen (evidence):** Quality gates enforce structured outcomes such as **`accepted`**, **`rectification`**, and **`pending_human_correction`**. Integration tests document that **single-model** geometry paths do **not** yield **`accepted`** without meeting agreed criteria; disagreement and filter dropouts route away from blind acceptance.

Separately, **send-to-review** implements an explicit transition from **`layout_detection`** to **`pending_human_correction`** and states that **in the automation-first model** this user action is the way a page enters human review during layout (alongside preprocessing failures and similar paths—not duplicated here).

**Tradeoff:** Stricter automation reduces silent bad accepts; more pages may need rectification or human review.

**Evidence:** `services/eep/app/gates/artifact_validation.py`; `tests/test_p3_gate_integration.py`; `services/eep/app/correction/send_to_review.py`.

---

## Decision 3: Human correction before retraining

**Chosen (evidence):** **Dataset builder** **`corrected-export`** queries **`page_lineage`** with **`WHERE human_corrected = TRUE`** (and non-null correction fields). The **retraining worker** registry supports **`corrected_hybrid`** / **`corrected_only`** modes and can prefer **approved** registry datasets (`approved` flag) before rebuilding.

**Tradeoff:** Training data grows only as corrections are verified and exported; improves label quality at the cost of throughput.

**Partial / configured:** Live training modes (`LIBRARYAI_RETRAINING_TRAIN`, golden eval) are environment-driven and often **stub** in ECS templates—see **`k8s/ecs/eep-task-def.json`** and **`.env.example`**.

**Evidence:** `services/dataset_builder/app/main.py`; `services/retraining_worker/app/dataset_registry.py`; `k8s/ecs/eep-task-def.json`; `.env.example`.

---

## Decision 4: Redis queue-based async processing vs synchronous API processing

**Chosen (evidence):** Job creation persists rows then **`enqueue_page_task`** to Redis; failures return **503** if enqueue cannot complete, with recovery semantics documented in **`create.py`**. Shared queue constants include **`libraryai:page_tasks`** and **`libraryai:page_tasks:dead_letter`**. The worker **`worker_loop`** implements claim/retry up to **`max_task_retries`**, then tasks can end in dead-letter handling paths (see **`shared/schemas/queue.py`**, **`services/eep_worker/app/worker_loop.py`**).

**Tradeoff:** More moving parts (Redis, recovery, drain tooling); better isolation between API latency and long-running page work.

**Evidence:** `services/eep/app/jobs/create.py`; `services/eep/app/queue.py`; `shared/schemas/queue.py`; `services/eep_worker/app/worker_loop.py`; `scripts/ecs_scaler/drain_monitor.py`.

---

## Decision 5: Multi-service pipeline vs monolithic service

**Chosen (evidence):** **EEP** exposes orchestration APIs; **eep_worker** runs the staged pipeline; each **IEP** is a separate service with its own Dockerfile and Compose entry; workers call IEPs via configured base URLs. This matches a **pipeline decomposition** rather than a single binary.

**Tradeoff:** More configuration (URLs, health, Service Connect aliases in ECS); clearer scaling and replacement per stage.

**Evidence:** `docker-compose.yml`; `services/eep/`; `services/eep_worker/`; `services/iep*/`; `k8s/ecs/*-task-def.json`; `.github/workflows/deploy.yml` (Service Connect JSON snippets).

---

## Decision 6: S3-compatible artifact storage vs database blob storage

**Chosen (evidence):** **`shared.io.storage`** routes **`s3://`** and **`file://`** via **`get_backend`**, with MinIO-friendly **`S3_ENDPOINT_URL`** for local dev. Compose provisions **minio** and maps credentials from env.

**Tradeoff:** Operators must manage buckets, permissions, and missing-object failures; databases hold metadata and pointers, not raw large blobs.

**Evidence:** `shared/io/storage.py`; `docker-compose.yml`; `.env.example`.

---

## Decision 7: Workflow-based scale-up/scale-down vs fully automatic autoscaling

**Chosen (evidence):** **GitHub Actions** workflows (**`scale-up.yml`**, **`scale-down.yml`**, **`scale-down-auto.yml`**, **`scheduled-window.yml`**) drive ECS updates and drain checks using **`k8s/ecs/drain-monitor-task-def.json`**. Application code **`normal_scaler`** triggers ECS/RunPod scale-up on enqueue when **`PROCESSING_START_MODE=immediate`**, with a Redis lock. **No** YAML/JSON in this repo defines ECS **Application Auto Scaling** policies tied to queue depth.

**Product intent — immediate vs scheduled-window (batching):** Library digitization was conceived as **batch-first**: staff upload **runs** of material, and **end-to-end prep does not need to start the moment each file lands** — completing work **within agreed windows** is enough for many archives. **`PROCESSING_START_MODE=scheduled_window`** implements that posture: **no** scale-up on enqueue; **`scheduled-window.yml`** (with **`vars.PROCESSING_START_MODE`**) raises worker/GPU capacity **only** inside configured windows when work exists — **cost-aware**, **off-peak-friendly**, and aligned with **batched** operations.

**Demo / instructor path:** **`PROCESSING_START_MODE=immediate`** is the default in checked-in ECS task definitions so **demos** and **staging smoke tests** get **responsive** processing after enqueue. This is a **deliberate** trade for **visibility and latency** over **maximum batching** of compute cost.

**Tradeoff:** Scaling is explicit and schedulable; it is **not** the same as fully automatic queue-depth autoscaling unless extended outside this repository.

**Evidence:** `.github/workflows/scale-up.yml`; `.github/workflows/scale-down.yml`; `.github/workflows/scale-down-auto.yml`; `.github/workflows/scheduled-window.yml`; `services/eep/app/scaling/normal_scaler.py`.

---

## Decision 8: Manual/gated model promotion vs automatic promotion

**Chosen (evidence):** **`POST /v1/models/promote`** ( **`services/eep/app/promotion_api.py`** ) promotes **IEP1a/IEP1b** staging candidates only after reading **`model_versions.gate_results`**. If gates fail or are absent → **409**. **`force=true`** skips the gate check and annotates audit notes—explicit admin override. **IEP2** is excluded from this automated pipeline per module docstring.

**Partial:** MLflow stage transition is **implemented** in **`_mlflow_transition()`** with graceful degradation when **`MLFLOW_TRACKING_URI`** / **`mlflow`** / registered versions are unavailable — **DB promotion** still completes (`promotion_api.py`). The module docstring historically understated this behavior.

**Tradeoff:** Safer promotion at the cost of evaluation prerequisites and admin actions.

**Evidence:** `services/eep/app/promotion_api.py`.

---

## Decision 9: Optional observability stack vs always-on observability

**Chosen (evidence):** Compose includes **prometheus**, **grafana**, **alertmanager** for local use. ECS has **`prometheus-task-def.json`** / **`grafana-task-def.json`** and **`observability-up.yml`** / **`observability-down.yml`**. **`normal_scaler`** explicitly does **not** start **`libraryai-prometheus`** / **`libraryai-grafana`** during normal processing scale-up.

**Tradeoff:** Lower baseline cloud cost and complexity; dashboards require deliberate startup.

**Evidence:** `docker-compose.yml`; `k8s/ecs/prometheus-task-def.json`; `k8s/ecs/grafana-task-def.json`; `.github/workflows/observability-up.yml`; `services/eep/app/scaling/normal_scaler.py`.

---

## Decision 10: Offline shadow/production gate comparison vs live candidate inference

**Chosen (evidence):** Admin pages **Model lifecycle** and **Observability** describe **offline** comparison of shadow vs production geometry IoU gate scores and state explicitly that **no candidate inference ran on live pages** for that comparison. **`promotion_api`** promotions rely on **pre-computed** **`gate_results`** from offline evaluation, not on-the-fly inference.

The repo **does** include a **`shadow_worker`** that processes Redis **shadow** tasks and **`ShadowEvaluation`** rows (`finalize` / reconcile)—that path is **not** the same UI promise as “live shadow inference on every page”; it supports the evaluation lifecycle and bookkeeping.

**Tradeoff:** Avoids running shadow candidate models on the live hot path for dashboard comparisons; offline metrics must be produced by evaluation jobs/workers.

**Note:** The repository **does not** contain an engineering comment explicitly citing “double inference cost”; the tradeoff above follows **UI and API text**, not an external cost model.

**Evidence:** `frontend/src/app/admin/model-lifecycle/page.tsx`; `frontend/src/app/admin/observability/page.tsx`; `services/eep/app/promotion_api.py`; `services/shadow_worker/app/main.py`.

---
