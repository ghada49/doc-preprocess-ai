# LibraryAI — Project-Level Outcome Spec

---

## 1. Goal

LibraryAI receives raw scanner output (OTIFF) from AUB Library staff, processes it through a quality-gated preprocessing and layout-detection pipeline, and produces corrected archival-quality artifacts (PTIFF + layout JSON where applicable).

Every page must end in either:
- a traceable final outcome, or
- an explicit active state requiring further action.

No incorrect page may be silently accepted, and no page may be silently lost.

---

## 2. Inputs

| Input | Type | Notes |
|---|---|---|
| Raw OTIFF | File via presigned S3 upload | Immutable after upload; never overwritten |
| Job configuration | JSON in `POST /v1/jobs` | Includes `collection_id`, `material_type`, `pipeline_mode`, `ptiff_qa_mode`, `policy_version` |
| Human correction payload | JSON in `POST /v1/jobs/{job_id}/pages/{page_number}/correction` | Corrected crop bounds, deskew angle, split position, confirmation |
| Human correction rejection | `POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject` | Routes page to `review` |
| PTIFF QA actions | `POST .../ptiff-qa/approve`, `.../approve-all`, `.../edit` | Reviewer approval or edit routing |
| JWT bearer token | `Authorization: Bearer <token>` | Required on all protected endpoints except `POST /v1/auth/token` |

### Job configuration enum rules

- `material_type` must be exactly one of:
  - `book`
  - `newspaper`
  - `archival_document`
- `pipeline_mode` must be exactly one of:
  - `preprocess`
  - `layout`
- `ptiff_qa_mode` must be exactly one of:
  - `manual`
  - `auto_continue`

Capture modality such as microfilm is collection metadata, not `material_type`.

---

## 3. Outputs

| Output | Type | Notes |
|---|---|---|
| Processed PTIFF artifact | TIFF file in S3 | One per leaf page, or per child page for split spreads |
| Layout JSON artifact | JSON file in S3 | One per leaf page; only for `pipeline_mode="layout"` |
| Job status response | JSON from `GET /v1/jobs/{job_id}` | Derived from leaf-page states |
| Per-page lineage | JSON from `GET /v1/lineage/{job_id}/{page_number}` | Complete audit trail: service calls, geometry selection, artifact URIs, human events |
| Correction queue | JSON from `GET /v1/correction-queue` | Pages in `pending_human_correction` with workspace payload |
| PTIFF QA status | JSON from `GET /v1/jobs/{job_id}/ptiff-qa` | Per-page QA state and job-level gate status |
| Prometheus metrics | Text from `GET /metrics` | Exposed by each service |
| Grafana dashboards | Rendered in Grafana | Pipeline health, acceptance rates, drift signals |

---

## 4. Non-Functional Requirements

### Authentication
- All protected endpoints except `POST /v1/auth/token` require a valid JWT.
- Missing or expired token must return `401`.

### RBAC
- `user` role: job submission, job status, correction actions, PTIFF QA actions allowed by workflow.
- `admin` role: admin dashboard, policy management, user management, promotion/rollback, service health.
- Wrong role must return `403`.

### Rate limiting
- Rate limiting must be enforced per `caller_id` derived from the JWT `sub` claim, not from an API key.

### Traceability
- Every page must have a lineage record.
- Every quality-gate decision must be logged to `quality_gate_log`.
- No pipeline stage may bypass lineage or gate logging.

### No silent page loss
- Every page submitted in a job must either:
  - reach a final leaf outcome, or
  - remain in a visible active non-terminal state.
- No page may disappear from the database.

### No OTIFF mutation
- Raw OTIFF objects in storage must never be overwritten or destructively modified by the pipeline.

### DB-first artifact semantics
Every durable artifact write must follow this protocol:
1. Record pending artifact intent in the DB
2. Write artifact to S3-compatible storage
3. Confirm artifact state in the DB

A confirmed artifact URI must be retrievable.

### Idempotency
- Worker restart or retry must not double-process a page.
- Worker restart or retry must not create duplicate child pages or duplicate durable artifacts.

### Recovery
- Abandoned or stuck pages must be detectable and safely re-queueable by recovery services.

### Failure classification
- Content failures such as bad geometry, disagreement, failed artifact validation, or low confidence must route to `pending_human_correction`.
- Only unrecoverable infrastructure or data-integrity failures may route to `failed`.

### Safety — IEP1
- No single-model auto-acceptance.
- First-pass disagreement may continue through the required rescue path.
- Second-pass disagreement must route to `pending_human_correction`.
- Confidence must never override structural-agreement requirements.

### Safety — IEP2
- No single-model auto-acceptance.
- Single-model fallback must force `agreed=False`.
- IEP2B unavailability must not allow auto-acceptance.

### Observability
- Prometheus scraping, Alertmanager rules, and Grafana dashboards must exist and function.
- Errors and routing decisions must be externally visible through logs, metrics, and lineage.

---

## 5. Constraints (Immutable)

- Architecture is fixed: EEP is the sole orchestrator.
- IEP services must not make routing decisions.
- State transitions must be enforced by shared transition logic in `shared/state_machine.py`.
- Spec-defined schema fields must not be renamed.
- `material_type` is exactly `book`, `newspaper`, or `archival_document`.
- Capture modality such as microfilm is collection metadata, not `material_type`.
- IEP1A and IEP1B are mock inference services for now, but their contracts must remain real and swappable later without EEP/schema redesign.
- `ptiff_qa_pending` is a non-terminal page state and must never appear in `TERMINAL_PAGE_STATES`.
- `pending_human_correction` is worker-terminal but not final-leaf-complete; human action may return the page to active processing.
- `split` is a routing-terminal state for a spread parent only; it is not a final leaf-page outcome.
- `failed` is reserved for unrecoverable non-displayable, non-retrievable, corrupt, or integrity-breaking failure cases.
- Tech stack:
  - Python 3.11
  - FastAPI
  - uv with `pyproject.toml`
  - PostgreSQL
  - Redis with AOF enabled
  - MinIO
  - MLflow in local Docker
  - Prometheus / Alertmanager / Grafana with pinned image tags
- CPU service images may use `python:3.11-slim`.
- GPU-capable services may use appropriate CUDA/PyTorch base images according to role.

---

## 6. Job-State Derivation Rules

Job status must be derived from **leaf pages only**.

### Active non-terminal leaf states
A job is `running` if at least one leaf page is in any of these states:
- `queued`
- `preprocessing`
- `rectification`
- `layout_detection`
- `ptiff_qa_pending`

A job with any leaf page in `ptiff_qa_pending` must remain `running`, not `done`.

### Final job-status rules
- `queued`: all leaf pages are still `queued`
- `running`: at least one leaf page is in an active non-terminal state
- `failed`: all leaf pages are `failed`
- `done`: no leaf page remains in an active non-terminal state

`done` may therefore include any completed mix of:
- `accepted`
- `review`
- `failed`

---

## 7. PTIFF QA Semantics

### Core state rule
- `ptiff_qa_pending` is non-terminal.
- In manual mode, it is a **worker stop point**.
- This means workers stop automatic progression there, but the page is not terminal.

### Manual QA mode
For `ptiff_qa_mode="manual"`:
- Pages enter `ptiff_qa_pending` after successful preprocessing.
- Reviewer approval records **approval intent** only.
- Page state remains `ptiff_qa_pending` until job-level gate release.
- Reviewer edit routes the page back into correction, after which it returns to `ptiff_qa_pending`.
- Downstream processing remains blocked until the job-level QA gate is satisfied.

### Gate release
When the PTIFF QA gate becomes fully satisfied, approved pages transition in a controlled batch:
- `pipeline_mode="preprocess"` → `accepted`
- `pipeline_mode="layout"` → `layout_detection`

### Auto-continue mode
For `ptiff_qa_mode="auto_continue"`:
- Pages may transition automatically through `ptiff_qa_pending` without waiting for manual review.
- No manual reviewer action is required.

---

# Project-Level Black-Box Acceptance Tests

These tests use only external interfaces:
- HTTP API
- storage-visible artifacts
- API-visible job/lineage state
- Prometheus endpoints

No internal implementation inspection is assumed.

---

## BT-AUTH — Authentication and Authorization

| ID | Test | Pass Condition |
|---|---|---|
| BT-AUTH-01 | Call a protected endpoint without a token | `401` |
| BT-AUTH-02 | Call a protected endpoint with an expired token | `401` |
| BT-AUTH-03 | Call `GET /v1/admin/dashboard-summary` with a user-role token | `403` |
| BT-AUTH-04 | Call `POST /v1/jobs` with a valid user-role token | `201` |
| BT-AUTH-05 | Call `POST /v1/auth/token` with valid credentials | `200` with JWT containing `sub` |
| BT-AUTH-06 | Submit rapid requests from the same `sub` exceeding the configured limit | `429` on excess requests |

---

## BT-JOB — Job Lifecycle

| ID | Test | Pass Condition |
|---|---|---|
| BT-JOB-01 | `POST /v1/jobs` with valid page URIs, `pipeline_mode="preprocess"`, `ptiff_qa_mode="auto_continue"` | `201`; job created; all pages initially `queued` |
| BT-JOB-02 | `GET /v1/jobs/{job_id}` while all leaf pages are `queued` | `status="queued"` |
| BT-JOB-03 | `GET /v1/jobs/{job_id}` when all leaf pages are `accepted` | `status="done"` |
| BT-JOB-04 | `GET /v1/jobs/{job_id}` when at least one leaf page is `pending_human_correction` and none are in active worker states | `status="running"` |
| BT-JOB-05 | `GET /v1/jobs/{job_id}` when all leaf pages are in completed final states (`accepted`, `review`, `failed`) | `status="done"` |
| BT-JOB-06 | `GET /v1/jobs/{job_id}` when all leaf pages are `failed` | `status="failed"` |
| BT-JOB-07 | `POST /v1/jobs` with a missing required field | `422` |
| BT-JOB-08 | `POST /v1/jobs` with `material_type="microfilm"` | `422` |
| BT-JOB-09 | `POST /v1/uploads/jobs/presign`, upload OTIFF via returned URL, then reference URI in `POST /v1/jobs` | Job created successfully; referenced object resolves |

---

## BT-PREPROCESS — Preprocessing Pipeline Paths

| ID | Test | Pass Condition |
|---|---|---|
| BT-PP-01 | Single-page preprocess job, auto-continue, mocks agree with high confidence | Page reaches `accepted`; PTIFF artifact exists and is non-empty; artifact confirmed |
| BT-PP-02 | First-pass disagreement, second-pass agreement | Page reaches `accepted` after rescue; lineage includes rectification and both geometry passes |
| BT-PP-03 | First-pass disagreement, second-pass disagreement | Page reaches `pending_human_correction`; review reasons include second-pass disagreement; page appears in correction queue |
| BT-PP-04 | IEP1D unavailable after low-trust first pass | Page reaches `pending_human_correction` with rectification failure reason |
| BT-PP-05 | Geometry services fail according to retry/failure policy | Final externally visible route matches the authoritative failure-classification and retry contract |
| BT-PP-06 | Two-page spread with both children valid | Parent reaches `split`; two child pages created; both children reach `accepted`; two PTIFF artifacts exist |
| BT-PP-07 | Spread with one child passing and one child failing validation | Parent reaches `split`; passing child `accepted`; failing child `pending_human_correction` |
| BT-PP-08 | Geometry fails all sanity checks | Page reaches `pending_human_correction` with sanity-failure reason |
| BT-PP-09 | Split-required page with split confidence below threshold | Page reaches `pending_human_correction` with split-confidence reason |
| BT-PP-10 | Same logical page submitted again with different SHA-256 hash | Page reaches `failed` for hash mismatch; original OTIFF not overwritten |
| BT-PP-11 | Worker crashes mid-processing; recovery runs | Page is safely recovered/re-queued and eventually reaches a valid visible state without duplicate artifacts |

---

## BT-PTIFFQA — PTIFF QA Checkpoint

| ID | Test | Pass Condition |
|---|---|---|
| BT-QA-01 | Auto-continue preprocess job passes preprocessing | Page transitions through `ptiff_qa_pending` and reaches `accepted` automatically |
| BT-QA-02 | Manual preprocess job passes preprocessing | Page enters `ptiff_qa_pending` and stays there until reviewer action |
| BT-QA-03 | Manual mode: approve one page | Page remains `ptiff_qa_pending`; approval intent recorded |
| BT-QA-04 | Manual preprocess job: approve all remaining pages | Gate releases; all approved pages transition to `accepted` in one controlled batch |
| BT-QA-05 | Manual layout job: approve all pages | Gate releases; all approved pages transition to `layout_detection` |
| BT-QA-06 | Call `approve-all` on a mixed-state job | Only pages currently in `ptiff_qa_pending` are affected |
| BT-QA-07 | Call `edit` on a page in `ptiff_qa_pending` | Page enters correction flow and later returns to `ptiff_qa_pending` |
| BT-QA-08 | Manual layout job before gate release | No layout invocation appears in lineage before PTIFF QA gate release |

---

## BT-CORRECTION — Human Correction Workflow

| ID | Test | Pass Condition |
|---|---|---|
| BT-COR-01 | `GET /v1/correction-queue` after page reaches `pending_human_correction` | Page appears with workspace payload including editable fields, reasons, and branch references |
| BT-COR-02 | Submit valid single-page correction | Page transitions to `ptiff_qa_pending`; new PTIFF artifact written; lineage records human correction |
| BT-COR-03 | Submit valid split correction | Parent transitions to `split`; child pages created; each child enters PTIFF QA flow independently |
| BT-COR-04 | Submit correction rejection | Page transitions to `review`; removed from correction queue |
| BT-COR-05 | Submit invalid correction payload | `422`; page remains in `pending_human_correction` |
| BT-COR-06 | Workspace response for page with all branch artifacts available | Response includes all branch outputs and source references without frontend reconstruction |

---

## BT-LAYOUT — Layout Detection Pipeline

| ID | Test | Pass Condition |
|---|---|---|
| BT-LAY-01 | `pipeline_mode="layout"` and both layout models agree | Page reaches `accepted`; layout JSON exists and is non-empty; canonical region types only |
| BT-LAY-02 | IEP2B unavailable while IEP2A returns results | Page reaches review route; single-model auto-acceptance does not occur |
| BT-LAY-03 | IEP2A and IEP2B disagree below required consensus | Page reaches review route for layout-consensus failure |
| BT-LAY-04 | IEP2A fails entirely | Page reaches review route for layout-detection failure |
| BT-LAY-05 | Manual PTIFF QA layout job | No layout detection occurs before PTIFF QA gate release |

---

## BT-LINEAGE — Audit Trail

| ID | Test | Pass Condition |
|---|---|---|
| BT-LIN-01 | `GET /v1/lineage/{job_id}/{page_number}` after page accepted | Includes input hash, geometry invocations, selection result, normalization, confirmed artifact URI, state-transition history |
| BT-LIN-02 | Lineage for page that used rectification | Includes IEP1D invocation, second geometry pass, second normalization |
| BT-LIN-03 | Lineage for human-corrected page | Includes human correction event with actor identity and timestamp |
| BT-LIN-04 | Lineage for page routed to correction without human action | Includes review reasons and gate decision responsible |

---

## BT-SAFETY — Safety Invariants

| ID | Test | Pass Condition |
|---|---|---|
| BT-SAF-01 | IEP1A high confidence, IEP1B fails | No first-pass auto-acceptance; required rescue flow still applies |
| BT-SAF-02 | Second-pass geometry disagreement on structure | Page must route to `pending_human_correction` |
| BT-SAF-03 | IEP2B unavailable, IEP2A high confidence | Page must not be accepted; single-model layout acceptance prohibited |
| BT-SAF-04 | Content/quality failure | Routes to `pending_human_correction`, not `failed` |
| BT-SAF-05 | OTIFF object read during processing | Raw OTIFF bytes unchanged before and after job |
| BT-SAF-06 | Illegal state transition attempted | Transition rejected; page state unchanged |

---

## BT-MLOPS — MLOps and Shadow Evaluation

| ID | Test | Pass Condition |
|---|---|---|
| BT-ML-01 | Shadow mode enabled and sampling condition met | Shadow task enqueued; live routing unaffected |
| BT-ML-02 | Shadow enqueue fails | Live routing unaffected; error logged |
| BT-ML-03 | Promote valid candidate model version | Model version record updated; live component uses promoted version |
| BT-ML-04 | Roll back model version | Previous active version restored |
| BT-ML-05 | Retraining webhook received | Retraining trigger recorded and retraining worker processes it |

---

## BT-OBS — Observability

| ID | Test | Pass Condition |
|---|---|---|
| BT-OBS-01 | `GET /metrics` after processing a job | Prometheus text returned; includes required counters/histograms |
| BT-OBS-02 | Prometheus scraping configured | All intended targets show UP |
| BT-OBS-03 | Grafana accessible | At least one dashboard renders live data |
| BT-OBS-04 | Alertmanager reachable | Alert API responds successfully |
| BT-OBS-05 | Drift detector skeleton loaded | Relevant metrics exposed; no runtime failure |

---

## BT-DEPLOY — Deployment and Infrastructure

| ID | Test | Pass Condition |
|---|---|---|
| BT-DEP-01 | `docker compose up` cold start | All declared containers reach running state |
| BT-DEP-02 | `GET /health` on every service | `200` from all services |
| BT-DEP-03 | `GET /ready` on every service after boot in Phase 0 | `200` from all Phase 0 skeleton services; later phases may tighten readiness |
| BT-DEP-04 | Cross-service Python imports inside containers | `import shared` and `import services.eep.app.db` resolve correctly |
| BT-DEP-05 | Redis AOF persistence enabled | Redis config reports append-only enabled |
| BT-DEP-06 | Kubernetes manifests dry-run validation | No schema/validation errors |
| BT-DEP-07 | CI pipeline on clean push | Lint, type-check, and tests complete successfully |
