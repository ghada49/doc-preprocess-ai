# LibraryAI — Frontend & Product Specification v2.0

**Scope:** Web application layer only
**System:** LibraryAI
**Audience:** Frontend engineers, product designers, backend integrators
**Backend source of truth:** EEP API contract and implemented auth/scoping rules. Behaviour must follow the live API contract, not legacy mockup assumptions.

---

## 1. Product Overview

LibraryAI is a web application for managing a document digitization pipeline with AI-assisted preprocessing, layout detection, human correction, PTIFF QA, lineage inspection, and model lifecycle operations.

The frontend has **two product surfaces**:

* a **Regular User Portal** for submitting jobs, tracking progress, viewing outputs, and resolving pages from the user’s own jobs
* an **Admin Console** for operational oversight, correction management across all jobs, lineage inspection, policy management, model evaluation, retraining visibility, and user administration

The UI must communicate three things clearly:

* **operational control**
* **traceability**
* **safety-first workflow**

This is not a generic dashboard. It is an operational console for a stateful, asynchronous, auditable pipeline.

---

## 2. Product Principles

### 2.1 State-first UI

The UI must reflect the real backend state machine. It must never imply synchronous processing where the backend is asynchronous.

### 2.2 No fake completeness

If a backend field is nullable or a feature is not ready, the UI must show a real empty or pending state instead of inventing values.

### 2.3 Human-in-the-loop clarity

Pages needing human correction or PTIFF QA must be visually obvious. The UI must clearly explain why operator action is required.

### 2.4 Explainability over decoration

The UI should be impressive, but the “wow” must come from:

* clarity of system status
* visibility of branch outputs and decisions
* strong correction tooling
* rich lineage and MLOps visibility

---

## 3. Roles

### 3.1 Regular User

A library staff member or job submitter. Regular users may self-register through the public signup page — no admin action is required to create a regular user account.

**Capabilities**

* self-register via the public signup page (`/signup`)
* log in
* submit jobs
* view own jobs only
* view own job details and artifacts
* access correction queue entries belonging to own jobs
* open correction workspace for own-job pages
* submit corrections for own-job pages
* reject own-job pages from correction
* access PTIFF QA for own jobs

**Restrictions**

* cannot access admin dashboard
* cannot access global jobs list
* cannot access lineage
* cannot access policy settings
* cannot access model evaluation, promotion, rollback, or retraining pages
* cannot manage users

### 3.2 Admin

A system overseer, senior operator, or MLOps user. Admin accounts are created only through the `scripts/create_admin.py` bootstrap script or by an authenticated admin via `POST /v1/users`. There is no public signup path for admin accounts.

**Capabilities**

* all regular user capabilities
* view all jobs
* access global correction queue
* open correction workspace for any page
* inspect lineage
* access admin dashboard and service health
* manage policy
* manage model evaluation, promotion, rollback
* view retraining status
* manage users

Role scoping must follow the backend contract exactly. For example, many endpoints use `require_user` with ownership checks, while admin-only endpoints use `require_admin`.

---

## 4. Information Architecture

## 4.1 Regular User Portal

* Signup
* Login
* Submit Job
* My Jobs
* Job Detail
* My Correction Queue
* Correction Workspace
* PTIFF QA

## 4.2 Admin Console

* Dashboard
* Jobs
* Correction Queue
* Correction Workspace
* Lineage
* Model Evaluation
* Retraining
* Settings / Policy
* Users

---

## 5. Global UX Rules

### 5.1 Polling

Job detail screens must poll while jobs have active non-terminal pages. The UI must stop polling only when the backend state no longer requires it.

### 5.2 Artifact rendering

APIs return raw `s3://` URIs. The frontend must use the artifact presign-read flow before attempting browser display or download.

### 5.3 Permission-aware navigation

The app shell must hide screens the current role cannot access.

### 5.4 Error handling

Errors must be contextual:

* field-level errors for invalid form input
* inline widget errors for partial failures
* full-screen errors only when the entire page cannot function

### 5.5 Null handling

Nullable fields must be handled explicitly. Example: in correction workspace, `current_deskew_angle` may be null and must not be assumed present.

---

## 6. Permission Matrix

| Screen / Action                 |  Regular User  | Admin |
| ------------------------------- | :------------: | :---: |
| Signup (self-register)          |       Yes      |  No   |
| Login                           |       Yes      |  Yes  |
| Submit job                      |       Yes      |  Yes  |
| View own jobs                   |       Yes      |  Yes  |
| View all jobs                   |       No       |  Yes  |
| View own job detail             |       Yes      |  Yes  |
| View any job detail             |       No       |  Yes  |
| View own correction queue items |       Yes      |  Yes  |
| View global correction queue    |       No       |  Yes  |
| Open own correction workspace   |       Yes      |  Yes  |
| Open any correction workspace   |       No       |  Yes  |
| Submit correction               | Yes (own jobs) |  Yes  |
| Reject page from correction     | Yes (own jobs) |  Yes  |
| PTIFF QA for own jobs           |       Yes      |  Yes  |
| PTIFF QA for any job            |       No       |  Yes  |
| Dashboard                       |       No       |  Yes  |
| Lineage                         |       No       |  Yes  |
| Model evaluation                |       No       |  Yes  |
| Retraining                      |       No       |  Yes  |
| Policy settings                 |       No       |  Yes  |
| User management                 |       No       |  Yes  |

These rules must match the API role scoping table.

---

## 7. Core Backend Concepts the Frontend Must Respect

### 7.1 Jobs are asynchronous

`POST /v1/jobs` creates the job and enqueues work. Processing happens later. The frontend must not present results as instantly ready after upload.

### 7.2 Page states are first-class

The UI must understand and display page states:

* `queued`
* `preprocessing`
* `rectification`
* `ptiff_qa_pending`
* `layout_detection`
* `pending_human_correction`
* `accepted`
* `review`
* `failed`
* `split`

### 7.3 PTIFF QA is a real gate

PTIFF QA is not cosmetic. In manual mode, pages can remain at `ptiff_qa_pending` until gate release. The UI must present that correctly.

### 7.4 Artifact access is indirect

The frontend must call `POST /v1/artifacts/presign-read` to obtain browser-ready URLs for images or JSON artifacts.

---

## 8. Regular User Portal Screens

## 8.0 Signup

**Purpose**
Allow a new user to self-register a regular user account without admin assistance.

**Endpoint**

* `POST /v1/auth/signup`

**Form fields**

* username
* password
* confirm password

**Behaviour**

* client-side: validate that password and confirm password match before submission
* on success: redirect to `/login` with a success message
* role is always `"user"` — the server enforces this; no role field is sent from the client

**States**

* loading on submit
* duplicate username (409) — inline field error
* validation failure (422) — inline error banner
* network error — inline error banner

**Link**

* `/signup` must link back to `/login`
* `/login` must link to `/signup` ("Don't have an account? Sign up")

**Important**

* there is no public signup path for admin accounts
* admins are created by the bootstrap script or by another admin via the Users admin console

---

## 8.1 Login

**Purpose**
Authenticate the user and start a role-aware session.

**Endpoint**

* `POST /v1/auth/token`

**Behaviour**

* on success: store bearer token securely in frontend session storage strategy
* decode role if needed for routing
* redirect user to appropriate landing page

**States**

* loading on submit
* invalid credentials
* network error

---

## 8.2 Submit Job

**Purpose**
Allow a user to upload TIFF pages and create a processing job.

**Required fields**

* page files
* collection_id
* material_type
* pipeline_mode
* ptiff_qa_mode
* policy_version
* optional shadow_mode

**Backend flow**

1. Call `POST /v1/uploads/jobs/presign`
2. Upload each TIFF to the returned `upload_url`
3. Collect `object_uri` values
4. Call `POST /v1/jobs` with those URIs as page inputs

**Important UX**

* this is a two-step upload flow
* show per-file upload progress
* only enable final submission when uploads complete successfully

**Success**

* redirect to the job detail page in `queued` state

**Errors**

* invalid file type
* upload failure
* job creation validation error
* network error

---

## 8.3 My Jobs

**Purpose**
List jobs visible to the current user.

**Endpoint**

* `GET /v1/jobs`

**Query parameters used**

* `search`
* `status`
* `pipeline_mode`
* `page`
* `page_size`

**Displayed columns**

* job_id
* collection_id
* material_type
* pipeline_mode
* ptiff_qa_mode
* status
* page_count
* accepted_count
* review_count
* failed_count
* pending_human_correction_count
* created_at
* completed_at

**Actions**

* open job detail
* submit new job

**Loading**

* skeleton rows

**Empty**

* “You have not submitted any jobs yet.”

---

## 8.4 Job Detail

**Purpose**
Show full job status with page-by-page detail and artifact access.

**Endpoint**

* `GET /v1/jobs/{job_id}`

**Displayed**

* job summary
* per-page rows
* page status badges
* review reasons
* artifact availability
* pending correction count
* PTIFF QA state where relevant

**Actions**

* open correction workspace for pages in `pending_human_correction`
* open PTIFF QA page if pages are in `ptiff_qa_pending`
* preview/download artifacts using presign-read flow

**Polling**

* every 5–10 seconds while active pages exist

**Important UX**

* show partial completion; do not wait for full job completion to display results
* do not show “done” unless the backend says so

---

## 8.5 My Correction Queue

**Purpose**
Show pages from the user’s own jobs currently in `pending_human_correction`.

**Endpoint**

* `GET /v1/correction-queue`

**Supported params**

* `job_id`
* `material_type`
* `review_reason`
* `page`
* `page_size`
* legacy `offset` / `limit` support also exists, but frontend should prefer `page` / `page_size`

**Displayed fields**

* job_id
* page_number
* sub_page_index
* material_type
* review_reasons
* waiting_since
* output_image_uri

**Actions**

* open correction workspace

---

## 8.6 Correction Workspace

**Purpose**
Allow the user to inspect and resolve pages in `pending_human_correction`.

**Endpoints**

* `GET /v1/correction-queue/{job_id}/{page_number}`
* `POST /v1/jobs/{job_id}/pages/{page_number}/correction`
* `POST /v1/jobs/{job_id}/pages/{page_number}/correction/reject`

**Workspace layout**

### Left panel

Source selection:

* Original OTIFF
* Best available output
* Branch outputs:

  * IEP1A geometry/output context
  * IEP1B geometry/output context
  * IEP1C normalized output
  * IEP1D rectified output if available

### Center panel

Interactive viewer:

* zoom
* pan
* crop box overlay
* split line overlay
* optional compare mode between original and processed

### Right panel

Editable controls:

* crop box
* deskew angle
* split_x
* reviewer notes
* page metadata
* review reasons

**Important nullable behaviour**

* `current_deskew_angle` may be null, especially for fresh correction routing
* branch outputs may be null
* best_output_uri may be null
* crop/split values may be absent if no geometry is available

**Submit Correction**
Request body:

```json
{
  "crop_box": [x1, y1, x2, y2],
  "deskew_angle": 1.5,
  "split_x": null
}
```

**Reject Page**
Routes page to `review`

**Errors**

* 404 page not found
* 409 page no longer pending
* 422 missing sub_page_index when required
* 403 not authorized

---

## 8.7 PTIFF QA

**Purpose**
Support manual PTIFF QA review for the user’s own jobs.

**Endpoints**

* `GET /v1/jobs/{job_id}/ptiff-qa`
* `POST /v1/jobs/{job_id}/ptiff-qa/approve-all`
* `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve`
* `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit`
* alias: `.../edit-and-return` also exists

**Displayed**

* page list
* current_state
* approval_status
* needs_correction
* pages_pending
* pages_approved
* pages_in_correction
* is_gate_ready

**Actions**

* approve one page
* approve all
* send page back to correction

**Important UX**

* in manual mode, page approval may not immediately move the page out of `ptiff_qa_pending`
* gate release behaviour must reflect backend response, not frontend assumption

---

## 9. Admin Console Screens

## 9.1 Dashboard

**Purpose**
Operational overview of system health and activity.

**Endpoints**

* `GET /v1/admin/dashboard-summary`
* `GET /v1/admin/service-health`
* optionally jobs list endpoint for recent jobs panels

**Show**

* throughput_pages_per_hour
* auto_accept_rate
* structural agreement / quality indicators where available
* pending corrections
* active jobs
* active workers
* shadow / evaluation counts
* service health summary

**Widgets**

* KPI cards
* pipeline health bars
* recent jobs / quick links
* queue pressure indicators

**States**

* widget-level loading
* independent widget error handling

The dashboard is already directly supported by admin endpoints in the contract.

---

## 9.2 Jobs

**Purpose**
Global, filterable jobs table for admins.

**Endpoint**

* `GET /v1/jobs`

**Additional field available**

* `created_by_username` is present and nullable in jobs list responses

**Displayed columns**

* job_id
* collection_id
* material_type
* pipeline_mode
* ptiff_qa_mode
* shadow_mode
* created_by_username
* status
* page_count
* accepted_count
* review_count
* failed_count
* pending_human_correction_count
* created_at
* updated_at
* completed_at

**Actions**

* open job detail
* filter
* search
* quick jump to correction queue

---

## 9.3 Global Correction Queue

**Purpose**
Show all pages currently needing human correction.

**Endpoint**

* `GET /v1/correction-queue`

**Displayed**

* queue items across all users
* waiting time
* reason codes
* material type
* output preview availability

**Actions**

* open correction workspace
* filter by material type, job, reason

This screen maps directly to the current queue API with role scoping handled server-side.

---

## 9.4 Admin Correction Workspace

Same base behaviour as user correction workspace, but admin can access any queued page.

Additional admin-focused enhancements may include:

* visible actor ownership
* queue analytics context
* direct lineage shortcut
* job owner information

---

## 9.5 Lineage

**Purpose**
Show the complete audit trail for a page.

**Endpoint**

* `GET /v1/lineage/{job_id}/{page_number}`

**Displayed**

* lineage metadata
* service invocations
* quality gate decisions
* model branch usage
* artifact URIs
* correction history
* timestamps

**Actions**

* copy IDs / URIs
* collapse / expand sections
* navigate back to related job or correction workspace

Lineage is admin-only by contract.

---

## 9.6 Model Evaluation

**Purpose**
Show candidate model evaluation records and gate results.

**Endpoints**

* `GET /v1/models/evaluation`
* `POST /v1/models/evaluate`
* `POST /v1/models/promote`
* `POST /v1/models/rollback`

**Displayed**

* model_id
* service_name
* version_tag
* stage
* dataset_version
* mlflow_run_id
* gate_results
* gate_summary
* promoted_at
* notes

**Actions**

* filter by candidate_tag
* filter by service
* trigger evaluation
* promote
* rollback

**Important UX**

* gate_results and gate_summary may be null if evaluation has not run yet
* evaluate action returns `202 Accepted`, so UI must support pending state and refresh flow

---

## 9.7 Retraining

**Purpose**
Show retraining pipeline activity and cooldown state.

**Endpoint**

* `GET /v1/retraining/status`

**Displayed**

* summary counts
* active_jobs
* queued_jobs
* recently_completed
* trigger_cooldowns
* as_of timestamp

**Actions**

* inspect active or completed jobs
* navigate to related evaluation or model screen where relevant

**Important UX**

* many retraining summary fields are nullable by design; UI must display “Not available yet” or “—” gracefully where appropriate

---

## 9.8 Settings / Policy

**Purpose**
View and update active policy configuration.

**Endpoints**

* `GET /v1/policy`
* `PATCH /v1/policy`

**Displayed**

* version
* config_yaml
* applied_at
* applied_by
* justification

**Actions**

* edit YAML/config text
* save new version
* validation feedback

---

## 9.9 Users

**Purpose**
Admin user management.

Regular users can self-register via the public `/signup` page. This admin screen is used to create admin accounts (or additional regular accounts) and to deactivate any user account. The public signup flow and this admin console coexist: regular users from signup appear in this list.

**Endpoints**

* `POST /v1/users`
* `GET /v1/users`
* `PATCH /v1/users/{user_id}/deactivate`

**Displayed**

* username
* role
* is_active
* created_at

**Actions**

* create user (any role, including admin)
* deactivate user

---

## 10. API-to-UI Mapping

| Page                      | Endpoint(s)                                              |
| ------------------------- | -------------------------------------------------------- |
| Signup                    | `POST /v1/auth/signup`                                   |
| Login                     | `POST /v1/auth/token`                                    |
| Submit Job                | `POST /v1/uploads/jobs/presign`, `POST /v1/jobs`         |
| My Jobs / Admin Jobs      | `GET /v1/jobs`                                           |
| Job Detail                | `GET /v1/jobs/{job_id}`                                  |
| PTIFF QA                  | `GET /v1/jobs/{job_id}/ptiff-qa`, approve/edit endpoints |
| Correction Queue          | `GET /v1/correction-queue`                               |
| Correction Workspace      | queue detail endpoint + correction / reject endpoints    |
| Dashboard                 | admin summary + service health                           |
| Lineage                   | `GET /v1/lineage/{job_id}/{page_number}`                 |
| Model Evaluation          | evaluation + evaluate + promote + rollback               |
| Retraining                | `GET /v1/retraining/status`                              |
| Settings                  | policy endpoints                                         |
| Users                     | users endpoints                                          |
| Artifact preview/download | `POST /v1/artifacts/presign-read`                        |

---

## 11. Artifact Read Flow

The frontend must follow this exact flow:

1. Receive raw `s3://...` URI from a jobs, queue, workspace, or lineage response
2. Call `POST /v1/artifacts/presign-read`
3. Receive `read_url`
4. Use that URL for image display or download in browser

The frontend must never assume raw `s3://` URIs are directly browser-loadable.

---

## 12. Frontend Technical Requirements

### 12.1 Recommended stack

* React or Next.js
* TypeScript
* Tailwind CSS
* component-based architecture
* centralized API client
* auth-aware routing
* query/caching library for polling and state sync

### 12.2 State management priorities

The frontend must handle:

* auth state
* role state
* polling state
* page selection state in correction workspace
* artifact URL lifecycle
* optimistic vs confirmed action state carefully

### 12.3 Reusable components

* AppShell
* RoleAwareRoute
* StatusBadge
* KPIStatCard
* JobsTable
* QueueTable
* ArtifactPreview
* BranchSwitcher
* CorrectionEditorPanel
* LineageTimeline
* EmptyState
* ErrorState
* PagedTableControls

---

## 13. Non-Goals

The frontend does not:

* bypass backend state machine logic
* access storage directly without presigned read flow
* infer model evaluation data outside API responses
* expose admin-only functionality to regular users
* assume layout always exists for preprocess-only jobs

---

## 14. Implementation Priorities

### Phase A — Must-have

* Signup
* Login
* Submit Job
* My Jobs
* Job Detail
* Correction Queue
* Correction Workspace
* PTIFF QA
* Admin Dashboard
* Admin Jobs
* Lineage

### Phase B — High-value admin

* Model Evaluation
* Retraining
* Settings / Policy
* Users

### Phase C — Polish

* compare mode in correction workspace
* keyboard shortcuts
* richer lineage visualization
* dashboard drilldowns

---

## 15. Final Product Goal

The finished frontend should feel like an operational control plane for a real AI document pipeline.

It must look polished, but more importantly it must make these things obvious:

* what the system is doing
* what needs human attention
* why a page was routed a certain way
* how to act safely and confidently

That is the standard for “wow” in this product.
