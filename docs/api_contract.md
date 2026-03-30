# LibraryAI EEP — API Contract

**Version:** post-frontend-integration
**Base URL:** `http://eep:8000`
**Auth:** JWT Bearer (`Authorization: Bearer <token>`)
**Roles:** `user` | `admin`

---

## Table of Contents

1. [Auth](#1-auth)
2. [Upload](#2-upload)
3. [Jobs](#3-jobs)
4. [PTIFF QA](#4-ptiff-qa)
5. [Correction Queue](#5-correction-queue)
6. [Lineage](#6-lineage)
7. [Admin](#7-admin)
8. [Policy](#8-policy)
9. [Model Management](#9-model-management)
10. [Retraining](#10-retraining)
11. [Artifacts](#11-artifacts)
12. [Pagination Rules](#12-pagination-rules)
13. [Nullable Fields Reference](#13-nullable-fields-reference)
14. [Artifact Read Flow](#14-artifact-read-flow)
15. [Role Scoping Rules](#15-role-scoping-rules)

---

## 1. Auth

### `POST /v1/auth/token`

Obtain a JWT access token.

**Auth:** none
**Request:** `application/x-www-form-urlencoded`

| Field | Type | Required |
|-------|------|----------|
| username | string | yes |
| password | string | yes |

**Response 200:**

```json
{
  "access_token": "<jwt>",
  "token_type": "bearer"
}
```

**Token payload claims:** `sub` (user_id), `role` ("user" | "admin"), `exp`

---

### `POST /v1/auth/signup`

Self-register a new regular user account.

**Auth:** none (public endpoint)
**Request:** `application/json`

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| username | string | yes | 1–64 characters |
| password | string | yes | minimum 8 characters |

**Response 201:**

```json
{
  "user_id": "<uuid>",
  "username": "alice",
  "role": "user",
  "is_active": true,
  "created_at": "2026-03-29T12:00:00Z"
}
```

**Security note:** `role` is always set to `"user"` by the server. The request body has no `role` field and there is no mechanism to create an admin account through this endpoint.

**Error responses:**
- `409` — username already taken
- `422` — validation error (empty username, password shorter than 8 characters)

**Admin accounts** are created only via `POST /v1/users` (admin-authenticated) or the `scripts/create_admin.py` bootstrap script. There is no public path to an admin account.

---

## 2. Upload

### `POST /v1/uploads/jobs/presign`

Generate a presigned S3 PUT URL for direct OTIFF upload.

**Auth:** `user` or `admin`
**Request:** no body
**Response 200:**

```json
{
  "upload_url": "https://...",
  "object_uri": "s3://libraryai/uploads/<uuid>.tiff",
  "expires_in": 3600
}
```

**Notes:**
- Upload the raw OTIFF to `upload_url` with `Content-Type: image/tiff` via HTTP PUT.
- Pass `object_uri` as `pages[n].input_uri` in `POST /v1/jobs`.

---

## 3. Jobs

### `POST /v1/jobs`

Create a new processing job.

**Auth:** `user` or `admin`
**Request:**

```json
{
  "collection_id": "col-001",
  "material_type": "book",
  "pages": [
    { "page_number": 1, "input_uri": "s3://libraryai/uploads/..." }
  ],
  "pipeline_mode": "layout",
  "ptiff_qa_mode": "manual",
  "policy_version": "v1",
  "shadow_mode": false
}
```

| Field | Type | Values | Default |
|-------|------|--------|---------|
| collection_id | string | — | required |
| material_type | string | book \| newspaper \| archival_document \| document | required |
| pages | array | 1–1000 PageInput | required |
| pipeline_mode | string | preprocess \| layout | layout |
| ptiff_qa_mode | string | manual \| auto_continue | manual |
| policy_version | string | — | required |
| shadow_mode | bool | — | false |

**Response 201:**

```json
{
  "job_id": "<uuid>",
  "status": "queued",
  "page_count": 5,
  "created_at": "2026-03-29T12:00:00Z"
}
```

---

### `GET /v1/jobs`

List jobs visible to the caller.

**Auth:** `user` or `admin`
**Query params:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| search | string | — | case-insensitive substring on job_id or collection_id |
| status | string | — | queued \| running \| done \| failed |
| pipeline_mode | string | — | preprocess \| layout |
| created_by | string | — | admin only; user_id filter |
| from_date | datetime | — | ISO 8601 UTC lower bound on created_at |
| to_date | datetime | — | ISO 8601 UTC upper bound on created_at |
| page | int | 1 | 1-indexed |
| page_size | int | 50 | max 200 |

**Response 200:**

```json
{
  "total": 42,
  "page": 1,
  "page_size": 50,
  "items": [
    {
      "job_id": "...",
      "collection_id": "...",
      "material_type": "book",
      "pipeline_mode": "layout",
      "ptiff_qa_mode": "manual",
      "policy_version": "v1",
      "shadow_mode": false,
      "created_by": "<user_id>",
      "created_by_username": "alice",
      "status": "done",
      "page_count": 5,
      "accepted_count": 4,
      "review_count": 1,
      "failed_count": 0,
      "pending_human_correction_count": 0,
      "created_at": "2026-03-29T12:00:00Z",
      "updated_at": "2026-03-29T12:05:00Z",
      "completed_at": "2026-03-29T12:05:00Z"
    }
  ]
}
```

**Notes:**
- `created_by_username`: nullable — null when `created_by` is null or user account deleted.
- Non-admin callers see only their own jobs.

---

### `GET /v1/jobs/{job_id}`

Full status for a single job with per-page detail.

**Auth:** `user` (own jobs) or `admin` (all jobs)
**Response 200:**

```json
{
  "summary": { /* JobStatusSummary — same fields as items[] above */ },
  "pages": [
    {
      "page_number": 1,
      "sub_page_index": null,
      "status": "accepted",
      "routing_path": "preprocessing_only",
      "output_image_uri": "s3://...",
      "output_layout_uri": "s3://...",
      "quality_summary": {
        "blur_score": 0.92,
        "border_score": 0.88,
        "skew_residual": 0.03,
        "foreground_coverage": 0.75
      },
      "review_reasons": null,
      "acceptance_decision": "accepted",
      "processing_time_ms": 1234.5
    }
  ]
}
```

**Page states:** queued | preprocessing | rectification | ptiff_qa_pending | layout_detection | pending_human_correction | accepted | review | failed | split

**Error responses:**
- `404` — job not found
- `403` — not authorized (non-admin accessing another user's job)

---

## 4. PTIFF QA

### `GET /v1/jobs/{job_id}/ptiff-qa`

**Auth:** `user` (own) or `admin`
**Response 200:**

```json
{
  "job_id": "...",
  "ptiff_qa_mode": "manual",
  "total_pages": 5,
  "pages_pending": 3,
  "pages_approved": 2,
  "pages_in_correction": 0,
  "is_gate_ready": false,
  "pages": [
    {
      "page_number": 1,
      "sub_page_index": null,
      "current_state": "ptiff_qa_pending",
      "approval_status": "pending",
      "needs_correction": false
    }
  ]
}
```

---

### `POST /v1/jobs/{job_id}/ptiff-qa/approve-all`

**Auth:** `user` (own) or `admin`
**Response 200:** `{ "approved_count": 3, "gate_released": true }`

---

### `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve`

**Auth:** `user` (own) or `admin`
**Response 200:** `{ "page_number": 1, "approved": true, "gate_released": false }`

**Error:** `409` — page not in ptiff_qa_pending

---

### `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit`

Transition ptiff_qa_pending → pending_human_correction.

**Auth:** `user` (own) or `admin`
**Response 200:** `{ "page_number": 1, "new_state": "pending_human_correction" }`

**Canonical route.** Also available as alias: `.../ptiff-qa/edit-and-return` (identical behavior).

**Error:** `409` — page not in ptiff_qa_pending

---

### `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit-and-return`

Alias for `/edit` above. Both routes are identical in behavior and return the same response.

---

## 5. Correction Queue

### `GET /v1/correction-queue`

**Auth:** `user` (own jobs) or `admin` (all)
**Query params:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| job_id | string | — | filter to a specific job |
| material_type | string | — | book \| newspaper \| archival_document \| document |
| review_reason | string | — | filter pages containing this reason code |
| page | int | — | 1-indexed page (takes precedence over offset) |
| page_size | int | — | items per page (takes precedence over limit) |
| offset | int | 0 | legacy: items to skip |
| limit | int | 50 | legacy: max items (max 200) |

**Pagination:** `page` / `page_size` takes precedence when provided; `offset` / `limit` preserved for backward compatibility.

**Response 200:**

```json
{
  "total": 10,
  "offset": 0,
  "limit": 50,
  "page": 1,
  "page_size": 50,
  "items": [
    {
      "job_id": "...",
      "page_number": 1,
      "sub_page_index": null,
      "material_type": "book",
      "pipeline_mode": "layout",
      "review_reasons": ["low_confidence"],
      "waiting_since": "2026-03-29T10:00:00Z",
      "output_image_uri": "s3://..."
    }
  ]
}
```

**Nullable fields:** `sub_page_index`, `waiting_since`, `output_image_uri`

---

### `GET /v1/correction-queue/{job_id}/{page_number}`

Return full correction workspace.

**Auth:** `user` (own) or `admin`
**Query params:** `sub_page_index` (int, optional — required when multiple sub-pages pending)

**Response 200:**

```json
{
  "job_id": "...",
  "page_number": 1,
  "sub_page_index": null,
  "material_type": "book",
  "pipeline_mode": "layout",
  "review_reasons": ["low_confidence"],
  "original_otiff_uri": "s3://...",
  "best_output_uri": "s3://...",
  "branch_outputs": {
    "iep1a_geometry": { "page_count": 1, "split_required": false, "geometry_confidence": 0.95 },
    "iep1b_geometry": null,
    "iep1c_normalized": "s3://...",
    "iep1d_rectified": null
  },
  "current_crop_box": [10, 20, 900, 1200],
  "current_deskew_angle": null,
  "current_split_x": null
}
```

**Nullable fields:**
- `sub_page_index` — null for unsplit pages; 0 or 1 for split children
- `original_otiff_uri` — null in edge case where lineage record is not yet created
- `best_output_uri` — null when no processed artifact exists
- `branch_outputs.iep1a_geometry` — null when IEP1A was not invoked
- `branch_outputs.iep1b_geometry` — null when IEP1B was not invoked
- `branch_outputs.iep1c_normalized` — null when preprocessing produced no output
- `branch_outputs.iep1d_rectified` — null when IEP1D was not invoked
- `current_crop_box` — null when no geometry data available
- **`current_deskew_angle`** — **always null** for fresh correction routing; the IEP1C normalization deskew angle is not persisted in the DB. Populated from `page_lineage.human_correction_fields` only for re-correction of previously corrected pages.
- `current_split_x` — null for single-page detections

**Error responses:**
- `404` — job or page not found
- `409` — page not in pending_human_correction
- `422` — multiple sub-pages pending; re-request with sub_page_index

---

### `POST /v1/jobs/{job_id}/pages/{page_number}/correction`

Submit human correction for a page.

**Auth:** `user` (own) or `admin`
**Request:** `{ "crop_box": [x, y, x2, y2], "deskew_angle": 1.5, "split_x": null }`

**Response 200:** `{ "page_id": "...", "new_state": "preprocessing" }`

---

### `POST /v1/jobs/{job_id}/pages/{page_number}/correction/reject`

Reject a page from correction (route to review).

**Auth:** `user` (own) or `admin`
**Response 200:** `{ "page_id": "...", "new_state": "review" }`

---

## 6. Lineage

### `GET /v1/lineage/{job_id}/{page_number}`

**Auth:** `admin` only
**Query params:** `sub_page_index` (int, optional)
**Response 200:** Full lineage record with service invocations.

---

## 7. Admin

### `GET /v1/admin/dashboard-summary`

**Auth:** `admin` only
**Response 200:** KPI counts (total jobs, pages by state, acceptance rates, etc.)

---

### `GET /v1/admin/service-health`

**Auth:** `admin` only
**Response 200:** Per-IEP service health status.

---

### `POST /v1/users`

Create a user.

**Auth:** `admin` only
**Request:** `{ "username": "alice", "password": "...", "role": "user" }`
**Response 201:** User record (no password).

---

### `GET /v1/users`

List all users.

**Auth:** `admin` only
**Response 200:** `{ "total": N, "items": [...] }`

---

### `PATCH /v1/users/{user_id}/deactivate`

Deactivate a user account.

**Auth:** `admin` only
**Response 200:** Updated user record.

---

## 8. Policy

### `GET /v1/policy`

**Auth:** `admin` only
**Response 200:** Current policy config (YAML string + metadata).

---

### `PATCH /v1/policy`

Update the active policy version.

**Auth:** `admin` only
**Request:** `{ "config_yaml": "...", "justification": "...", "version": "v2" }`
**Response 200:** Updated policy record.

---

## 9. Model Management

### `POST /v1/models/promote`

Promote IEP1 staging candidate to production.

**Auth:** `admin` only
**Request:** `{ "service": "iep1a", "force": false }`

| Field | Type | Notes |
|-------|------|-------|
| service | string | iep1a \| iep1b only (IEP2 excluded from promotion pipeline) |
| force | bool | skip gate check when true; recorded in notes |

**Response 200:** `ModelVersionRecord`
**Error:** `404` — no staging candidate; `409` — gate check failed

---

### `POST /v1/models/rollback`

Restore most recently archived version to production.

**Auth:** `admin` only
**Request:** `{ "service": "iep1a", "reason": "manual" }`

**Response 200:** `ModelVersionRecord`
**Error:** `404` — no archived version; `409` — automated rollback window expired (2h)

---

### `GET /v1/models/evaluation`

Return model version records with gate results for the evaluation dashboard.

**Auth:** `admin` only
**Query params:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| candidate_tag | string | — | filter by version_tag; omit for most recent |
| service | string | — | filter by service name |
| stage | string | — | experimental \| staging \| shadow \| production \| archived |
| limit | int | 20 | max 100 |

**Response 200:**

```json
{
  "total": 5,
  "records": [
    {
      "model_id": "...",
      "service_name": "iep1a",
      "version_tag": "v1.2.0",
      "stage": "staging",
      "dataset_version": "ds-2026-03",
      "mlflow_run_id": "abc123",
      "gate_results": { "accuracy": { "pass": true, "value": 0.95 } },
      "gate_summary": {
        "total_gates": 1,
        "passed_gates": 1,
        "failed_gates": 0,
        "all_pass": true,
        "failed_names": []
      },
      "promoted_at": null,
      "notes": null,
      "created_at": "2026-03-28T10:00:00Z"
    }
  ]
}
```

**Nullable fields:** `dataset_version`, `mlflow_run_id`, `gate_results`, `gate_summary`, `promoted_at`, `notes`

---

### `POST /v1/models/evaluate`

Trigger an offline evaluation run for a candidate model.

**Auth:** `admin` only
**Request:** `{ "candidate_tag": "v1.2.0", "service": "iep1a" }`

**Response 202:**

```json
{
  "evaluation_job_id": "<uuid>",
  "model_id": "...",
  "service_name": "iep1a",
  "version_tag": "v1.2.0",
  "status": "pending",
  "message": "Evaluation queued for iep1a v1.2.0. Results will be written to model_versions.gate_results when complete."
}
```

**Error responses:**
- `404` — no model version found for service + candidate_tag
- `409` — evaluation already pending/running for this candidate

---

## 10. Retraining

### `POST /v1/retraining/webhook`

Receive Alertmanager webhook notifications.

**Auth:** `X-Webhook-Secret: <secret>` header (shared secret)
**Configuration:** Set `RETRAINING_WEBHOOK_SECRET` env var in EEP.
**Request:** Standard Alertmanager v4 webhook payload.
**Response 200:** Always 200 after successful auth (Alertmanager retry safety).

```json
{
  "processed": 2,
  "results": [
    { "trigger_id": "...", "trigger_type": "escalation_rate_anomaly", "status": "recorded" }
  ]
}
```

**Result statuses:** `recorded` | `skipped_cooldown` | `skipped_unknown` | `skipped_resolved`

**Trigger types:** `escalation_rate_anomaly` | `auto_accept_rate_collapse` | `structural_agreement_degradation` | `drift_alert_persistence` | `layout_confidence_degradation`

---

### `GET /v1/retraining/status`

Retraining pipeline status for the MLOps dashboard.

**Auth:** `admin` only
**Response 200:**

```json
{
  "summary": {
    "active_count": 1,
    "queued_count": 2,
    "completed_count": 3,
    "failed_count": 0,
    "total_triggers": 12,
    "pending_triggers": 2
  },
  "active_jobs": [ /* RetrainingJobSummary[] */ ],
  "queued_jobs": [ /* RetrainingJobSummary[] */ ],
  "recently_completed": [ /* up to 10 jobs from last 72h */ ],
  "trigger_cooldowns": [
    {
      "trigger_type": "escalation_rate_anomaly",
      "in_cooldown": true,
      "cooldown_until": "2026-04-05T10:00:00Z",
      "last_fired_at": "2026-03-29T10:00:00Z",
      "last_status": "pending"
    }
  ],
  "as_of": "2026-03-29T12:00:00Z"
}
```

**RetrainingJobSummary fields:** `job_id`, `pipeline_type`, `status`, `trigger_id`, `dataset_version`, `mlflow_run_id`, `result_mAP`, `promotion_decision`, `started_at`, `completed_at`, `error_message`, `created_at`

**Nullable:** `trigger_id`, `dataset_version`, `mlflow_run_id`, `result_mAP`, `promotion_decision`, `started_at`, `completed_at`, `error_message`

---

## 11. Artifacts

### `POST /v1/artifacts/presign-read`

Generate a short-lived presigned GET URL for browser display or download.

**Auth:** `user` (own jobs) or `admin` (all)
**Request:**

```json
{
  "uri": "s3://libraryai/jobs/<job_id>/input/otiff/1.tiff",
  "expires_in": 300
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| uri | string | yes | s3:// URI known to the system |
| expires_in | int | no | TTL in seconds (1–3600); default: `ARTIFACT_READ_PRESIGN_EXPIRES_SECONDS` (300) |

**Response 200:**

```json
{
  "uri": "s3://libraryai/jobs/.../1.tiff",
  "read_url": "https://minio:9000/libraryai/jobs/.../1.tiff?X-Amz-...",
  "expires_in": 300,
  "content_type_hint": "image/tiff"
}
```

**Content-type hints:** `image/tiff` (.tiff, .tif) | `application/json` (.json) | `image/png` (.png) | `image/jpeg` (.jpg, .jpeg) | `application/octet-stream` (default)

**Error responses:**
- `400` — URI scheme is not s3:// or bucket mismatch
- `403` — artifact belongs to another user's job
- `404` — URI not found in system records
- `503` — storage unavailable

**Supported artifact types:**
| Type | Path pattern |
|------|-------------|
| Original OTIFF | `s3://{bucket}/jobs/{job_id}/input/otiff/…` |
| Preprocessing output | `s3://{bucket}/jobs/{job_id}/output/…` |
| Correction output | `s3://{bucket}/jobs/{job_id}/corrections/…` |
| Split child | `s3://{bucket}/jobs/{job_id}/splits/…` |
| Layout JSON | `s3://{bucket}/jobs/{job_id}/layout/…` |
| Upload staging | `s3://{bucket}/uploads/…` |

---

## 12. Pagination Rules

### Jobs list (`GET /v1/jobs`)
- **Style:** `page` / `page_size` (1-indexed)
- **Defaults:** page=1, page_size=50
- **Max page_size:** 200
- **Sort:** `created_at DESC`

### Correction queue (`GET /v1/correction-queue`)
- **Preferred style:** `page` / `page_size` (1-indexed)
- **Legacy style:** `offset` / `limit` (backward compatible)
- `page` / `page_size` takes precedence when provided
- **Defaults:** offset=0, limit=50
- **Max:** 200 items
- **Sort:** `status_updated_at ASC` (oldest first)
- **Response always includes both:** `offset`/`limit` and `page`/`page_size`

### Model evaluation (`GET /v1/models/evaluation`)
- **Style:** `limit` only (no pagination)
- **Default:** 20, max 100
- **Sort:** `created_at DESC`

---

## 13. Nullable Fields Reference

| Endpoint | Field | Nullable? | When null |
|----------|-------|-----------|-----------|
| `GET /v1/jobs` items | `created_by` | yes | job created without auth (legacy) |
| `GET /v1/jobs` items | `created_by_username` | yes | user deleted, or created_by null |
| `GET /v1/jobs` items | `completed_at` | yes | job not yet complete |
| `GET /v1/correction-queue` items | `sub_page_index` | yes | unsplit page |
| `GET /v1/correction-queue` items | `waiting_since` | yes | status_updated_at not recorded |
| `GET /v1/correction-queue` items | `output_image_uri` | yes | no processed artifact yet |
| Workspace | `current_deskew_angle` | **always** | IEP1C deskew angle not persisted; populated only from prior human correction |
| Workspace | `current_crop_box` | yes | no geometry data |
| Workspace | `current_split_x` | yes | single-page (no split) |
| `GET /v1/models/evaluation` | `gate_results` | yes | evaluation not yet run |
| `GET /v1/models/evaluation` | `gate_summary` | yes | when gate_results is null |
| `GET /v1/models/evaluation` | `dataset_version` | yes | not recorded |
| `GET /v1/models/evaluation` | `mlflow_run_id` | yes | not started / not connected |
| `GET /v1/models/evaluation` | `promoted_at` | yes | never promoted to production |
| `GET /v1/retraining/status` jobs | `started_at` | yes | not yet started |
| `GET /v1/retraining/status` jobs | `completed_at` | yes | not yet complete |
| `GET /v1/retraining/status` jobs | `result_mAP` | yes | not completed |
| `GET /v1/retraining/status` trigger_cooldowns | `cooldown_until` | yes | not in cooldown |
| `GET /v1/retraining/status` trigger_cooldowns | `last_fired_at` | yes | never fired |
| `GET /v1/retraining/status` trigger_cooldowns | `last_status` | yes | never fired |

---

## 14. Artifact Read Flow

The correction workspace and job detail UI receive raw `s3://` URIs in API responses. To display or download artifacts in the browser:

```
1. Frontend receives artifact URI from API response
   (e.g., page.output_image_uri = "s3://libraryai/jobs/…/1.tiff")

2. Frontend calls:
   POST /v1/artifacts/presign-read
   { "uri": "s3://libraryai/jobs/…/1.tiff", "expires_in": 300 }

3. Backend:
   a. Verifies the URI exists in page_lineage or job_pages (404 if not)
   b. Enforces ownership (403 if regular user accessing another user's job)
   c. Generates a presigned S3 GET URL via boto3

4. Backend returns:
   { "read_url": "https://minio:9000/…?X-Amz-...", "expires_in": 300, … }

5. Frontend renders <img src={read_url}> or downloads via the URL
   (URL expires after expires_in seconds)
```

**Environment variables for storage:**

| Variable | Default | Notes |
|----------|---------|-------|
| `S3_ENDPOINT_URL` | — | MinIO/LocalStack endpoint (e.g. http://minio:9000) |
| `S3_ACCESS_KEY` | — | Access key |
| `S3_SECRET_KEY` | — | Secret key |
| `S3_BUCKET_NAME` | libraryai | Bucket name |
| `ARTIFACT_READ_PRESIGN_EXPIRES_SECONDS` | 300 | Default presigned URL TTL |
| `S3_PRESIGN_EXPIRES_SECONDS` | 3600 | Upload presign TTL |

---

## 15. Role Scoping Rules

| Endpoint | user role | admin role |
|----------|-----------|------------|
| `POST /v1/auth/signup` | n/a (public) | n/a (public) |
| `GET /v1/jobs` | own jobs only | all jobs |
| `GET /v1/jobs/{job_id}` | own job only | any job |
| `GET /v1/jobs/{job_id}/ptiff-qa` | own job only | any job |
| `POST /v1/jobs/{job_id}/ptiff-qa/*` | own job only | any job |
| `GET /v1/correction-queue` | own jobs only | all queued pages |
| `GET /v1/correction-queue/{job_id}/{page_number}` | own job only | any job |
| `POST /v1/jobs/{job_id}/pages/{n}/correction` | own job only | any job |
| `POST /v1/jobs/{job_id}/pages/{n}/correction/reject` | own job only | any job |
| `POST /v1/artifacts/presign-read` | own job artifacts | any artifact in DB |
| `GET /v1/lineage/*` | **403** | allowed |
| `GET /v1/policy` | **403** | allowed |
| `PATCH /v1/policy` | **403** | allowed |
| `POST /v1/models/promote` | **403** | allowed |
| `POST /v1/models/rollback` | **403** | allowed |
| `GET /v1/models/evaluation` | **403** | allowed |
| `POST /v1/models/evaluate` | **403** | allowed |
| `GET /v1/retraining/status` | **403** | allowed |
| `GET /v1/admin/*` | **403** | allowed |
| `POST /v1/users` | **403** | allowed |
| `GET /v1/users` | **403** | allowed |
| `PATCH /v1/users/*` | **403** | allowed |
| `POST /v1/retraining/webhook` | n/a (secret header) | n/a |
