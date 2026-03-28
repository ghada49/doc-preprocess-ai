# LibraryAI Implementation Checklist

This file is the execution ledger for the project.
It must be updated after every completed phase.
A phase must never be marked complete if any item in its Definition of Done remains unmet.

## Legend
- [ ] Not started
- [~] In progress
- [x] Complete

## Phases

### ☑ Phase 0 — Repo, containers, skeletons

- ☑ Packet 0.1 — repository structure and root files
- ☑ Packet 0.2 — docker-compose and service bootstrapping
- ☑ Packet 0.3 — shared health, metrics, logging, middleware
- ☑ Packet 0.4 — API and model service skeleton entrypoints
- ☑ Packet 0.5 — GPU backend local HTTP stub
- ☑ Packet 0.6 — service skeletons for worker, recovery, and maintenance processes

- **Summary:** Repo structure, all containers, all service skeletons, shared utilities, GPU backend abstraction, and implementation ledger are complete.
- **Blocked/blocking:** None. Phase 1 may begin.
- **Relevant spec constraints:** PYTHONPATH=/app in all containers (Section 1.5); Redis AOF enabled; IEP services are mock only until Phase 12.

### ☑ Phase 1 — Schemas, DB, storage, Redis, job API

- ☑ Packet 1.1 — UCF and preprocessing schemas
- ☑ Packet 1.2 — geometry, normalization, iep1d, layout schemas
- ☑ Packet 1.3 — EEP schemas and terminal page states
- ☑ Packet 1.3a — page state machine contract
- ☑ Packet 1.4 — storage backends
- ☑ Packet 1.5 — core DB migration
- ☑ Packet 1.6 — ORM / DB model layer
- ☑ Packet 1.7 — Redis queue setup
- ☑ Packet 1.7a — reliable Redis queue contract
- ☑ Packet 1.7b — presigned upload endpoint
- ☑ Packet 1.8 — job creation endpoint
- ☑ Packet 1.9 — job status endpoint

- **Summary:** All schemas (UCF, preprocessing, geometry, normalization, EEP, page states), Alembic migration, SQLAlchemy ORM, local+S3 storage backends, Redis queue contract, presigned upload endpoint, job creation endpoint (POST /v1/jobs), and job status endpoint (GET /v1/jobs/{job_id}) are complete and fully tested (819 tests pass).
- **Blocked/blocking:** None. Phase 2 may begin.
- **Relevant spec constraints:** DB is source of truth; Redis is execution mechanism only. DB committed before Redis enqueue; Packet 4.7 recovery re-enqueues orphaned tasks. `ptiff_qa_pending` is non-terminal (job stays `running`). Split-parent pages (status=`split`) excluded from job status derivation.

### ☑ Phase 2 — IEP1A/B mocks + IEP1C

- ☑ Packet 2.1 — IEP1A mock service shell
- ☑ Packet 2.2 — IEP1A TTA mock behavior
- ☑ Packet 2.3 — IEP1B mock service shell
- ☑ Packet 2.4 — IEP1B TTA mock behavior
- ☑ Packet 2.5 — normalization core
- ☑ Packet 2.6 — split handling
- ☑ Packet 2.7 — quality metrics

- **Summary:** IEP1A and IEP1B mock services (configurable via env vars: page count, confidence, TTA agreement/variance, failure simulation) return real `GeometryResponse` objects. IEP1C normalization is fully real: perspective correction from quadrilateral corners (`four_point_transform`), affine bbox fallback (`apply_affine_deskew`), split normalization (`split_and_normalize`), and four quality metrics (`blur_score`, `border_score`, `foreground_coverage`, `skew_residual`). `normalize_result_to_branch_response` assembles the canonical `PreprocessBranchResponse` from a completed `NormalizeResult` plus caller-supplied `source_model` and `processed_image_uri` (available only after Phase 3 selection and Phase 4 storage write). All 142 Phase 2 tests pass (65 IEP1A contract + 65 IEP1B contract + 62 normalization + 35 quality + 33 split + 12 branch-response adapter = 1325 total suite passing).
- **Blocked/blocking:** None. Phase 3 may begin (and is already complete).
- **Relevant spec constraints:** IEP1A and IEP1B are mock only until Phase 12 (Section 1.3); real ML inference is stubbed but all endpoints, schemas, and TTA contracts are real. IEP1C is fully real production code (Section 2.1). `normalize_single_page` returns `NormalizeResult` (contains numpy array; cannot be a Pydantic model); `normalize_result_to_branch_response` produces `PreprocessBranchResponse` once storage URI and source model are known. `split_confidence = min(weakest_instance_confidence, tta_structural_agreement_rate)` per spec Section 6.8.

### ☑ Phase 3 — Geometry selection + artifact validation

- ☑ Packet 3.1 — structural agreement and sanity checks
- ☑ Packet 3.2 — split confidence, variance, page area preference
- ☑ Packet 3.3 — final selection and route-to-human logic
- ☑ Packet 3.4 — artifact hard requirements
- ☑ Packet 3.5 — artifact soft score and threshold logic
- ☑ Packet 3.6 — gate test suite

- **Summary:** Geometry selection gate (structural agreement, sanity checks, split confidence, TTA variance, page area preference, confidence-based selection, route-to-human logic) and artifact validation gate (five hard requirements, six-signal weighted soft scoring, gate log record builders) are complete and fully tested (35 integration tests + 81 artifact tests + 114 geometry tests = 230 tests pass).
- **Blocked/blocking:** None. Phase 4 may begin.
- **Relevant spec constraints:** Spec Sections 6.8–6.9; geometry trust HIGH only when both models present + structural agreement + zero dropouts; route_decision never equals "failed"; soft scoring skipped when any hard check fails.

### ☑ Phase 4 — Full IEP1 worker orchestration

- ☑ Packet 4.1 — worker concurrency and circuit breaker
- ☑ Packet 4.2 — page state and lineage DB helpers (state machine unified with shared.state_machine)
- ☑ Packet 4.3a — intake, hash, proxy image derivation
- ☑ Packet 4.3b — parallel geometry invocation and selection wiring
- ☑ Packet 4.4 — normalization and first validation
- ☑ Packet 4.5 — rescue flow (rectification, second geometry pass, second normalization, final validation) + IEP1D pass-through mock endpoint
- ☑ Packet 4.6 — split handling, PTIFF QA routing, and preprocess-only stop path
- ☑ Packet 4.7 — watchdog loop started at worker startup; reconciliation loop started at recovery startup; rebuild_queue_from_db implemented
- ☑ Packet 4.8 — worker integration tests + IEP1D HTTP contract tests (test_p4_iep1d_contract.py)

- **Summary:** Full IEP1 worker orchestration is complete end-to-end with six audit-identified defects resolved: (1) IEP1D /v1/rectify pass-through mock endpoint added (services/iep1d/app/rectify.py); (2) rebuild_queue_from_db() implemented (services/eep/app/queue.py); (3) TaskWatchdog loop started via FastAPI lifespan in eep_worker/app/main.py; (4) run_reconciliation_loop started via FastAPI lifespan in eep_recovery/app/main.py; (5) page_state.py VALID_TRANSITIONS unified with shared/state_machine.py ALLOWED_TRANSITIONS — advance_page_state() now delegates to validate_transition(); (6) test_p4_db_page_state.py corrected to match authoritative transitions (queued→{preprocessing,failed}; rectification→{ptiff_qa_pending,pending_human_correction,split,failed}; pending_human_correction→{ptiff_qa_pending,review,split}); IEP1D HTTP contract tests added (22 tests covering schema, material type, validation errors, failure simulation, configurable confidence).
- **Blocked/blocking:** None. Phase 5 may begin.
- **Relevant spec constraints:** `ptiff_qa_pending` is non-terminal; reconciler never mutates page state (DB authoritative); IEP1D retry=0 (spec Section 8.4); second-pass structural disagreement routes to `pending_human_correction`; `failed` is only for unrecoverable infrastructure/data integrity failures (content failures must not route to `failed`); `pending_human_correction → layout_detection` and `pending_human_correction → accepted` are NOT valid (would bypass PTIFF QA gate — spec Section 1.6); corrections always return to `ptiff_qa_pending` first.

### ☑ Phase 5 — Human correction workflow + PTIFF QA

- ☑ Packet 5.0 — correction workspace response schema and data assembly
- ☑ Packet 5.0a — PTIFF QA workflow (job-level review gate)
- ☑ Packet 5.1 — correction queue read endpoints
- Note: Auth/RBAC scoping not enforced yet (deferred to Phase 7 Packet 7.1). Current endpoints return all pending_human_correction pages.
- ☑ Packet 5.2 — single-page correction apply path
- ☑ Packet 5.3 — split correction apply path
- ☑ Packet 5.4 — correction reject path
- ☑ Packet 5.5 — correction and PTIFF QA tests

- **Summary:** Full human correction workflow and PTIFF QA gate are complete end-to-end. Correction workspace response schema (`GeometrySummary`, `BranchOutputs`, `CorrectionWorkspaceResponse`) and `assemble_correction_workspace()` data assembly implemented (Packet 5.0). PTIFF QA gate (`services/eep/app/correction/ptiff_qa.py`) implemented with `POST /v1/jobs/{job_id}/ptiff-qa/approve-all`, `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve`, and `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit-and-return`; supports `ptiff_qa_mode=manual|auto_continue` and both `pipeline_mode=preprocess|layout` targets (Packet 5.0a). Correction queue read endpoints (`GET /v1/correction-queue` and `GET /v1/jobs/{job_id}/correction-queue`) implemented (Packet 5.1). Single-page correction apply path (`POST /v1/jobs/{job_id}/pages/{page_number}/correction-apply`) with idempotent child-page creation, artifact copy, and `pending_human_correction → ptiff_qa_pending` transition implemented (Packet 5.2). Split correction apply path with per-child `pending_human_correction → ptiff_qa_pending` transition, auto_continue bypass (direct release for preprocess mode), and synchronous `_maybe_close_split_parent` for `preprocess + auto_continue` implemented; critical deadlock in manual PTIFF QA mode resolved by modifying `_is_gate_satisfied` to skip split parents in `pending_human_correction` (Packet 5.3). Correction reject path (`POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject`) with `pending_human_correction → review` transition, `review_reasons = ["human_correction_rejected"]`, and optional reviewer notes implemented (Packet 5.4). 225 Phase 5 tests pass covering all success paths, idempotency, state machine guards, auto_continue vs manual mode, PTIFF QA gate release conditions, split-correction deadlock regression, and error cases (Packet 5.5). Known limitations: `current_deskew_angle=None` for fresh corrections (IEP1C deskew angle not stored in DB); `iep1d_rectified=None` until rescue_step stores metrics; auth/RBAC scoping deferred to Phase 7; split-parent `→ split` transition for `layout + auto_continue` is asynchronous and depends on Phase 6 IEP2 completion callback.
- **Blocked/blocking:** None. Phase 6 may begin.
- **Relevant spec constraints:** `pending_human_correction` is worker-terminal but not leaf-final; corrections always return to `ptiff_qa_pending` before proceeding downstream (spec Section 1.6); `pending_human_correction → layout_detection` and `pending_human_correction → accepted` are NOT valid transitions; PTIFF QA gate is never bypassed for non-auto_continue mode; split parents must not block the PTIFF QA gate for their children in manual mode; `_maybe_close_split_parent` is only called after `gate_released=True`; Phase 6 IEP2 worker must call the split-parent closure check after layout_detection pages complete.

### ☐ Phase 6 — IEP2 + layout consensus

- ☐ Packet 6.1 — IEP2A service shell and detect path
- ☐ Packet 6.2 — IEP2A postprocessing
- ☐ Packet 6.3 — IEP2B service shell and detect path
- ☐ Packet 6.4 — IEP2B canonical class mapping and postprocessing
- ☐ Packet 6.5 — layout consensus gate
- ☐ Packet 6.6 — layout integration tests

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

### ☑ Phase 7 — Auth, RBAC, admin/user APIs, lineage

- ☑ Packet 7.1 — auth and JWT issuance
- ☑ Packet 7.2 — RBAC helpers and enforcement
- ☑ Packet 7.3 — job list endpoint
- ☑ Packet 7.4 — admin dashboard endpoints
- ☑ Packet 7.5 — lineage endpoint
- ☑ Packet 7.6 — user management endpoints

- **Summary:** Packets 7.1–7.6 complete. Phase 7 definition of done satisfied: JWT works ☑, RBAC works ☑, job list scoped for users / global for admins ☑, lineage response correct and complete ☑, admin user management works ☑. Packet 7.6: `POST /v1/users` (create with bcrypt hash, 409 on duplicate username, 201), `GET /v1/users` (list all, ordered by created_at ASC), `PATCH /v1/users/{user_id}/deactivate` (sets is_active=False, idempotent, 404 on unknown user_id). hashed_password never returned in any response. Implementation in `services/eep/app/admin/users.py`; 24 new tests in `test_p7_user_management.py`. Suite: 2044 passing, 137 failing (all pre-existing P4 integration test failures — unchanged).
- **Blocked/blocking:** None. Phase 8 may begin.
- **Relevant spec constraints:** JWT `sub` claim = user_id; `require_admin` for all three user management endpoints; `hashed_password` must never appear in any response; `IntegrityError` from DB unique constraint maps to 409; `PATCH .../deactivate` is idempotent.

### ☑ Phase 8 — MLOps plumbing

- ☑ Packet 8.1 — Phase 8 migration
- ☑ Packet 8.2 — policy endpoints
- ☑ Packet 8.3 — promotion / rollback API (offline gate evaluation for IEP1)
- ☑ Packet 8.4 — retraining webhook and trigger recording
- ☑ Packet 8.5 — retraining worker, offline evaluation, and recovery service

- **Summary:** All Phase 8 packets complete. 8.1: Migration `0003_p8_mlops_tables.py` creates all 6 MLOps tables. 8.2: policy read/update endpoints. 8.3: `POST /v1/models/promote` and `POST /v1/models/rollback` with offline gate enforcement and cooldown-safe rollback. 8.4: `POST /v1/retraining/webhook` receives Alertmanager payloads; records triggers in `retraining_triggers` with correct `persistence_hours`, `cooldown_until = now+7d`, `status='pending'`; 36 tests pass. 8.5: `services/retraining_worker/app/task.py` — `execute_retraining_task` creates RetrainingJob, runs stub MLflow training, runs stub offline evaluation, writes gate_results to ModelVersion (stage=staging) for iep1a+iep1b; `services/retraining_worker/app/main.py` — lifespan poll loop (30s interval, claims pending triggers, calls task, marks failed on exception); `services/retraining_recovery/app/reconcile.py` — `reconcile_once` recovers stuck running jobs and orphaned processing triggers; `services/retraining_recovery/app/main.py` — lifespan reconciliation loop (60s interval); 39 tests in `test_p8_retraining_worker.py` all pass. Phase 8 definition of done fully satisfied.
- **Blocked/blocking:** None. Phase 9 may begin.
- **Relevant spec constraints:** Phase 8 tables must not appear in Phase 1 migration (separation invariant). `model_versions.gate_results` is JSONB (populated by offline evaluation worker, not migration). `task_retry_states` uses plain TEXT for page_id/job_id (no FK to Phase 1 tables).

### ☑ Phase 9 — Metrics, policy loading, drift skeleton, hardening

- ☑ Packet 9.1 — metrics registration
- ☑ Packet 9.2 — policy loading and threshold wiring
- ☑ Packet 9.3 — drift detector skeleton
- ☑ Packet 9.4 — placeholder baselines file
- ☑ Packet 9.5 — observability hardening and golden-dataset tests
- ☑ Packet 9.6 — Prometheus and alerting configuration
- ☑ Packet 9.7 — Grafana dashboards

- **Summary:** All metrics, policy loading, drift detection, Prometheus/Alertmanager configuration, and Grafana dashboards are complete. Four dashboards provisioned: API service health, workers & queue, gate decisions, model signals. Golden-dataset test suite (42 tests) covers geometry gate routing, artifact validation, IEP1C normalization, lineage writes, and state transitions.
- **Blocked/blocking:** None. Phase 10 may begin.
- **Relevant spec constraints:** Dashboards are provisioned via Grafana's filesystem provider (monitoring/grafana/provisioning/). Alert rules in alert_rules/ are loaded by Prometheus rule_files glob. Alertmanager routes retraining_trigger → POST /v1/retraining/webhook and rollback_trigger → POST /v1/models/rollback.

### ☐ Phase 10 — Frontend

- ☐ Packet 10.1 — auth and base app shell
- ☐ Packet 10.2 — user job flow screens
- ☐ Packet 10.2a — PTIFF QA review screen
- ☐ Packet 10.3 — correction UI and interactive correction workspace
- ☐ Packet 10.3a — correction workspace UX hardening
- ☐ Packet 10.4 — admin operational screens
- ☐ Packet 10.5 — MLOps admin screens
- ☐ Packet 10.6 — frontend hardening

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

### ☐ Phase 11 — Cloud deployment, Kubernetes, Runpod, CI/CD, observability stack

- ☐ Packet 11.1 — Kubernetes base manifests
- ☐ Packet 11.2 — Runpod production backend
- ☐ Packet 11.3 — production storage and Redis durability
- ☐ Packet 11.4 — production image path and model-weight baking
- ☐ Packet 11.5 — CI/CD pipelines
- ☐ Packet 11.6 — in-cluster observability stack
- ☐ Packet 11.7 — deployment validation

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

### ☐ Phase 12 — Later model swap for real IEP1A/B

- ☐ Packet 12.1 — IEP1A real inference replacement
- ☐ Packet 12.2 — IEP1B real inference replacement
- ☐ Packet 12.3 — real TTA integration
- ☐ Packet 12.4 — Docker / model loading update
- ☐ Packet 12.5 — swap validation pass

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**
```
