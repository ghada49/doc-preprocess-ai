# LibraryAI — Frontend & Product Specification v1.0

> **Source:** `pre-implementation_spec.md` v2.1 · A.19 RBAC definition · `ui-pre-implementation_html.md` mockups
> **Scope:** Web application layer only. Backend pipeline behaviour is unchanged. This document defines roles, screen requirements, API surface, and backend implications needed to build the admin console and regular user portal.
> **HTML mockup policy:** The HTML mockups in `ui-pre-implementation_html.md` are treated as UI evidence only — they inform layout direction and widget choices but are not the authoritative source of truth for behaviour or access rules. Behaviour follows `pre-implementation_spec.md` v2.1 and A.19.

---

## 1. Role Definitions

### 1.1 Regular User

A library staff member or job submitter. Regular users interact with the pipeline at a day-to-day operational level.

**Capabilities:**
- Submit new processing jobs via the user portal
- View status and outputs of their own jobs
- Access the correction queue scoped to their own jobs (pages from jobs they submitted)
- Submit corrections or reject pages for their own jobs' pending pages

**Restricted from:**
- Jobs submitted by other users
- System-wide dashboard, service health, or throughput metrics
- Shadow model evaluation, promotion, and rollback
- Retraining management
- Lineage inspection (full audit trail)
- Policy configuration
- User management (create, deactivate)

### 1.2 Admin

A system overseer, MLOps engineer, or senior operator with full access to all pipeline operations and system configuration.

**Capabilities (includes all regular user capabilities plus):**
- View and manage all jobs across all users
- Access the global correction queue (pages from all jobs, all users)
- Act on corrections for any page regardless of submitting user
- Inspect full page lineage (preprocessing history, consensus, corrections, shadow link)
- Shadow model evaluation, gate review, promotion, force promotion, and rollback
- Retraining trigger history and job monitoring
- System policy read and edit
- User account management (create, list, deactivate)
- Operational dashboard with system-wide KPIs and pipeline health

---

## 2. Permission Matrix

| Screen / Action | Regular User | Admin |
|---|:---:|:---:|
| **Authentication** | | |
| Login (`POST /v1/auth/token`) | Yes | Yes |
| **Regular User Portal** | | |
| Submit new job | Yes | Yes |
| View own job list | Yes | Yes |
| View own job detail and outputs | Yes | Yes |
| Download output artifact (PTIFF / layout JSON) | Yes (own jobs) | Yes (all jobs) |
| **Correction** | | |
| View correction queue (own jobs) | Yes | Yes |
| View correction queue (all jobs, global) | No | Yes |
| Open correction workspace (own job page) | Yes | Yes |
| Open correction workspace (other user's page) | No | Yes |
| Submit correction (`POST .../correction`) | Yes (own jobs) | Yes (all) |
| Reject page (`POST .../correction-reject`) | Yes (own jobs) | Yes (all) |
| **Admin Console** | | |
| Admin dashboard (KPIs, health, activity) | No | Yes |
| Global jobs list (all jobs, all users) | No | Yes |
| Lineage page | No | Yes |
| Shadow models page | No | Yes |
| Retraining page | No | Yes |
| Settings / policy | No | Yes |
| User management | No | Yes |

> **Implementation note:** Access scoping follows A.19. `GET /v1/correction-queue` and both correction POST endpoints use `require_user`. `GET /v1/jobs/{job_id}` uses `require_user` with server-side scoping: regular users only see jobs where `jobs.created_by = current_user.user_id`. Admin-only endpoints use `require_admin`. The new endpoints defined in Section 5 follow the same dependency pattern.

---

## 3. Screen Requirements

### 3.1 Admin Dashboard

**Purpose:** Operational real-time snapshot of the pipeline for admin users. Consolidates throughput, error rates, correction backlog, worker activity, and pipeline-stage health in a single view.

**Access:** Admin only (`require_admin`).

**Data displayed:**

| Widget | Content |
|--------|---------|
| **Throughput** | Pages processed per hour (rolling 1-hour window). Delta from previous hour. |
| **Auto Accept Rate** | `accepted_pages / (accepted + review + failed)` as a percentage. Health label: "Healthy" (above alert threshold) or "Degraded" (below). |
| **Pending Corrections** | Count of pages currently in `pending_human_correction` across all jobs. Warning label when non-zero. |
| **Active Jobs** | Count of jobs with at least one page not yet in a terminal state. Active worker count. |
| **Shadow Evaluations** | Count of `shadow_results` rows for the current staging candidate, if any. |
| **Pipeline Health** | Per-stage success rates as progress bars: Preprocessing, Rectification, Layout Detection, Human Review Throughput. Each bar shows the percentage of pages that passed that stage successfully over the last rolling hour. |
| **Quick Links** | Shortcuts to: Open Correction Queue, View Failed Jobs, Inspect Shadow Models. |
| **Recent Activity** | Latest system events: job submitted, page accepted, page failed, correction submitted. Timestamp and actor shown. |

**User actions:**
- Click Quick Link to navigate to the target screen
- Click a recent activity item to navigate to the relevant job or page detail

**Required backend endpoints:**
- `GET /v1/admin/dashboard-summary` (**NEW**) — aggregate KPIs
- `GET /v1/admin/service-health` (**NEW**) — per-stage success rates
- `GET /v1/jobs?status=failed&page=1&page_size=5` (**NEW** list endpoint) — recent failures for quick link

**Loading state:** Each KPI card loads independently with a spinner. Cards populate as each response arrives. Pipeline Health bars populate from the service-health response.

**Empty state:** Counters display zero values. Pipeline Health bars show 0% with a "No data yet" caption if the system has not processed any pages.

**Error state:** Individual card shows "Unavailable" with a retry icon if its endpoint returns an error. Other cards continue to render. A global error banner is shown only if all endpoints fail simultaneously (network issue).

---

### 3.2 Admin Jobs Page

**Purpose:** Full searchable, filterable, paginated list of all jobs across all users. Admins use this to monitor overall job throughput, diagnose failures, and navigate to correction queues or lineage for specific jobs.

**Access:** Admin only (`require_admin`).

**Data displayed per row:**

| Column | Content |
|--------|---------|
| Job ID | Truncated UUID; click to copy full ID |
| Submitted | Timestamp (relative + absolute on hover) |
| Submitted by | Username of the creating user |
| Pipeline mode | `preprocess` or `layout` badge |
| Shadow mode | On/Off indicator |
| Pages | Total page count |
| Status summary | Accepted / Total; pending count; failed count |
| Actions | View Detail, Open Correction Queue (shown only when pending_human_correction count > 0) |

**User actions:**
- Search by job ID, collection label, or submitting username
- Filter by status category (in-progress / completed / failed / any), pipeline mode, date range
- Sort by submitted timestamp
- Server-side pagination
- Click job row to open job detail
- Click "Open Correction Queue" to navigate to the correction queue filtered to that job

**Required backend endpoints:**
- `GET /v1/jobs?search=&status=&pipeline_mode=&from_date=&to_date=&page=&page_size=` (**NEW**)
- `GET /v1/jobs/{job_id}` (existing) — for the drill-down detail view

**Loading state:** Table skeleton (5 placeholder rows) while request is in flight. Pagination controls disabled during load.

**Empty state:** "No jobs found. Adjust your filters or submit a new job." with a clear-filters button and a link to the job submission screen.

**Error state:** Inline error message with retry button. Existing results are kept visible if a filter change fails.

---

### 3.3 Correction Queue

**Purpose:** Lists all pages currently in `pending_human_correction` state that the authenticated user is authorised to act on. Admins see all pending pages across all jobs; regular users see only pages from their own submitted jobs.

**Access:** Any authenticated user (`require_user`). Server-side scoping applies.

**Data displayed per queue item:**

| Field | Content |
|-------|---------|
| Page ID | Internal page identifier |
| Job ID | Linkable to job detail |
| Material type | `book` / `newspaper` / `archival_document` |
| Review reason(s) | E.g. "preprocessing_disagreement", "layout_disagreement", "single_model_layout" |
| Best output thumbnail | Thumbnail of the best available preprocessing output URI (if available) |
| Waiting since | Duration since the page entered `pending_human_correction` |
| Action | "Open Workspace" button |

**User actions:**
- Filter by review reason, material type, job ID
- Search by page ID or job ID
- Sort by waiting duration (oldest first by default)
- Paginate (server-side)
- Open correction workspace for a specific page

**Required backend endpoints:**
- `GET /v1/correction-queue?reason=&job_id=&search=&page=&page_size=` (existing endpoint; **requires addition of filter and pagination query parameters**)

**Loading state:** Skeleton rows (5 placeholders) while loading.

**Empty state:** "No pages pending correction." — a positive indicator for operators. Shown with the current filter state. If filters are active, include a "Clear filters" link.

**Error state:** Inline error banner with retry. Last successful data kept visible.

---

### 3.4 Correction Workspace

**Purpose:** Single-page interface for a human operator to inspect a page in `pending_human_correction`, compare branch preprocessing outputs, apply corrections, and submit or reject the page. This is the primary human-in-the-loop screen for resolving pipeline disagreements.

**Access:** Any authenticated user (`require_user`), scoped to pages from their own jobs. Admin users can act on any page.

**Data displayed:**

| Panel / Field | Content |
|--------------|---------|
| Image viewer | Pannable, zoomable image panel with toggle between: Original OTIFF, Best available output, Per-branch: IEP1A, IEP1B, IEP1C, IEP1D (greyed out if branch not available for this page) |
| Crop box overlay | Visual overlay on the active image showing the current crop box; draggable handles |
| Deskew angle | Numeric input and slider; current detected angle pre-populated |
| Split X | Numeric input and visual vertical line on image; clearable; shows "No split" when null |
| Review reasons | Read-only list of reasons why this page entered the queue |
| Job metadata | Job ID, page number, sub-page index (if split), material type, collection label |
| Reviewer notes | Free-text field; content stored in `human_correction_fields.reviewer_notes` in page_lineage |
| Status | Shows page current status (`pending_human_correction`) and time in queue |

**User actions:**
- Switch between image panels (original / best output / per-branch)
- Adjust crop box via drag handles on the overlay or direct numeric input
- Adjust deskew angle via slider or numeric input
- Set split_x by clicking on the image at the desired split position, or enter numeric value; clear split with a "No split" toggle
- Add or edit reviewer notes (free text)
- **Submit Correction** — validates inputs locally (x_min < x_max, y_min < y_max, deskew_angle within [-45, 45]), then calls the correction API; page transitions to `layout_detection` and re-enters the pipeline
- **Reject Page** — calls the rejection API with confirmation dialog; page transitions to `review` with `review_reasons=["human_correction_rejected"]`; used for pages too damaged or ambiguous to process

**Required backend endpoints:**
- `GET /v1/correction-queue/{job_id}/{page_number}?sub_page_index=N` (**NEW**) — workspace detail. `sub_page_index` is optional; required only when the same `page_number` has multiple sub-pages both in `pending_human_correction` — omitting it in that case returns HTTP 422.
- `POST /v1/jobs/{job_id}/pages/{page_number}/correction?sub_page_index=N` (existing) — body: `{crop_box, deskew_angle, split_x}`. Same `sub_page_index` resolution rule applies.
- `POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject?sub_page_index=N` (existing) — no required body. Same `sub_page_index` resolution rule applies.

**Loading state:** Full-page skeleton while workspace detail loads. Image panel shows a placeholder with a spinner. Branch toggle buttons are disabled until their respective image URIs have been confirmed accessible.

**Empty state:** N/A — the workspace is always opened for a specific page that exists in the queue.

**Error states:**
- Image load failure (S3 access error): show "Image unavailable" placeholder with the raw URI shown for debugging; allow other panels to remain functional
- Correction submit failure (HTTP 422): show inline field-level validation messages without losing user-entered values; allow retry
- Page already resolved by another operator (HTTP 409): show "This page was already processed by another operator." banner; disable action buttons; provide "Back to Queue" link
- Page not found (HTTP 404): redirect to correction queue with a "Page not found" toast
- Access denied (HTTP 403): "You do not have permission to act on this page." banner

---

### 3.5 Lineage Page

**Purpose:** Full audit trail for a specific page. Exposes every service invocation, consensus result, artifact URI, human correction event, and shadow evaluation link recorded during that page's processing history. Used for debugging, quality audits, and compliance review.

**Access:** Admin only (`require_admin`).

**Data displayed:**

| Section | Content |
|---------|---------|
| Page identity | job_id, page_number, sub_page_index, material_type, pipeline_mode |
| Status | Final status; `review_reasons` if applicable |
| Preprocessing branches | Per branch (IEP1A, IEP1B, IEP1C, IEP1D): `processed_image_uri`, deskew angle/residual/method, crop box/border_score/method, split decision/confidence/method, quality metrics |
| Preprocessing consensus | `overall_agreed`, `chosen_branch`, `consensus_confidence`, `route_to_rectification`, `route_to_review` |
| Rectification | If triggered: `rectified_image_uri`, `rectification_confidence`, `quality_delta` |
| Layout detection | IEP2A and IEP2B region counts, `matched_regions`, `type_histogram_match`, `agreed`, `consensus_confidence`, final canonical layout URI |
| Human correction history | `human_corrected` flag, `corrected_by` username, `corrected_at`, correction fields (`crop_box`, `deskew_angle`, `split_x`, `reviewer_notes`); `reviewed_by` and `reviewed_at` if page was rejected |
| Shadow evaluation | `shadow_eval_id` link; if present, link to shadow result detail (future: expandable panel) |
| Artifact paths | All S3 URIs with copy-to-clipboard action |

**User actions:**
- Navigate to correction workspace if page is still in `pending_human_correction`
- Copy any artifact URI to clipboard
- Expand/collapse individual lineage sections

**Required backend endpoints:**
- `GET /v1/lineage/{job_id}/{page_number}` (existing — admin only)

**Loading state:** Each section loads as a skeleton block; collapses on load completion.

**Empty state:** "No lineage record found for this page." — shown for a 404 response. Includes navigation back to the job.

**Error state:** Full-page error with retry and navigation back.

---

### 3.6 Model Evaluation & Promotion Page

**Purpose:** Allows admins to monitor offline evaluation results for IEP1 candidate models (IEP1A, IEP1B), inspect per-gate results, trigger evaluation runs, and manage the promotion and rollback lifecycle.

**Access:** Admin only (`require_admin`).

**Data displayed:**

| Section | Content |
|---------|---------|
| Production model | Current version tag, `promoted_at` timestamp, `model_versions` DB entry |
| Staging candidate | Current candidate version tag; "No candidate" if MLflow Staging is empty |
| Evaluation status | Last evaluation run timestamp; "Not yet evaluated" if no gate results exist; "Evaluation in progress" if worker is running |
| Gate results | Geometry IoU (≥ production − 0.02), Split precision (≥ production − 0.03), Structural agreement rate (≥ production − 0.05), Golden dataset (zero regressions), Latency p95 (≤ production × 1.25) — sourced from `model_versions.gate_results` |
| Promotion window | "Active" (within 2h post-promotion) or "Closed" (no recent promotion); shows pre-promotion accept rate and current accept rate for rollback decision support |
| Retraining history | Link to the Retraining page for context on when this candidate was trained |

**User actions:**
- Load evaluation results (`GET /v1/models/evaluation?candidate_tag=...`) — shows per-gate pass/fail with details
- Trigger evaluation run (`POST /v1/models/evaluate?candidate_tag=...`) — evaluation runs asynchronously; page polls for completion
- Promote with gate check (`POST /v1/models/promote?force=false`) — blocked with 409 if any gate fails; shows per-gate failure details
- Force promote (`POST /v1/models/promote?force=true`) — shown with a confirmation dialog warning "Gate checks will be bypassed"
- Manual rollback (`POST /v1/models/rollback` with `{"reason": "manual"}`) — shown with confirmation dialog; only enabled when an Archived version is available

**Required backend endpoints:**
- `GET /v1/models/evaluation?candidate_tag=...`
- `POST /v1/models/evaluate?candidate_tag=...`
- `POST /v1/models/promote?candidate_tag=...&force=false|true`
- `POST /v1/models/rollback`

**Loading state:** Gate results panel shows skeleton until `GET /v1/models/evaluation` responds. Panel shows "No evaluation data" if `gate_results` is null in `model_versions`.

**Empty state:** "No staging candidate available in MLflow. A retraining job must complete and register a candidate before evaluation can begin." Shown when no candidate tag is active in MLflow Staging.

**Error states:**
- Promote with failing gates (409): show per-gate failure detail; offer force promote as fallback
- Rollback with no Archived version (409): "No archived version is available to restore"
- Evaluation already in progress (409): show spinner with "Evaluation is running — results will appear when complete"

---

### 3.7 Retraining Page

**Purpose:** Shows the history of retraining triggers, the status of active and completed retraining jobs, and cooldown state per pipeline type. Admins use this to understand why retraining was triggered and what the outcome was.

**Access:** Admin only (`require_admin`).

**Data displayed:**

| Section | Content |
|---------|---------|
| Trigger history | Recent `retraining_triggers` rows: trigger_type, triggered_at, trigger reason/source (alertmanager alert name or manual) |
| Active/queued jobs | Jobs with `status in ("queued", "running")`: pipeline_type, status, enqueued_at |
| Completed jobs | Jobs with `status in ("completed", "failed")`: finished_at, result_model_version, result_mAP, promotion_decision (`pending` / `polling` / `approved` / `rejected`) |
| Cooldown status | Per pipeline type: shows "Cooldown active — X days remaining" or "Available" |

**User actions:**
- View detailed status of an active retraining job
- Navigate to Shadow Models page when a job enters `promotion_decision="polling"` state
- (Retraining is triggered by alertmanager webhook `POST /v1/retraining/webhook`; no manual trigger is exposed via UI to prevent accidental double-triggers)

**Required backend endpoints:**
- `GET /v1/retraining/status` (existing)

**Loading state:** Skeleton table rows while loading.

**Empty state:** "No retraining jobs have been triggered yet." Shown when both trigger history and jobs list are empty.

**Error state:** Inline error with retry. Stale data kept visible.

---

### 3.8 Regular User Portal

The regular user portal is a distinct routing subtree from the admin console. It provides job submission, job tracking, and output access. The sidebar navigation contains only portal-relevant items; admin-only screens are not rendered or linked.

#### 3.8.1 Job Submission Screen

**Purpose:** Allow an authenticated user to submit a new processing job.

**Access:** Any authenticated user (`require_user`).

**Form fields:**

| Field | Type | Notes |
|-------|------|-------|
| Page files | File upload (multi-select) | Accept `.tiff`, `.tif` only; max 1000 files per submission |
| Material type | Select: `book` / `newspaper` / `archival_document` | Required |
| Pipeline mode | Select: `preprocess` / `layout` | Default: `layout` |
| Collection label | Text input | Optional; stored as job metadata |
| Shadow mode | Toggle (on/off) | Default: off |

**User actions:**
- Select and upload TIFF files (drag-and-drop supported)
- Configure job parameters
- Submit — on success, redirect to the new job's detail screen

**Required backend endpoints:**
- `POST /v1/jobs` (existing)

**Loading state:** Upload progress bar per file; submit button disabled and shows spinner during upload and job creation request.

**Empty state:** N/A.

**Asynchronous submission behaviour:** `POST /v1/jobs` is asynchronous. The API creates the job and enqueues all pages immediately, then returns `HTTP 201` with `status="queued"`. Processing happens in the background worker pool after the response is returned — pages are NOT processed inline within the upload request. After redirect to the job detail screen, the UI must poll `GET /v1/jobs/{job_id}` for status updates. For large batches the time between submission and full completion may be substantial; the UI must not imply that results are immediately available after upload completes.

**Error states:**
- Non-TIFF file selected: client-side validation error, file rejected before upload
- File count > 1000: "A single job may not exceed 1000 pages" error
- HTTP 422 from API: show server-side validation errors inline next to relevant fields

#### 3.8.2 My Jobs Screen

**Purpose:** Show the authenticated user's own jobs with status, quick navigation to detail, and link to submit a new job.

**Access:** Any authenticated user (`require_user`). Scoped server-side to `jobs.created_by = current_user.user_id`.

**Data displayed per row:**

| Column | Content |
|--------|---------|
| Job ID | Truncated; click to copy |
| Submitted | Relative timestamp |
| Pipeline mode | Badge |
| Pages | Total count |
| Status summary | Accepted / Total, pending corrections count; a `queued` or `running` badge when the job has pages not yet in a terminal state |
| Actions | View Detail |

**User actions:**
- Filter by status and date range
- Search by job ID
- Paginate
- Click row to view job detail
- "Submit New Job" button links to the submission screen

**Large-batch expectation:** Jobs with many pages may remain in a partially-processed state for an extended period. The list must display an active progress indicator (e.g., a running badge or partial completion fraction) for any job that has non-terminal pages, so users understand processing is ongoing and do not resubmit the same job assuming failure.

**Required backend endpoints:**
- `GET /v1/jobs?page=&page_size=&search=&status=` (**NEW** list endpoint; server-side scoped to own jobs for regular users)

**Loading state:** Table skeleton.

**Empty state:** "You haven't submitted any jobs yet." with a prominent "Submit a Job" button.

**Error state:** Inline error with retry.

#### 3.8.3 Job Detail / Output Screen

**Purpose:** View the status and results of a specific job, including per-page processing outcomes and output artifact links.

**Access:** Any authenticated user (`require_user`). Scoped to own jobs; admin may view any job.

**Data displayed:**

| Section | Content |
|---------|---------|
| Job header | Job ID, submitted at, pipeline mode, material type, shadow mode flag, page count |
| Pages table | Page number, status badge, review reasons (if `review` or `failed`), output artifact link (download PTIFF or layout JSON for `accepted` / `preprocessing_complete` pages) |
| Pending corrections panel | Pages in `pending_human_correction` — shown with an "Open Workspace" button; non-zero count shown with an orange badge |
| Pages in review / failed | Pages in `review` or `failed` — shows `review_reasons` list; these pages require admin intervention and are read-only for regular users |

**User actions:**
- Download output artifact (preprocessed PTIFF or layout JSON URI)
- Navigate to correction workspace for pages in `pending_human_correction` (own job pages only)
- Navigate back to My Jobs

**Required backend endpoints:**
- `GET /v1/jobs/{job_id}` (existing — scoped by auth)

**Loading state:** Skeleton rows for pages table.

**Empty state:** A job always has at least one page (`JobCreateRequest.pages` min_length=1). An empty pages table is not a valid state.

**Large-batch progress display (required):** Because job processing is asynchronous and may take substantial time for large TIFF batches, the job detail screen must:
- Display a `queued` or `running` progress indicator (e.g., a progress bar showing `X / total pages completed`) whenever the job has pages not yet in a terminal state.
- Update page statuses incrementally as individual pages complete — do not require the full job to finish before showing partial results; each completed page should become visible as soon as its status transitions to a terminal state on the next poll.
- Never display a "processing complete" or "results ready" message immediately after upload; the redirect from the submission screen lands on this screen in a `queued` state and status updates must arrive via polling.
- Show the time elapsed since job submission to set correct expectations for large batches.

**Polling behaviour:** The UI polls `GET /v1/jobs/{job_id}` on a reasonable interval (e.g., every 5–10 seconds) while the job has non-terminal pages. Polling stops once all pages are in terminal states (`accepted`, `preprocessing_complete`, `review`, `failed`, `split`).

**Error states:**
- 404: "Job not found or you do not have access to this job." with a "Back to My Jobs" link
- 500: Inline error with retry

---

## 4. UI-to-API Mapping

| Screen | UI Interaction | Method + Endpoint | Status |
|--------|---------------|-------------------|--------|
| Login | Submit login form | `POST /v1/auth/token` | Existing |
| Admin Dashboard | KPI cards load | `GET /v1/admin/dashboard-summary` | **NEW** |
| Admin Dashboard | Pipeline health bars | `GET /v1/admin/service-health` | **NEW** |
| Admin Dashboard | "View Failed Jobs" quick link | `GET /v1/jobs?status=failed&page_size=5` | **NEW** list endpoint |
| Admin Jobs | Load jobs table | `GET /v1/jobs?search=&status=&pipeline_mode=&page=&page_size=` | **NEW** |
| Admin Jobs | Open job detail | `GET /v1/jobs/{job_id}` | Existing |
| Correction Queue | Load queue list | `GET /v1/correction-queue?reason=&job_id=&search=&page=&page_size=` | Existing; needs filter/pagination params |
| Correction Queue | Click "Open Workspace" | Navigate to workspace route; triggers workspace detail load | — |
| Correction Workspace | Load workspace detail | `GET /v1/correction-queue/{job_id}/{page_number}` | **NEW** |
| Correction Workspace | Submit correction | `POST /v1/jobs/{job_id}/pages/{page_number}/correction` | Existing |
| Correction Workspace | Reject page | `POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject` | Existing |
| Lineage | Load lineage detail | `GET /v1/lineage/{job_id}/{page_number}` | Existing (admin only) |
| Model Evaluation | Load evaluation results | `GET /v1/models/evaluation?candidate_tag=...` | **NEW** |
| Model Evaluation | Trigger evaluation | `POST /v1/models/evaluate` | **NEW** |
| Model Evaluation | Promote candidate | `POST /v1/models/promote` | **NEW** |
| Model Evaluation | Rollback | `POST /v1/models/rollback` | **NEW** |
| Retraining | Load status | `GET /v1/retraining/status` | Existing |
| Settings | Load policy | `GET /v1/policy` | Existing (admin only) |
| Settings | Save policy changes | `PATCH /v1/policy` | Existing (admin only) |
| User Portal — Submit | Submit job form | `POST /v1/jobs` | Existing |
| User Portal — My Jobs | Load jobs table | `GET /v1/jobs?page=&page_size=` (scoped) | **NEW** list endpoint |
| User Portal — Job Detail | Load job | `GET /v1/jobs/{job_id}` | Existing |
| User Portal — Corrections | Load own pending queue | `GET /v1/correction-queue` (scoped) | Existing |
| User Portal — Workspace | Load workspace detail | `GET /v1/correction-queue/{job_id}/{page_number}` | **NEW** |
| User Portal — Workspace | Submit correction | `POST /v1/jobs/{job_id}/pages/{page_number}/correction` | Existing |
| User Portal — Workspace | Reject page | `POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject` | Existing |

---

## 5. Backend Implications

### 5.1 Existing Endpoints Sufficient As-Is

These endpoints are fully specified in `pre-implementation_spec.md` v2.1 and A.19. No changes are needed to serve the UI requirements above.

| Endpoint | UI Usage | Auth |
|----------|---------|------|
| `POST /v1/auth/token` | Login | None |
| `GET /v1/jobs/{job_id}` | Job detail (admin + user portal) | `require_user` |
| `POST /v1/jobs` | Job submission | `require_user` |
| `POST /v1/jobs/{job_id}/pages/{page_number}/correction` | Correction workspace | `require_user` |
| `POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject` | Correction workspace | `require_user` |
| `GET /v1/lineage/{job_id}/{page_number}` | Lineage page | `require_admin` |
| `GET /v1/models/evaluation` | Model evaluation page | `require_admin` |
| `POST /v1/models/evaluate` | Model evaluation page | `require_admin` |
| `POST /v1/models/promote` | Model evaluation page | `require_admin` |
| `POST /v1/models/rollback` | Model evaluation page | `require_admin` |
| `GET /v1/retraining/status` | Retraining page | `require_admin` |
| `GET /v1/policy` | Settings page | `require_admin` |
| `PATCH /v1/policy` | Settings page | `require_admin` |
| `POST /v1/users` | (Admin user management, future UI task) | `require_admin` |
| `GET /v1/users` | (Admin user management, future UI task) | `require_admin` |
| `PATCH /v1/users/{user_id}/deactivate` | (Admin user management, future UI task) | `require_admin` |

### 5.2 Existing Endpoints Requiring Enhancement

| Endpoint | Required Change |
|----------|----------------|
| `GET /v1/correction-queue` | Add query parameters: `reason` (filter by review_reason string), `job_id` (filter to a specific job), `search` (text match on page_id or job_id), `page` (1-based page offset, default 1), `page_size` (default 20, max 100). Wrap response in a pagination envelope: `{"items": [...], "total": int, "page": int, "page_size": int}`. Existing fields per item are unchanged. This is a backwards-compatible additive change (query params are optional; the envelope wraps the existing array). |

### 5.3 New Endpoints Required

All new endpoints are implemented in `services/eep/app/` and registered in `services/eep/app/main.py`.

---

#### `GET /v1/jobs`

**Auth:** `require_user`. Admins see all jobs; regular users see only jobs where `jobs.created_by = current_user.user_id`.

**Purpose:** Paginated, filterable job list for both the admin jobs page and the user portal my-jobs screen.

**Query parameters:**

| Parameter | Type | Notes |
|-----------|------|-------|
| `search` | `str \| None` | Text match on `job_id` prefix or collection label |
| `status` | `str \| None` | Filter by any `PageStatus` value OR meta-status "in_progress" (any page not terminal) / "completed" (all pages terminal) |
| `pipeline_mode` | `"preprocess" \| "layout" \| None` | Filter by pipeline mode |
| `created_by` | `UUID \| None` | **Admin only.** Filter by submitting user UUID. Regular users may not set this param; it is silently ignored if provided by a non-admin. |
| `from_date` | `date \| None` | Filter jobs created on or after this date |
| `to_date` | `date \| None` | Filter jobs created on or before this date |
| `page` | `int` | 1-based; default 1 |
| `page_size` | `int` | Default 20; max 100 |

**Response schema:**
```json
{
  "items": [
    {
      "job_id": "string",
      "created_at": "datetime",
      "created_by_username": "string",
      "pipeline_mode": "preprocess | layout",
      "shadow_mode": false,
      "page_count": 42,
      "status_counts": {
        "accepted": 30,
        "pending_human_correction": 5,
        "review": 3,
        "failed": 2,
        "queued": 2
      }
    }
  ],
  "total": 150,
  "page": 1,
  "page_size": 20
}
```

**Implementation note:** `status_counts` is a single `COUNT(*) GROUP BY status` aggregate query on `job_pages` per job. This must not issue one query per job row (N+1 pattern). Batch or JOIN-aggregate instead.

---

#### `GET /v1/admin/dashboard-summary`

**Auth:** `require_admin`.

**Purpose:** Aggregate KPIs for the admin dashboard.

**Response schema:**
```json
{
  "throughput_pages_per_hour": 1200.0,
  "auto_accept_rate": 0.847,
  "pending_corrections_count": 156,
  "active_jobs_count": 42,
  "active_workers_count": 8,
  "shadow_evaluations_count": 318
}
```

**Computation notes:**
- `throughput_pages_per_hour`: count of pages that reached a terminal state in the last 60 minutes. Implementation note: A.10 does not explicitly define an `updated_at` column on `job_pages`. Implementors must either (a) add a `status_updated_at TIMESTAMPTZ` column to `job_pages` in the UI migration, or (b) derive throughput from `page_lineage` timestamps (e.g., `human_correction_timestamp` for corrected pages, or a dedicated terminal-state timestamp). Option (a) is preferred for performance. The query pattern is `COUNT(*) FROM job_pages WHERE status = ANY(TERMINAL_PAGE_STATES) AND status_updated_at >= NOW() - INTERVAL '1 hour'`.
- `auto_accept_rate`: same computation as `eep_auto_accept_rate` Prometheus gauge (B.28) — `accepted / (accepted + review + failed)` from DB aggregate. Read from DB, not from Prometheus.
- `pending_corrections_count`: `COUNT(*) FROM job_pages WHERE status = 'pending_human_correction'`.
- `active_jobs_count`: count of jobs with at least one page not in a terminal state. `SELECT COUNT(DISTINCT job_id) FROM job_pages WHERE status NOT IN (TERMINAL_PAGE_STATES)`.
- `active_workers_count`: read from Redis `GET libraryai:worker_slots` and subtract from `max_concurrent_pages` config value. `active_workers = max_concurrent_pages - available_slots`.
- `shadow_evaluations_count`: `COUNT(*) FROM shadow_results WHERE candidate_tag = <current staging candidate tag>`. Returns 0 if no staging candidate.

---

#### `GET /v1/admin/service-health`

**Auth:** `require_admin`.

**Purpose:** Per-pipeline-stage success rates for the pipeline health widget.

**Response schema:**
```json
{
  "preprocessing_success_rate": 0.91,
  "rectification_success_rate": 0.74,
  "layout_success_rate": 0.86,
  "human_review_throughput_rate": 0.63,
  "window_hours": 1
}
```

**Computation notes:**
- `preprocessing_success_rate`: fraction of pages that passed the preprocessing consensus gate (reached `preprocessing_done` or beyond) in the last hour.
- `rectification_success_rate`: fraction of pages that triggered rectification and produced a `rectified_image_uri` (i.e., IEP1C rectification did not fail) in the last hour.
- `layout_success_rate`: fraction of pages that reached `layout_done` or `accepted` vs those that entered `layout_detection` in the last hour.
- `human_review_throughput_rate`: corrections resolved (submitted or rejected) in the last hour / pages that entered `pending_human_correction` in the last hour. Returns `0.0` if no pages entered the queue in the window (denominator is zero).
- All rates sourced from `page_lineage` and `service_invocations` aggregate queries over the last rolling hour. Returns `0.0` for a stage with no activity in the window (not null).

---

#### `GET /v1/correction-queue/{job_id}/{page_number}`

**Auth:** `require_user`. Scoped: regular users may only access pages from their own jobs (HTTP 403 otherwise). Admins may access any page.

**Purpose:** Single-page detail for the correction workspace. Provides all data needed to render the operator interface: original image, best output, per-branch outputs, current correction parameters.

**Path parameters:** `job_id` (str), `page_number` (int).

**Query parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sub_page_index` | int | Conditional | Identifies which sub-page to load when `page_number` has multiple sub-pages in `pending_human_correction`. Omit when only one sub-page matches (the single match is returned automatically). If omitted when multiple sub-pages are pending, returns HTTP 422. |

**Response schema:**
```json
{
  "job_id": "string",
  "page_number": 1,
  "sub_page_index": null,
  "material_type": "book",
  "review_reasons": ["preprocessing_disagreement"],
  "original_otiff_uri": "s3://bucket/jobs/{job_id}/input/{page_number}.tiff",
  "best_output_uri": "s3://bucket/jobs/{job_id}/preprocessed/{page_number}.tiff",
  "branch_outputs": {
    "iep1a": "s3://bucket/...",
    "iep1b": "s3://bucket/...",
    "iep1c": "s3://bucket/...",
    "iep1d": null
  },
  "current_crop_box": [100, 80, 2400, 3200],
  "current_deskew_angle": 1.3,
  "current_split_x": null
}
```

**Data sources:**
- `original_otiff_uri`: `job_pages.input_image_uri` (or `page_lineage.input_image_hash` → constructed path)
- `best_output_uri`: `job_pages.output_image_uri`
- `branch_outputs`: `SELECT service_name, processed_image_uri FROM service_invocations WHERE job_id = ? AND page_number = ?` — one row per branch
- `current_crop_box` / `current_deskew_angle` / `current_split_x`: from the chosen branch's result in `page_lineage` or `consensus_log`
- `review_reasons`: `job_pages.review_reasons`

**Error responses:**
- 404: page not found or not in `pending_human_correction` state
- 403: page belongs to another user's job and caller is not admin
- 422: `page_number` has multiple sub-pages in `pending_human_correction` and `sub_page_index` was not provided

---

## 6. Ambiguities and Decisions

The following decisions were made in the absence of explicit spec guidance. They are flagged here for implementor awareness.

| Decision | Rationale |
|----------|-----------|
| Regular users CAN access the correction queue (for own jobs) | A.19 explicitly uses `require_user` for `GET /v1/correction-queue` and both correction endpoints. The HTML mockup places correction queue in the admin console sidebar, but that reflects the admin's global view. Regular users have a scoped view in their own portal. |
| `GET /v1/jobs` single endpoint for both admin and user portal | One endpoint with server-side auth scoping is simpler than a separate `/v1/admin/jobs` vs `/v1/my-jobs`. Admin param `created_by` is silently ignored for non-admin callers. |
| `GET /v1/correction-queue/{job_id}/{page_number}` as a new endpoint | The existing `GET /v1/correction-queue` returns a list; the workspace needs single-page detail including branch-level `processed_image_uri` values from `service_invocations`. A dedicated detail endpoint is cleaner than reusing the lineage endpoint (which is admin-only and returns more data than needed). |
| `GET /v1/admin/service-health` and `GET /v1/admin/dashboard-summary` are separate | Dashboard KPIs and service health are computed from different data sources (DB aggregates vs pipeline metrics). Separating them allows the frontend to load them in parallel and handle partial failure independently. |
| Settings screen maps to existing `GET/PATCH /v1/policy` | Policy edit is already specified in the spec. No new endpoint needed for a basic settings screen. |
| User management (admin) deferred to a later UI task | The backend endpoints exist (A.19). The UI for user management is straightforward but not shown in the HTML mockups. It is tracked as a separate UI task (UI.16) in the checklist. |
| `active_workers_count` computed from Redis semaphore | `available_slots = GET libraryai:worker_slots`; `active = max_concurrent_pages - available_slots`. This is an approximation consistent with B.17C backpressure logic. |
| Reviewer notes stored in `human_correction_fields.reviewer_notes` | `page_lineage.human_correction_fields` is JSONB. Storing reviewer notes there avoids adding a new column and is consistent with the audit trail purpose of that field. |
