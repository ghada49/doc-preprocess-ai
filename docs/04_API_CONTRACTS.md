# EEP API contracts

This document reflects the **FastAPI application** mounted at **`services/eep/app/main.py`** as of the audited repository revision. Authoritative sources are the router modules under `services/eep/app/` and shared Pydantic schemas under `shared/schemas/`. If other docs disagree on paths or behavior, **verify against code** or **`GET /openapi.json`** on a running EEP instance.

## How to explore the live contract

- Interactive docs: **`GET /docs`** (Swagger UI) and **`GET /openapi.json`** on the running EEP server.
- **Base path:** all application routes use the **`/v1`** prefix (except middleware/metrics provided by `configure_observability` in `shared/middleware`).
- **Local dev:** Docker Compose maps host **`http://localhost:8888`** → container port **8000** (`docker-compose.yml` **eep** service).

## Authentication and authorization

| Mechanism | Details |
|-----------|---------|
| **JWT access token** | Issued by **`POST /v1/auth/token`**. Algorithm and expiry from env: **`JWT_SECRET_KEY`**, **`JWT_ALGORITHM`** (default `HS256`), **`JWT_ACCESS_TOKEN_EXPIRE_MINUTES`** (default `1440`). Claims: **`sub`** (user id), **`role`** (`user` \| `admin`), **`exp`**. |
| **Bearer header** | Protected endpoints use **`Authorization: Bearer <token>`** via `HTTPBearer` (`services/eep/app/auth.py`). Missing/invalid token → **401** with `WWW-Authenticate: Bearer`. |
| **`require_user`** | Any authenticated user (`user` or `admin`). |
| **`require_admin`** | **`role == "admin"`** only; otherwise **403** `Admin role required`. |
| **Job ownership** | For many job-scoped routes, non-admin users may only access jobs where **`jobs.created_by == CurrentUser.user_id`** (`assert_job_ownership`). Admins bypass ownership checks. |
| **Form login** | **`POST /v1/auth/token`** expects **`application/x-www-form-urlencoded`** body fields **`username`**, **`password`** (`OAuth2PasswordRequestForm`). |

## Cross-cutting configuration

| Env / behavior | Effect on API |
|----------------|----------------|
| **`CORS_ALLOW_ORIGINS`** | Comma-separated browser origins; default `http://localhost:3000,http://127.0.0.1:3000` (`main.py`). |
| **S3 / MinIO** | Upload presign, artifact presign, downloads use **`S3_*`** variables (`uploads.py`, `artifacts_api.py`, `jobs/download.py`) — see `.env.example`. |
| **Redis** | Job creation, correction apply, PTIFF QA release paths enqueue **`PageTask`** JSON via **`enqueue_page_task`** (`services/eep/app/queue.py`). |

## Router inventory (mount order)

Routers are included **without** an API-wide `prefix`; each route declares the full `/v1/...` path.

| Router module | FastAPI `tags` (typical) | Notes |
|---------------|--------------------------|--------|
| `auth.py` | `auth` | Token + signup |
| `uploads.py` | `uploads` | Presigned PUT for staging OTIFF |
| `jobs/create.py` | `jobs` | Create job |
| `jobs/list.py` | `jobs` | List jobs |
| `jobs/status.py` | `jobs` | Job detail |
| `jobs/actions.py` | `jobs` | Cancel / delete |
| `jobs/download.py` | `jobs`, `download` | Manifest + ZIP |
| `admin/dashboard.py` | `admin` | KPIs, service health, shadow comparisons, promotion audit |
| `admin/infra.py` | `admin` | Queue, inventory, deployment |
| `admin/users.py` | `admin` | User CRUD-style admin |
| `lineage_api.py` | `lineage` | Page audit trail |
| `policy_api.py` | `policy` | Policy read/update |
| `promotion_api.py` | `mlops` | Model promote / rollback |
| `retraining_webhook.py` | `mlops` | Alertmanager webhook |
| `retraining_api.py` | `mlops` | Retraining status, manual trigger, RunPod callback |
| `models_api.py` | `mlops` | Evaluation listing + trigger |
| `artifacts_api.py` | `artifacts` | Presign read + preview |
| `correction/queue.py` | `correction-queue` | Correction queue |
| `correction/apply.py` | `correction` | Apply correction |
| `correction/reject.py` | `correction` | Reject correction |
| `correction/send_to_review.py` | `correction` | Send page to review |
| `correction/ptiff_qa.py` | `ptiff-qa` | PTIFF QA gate |
| `correction/ptiff_qa_viewer.py` | `ptiff-qa` | QA viewer + flag |

---

## Endpoints (summary tables)

### Meta

| Method | Path | Auth | Response | Notes |
|--------|------|------|----------|--------|
| GET | `/v1/status` | None | `{"status":"ok","service":"eep"}` | Liveness / smoke (`main.py`). |

### Auth (`services/eep/app/auth.py`)

| Method | Path | Auth | Request | Success | Main errors |
|--------|------|------|---------|---------|-------------|
| POST | `/v1/auth/token` | None | Form: `username`, `password` | **200** `TokenResponse` (`access_token`, `token_type`) | **401** wrong password / inactive user |
| POST | `/v1/auth/signup` | None | JSON **`SignupRequest`**: `username` (1–64), `password` (min 8) | **201** `SignupResponse` (`user_id`, `username`, `role`=`user`, …) | **409** duplicate username |

### Uploads (`services/eep/app/uploads.py`)

| Method | Path | Auth | Response | Main errors / side effects |
|--------|------|------|----------|----------------------------|
| POST | `/v1/uploads/jobs/presign` | `require_user` | **`PresignUploadResponse`**: `upload_url`, `object_uri`, `expires_in` | **503** S3 client failure; client PUTs raw TIFF to `upload_url`, passes `object_uri` into job create |

### Jobs (`create`, `list`, `status`, `actions`)

| Method | Path | Auth | Request / query | Response | Main errors | Side effects |
|--------|------|------|-----------------|----------|-------------|--------------|
| POST | `/v1/jobs` | `require_user` | Body **`JobCreateRequest`** (`shared/schemas/eep.py`): `collection_id`, `material_type`, `pages[]` (`page_number`, `input_uri`, optional `reference_ptiff_uri`), `pipeline_mode` (`preprocess`\|`layout`\|`layout_with_ocr`), `ptiff_qa_mode`, `policy_version`, `shadow_mode` | **201** `JobCreateResponse` | **422** validation; **503** Redis enqueue failed after DB commit | DB insert job + pages; **`enqueue_page_task`** each page; **`maybe_trigger_scale_up`** background task |
| GET | `/v1/jobs` | `require_user` | Query: `search`, `status`, `pipeline_mode`, `created_by` (admin-only filter), `from_date`, `to_date`, `page`, `page_size` | **200** `JobListResponse` | **401** | Non-admin: scoped to `created_by = caller` |
| GET | `/v1/jobs/{job_id}` | `require_user` | Path `job_id` | **200** `JobStatusResponse` | **404**; **403** ownership | Derived job status from leaf pages |
| POST | `/v1/jobs/{job_id}/cancel` | `require_user` | Path | **200** `JobActionResponse` | **404** | Marks cancelable pages failed `review_reasons=["job_canceled"]`; sync job summary |
| DELETE | `/v1/jobs/{job_id}` | `require_user` | Path | **200** `JobActionResponse` (`status="deleted"`) | **404** | Deletes job + related rows (lineage, gates, shadow eval, etc.) |

### Downloads (`services/eep/app/jobs/download.py`)

| Method | Path | Auth | Response | Main errors |
|--------|------|------|----------|-------------|
| GET | `/v1/jobs/{job_id}/output/download-manifest` | `require_user` | **200** `DownloadManifestResponse` | **404** job |
| GET | `/v1/jobs/{job_id}/output/download.zip` | `require_user` | **200** streaming ZIP (`StreamingResponse`) | **404** job or no downloadable pages; **503** storage |

### Artifacts (`services/eep/app/artifacts_api.py`)

| Method | Path | Auth | Request | Response | Main errors | Notes |
|--------|------|------|---------|----------|-------------|--------|
| POST | `/v1/artifacts/presign-read` | `require_user` | **`ArtifactPresignReadRequest`**: `uri`, optional `expires_in` (≤3600) | **200** `ArtifactPresignReadResponse` | **400** bad URI; **403**/`404` auth lookup | URI must appear in DB lineage/pages |
| POST | `/v1/artifacts/preview` | `require_user` | **`ArtifactPreviewRequest`**: `uri`, `page_index`, `max_width`, **`return_url`** | **200** — **`JSONResponse`** with preview metadata when cache/`return_url` path applies, else **`StreamingResponse`** PNG bytes with dimension headers | **400** unsupported scheme; **403**/`404`; **415** decode failure; **503** Pillow missing / storage | **`file://`** allowed for dev (`artifact_preview`); exposes `X-Original-Width`, etc. on stream |

### Correction (`queue`, `apply`, `reject`, `send_to_review`)

| Method | Path | Auth | Request | Response | Main errors | Side effects |
|--------|------|------|---------|----------|-------------|--------------|
| GET | `/v1/correction-queue` | `require_user` | Query filters + pagination (`page`/`page_size` preferred over `offset`/`limit`) | **200** `CorrectionQueueResponse` | — | Read-only |
| GET | `/v1/correction-queue/{job_id}/{page_number}` | `require_user` | Query optional `sub_page_index` | **200** `CorrectionWorkspaceResponse` | **404**, **409**, **422** ambiguous split children | Read-only |
| POST | `/v1/jobs/{job_id}/pages/{page_number}/correction` | `require_user` | JSON **`CorrectionApplyRequest`** (crop, deskew, `page_structure`, `split_x`, quad selection, `source_artifact_uri`, `notes`); query optional **`sub_page_index`** | **200** `CorrectionApplyResponse` (`status="ok"`) | **404**, **409**, **422**, **500** | DB update; may **`enqueue_page_task`** after commit; **`maybe_trigger_scale_up`** if enqueue OK |
| POST | `/v1/jobs/{job_id}/pages/{page_number}/correction-reject` | `require_user` | Optional body **`CorrectionRejectRequest`** (`notes`); query **`sub_page_index`** | **200** `CorrectionRejectResponse` | **404**, **409** | Terminal **`review`** |
| POST | `/v1/jobs/{job_id}/pages/{page_number}/send-to-review` | `require_user` | Query **`sub_page_index`** | **200** `SendToReviewResponse` | **404**, **409** (must be `layout_detection`) | Invalidates stale layout state |

### PTIFF QA (`services/eep/app/correction/ptiff_qa.py`, `ptiff_qa_viewer.py`)

| Method | Path | Auth | Request | Response | Main errors | Side effects |
|--------|------|------|---------|----------|-------------|--------------|
| GET | `/v1/jobs/{job_id}/ptiff-qa` | `require_user` | — | **200** `PtiffQaStatusResponse` | **404** | Read-only |
| POST | `/v1/jobs/{job_id}/ptiff-qa/approve-all` | `require_user` | `BackgroundTasks`; **`Depends(get_redis)`** | **200** `ApproveAllResponse` | **404** | May release gate → state transitions + **Redis enqueue** for layout |
| POST | `/v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve` | `require_user` | Query optional `sub_page_index`; **`Depends(get_redis)`** | **200** `ApprovePageResponse` | **404**, **409** | Same gate-release semantics |
| POST | `/v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit` | `require_user` | Query optional `sub_page_index` | **200** `EditPageResponse` | **404**, **409**, concurrent **409** | `ptiff_qa_pending` → `pending_human_correction` |
| POST | `/v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit-and-return` | `require_user` | Alias of **`/edit`** (`include_in_schema=True`) | Same | Same | Same |
| GET | `/v1/jobs/{job_id}/ptiff-qa/viewer` | `require_user` | Query optional `page_number`, `sub_page_index` | **200** `PtiffQaViewerResponse` | **404** | Presigned preview URLs for carousel |
| POST | `/v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/flag` | `require_user` | Query optional `sub_page_index` | **200** `FlagPageResponse` | **404**, **409** | `accepted` or `ptiff_qa_pending` → `pending_human_correction` |

### Lineage (`services/eep/app/lineage_api.py`)

| Method | Path | Auth | Response | Errors |
|--------|------|------|----------|--------|
| GET | `/v1/lineage/{job_id}/{page_number}` | **`require_admin`** | **200** `LineageResponse` (lineage rows + service invocations + quality gates) | **404** no lineage |

### Policy (`services/eep/app/policy_api.py`)

| Method | Path | Auth | Request | Response | Errors |
|--------|------|------|---------|----------|--------|
| GET | `/v1/policy` | **`require_admin`** | — | **200** `PolicyRecord` | **404** no policy row yet |
| PATCH | `/v1/policy` | **`require_admin`** | **`UpdatePolicyRequest`**: `config_yaml`, `justification`, optional `audit_evidence`, `slo_validation` | **200** `PolicyRecord` | **400** invalid YAML; **422** guardrails |

### Admin users (`services/eep/app/admin/users.py`)

| Method | Path | Auth | Request | Response | Errors |
|--------|------|------|---------|----------|--------|
| POST | `/v1/users` | **`require_admin`** | **`CreateUserRequest`**: `username`, `password`, `role` | **201** `UserRecord` | **409** duplicate username |
| GET | `/v1/users` | **`require_admin`** | — | **200** `list[UserRecord]` | — |
| PATCH | `/v1/users/{user_id}/deactivate` | **`require_admin`** | — | **200** `UserRecord` | **404** |

### Admin dashboard & infra

| Method | Path | Auth | Query / body | Response |
|--------|------|------|--------------|----------|
| GET | `/v1/admin/dashboard-summary` | **`require_admin`** | — | **`DashboardSummaryResponse`** (KPIs, Redis worker depth proxy) |
| GET | `/v1/admin/service-health` | **`require_admin`** | `window_hours` (1–720, default 24) | **`ServiceHealthResponse`** |
| GET | `/v1/admin/model-gate-comparisons` | **`require_admin`** | `job_id`, `status`, `limit`, `offset` | **`ModelGateComparisonsResponse`** |
| GET | `/v1/admin/promotion-audit` | **`require_admin`** | `service`, `action`, `model_id`, `limit`, `offset` | **`PromotionAuditResponse`** |
| GET | `/v1/admin/queue-status` | **`require_admin`** | — | **`QueueStatusResponse`** (Redis depths + worker slots) |
| GET | `/v1/admin/service-inventory` | **`require_admin`** | — | **`ServiceInventoryResponse`** |
| GET | `/v1/admin/deployment-status` | **`require_admin`** | — | **`DeploymentStatusResponse`** |

### MLOps — promotion (`services/eep/app/promotion_api.py`)

| Method | Path | Auth | Request | Response | Errors |
|--------|------|------|---------|----------|--------|
| POST | `/v1/models/promote` | **`require_admin`** | **`PromoteRequest`**: `service` (`iep1a`\|`iep1b`), `force` | **200** `ModelVersionRecord` (+ `mlflow_transition_result` best-effort) | **404** no staging; **409** gates failed |
| POST | `/v1/models/rollback` | **`require_admin`** | **`RollbackRequest`**: `service`, `reason` (default `"manual"`) | **200** `ModelVersionRecord` | **404**; **409** automated rollback window |

### MLOps — retraining & evaluation

| Method | Path | Auth | Request / headers | Response | Notes |
|--------|------|------|-------------------|----------|--------|
| POST | `/v1/retraining/webhook` | **`X-Webhook-Secret`** (not JWT) | JSON **`AlertmanagerPayload`** | **200** `WebhookResponse` | Secret from **`RETRAINING_WEBHOOK_SECRET`** (default dev string in code — replace in prod). Always **200** after auth so Alertmanager does not retry. Records **`retraining_triggers`**. |
| GET | `/v1/retraining/status` | **`require_admin`** | — | **200** `RetrainingStatusResponse` | — |
| POST | `/v1/retraining/trigger` | **`require_admin`** | Optional JSON **`ManualRetrainingRequest`** (`reason` ≤500 chars) | **202** `ManualRetrainingResponse` | **409** manual trigger already pending/processing; may start RunPod worker |
| POST | `/v1/retraining/runpod/callback` | **`X-Retraining-Callback-Secret`** | JSON **`RunPodRetrainingCallbackRequest`** | **200** `RunPodRetrainingCallbackResponse` | **`RETRAINING_CALLBACK_SECRET`** required (503 if unset); **401** bad secret; **404** bad ids. **Internal / integration** — not for browser clients. |
| GET | `/v1/models/evaluation` | **`require_admin`** | Query: `candidate_tag`, `service`, `stage`, `limit` | **200** `ModelEvaluationResponse` | — |
| POST | **`/v1/models/evaluate`** | **`require_admin`** | **`EvaluateRequest`**: `candidate_tag`, `service` | **202** `EvaluateResponse` | **404** unknown model; **409** duplicate pending eval trigger |

**Important (evaluate):** Implementation creates a **`retraining_triggers`** row (`trigger_type='manual_evaluation'`, `notes=model_id`) for the **`retraining_worker`** to process (`services/eep/app/models_api.py`). The **`evaluation_job_id`** field in **`EvaluateResponse`** is set to **`trigger_id`**, not a separate `retraining_jobs.id` (despite the historical docstring mentioning only `retraining_jobs`).

---

## Request bodies defined in code (non-exhaustive pointers)

| Contract | Definition location |
|----------|---------------------|
| Job create / status models | `shared/schemas/eep.py` (`JobCreateRequest`, `JobStatusResponse`, …) |
| Correction apply | `CorrectionApplyRequest` in `services/eep/app/correction/apply.py` |
| Policy update | `UpdatePolicyRequest` in `services/eep/app/policy_api.py` |
| Promotion | `PromoteRequest`, `RollbackRequest` in `services/eep/app/promotion_api.py` |
| RunPod callback | `RunPodRetrainingCallbackRequest`, `RunPodModelVersionPayload` in `services/eep/app/retraining_api.py` |
| Artifact preview / presign | `artifacts_api.py` |
| Upload presign response | `PresignUploadResponse` in `uploads.py` |

---

## Partial, internal, or operator-only surfaces

| Item | Classification |
|------|----------------|
| **`POST /v1/retraining/webhook`** | Operator integration — **shared secret** header, not JWT. |
| **`POST /v1/retraining/runpod/callback`** | **External worker → EEP** callback; **secret** header; fails closed if secret unset. |
| **`POST /v1/models/evaluate`** | Depends on **`retraining_worker`** picking up **`manual_evaluation`** triggers — pipeline completeness is an operational concern. |
| **`POST /v1/artifacts/preview`** | Returns **either** JSON (**`ArtifactPreviewResponse`**) **or** raw PNG stream depending on `return_url` and caching; clients must handle both (**200** content negotiation by implementation). |

---

## Canonical contract

**`docs/04_API_CONTRACTS.md`** is the project’s maintained HTTP contract for EEP. The former `docs/api_contract.md` was removed; any extra client notes (pagination, nullability, role scoping) that were only in that file can be reintroduced here if still needed, after checking them against the current code.

**Resolution rule:** For **paths, methods, auth dependencies, and request/response model names**, **`services/eep/app` + `shared/schemas`** and **`/openapi.json`** win over any prose when they disagree.
