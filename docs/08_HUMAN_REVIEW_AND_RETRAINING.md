# Human review and retraining



## Overview

LibraryAI routes uncertain preprocessing outcomes to **`pending_human_correction`** (and related states) using geometry selection and artifact gates, then exposes a **correction queue** and workspace APIs for reviewers. Corrections are persisted on **`page_lineage`** (including **`human_corrected`**, **`human_correction_fields`**, artifacts). **Dataset export** for retraining selects rows with **`human_corrected = TRUE`**, **`acceptance_decision = 'accepted'`**, and non-null **`human_correction_fields`**, and writes **YOLO-style segmentation label lines** plus **`data.yaml`** when thresholds are met.

The **retraining worker** (`services/retraining_worker/app/task.py`) defaults to **stub** training/evaluation unless **`LIBRARYAI_RETRAINING_TRAIN`** / **`LIBRARYAI_RETRAINING_GOLDEN_EVAL`** are set to **`live`** with valid manifests and infrastructure. **Promotion** of IEP1 models is **admin-driven** and **gate-checked** unless **`force=true`** (`services/eep/app/promotion_api.py`).

---

## Human review triggers

Automation combines **geometry selection** (`route_decision`: **`accepted`** | **`rectification`** | **`pending_human_correction`**), **artifact soft scoring** vs material thresholds, and **worker rescue/normalization** paths that document explicit **`review_reason`** strings when routing to **`pending_human_correction`**. Policy defaults include **`split_confidence_threshold`** **0.75**, **`page_area_preference_threshold`** **0.30**, and **`artifact_validation_threshold`** **0.60** (with per-material overrides in **`PreprocessingGateConfig`** — `services/eep/app/gates/geometry_selection.py`).

| Trigger / condition | What happens | Evidence | Status |
|---------------------|--------------|----------|--------|
| Geometry gate returns **`pending_human_correction`** (disagreement, low trust, filters, etc.) | Worker routes page toward human correction with gate **`review_reason`** | `tests/test_p3_gate_integration.py`; `services/eep_worker/app/rescue_step.py` (documents mapping) | **Implemented** |
| Artifact validation fails soft threshold / material rules | **`route_decision`** toward correction/review path per pipeline context | `services/eep/app/gates/artifact_validation.py`; `tests/test_p3_gate_integration.py` | **Implemented** |
| Rescue / IEP failures (rectification failure, geometry failures after rescue, circuit breaker, timeouts per module docs) | **`RescueOutcome`** / normalization maps to **`pending_human_correction`** with reason codes | `services/eep_worker/app/rescue_step.py`; `services/eep_worker/app/normalization_step.py` | **Implemented** |
| User sends page from **`layout_detection`** to review | **`POST …/send-to-review`** → **`pending_human_correction`**, **`review_reasons`**: **`user_requested_review`** | `services/eep/app/correction/send_to_review.py` | **Implemented** |
| PTIFF QA sends page to correction | **`ptiff_qa_pending`** → **`pending_human_correction`** (approval cleared) | `services/eep/app/correction/ptiff_qa.py` | **Implemented** |
| Reviewer flags **`accepted`** or **`ptiff_qa_pending`** page for correction | **`POST …/ptiff-qa/edit`** (viewer) → **`pending_human_correction`**, **`user_flagged_for_correction`** | `services/eep/app/correction/ptiff_qa_viewer.py` | **Implemented** |
| Reviewer rejects correction queue item | **`POST …/correction-reject`** → terminal **`review`**, **`human_correction_rejected`** | `services/eep/app/correction/reject.py` | **Implemented** |

---

## Human review workflow

**Implemented flow (high level):**

1. Page enters **`pending_human_correction`** (worker routing, QA path, or user action above).
2. Page appears in **`GET /v1/correction-queue`** (lists **`JobPage.status == "pending_human_correction"`**).
3. Reviewer loads **`GET /v1/correction-queue/{job_id}/{page_number}`** (workspace assembly from lineage + gates).
4. Reviewer submits **`POST …/correction`** with crop/quad/deskew/split parameters (`CorrectionApplyRequest` in **`apply.py`**).
5. Backend validates state (**must** be **`pending_human_correction`**), runs **`advance_page_state`** (CAS — concurrent update → **409**), writes corrected artifact bytes to storage, updates **`page_lineage`** (`human_corrected`, fields, URIs), enqueues **`PageTask`** for **`semantic_norm`** continuation.

| Step | User/system action | Stored / persisted data | Evidence |
|------|--------------------|-------------------------|----------|
| Queue listing | System lists pending pages | **`job_pages`** rows with **`status`**, **`review_reasons`**, timestamps | `services/eep/app/correction/queue.py` |
| Workspace read | Reviewer opens workspace | **`human_correction_fields`** (if any), gate-derived defaults | `services/eep/app/correction/workspace_assembly.py` |
| Submit correction | Reviewer POSTs geometry | **`human_correction_fields`** (selection_mode, quad_points, crop_box, deskew_angle, source_artifact_uri), **`human_corrected=True`**, **`human_correction_timestamp`**, **`output_image_uri`**, optional **`reviewer_notes`** | `services/eep/app/correction/apply.py` |
| Split children | Reviewer chooses spread | Child **`job_pages`** / **`page_lineage`** rows, parent **`split`** closure per **`_split_parent`** | `services/eep/app/correction/apply.py`, `_split_parent.py` |
| Reject | Reviewer declines | **`review`** status, **`review_reasons`** includes **`human_correction_rejected`** | `services/eep/app/correction/reject.py` |

**Note:** **`page_lineage.reviewed_by`** exists in the schema (`services/eep/app/db/models.py`) and **`merge_page_lineage`** supports it (`services/eep/app/db/lineage.py`), but **`services/eep/app/correction/apply.py`** does not set **`reviewed_by`** in the excerpted single-page apply path—**partial** linkage between auth user and lineage reviewer id unless set elsewhere.

---

## What gets saved

| Saved field / concept | Purpose | Storage | Evidence | Status |
|----------------------|---------|---------|----------|--------|
| **`human_corrected`** | Marks lineage row as corrected for downstream dataset SQL | **`page_lineage.human_corrected`** | `services/eep/app/db/models.py`; `services/eep/app/correction/apply.py` | **Implemented** |
| **`human_correction_fields`** | Geometry + source URI JSON | **`page_lineage.human_correction_fields`** (JSONB) | `apply.py` (`correction_fields` dict) | **Implemented** |
| **`human_correction_timestamp`** | When correction saved | **`page_lineage`** | `apply.py` | **Implemented** |
| **`reviewer_notes`** | Optional text | **`page_lineage.reviewer_notes`** | `apply.py` | **Implemented** |
| **`reviewed_by`** | Reviewer user id | **`page_lineage.reviewed_by`** | **Schema + merge helper** | **Partial** (column exists; apply path not shown setting it) |
| **`review_reasons`** on page | Why correction was needed / user actions | **`job_pages.review_reasons`** (JSONB) | `send_to_review.py`, `ptiff_qa_viewer.py`, `reject.py` | **Implemented** |
| **`review_reason`** codes from gates | Traceability | **`quality_gate_log`**, lineage **`gate_results`** | `services/eep/app/db/models.py`; gate builders | **Implemented** |
| **`output_image_uri`** / **`input_image_uri`** | Artifact pointers | **`job_pages`**, **`page_lineage`** | `models.py`; `apply.py` | **Implemented** |
| Parent/child split | Split lineage linkage | **`parent_page_id`**, **`sub_page_index`**, **`split_source`** | `models.py` | **Implemented** |
| **YOLO label rows** (export) | Training labels | Files under **`labels/train`** / **`labels/val`** | `services/dataset_builder/app/main.py` (`_seg_line`) | **Implemented on export** (not stored as DB blobs) |

---

## Lineage and audit trail

**`page_lineage`** rows record per-page material, routing, **`gate_results`**, **`acceptance_decision`**, **`policy_version`**, artifact states, human-correction fields, and optional **`shadow_eval_id`** (`services/eep/app/db/models.py`). **`service_invocations`** stores each IEP call with **`service_version`**, **`model_version`**, timing, and status. **`quality_gate_log`** stores immutable gate decisions (`services/eep/app/db/models.py`).

**`GET /v1/lineage/{job_id}/{page_number}`** (admin) returns lineage rows, invocations, and gate logs for audit (`services/eep/app/lineage_api.py`).

**Why it matters:** Supports reproducibility (which models/policies processed the page) and QA (why **`accepted`** vs correction).

---

## Retraining data flow

| Stage | Description | Evidence | Status |
|-------|-------------|----------|--------|
| Accepted human corrections selected | SQL requires **`human_corrected = TRUE`**, **`human_correction_fields IS NOT NULL`**, **`acceptance_decision = 'accepted'`** | `services/dataset_builder/app/main.py` (`_fetch_corrected_rows`) | **Implemented** |
| Dataset builder exports examples | Writes images + label files + **`data.yaml`** + manifest under **`training/preprocessing/corrected_export/{dataset_version}`** | `services/dataset_builder/app/main.py` | **Implemented** |
| Source artifacts downloaded | **`s3://`**, **`file://`**, or local path via **`boto3`** / copy | `services/dataset_builder/app/main.py` (`_download_source_image`) | **Implemented** |
| Labels generated | Normalized polygon → **`_seg_line`** → class **`0`** + xy pairs (YOLO seg–style line format) | `services/dataset_builder/app/main.py` | **Implemented** |
| Minimum samples per family/material | **`RETRAINING_MIN_CORRECTED_{IEP1A|IEP1B}_{BOOK|NEWSPAPER|MICROFILM}`** default **10**; export may return **`min_samples_not_met`** | `services/dataset_builder/app/main.py` | **Implemented** |
| Training job | **`LIBRARYAI_RETRAINING_TRAIN`** **`stub`** (default) vs **`live`** subprocess to **`training/scripts/train_iep*.py`** | `services/retraining_worker/app/task.py`; `services/retraining_worker/app/live_train.py` | **Stub default**; **live optional** |
| Offline evaluation | **`LIBRARYAI_RETRAINING_GOLDEN_EVAL`** stub vs **`live`** (`evaluate_golden_dataset.py`) | `services/retraining_worker/app/task.py` | **Stub default**; **live optional** |
| **`gate_results`** on **`model_versions`** | Written for **`staging`** candidates | `services/retraining_worker/app/task.py` | **Implemented** (stub or live) |
| Promotion | **`POST /v1/models/promote`** with **`gate_results`** check or **`force`** | `services/eep/app/promotion_api.py` | **Manual / gated** |
| Rollback | **`POST /v1/models/rollback`** | `services/eep/app/promotion_api.py` | **Manual** |
| Retraining trigger | **`POST /v1/retraining/trigger`**, Alertmanager webhook, cooldowns | `services/eep/app/retraining_api.py`; `services/eep/app/retraining_webhook.py` | **Implemented** |

---

## Dataset builder

| Topic | Detail |
|-------|--------|
| **Location** | `services/dataset_builder/app/main.py` — CLI **`python services/dataset_builder/app/main.py`** |
| **Modes** | **`corrected-export`** (plus **`scheduled`** / **`triggered`** registry modes per argparse) |
| **Inputs** | **`DATABASE_URL`**; optional S3 env for downloads (`S3_*`) |
| **Filter** | **`human_corrected`**, **`human_correction_fields`**, **`acceptance_decision = 'accepted'`** |
| **Outputs** | Per-family/material **`images/{train,val}`**, **`labels/{train,val}`**, **`data.yaml`**, **`retraining_train_manifest.json`** |
| **YOLO-style labels** | **`_seg_line`** emits **`0 x1 y1 …`** normalized polygon lines |
| **Limits** | **`source_window`** filtering; **`min_samples_not_met`** early exit; skip counters for missing URI / invalid geometry |
| **Compose** | Service **`dataset-builder`** uses **`profiles: ["dataset-build"]`** — not started by default (`docker-compose.yml`) |
| **ECS** | **`k8s/ecs/dataset-builder-task-def.json`** |

---

## Retraining worker

| Topic | Detail |
|-------|--------|
| **Path** | `services/retraining_worker/app/main.py` — FastAPI app + poll/reconcile loops |
| **Poll** | Claims **`RetrainingTrigger`** **`status='pending'`** → **`processing`** → **`execute_retraining_task`** (`services/retraining_worker/app/task.py`) |
| **Modes** | **`LIBRARYAI_RETRAINING_TRAIN`** stub vs live; **`LIBRARYAI_RETRAINING_GOLDEN_EVAL`** stub vs live; dataset selection via **`services/retraining_worker/app/dataset_registry.py`** (corrected hybrid / corrected only) |
| **MLflow** | Stub returns synthetic run id; live training parses **`LIBRARYAI_MLFLOW_RUN_ID=`** from training script stdout (`live_train.py`) |
| **Artifacts** | Live path can upload **`best.pt`** to S3 and embed URIs in **`model_versions.notes`** (see **`task.py`** helpers) |
| **Compose / ECS** | `docker-compose.yml` **`retraining-worker`**; **`k8s/ecs/retraining-worker-task-def.json`**; **`deploy.yml`** builds image |

---

## Evaluation and promotion

| Feature | Evidence | Status | Notes |
|---------|----------|--------|-------|
| Offline evaluation metadata | **`GET /v1/models/evaluation`**, **`POST /v1/models/evaluate`** | **Implemented** | `services/eep/app/models_api.py` |
| Promotion gates | **`gate_results`** JSON must pass **`promotion_api._check_gates`** unless **`force=true`** | **Implemented** | `services/eep/app/promotion_api.py` |
| Rollback | **`POST /v1/models/rollback`** with reason / window rules | **Implemented** | `promotion_api.py` |
| Audit log | **`model_promotion_audit`** rows (**`forced`**, **`failed_gates_bypassed`**) | **Implemented** | `services/eep/app/db/models.py`; `promotion_api.py` |
| Model versioning | **`model_versions`** stages **`experimental`** … **`archived`** | **Implemented** | `models.py` |
| **`model-info`-style API** | No literal **`/v1/model-info`** found; evaluation/promotion routers cover related ops | **Not found** | Use **`models_api`** / **`promotion_api`** |

---

## Quality and safety rationale

- **Gated acceptance** (`tests/test_p3_gate_integration.py`, geometry selection) reduces blind **`accepted`** when models disagree or signals fail thresholds.
- **Human correction** persists authoritative geometry and corrected artifacts (`apply.py`) before downstream stages resume.
- **Dataset export** restricts to **accepted** corrected rows (`dataset_builder`) so retraining does not ingest unaccepted drafts.
- **Promotion** requires passing **`gate_results`** unless admin **`force`** (`promotion_api.py`), preserving production quality.
- **Lineage + gate logs** support audit and debugging (`lineage_api.py`, DB models).

---

## Edge cases and failure handling

| Edge case | Handling | Evidence | Status |
|-----------|----------|----------|--------|
| Rejected correction | Terminal **`review`** state, rejection reason | `services/eep/app/correction/reject.py` | **Implemented** |
| Missing source URI for export | Row skipped; counted in **`skipped_counts`** | `services/dataset_builder/app/main.py` | **Implemented** |
| Invalid geometry / dimensions | Row skipped | `services/dataset_builder/app/main.py` | **Implemented** |
| Insufficient corrected counts | Returns **`min_samples_not_met`**; registry may **defer** (`DatasetSelectionDeferred`) | `dataset_builder`; `dataset_registry.py` | **Implemented** |
| Concurrent correction | **`advance_page_state`** CAS → **409** | `services/eep/app/correction/apply.py` | **Implemented** |
| Failed retraining job | Trigger/job marked failed; reconcile loop | `services/retraining_worker/app/main.py`; `reconcile.py` | **Implemented** |
| Worker retries / dead letter | **`max_task_retries`**, **`libraryai:page_tasks:dead_letter`** | `services/eep_worker/app/worker_loop.py`; `shared/schemas/queue.py` | **Implemented** |
| Split parent/child | Dedicated split helpers and closure rules | `apply.py`, `_split_parent.py` | **Implemented** |

---

## Frontend / admin evidence

| UI area | Purpose | Evidence | Status |
|---------|---------|----------|--------|
| **`/queue`**, **`/admin/queue`** | Correction queue table | `frontend/src/app/queue/page.tsx`; `frontend/src/app/admin/queue/page.tsx` | **Present** |
| **`/queue/.../workspace`**, **`/admin/queue/.../workspace`** | Correction workspace | `frontend/src/app/queue/[job_id]/[page_number]/workspace/page.tsx`; admin variant | **Present** |
| **`/admin/lineage/...`** | Lineage view | `frontend/src/app/admin/lineage/.../page.tsx` | **Present** |
| **`/admin/retraining`** | Retraining jobs/status | `frontend/src/app/admin/retraining/page.tsx` | **Present** |
| **`/admin/model-lifecycle`** | Model stages, promotion audit, offline gate comparison copy | `frontend/src/app/admin/model-lifecycle/page.tsx` | **Present** |
| **`/admin/dashboard`**, **`/admin/deployment`** | KPIs, **`retraining_mode`** / **`golden_eval_mode`** flags | `frontend/src/app/admin/dashboard/page.tsx`; `frontend/src/app/admin/deployment/page.tsx` | **Present** |

---
