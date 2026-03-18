

# LibraryAI Implementation Roadmap — Phase → Work Packet Version

## 0. Purpose

This roadmap is the execution plan for implementing LibraryAI from `full_updated_spec.md`.

It is designed for AI-agent coding and must be followed strictly.

The goal is to implement the full system around the models now, while deferring actual IEP1A and IEP1B model training/inference until teammates finish data labeling and training.

This roadmap preserves the architecture, terminology, safety rules, state semantics, and contracts defined in the specification. It must not introduce architectural changes, weaken safety gates, rename concepts, or invent substitute workflows.

---

## 1. Non-Negotiable Constraints

These constraints apply to every phase and every work packet.

### 1.1 Architecture must remain unchanged

Do not change:

- EEP as central orchestrator
- IEP1A = YOLOv8-seg geometry service
- IEP1B = YOLOv8-pose geometry service
- IEP1C = deterministic normalization shared module
- IEP1D = UVDoc rectification fallback
- IEP2A = Detectron2 layout detector
- IEP2B = DocLayout-YOLO layout detector
- DB-first artifact write protocol
- Redis queues and semaphore model
- PostgreSQL as source of truth
- S3-compatible storage model
- JWT + RBAC auth model

### 1.2 Safety must not be weakened

Do not change or weaken:

- no single-model auto-acceptance in IEP1 final acceptance
- no single-model auto-acceptance in IEP2
- first-pass structural disagreement in IEP1 may continue with low trust
- second-pass structural disagreement in IEP1 must route to `pending_human_correction`
- content failures must not route to `failed`
- `failed` is only for unrecoverable infrastructure/data integrity failures
- no silent page loss
- no destructive overwrites of OTIFF
- no pipeline stage may silently bypass lineage or quality gate logging

### 1.3 Current implementation constraint

For now:

- IEP1A and IEP1B must be implemented as mock inference services
- their endpoints, schemas, metrics, readiness behavior, and contracts must be real
- only the actual ML inference internals are stubbed
- the system must be architected so real models can later replace mock inference without changing EEP or schemas

### 1.4 AI agent behavior constraint

The coding agent must:

- work phase by phase
- execute one work packet at a time unless explicitly instructed otherwise
- not skip ahead unless instructed
- not redesign modules
- not rename schema fields from the spec
- not silently simplify state machines
- not replace required database tables with "temporary" alternatives
- not weaken validations because a packet or phase is incomplete
- always update `docs_pre_implementation/implementation_checklist.md` after each completed phase
- before starting any work packet, restate:
  - the exact packet scope
  - files it will create or modify
  - dependencies it assumes already exist
  - what is stubbed vs fully implemented in that packet
- if a packet cannot be completed fully, stop at the exact blocking point, explain what is complete vs incomplete, and do not claim the packet is complete
- not mark a phase complete until all work packets in that phase satisfy the phase definition of done

### 1.5 Cross-service Python import strategy

The repository uses a monorepo layout. All services and the `shared/` package live under the same repository root.

- `shared/` is a Python package importable by all services
- `services/eep/app/db/` contains DB helpers (models, page state, lineage, quality gate) that are shared between the EEP API service and the EEP worker service
- the EEP worker service (`services/eep_worker/`) imports from `services/eep/app/db/` and from `shared/`
- shadow worker, retraining worker, and recovery services import from `shared/` and from `services/eep/app/db/` where needed
- each service's Dockerfile and the docker-compose configuration must set `PYTHONPATH` to include the repository root so that `import shared`, `import services.eep.app.db`, etc. resolve correctly
- no service may duplicate DB helper code instead of importing from the canonical location

This must be established in Phase 0 (Packet 0.2) and maintained throughout.

### 1.6 PTIFF-stage QA checkpoint

LibraryAI must implement a PTIFF-stage quality assurance checkpoint between preprocessing and any downstream stages.

#### State model

- `ptiff_qa_pending` is a **page-level state** in the page state machine
- `ptiff_qa_pending` is **not** a terminal page state
- pages enter `ptiff_qa_pending` after successful preprocessing completion (after artifact validation passes or after human correction is accepted)
- pages exit `ptiff_qa_pending` via one of:
  - reviewer approval records approval intent only; page state remains `ptiff_qa_pending` until gate release
- gate release transitions:
  - for `pipeline_mode = "preprocess"` → `accepted`
  - for `pipeline_mode = "layout"` → `layout_detection`
- reviewer edit → re-enters correction flow, then returns to `ptiff_qa_pending` after correction
- auto-continue mode → automatic transition to `accepted` or `layout_detection` without manual review

#### QA modes

QA behavior must support two modes, selected at job creation:

1. **Manual QA mode (`ptiff_qa_mode = "manual"`):**
   - pages enter `ptiff_qa_pending` and wait for reviewer action
   - reviewers may inspect pages individually
   - reviewers may approve individual pages or approve all remaining pages at once
   - reviewers may edit pages (which routes through the correction workflow)
   - job remains blocked from downstream processing until all pages are approved or edited

2. **Auto-continue mode (`ptiff_qa_mode = "auto_continue"`):**
   - pages that were auto-accepted by the pipeline transition through `ptiff_qa_pending` automatically without waiting for manual review
   - pages that were previously human-corrected are also considered accepted
   - no manual review is required

#### Integration points

- `ptiff_qa_pending` must be included in the page state enumeration exported by `shared/schemas/eep.py` (Packet 1.3)
- the DB schema must support this state (Packet 1.5)
- the worker must route pages to `ptiff_qa_pending` after preprocessing (Packet 4.6)
- the QA workflow endpoints and logic are implemented in Phase 5 (Packet 5.0a)
- layout execution in Phase 6 must not start for any page unless that page has exited `ptiff_qa_pending`

### PTIFF QA approval semantics clarification (manual mode)

For jobs with `ptiff_qa_mode = "manual"` and `pipeline_mode = "layout"`:

- Approving a single page in PTIFF QA does **not** immediately transition that page to `layout_detection`.
- Individual approval records reviewer intent only (approval metadata is stored), while page state remains `ptiff_qa_pending` until the job-level gate is satisfied.
- When no pages for the job remain unapproved in PTIFF QA, all approved pages are released to `layout_detection` in one controlled transition.
- For `pipeline_mode = "preprocess"`, approved pages transition to `accepted` at gate release.

---

## 2. Build-Now vs Stub-Now vs Defer

### 2.1 Must build now

These must be implemented now:

- repo structure
- Docker / docker-compose
- shared schemas
- storage backends
- PostgreSQL schema
- Redis queues / semaphore usage
- strong queue reliability contract
- EEP API
- EEP worker
- EEP recovery service
- geometry selection gate
- artifact validation gate
- IEP1C normalization
- full IEP1 orchestration
- split handling
- correction workflow
- PTIFF QA checkpoint
- presigned upload API for job intake
- RBAC and auth
- lineage API
- admin endpoints
- policy loading
- metrics
- Prometheus scrape configuration
- Alertmanager rule/config generation
- Grafana dashboard definitions
- drift detection skeleton
- MLOps plumbing
- shadow worker
- shadow recovery service
- retraining worker
- retraining recovery service
- contract tests
- simulation tests
- golden-dataset tests
- tests

### 2.2 Stub now, replace later

These may be stubs now but must preserve real contracts:

- IEP1A inference
- IEP1B inference

The stubs must return real `GeometryResponse` objects and support configurable behavior for testing:

- agreement
- disagreement
- split/no-split
- high confidence / low confidence
- high TTA variance
- malformed/failure simulation

### 2.3 Can be mocked temporarily if needed

If timeline becomes tight, these may start mocked but must preserve their service/API contracts:

- IEP1D rectification internals
- IEP1D eligibility logic
- IEP2A internals
- IEP2B internals
- shadow candidate inference differences
- MLflow backend integration

### 2.4 Deferred until later

Deferred work:

- real IEP1A YOLOv8-seg inference
- real IEP1B YOLOv8-pose inference
- real TTA implementation for IEP1A/B
- real IEP1D UVDoc model internals if initially mocked
- training pipelines for preprocessing models
- IEP1D eligibility model calibration from real data
- threshold calibration from real validation data
- final production tuning
- model-weight baking optimization for production images after first working deployment

---

## 3. Implementation Strategy

Implement in dependency order so the system becomes usable end-to-end as early as possible.

### Execution order

- Phase 0 — repo, containers, shared foundations
- Phase 1 — schemas, DB, storage, Redis, core EEP job API
- Phase 2 — IEP1A/B mock services + IEP1C normalization
- Phase 3 — geometry selection gate + artifact validation gate
- Phase 4 — full IEP1 orchestration in worker
- Phase 5 — correction workflow + PTIFF QA + split lifecycle
- Phase 6 — IEP2 services + layout consensus gate
- Phase 7 — admin/user APIs + RBAC + lineage
- Phase 8 — MLOps plumbing + shadow + retraining hooks
- Phase 9 — metrics, policy loading, drift skeleton, hardening
- Phase 10 — frontend
- Phase 11 — cloud deployment, Kubernetes, Runpod, CI/CD, observability stack
- Phase 12 — real model swap for IEP1A/B later

### Execution granularity rule

Each phase below is a planning unit, not necessarily a single coding run.

The coding agent should normally implement one bounded work packet at a time inside a phase.

A work packet should usually:

- be limited to one component or one closely related set of files
- have a clear interface or contract
- have a clear definition of done
- be testable in isolation
- avoid forcing the agent to complete a whole later subsystem in the same run

A phase is complete only when all of its work packets are complete and the phase definition of done is satisfied.

### Test track assignment rule

Section 7 defines mandatory test tracks (contract tests, simulation tests, golden-dataset tests). These are assigned to phases as follows:

- **Contract tests** for each service must be implemented in the same phase that implements that service's endpoints or worker logic. Specifically:
  - IEP1A, IEP1B contract tests → Phase 2
  - EEP API contract tests → Phase 1
  - EEP worker queue contract tests → Phase 4 (Packet 4.8)
  - IEP1D contract tests → Phase 4 (Packet 4.8, covering the rescue path)
  - IEP2A, IEP2B contract tests → Phase 6 (Packet 6.6)
  - shadow worker contract tests → Phase 8 (Packet 8.4)
  - retraining worker contract tests → Phase 8 (Packet 8.7)

- **Simulation tests** must be implemented in Phase 4, Packet 4.8. This packet already requires happy path, rescue path, split path, failure classification, worker restart, Redis reconnect, and abandoned task reconciliation tests. The simulation test track formalizes these requirements.

- **Golden-dataset tests** must be implemented in Phase 9, Packet 9.5. This packet covers observability hardening and is the appropriate place to verify deterministic paths (IEP1C normalization outputs, geometry gate routing for fixed synthetic cases, artifact validation for fixed cases, lineage write expectations, page state transitions) against known-good reference data.

---

## 4. Phase-by-Phase Roadmap with Work Packets

### Phase 0 — Repo, containers, and service skeletons

#### Goals

Create the repository skeleton exactly as expected by the specification. Bring up all services under Docker. Expose health, readiness, and metrics endpoints everywhere. Establish the cross-service Python import strategy.

#### Files / modules

- `README.md`
- `pyproject.toml`
- `Makefile`
- `docker-compose.yml`
- `.pre-commit-config.yaml`
- `.env.example`
- `services/eep/app/main.py`
- `services/eep_worker/app/main.py`
- `services/eep_recovery/app/main.py`
- `services/shadow_worker/app/main.py`
- `services/shadow_recovery/app/main.py`
- `services/retraining_worker/app/main.py`
- `services/retraining_recovery/app/main.py`
- `services/artifact_cleanup/app/main.py`
- `services/iep1a/app/main.py`
- `services/iep1b/app/main.py`
- `services/iep1d/app/main.py`
- `services/iep2a/app/main.py`
- `services/iep2b/app/main.py`
- `shared/health.py`
- `shared/metrics.py`
- `shared/logging_config.py`
- `shared/middleware.py`
- `shared/gpu/backend.py`

#### GPUBackend local implementation requirement

For local development, the GPUBackend implementation must:

- invoke services over HTTP using Docker container names and ports defined in the spec service inventory
  (for example `http://iep1a:8001`, `http://iep1b:8002`, `http://iep1d:8003`, `http://iep2a:8004`, `http://iep2b:8005`)
- accept:
  - `cold_start_timeout_seconds`
  - `execution_timeout_seconds`
- distinguish between:
  - cold-start timeout
  - warm inference timeout
  - service error

This behavior must be implemented even if local Docker does not scale to zero.

#### GPUBackend production implementation requirement

The GPUBackend abstraction must support a production backend targeting Runpod.

Production backend requirements:

- support Runpod CPU endpoints for non-GPU-compatible or control-plane tasks where applicable
- support Runpod on-demand GPU endpoints for IEP1A, IEP1B, IEP1D, IEP2A, and IEP2B when configured
- preserve the same request/response contracts used by the local HTTP backend
- accept:
  - `cold_start_timeout_seconds`
  - `execution_timeout_seconds`
- distinguish between:
  - cold-start timeout
  - warm inference timeout
  - provider/service error
- not require any schema or orchestrator changes when switching between local HTTP backend and Runpod backend

#### Required endpoints now

All services:

- `GET /health`
- `GET /ready`
- `GET /metrics`

EEP temporary placeholders:

- `POST /v1/auth/token`
- `POST /v1/jobs`
- `GET /v1/jobs/{job_id}`

#### What is allowed to be stubbed

All business logic.

#### Work packets

**Packet 0.1 — repository structure and root files**

Implement:
- repository folder structure
- `README.md`
- `pyproject.toml`
- `Makefile`
- `.pre-commit-config.yaml`
- `.env.example`

Done when:
- repository structure matches roadmap
- root project files exist and are internally consistent

**Packet 0.2 — docker-compose and service bootstrapping**

Implement:
- `docker-compose.yml`
- service definitions for EEP API, EEP worker, EEP recovery, shadow worker, shadow recovery, retraining worker, retraining recovery, artifact cleanup, IEP1A, IEP1B, IEP1D, IEP2A, IEP2B
- local PostgreSQL
- local Redis with AOF enabled
- one local S3-compatible object storage service
- Prometheus
- Alertmanager
- Grafana
- `PYTHONPATH` configuration in all service containers so that `import shared` and `import services.eep.app.db` resolve correctly from the repository root (per Section 1.5)
- local MLflow service (or explicit external MLflow configuration for local development)

Done when:
- `docker-compose up` starts all declared services
- local development docker-compose includes PostgreSQL, Redis, and one S3-compatible object storage service
- Redis AOF persistence is enabled in local development
- Prometheus, Alertmanager, and Grafana are reachable in local development
- cross-service Python imports work correctly in all containers

**Packet 0.3 — shared health, metrics, logging, middleware**

Implement:
- `shared/health.py`
- `shared/metrics.py`
- `shared/logging_config.py`
- `shared/middleware.py`

Done when:
- every service can expose `/health`, `/ready`, `/metrics`
- shared logging and middleware utilities are importable

**Packet 0.4 — API and model service skeleton entrypoints**

Implement:
- `services/eep/app/main.py`
- `services/iep1a/app/main.py`
- `services/iep1b/app/main.py`
- `services/iep1d/app/main.py`
- `services/iep2a/app/main.py`
- `services/iep2b/app/main.py`

Done when:
- each service starts
- each service responds to `/health`, `/ready`, `/metrics`

**Packet 0.5 — GPU backend local HTTP stub**

Implement:
- `shared/gpu/backend.py`

Done when:
- local implementation targets container-name HTTP endpoints
- timeout/error classification exists per roadmap requirements

**Packet 0.6 — service skeletons for worker, recovery, and maintenance processes**

Implement:
- `services/eep_worker/app/main.py`
- `services/eep_recovery/app/main.py`
- `services/shadow_worker/app/main.py`
- `services/shadow_recovery/app/main.py`
- `services/retraining_worker/app/main.py`
- `services/retraining_recovery/app/main.py`
- `services/artifact_cleanup/app/main.py`

Done when:
- each process starts as an independent service
- each process exposes `/health`, `/ready`, `/metrics`
- service topology matches the roadmap


#### Phase definition of done

- all declared containers start via `docker-compose up`
- every service and background process returns 200 on `/health`
- every service and background process returns 200 or 503 appropriately on `/ready`
- every service and background process returns Prometheus text on `/metrics`
- EEP placeholder endpoints exist
- local Redis AOF is enabled
- local Prometheus, Alertmanager, and Grafana are running
- cross-service Python imports work correctly (per Section 1.5)


---

### Phase 1 — Shared schemas, DB, storage, Redis, core EEP job API

#### Goals

Implement the canonical shared schemas from the spec, PostgreSQL schema, Redis integration, storage backends, and real job creation/status endpoints.

#### Files / modules

- `shared/schemas/ucf.py`
- `shared/schemas/preprocessing.py`
- `shared/schemas/geometry.py`
- `shared/schemas/normalization.py`
- `shared/schemas/iep1d.py`
- `shared/schemas/layout.py`
- `shared/schemas/eep.py`
- `shared/io/storage.py`
- `services/eep/app/uploads.py`
- `services/eep/app/queue.py`
- DB migrations / SQL / ORM for:
  - `jobs`
  - `job_pages`
  - `page_lineage`
  - `service_invocations`
  - `quality_gate_log`
  - `users`
- `services/eep/app/db/models.py`
- `services/eep/app/jobs/create.py`
- `services/eep/app/jobs/status.py`
- `services/eep/app/main.py`

#### Key requirements

- validators must match spec semantics
- export `TERMINAL_PAGE_STATES` from `shared/schemas/eep.py`
- the page state enumeration must include `ptiff_qa_pending` (see Section 1.6)
- `ptiff_qa_pending` must NOT be included in `TERMINAL_PAGE_STATES`
- `POST /v1/jobs` must create job + page rows and enqueue page tasks to Redis
- `GET /v1/jobs/{job_id}` must derive status exactly per leaf-page rules in spec
- `POST /v1/uploads/jobs/presign` must return presigned upload information for raw OTIFF job intake
- job creation must accept storage URIs produced by the presigned upload flow
- job creation must accept `ptiff_qa_mode` (`"manual"` or `"auto_continue"`) as part of job configuration
- Redis queue logic must implement a reliable processing pattern with:
  - atomic claim/move semantics using `BLMOVE` where available
  - `BRPOPLPUSH` fallback where `BLMOVE` is unavailable
  - processing list ownership
  - dead-letter handling
  - reconnect-safe control-plane recovery
  - DB-authoritative reconciliation entry points (the queue module exposes hooks for reconciliation; the actual task-state reconciliation logic comparing DB page states against queue contents is implemented in Phase 4, Packet 4.7)

#### What can be stubbed

- worker may still be a stub
- page processing may not yet happen

#### Work packets

**Packet 1.1 — UCF and preprocessing schemas**

Implement:
- `shared/schemas/ucf.py`
- `shared/schemas/preprocessing.py`

Done when:
- validators work
- schema fields match spec

**Packet 1.2 — geometry, normalization, iep1d, layout schemas**

Implement:
- `shared/schemas/geometry.py`
- `shared/schemas/normalization.py`
- `shared/schemas/iep1d.py`
- `shared/schemas/layout.py`

Done when:
- all service request/response models validate correctly

**Packet 1.3 — EEP schemas and terminal page states**

Implement:
- `shared/schemas/eep.py`

Done when:
- `TERMINAL_PAGE_STATES` exported correctly
- `ptiff_qa_pending` is present in the page state enumeration
- `ptiff_qa_pending` is NOT in `TERMINAL_PAGE_STATES`
- job-related schemas match spec
- job configuration schema includes `ptiff_qa_mode` field

**Packet 1.3a — page state machine contract**

Implement:
- `shared/state_machine.py`
- authoritative allowed transitions map
- transition validator used by API, worker, watchdog, and recovery services

Done when:
- all page transitions are centrally validated
- `ptiff_qa_pending` transitions are explicitly defined
- terminal-state automation stop rules are enforced in one shared module

**Packet 1.4 — storage backends**

Implement:
- `shared/io/storage.py`

Done when:
- backend selection by URI scheme works
- local and S3-compatible interfaces exist

**Packet 1.5 — core DB migration**

Implement:
- migration for `jobs`, `job_pages`, `page_lineage`, `service_invocations`, `quality_gate_log`, `users`

Done when:
- schema matches spec for these six core tables only
- `job_pages` supports `ptiff_qa_pending` as a valid page state
- `jobs` table stores `ptiff_qa_mode`

**Packet 1.6 — ORM / DB model layer**

Implement:
- `services/eep/app/db/models.py`

Done when:
- ORM models map correctly to the migrated schema

**Packet 1.7 — Redis queue setup**

Implement:
- Redis integration required for page task enqueueing

Done when:
- page tasks can be pushed to Redis from EEP

**Packet 1.7a — reliable Redis queue contract**

Implement:
- `services/eep/app/queue.py`
- reliable queue claim/move semantics
- processing-list ownership semantics
- dead-letter queue support
- reconnect-safe queue control-plane rebuild hooks
- DB-authoritative reconciliation entry points (callable hooks that Phase 4 Packet 4.7 will use to implement full task-state reconciliation)

Done when:
- queue semantics are reliable under worker restart and Redis reconnect scenarios
- `BLMOVE` is used where available
- `BRPOPLPUSH` fallback exists where required
- dead-letter path exists
- reconciliation entry points are exposed and documented
- queue/task reconciliation remains DB-authoritative

**Packet 1.7b — presigned upload endpoint**

Implement:
- `services/eep/app/uploads.py`
- `POST /v1/uploads/jobs/presign`

Done when:
- frontend can request presigned upload information
- uploaded raw OTIFF objects can be referenced by URI in `POST /v1/jobs`

**Packet 1.8 — job creation endpoint**

Implement:
- `services/eep/app/jobs/create.py`
- `POST /v1/jobs`
- job configuration must include:
  - processing mode: `preprocess` or `layout`
  - `ptiff_qa_mode`: `manual` or `auto_continue`

Done when:
- job and page rows are created
- page tasks are enqueued
- job stores both processing mode and `ptiff_qa_mode`
- EEP API contract tests for job creation validate request/response schema

**Packet 1.9 — job status endpoint**

Implement:
- `services/eep/app/jobs/status.py`
- `GET /v1/jobs/{job_id}`

Done when:
- job status derivation follows leaf-page rules exactly
- `ptiff_qa_pending` pages are correctly counted as non-terminal in status derivation
- EEP API contract tests for job status validate response schema

#### Phase definition of done

- schemas validate correctly
- DB schema exists and matches spec
- `ptiff_qa_pending` exists in page state enumeration and DB schema
- job creation works and stores `ptiff_qa_mode`
- job status endpoint works
- Redis queue receives page tasks
- reliable queue semantics exist for claim, processing ownership, reconnect recovery, dead-letter handling, and reconciliation entry points
- presigned upload endpoint works
- leaf-page status derivation is centralized and correct
- EEP API contract tests pass

---

### Phase 2 — IEP1A/IEP1B mock services and IEP1C normalization

#### Goals

Create real mock geometry services for IEP1A and IEP1B and implement IEP1C as real production code.

#### Files / modules

- `services/iep1a/app/main.py`
- `services/iep1a/app/inference.py`
- `services/iep1a/app/tta.py`
- `services/iep1b/app/main.py`
- `services/iep1b/app/inference.py`
- `services/iep1b/app/tta.py`
- `shared/normalization/normalize.py`
- `shared/normalization/perspective.py`
- `shared/normalization/deskew.py`
- `shared/normalization/split.py`
- `shared/normalization/quality.py`

#### Mock requirements for IEP1A/IEP1B

Both must support deterministic configurable output for tests:

- `page_count` 1 or 2
- `split_required` true/false
- `split_x` set/null
- confidence high/low
- TTA agreement high/low
- TTA variance low/high
- service failure simulation

They must return real `GeometryResponse`.

#### IEP1C must be real

Implement:

- crop from geometry
- perspective correction from quadrilateral
- affine fallback from bbox
- split handling
- quality metrics:
  - `blur_score`
  - `border_score`
  - `foreground_coverage`
  - `skew_residual`

#### Work packets

**Packet 2.1 — IEP1A mock service shell**

Implement:
- `services/iep1a/app/main.py`
- `services/iep1a/app/inference.py`

Done when:
- mock service returns valid `GeometryResponse`
- IEP1A contract tests validate request/response schema and error behavior

**Packet 2.2 — IEP1A TTA mock behavior**

Implement:
- `services/iep1a/app/tta.py`

Done when:
- configurable TTA agreement/variance behavior exists

**Packet 2.3 — IEP1B mock service shell**

Implement:
- `services/iep1b/app/main.py`
- `services/iep1b/app/inference.py`

Done when:
- mock service returns valid `GeometryResponse`
- IEP1B contract tests validate request/response schema and error behavior

**Packet 2.4 — IEP1B TTA mock behavior**

Implement:
- `services/iep1b/app/tta.py`

Done when:
- configurable TTA agreement/variance behavior exists

**Packet 2.5 — normalization core**

Implement:
- `shared/normalization/normalize.py`
- `shared/normalization/perspective.py`
- `shared/normalization/deskew.py`

Done when:
- single-page normalization works from geometry input

**Packet 2.6 — split handling**

Implement:
- `shared/normalization/split.py`

Done when:
- split normalization path works

**Packet 2.7 — quality metrics**

Implement:
- `shared/normalization/quality.py`

Done when:
- required quality metrics are computed and testable

#### Phase definition of done

- IEP1A/B mock endpoints fully usable
- IEP1C produces real `PreprocessBranchResponse`
- normalization works on single-page and split paths
- quality metrics exist and are testable
- IEP1A and IEP1B contract tests pass

---

### Phase 3 — Geometry selection and artifact validation gates

#### Goals

Implement the core safety gates exactly as defined in the spec.

#### Files / modules

- `services/eep/app/gates/geometry_selection.py`
- `services/eep/app/gates/artifact_validation.py`

#### Geometry selection must implement

- structural agreement check
- all six sanity checks
- split confidence filter
- TTA variance filter
- page area preference
- confidence-based selection
- route-to-human logic
- logging to `quality_gate_log`

#### Artifact validation must implement

- hard requirements
- weighted soft score
- configurable threshold
- logging to `quality_gate_log`

#### Work packets

**Packet 3.1 — structural agreement and sanity checks**

Implement:
- structural agreement
- six sanity checks

Done when:
- agreement and sanity filtering behave per spec

**Packet 3.2 — split confidence, variance, page area preference**

Implement:
- split confidence filter
- TTA variance filter
- page area preference

Done when:
- filtering and tiebreak behavior work correctly

**Packet 3.3 — final selection and route-to-human logic**

Implement:
- confidence-based selection
- low-trust routing behavior
- `quality_gate_log` writes for geometry selection

Done when:
- all geometry selection decisions are logged and routed correctly

**Packet 3.4 — artifact hard requirements**

Implement:
- hard validation checks

Done when:
- invalid artifacts fail before scoring

**Packet 3.5 — artifact soft score and threshold logic**

Implement:
- weighted soft score
- configurable threshold
- logging to `quality_gate_log`

Done when:
- artifact validation decisions follow spec

**Packet 3.6 — gate test suite**

Implement:
- tests for geometry selection
- tests for artifact validation

Done when:
- routing paths and failure paths are covered

#### Phase definition of done

- all routing paths covered by tests
- low-trust first-pass behavior works
- no candidate path routes incorrectly to `failed`
- single-model confidence never bypasses structural agreement requirement for final acceptance
- tests cover all gate routing paths and failure paths

---

### Phase 4 — Full IEP1 worker orchestration

#### Phase numbering note

Roadmap phase numbering in this document is implementation sequencing for this project and is independent of the model-training/build sequence numbering described in spec Section 10.4.

#### Goals

Implement `process_page()` through the full preprocessing flow.

#### Files / modules

- `services/eep_worker/app/task.py`
- `services/eep_worker/app/concurrency.py`
- `services/eep_worker/app/circuit_breaker.py`
- `services/eep_worker/app/watchdog.py`
- `services/eep_recovery/app/main.py`
- `services/eep_recovery/app/reconcile.py`
- `services/eep/app/db/page_state.py`
- `services/eep/app/db/lineage.py`
- `services/eep/app/db/quality_gate.py`

#### Required steps

Implement Steps 0–8 from the spec:

- OTIFF intake
- hash calculation
- proxy generation
- parallel geometry inference
- geometry selection
- normalization
- validation
- rectification fallback
- second geometry pass
- second normalization
- final validation
- split handling
- PTIFF QA routing
- preprocess-only stop path

#### Important routing rules

- content failure → `pending_human_correction`
- only non-displayable/unretrievable/corrupt data integrity cases → `failed`
- split parent lifecycle must follow spec
- circuit breakers and Redis semaphore must be respected
- after successful preprocessing, pages must transition to `ptiff_qa_pending`
- in auto-continue mode, pages must automatically transition through `ptiff_qa_pending` to `accepted` (for preprocess)
 or `layout_detection` (for layout)
- in manual mode, pages must remain in `ptiff_qa_pending` until reviewer action (handled in Phase 5)

#### IEP1D mock note

IEP1D may use a pass-through mock body at this stage. The rescue path must be implemented as if IEP1D is a real external service. The unavailability path (IEP1D fails or circuit breaker open) must also be tested.

#### Work packets

**Packet 4.1 — worker concurrency and circuit breaker**

Implement:
- `services/eep_worker/app/concurrency.py`
- `services/eep_worker/app/circuit_breaker.py`

Done when:
- semaphore and circuit breaker behavior exist

**Packet 4.2 — page state and lineage DB helpers**

Implement:
- `services/eep/app/db/page_state.py`
- `services/eep/app/db/lineage.py`
- `services/eep/app/db/quality_gate.py`

The page state helper must support the `ptiff_qa_pending` state and its valid transitions as defined in Section 1.6.

Done when:
- worker can persist state, lineage, and gate logs cleanly
- `ptiff_qa_pending` transitions are supported

**Packet 4.3a — intake, hash, proxy image derivation**

Implement:
- OTIFF intake
- hash computation
- proxy image derivation

Done when:
- worker can load an OTIFF, compute its hash, and generate a proxy image

**Packet 4.3b — parallel geometry invocation and selection wiring**

Implement:
- parallel IEP1A + IEP1B invocation with circuit breaker handling
- result collection
- geometry selection gate wiring

Done when:
- worker invokes both geometry services in parallel
- results are collected and passed to the geometry selection gate
- circuit breaker and timeout paths are handled

**Packet 4.4 — normalization and first validation**

Implement:
- normalization
- first validation

Done when:
- worker can decide accept-now vs rescue-required

**Packet 4.5 — rescue flow (rectification, second geometry pass, second normalization, final validation)**

IEP1D may use a pass-through mock body at this stage. The rescue path must be implemented as if IEP1D is a real external service. The unavailability path (IEP1D fails or circuit breaker open) must also be tested.

Implement:
- rectification fallback
- second geometry pass
- second normalization
- final validation

Done when:
- rescue path behaves correctly
- IEP1D unavailability path is handled and testable
- IEP1D contract tests validate request/response schema and error behavior

**Packet 4.6 — split handling, PTIFF QA routing, and preprocess-only stop path**

Implement:
- split parent/child lifecycle
- PTIFF QA routing: after successful preprocessing (artifact validation passes), the worker writes `ptiff_qa_pending` to the page state in the DB
- auto-continue mode: if the job's `ptiff_qa_mode` is `"auto_continue"`, the worker immediately transitions the page from `ptiff_qa_pending` to `accepted` (or enqueues for layout if `layout`)
- manual mode: if the job's `ptiff_qa_mode` is `"manual"`, the page remains in `ptiff_qa_pending` and waits for reviewer action (implemented in Phase 5, Packet 5.0a)
- preprocess-only exit path

Done when:
- split pages behave correctly and child enqueueing works
- pages route to `ptiff_qa_pending` after successful preprocessing
- auto-continue mode transitions pages through QA automatically
- manual mode leaves pages in `ptiff_qa_pending`

**Packet 4.7 — watchdog and recovery service**

Implement:
- `services/eep_worker/app/watchdog.py`
- `services/eep_recovery/app/main.py`
- `services/eep_recovery/app/reconcile.py`

The recovery service must use the reconciliation entry points exposed by the queue module (Packet 1.7a) to implement full task-state reconciliation: comparing DB page states against queue contents to detect abandoned, stuck, or orphaned tasks.

Done when:
- worker has stuck-task support
- recovery service can detect abandoned/in-flight tasks
- recovery service can reconcile queue ownership and task state safely using the queue module's reconciliation entry points
- restart-safety scaffolding exists

**Packet 4.8 — worker integration tests**

Implement:
- happy path
- rescue path
- split path
- failure classification tests
- worker restart recovery tests
- Redis reconnect recovery tests
- abandoned task reconciliation tests
- PTIFF QA routing tests (manual mode and auto-continue mode)
- EEP worker queue contract tests
- IEP1D contract tests (request/response and error behavior)
- simulation tests: first-pass disagreement, second-pass disagreement, service timeout, cold-start timeout, malformed model response, Redis reconnect, worker crash during processing, split retry/idempotency

Done when:
- orchestration behavior is covered end-to-end
- restart, reconnect, and abandoned-task recovery behavior are covered
- PTIFF QA routing is verified for both modes
- contract tests pass for EEP worker queue and IEP1D
- simulation test scenarios pass

#### Phase definition of done

- preprocessing pipeline works end-to-end with mocks
- split path works
- rescue path works
- second-pass disagreement routes correctly
- hash mismatch routes to `failed`
- worker is restart-safe and idempotent
- recovery service can reconcile abandoned work safely
- pages route to `ptiff_qa_pending` correctly based on job configuration
- auto-continue mode transitions pages through QA automatically
- manual mode leaves pages in `ptiff_qa_pending`
- integration tests cover happy path, rescue path, split path, failure classification, and PTIFF QA routing
- contract tests pass for EEP worker queue and IEP1D
- simulation tests pass

---

### Phase 5 — Human correction workflow and PTIFF QA

#### Goals

Implement the full correction queue, correction actions, and PTIFF QA review workflow.

#### Files / modules

- `services/eep/app/correction/workspace.py`
- `services/eep/app/correction/queue.py`
- `services/eep/app/correction/apply.py`
- `services/eep/app/correction/split.py`
- `services/eep/app/correction/ptiff_qa.py`

#### Required endpoints

- `GET /v1/correction-queue`
- `GET /v1/correction-queue/{job_id}/{page_number}`
- `POST /v1/jobs/{job_id}/pages/{page_number}/correction`
- `POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject`
- `GET /v1/jobs/{job_id}/ptiff-qa`
- `POST /v1/jobs/{job_id}/ptiff-qa/approve-all`
- `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve`
- `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit`

#### Must implement

- correction workspace response structure
- single-page correction
- split correction
- parent waiting logic for human-submitted split
- rejection path to review
- PTIFF QA review workflow

#### Work packets

**Packet 5.0 — correction workspace response schema and data assembly**

Implement:
- correction workspace response schema matching spec Section 11.3 (including `branch_outputs`, `iep1a_geometry`, `iep1b_geometry`, `iep1c_normalized`, `iep1d_rectified`)
- workspace payload must include all frontend-editable values required by the correction workspace, including crop bounds, deskew angle, split position when applicable, review reason, page metadata, and branch/source image references
- workspace payload must include source image references according to availability:
  - original raw/displayable page preview is always included when available
  - best available derived artifact is included when available
  - each available branch artifact is included individually when available
- data assembly logic to populate the workspace response from DB and storage

Done when:
- workspace response schema is defined and validated
- data assembly can produce a complete workspace response for a page in `pending_human_correction`
- workspace response provides all data required for interactive frontend editing without ad hoc frontend reconstruction
- workspace response supports source selection logic for original-only and original-plus-derived-artifacts scenarios without frontend guesswork

**Packet 5.0a — PTIFF QA workflow (job-level review gate)**

Implement:
- `services/eep/app/correction/ptiff_qa.py`
- `GET /v1/jobs/{job_id}/ptiff-qa` — returns QA status for the job, including per-page states
- `POST /v1/jobs/{job_id}/ptiff-qa/approve-all` — approves only pages currently in `ptiff_qa_pending` for this job and must not alter pages already approved, already completed, or currently in correction
- `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve` — approves a single page
- `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit` — routes a page from `ptiff_qa_pending` back into correction workflow (page returns to `ptiff_qa_pending` after correction is applied)
- job-level PTIFF QA state tracking
- job transition logic:
  - from PTIFF QA → `accepted` (preprocess jobs) at gate release
  - from PTIFF QA → `layout_detection` (layout jobs) at gate release
- for manual mode on layout jobs, individual page approval records approval intent only; release to `layout_detection` occurs only when the job-level PTIFF QA gate is fully satisfied
- job-level PTIFF QA is complete only when:
  - no pages remain in `ptiff_qa_pending`
  - all pages have been explicitly resolved for QA (approved or edited)
  - no page is currently in a correction flow that must return to PTIFF QA

Done when:
- job can pause at PTIFF QA stage (manual mode)
- reviewer can approve all pages without inspection
- reviewer can approve individual pages
- reviewer can route individual pages to correction and have them return to `ptiff_qa_pending` after correction
- job resumes correctly after all pages exit `ptiff_qa_pending`
- auto-continue mode pages (already transitioned in Phase 4) are not affected by manual QA endpoints
- job-level PTIFF QA gate release occurs only when all relevant pages have been QA-resolved according to mode and no page remains in correction flow that must return to PTIFF QA
- in manual mode, page approval alone does not change page state out of `ptiff_qa_pending`; gate release performs the controlled batch transition

##### Note: 
Approve-all must operate only on rows where `job_id = ? AND status = 'ptiff_qa_pending'`.

In `ptiff_qa_mode="manual"`, approve-all records approval intent only and must not immediately transition page state out of `ptiff_qa_pending`.

If approve-all causes the PTIFF QA gate to become fully satisfied, a controlled gate-release step must transition approved pages in batch to:
- `accepted` for preprocess jobs
- `layout_detection` for layout jobs

It must not blanket-update all pages in the job regardless of state.

**Packet 5.1 — correction queue read endpoints**

Implement:
- `GET /v1/correction-queue`
- `GET /v1/correction-queue/{job_id}/{page_number}`

Done when:
- pending pages and workspace detail can be retrieved
- workspace detail uses the schema and assembly from Packet 5.0

**Packet 5.2 — single-page correction apply path**

Implement:
- non-split correction logic

Done when:
- corrected non-split page returns to `ptiff_qa_pending`
- if the job is in `auto_continue` mode, corrected page transitions automatically through `ptiff_qa_pending` per Section 1.6
- if the job is in `manual` mode, corrected page remains in `ptiff_qa_pending` until reviewer action

**Packet 5.3 — split correction apply path**

Implement:
- human-submitted split logic
- parent waiting behavior

Done when:
- child pages are created and parent lifecycle follows spec
- corrected split outputs return to `ptiff_qa_pending`
- downstream behavior respects the job's `ptiff_qa_mode`

**Packet 5.4 — correction reject path**

Implement:
- `POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject`

Done when:
- rejection routes to review correctly

**Packet 5.5 — correction and PTIFF QA tests**

Implement:
- tests for single-page correction
- split correction
- rejection
- idempotency
- PTIFF QA approve-all workflow
- PTIFF QA individual page approve workflow
- PTIFF QA edit-and-return workflow
- PTIFF QA interaction with auto-continue mode (verify no interference)
- PTIFF QA job transition to downstream stages

Done when:
- correction paths are covered
- PTIFF QA paths are covered
- all Phase 5 definition of done items are verified by tests

#### Phase definition of done

- staff can correct crop, deskew, split on raw/displayable OTIFF-derived page
- corrected pages re-enter at correct stage
- rejection is terminal
- correction paths are idempotent
- PTIFF QA supports:
  - approve-all workflow
  - page-by-page review/edit workflow
  - edit routes through correction and returns to `ptiff_qa_pending`
- job-level QA gate works before downstream stages
- auto-continue mode is not disrupted by manual QA endpoints
- tests cover single-page correction, split correction, rejection, idempotency, and all PTIFF QA workflows

---

### Phase 6 — IEP2 services and layout consensus gate

#### Goals

- Implement layout services and layout consensus.
- Layout execution must only occur after PTIFF-stage QA is complete or bypassed via auto-continue mode.

#### Files / modules

- `services/iep2a/app/main.py`
- `services/iep2a/app/detect.py`
- `services/iep2a/app/postprocess.py`
- `services/iep2b/app/main.py`
- `services/iep2b/app/detect.py`
- `services/iep2b/app/class_mapping.py`
- `services/iep2b/app/postprocess.py`
- `services/eep/app/gates/layout_gate.py`

#### Required behavior

- both services return canonical 5-class schema
- layout consensus uses IoU + same canonical type
- single-model fallback must force `agreed=False`
- accepted pages use IEP2A regions as canonical output when agreed
- layout must not execute for any page still in `ptiff_qa_pending`

#### Work packets

**Packet 6.1 — IEP2A service shell and detect path**

Implement:
- `services/iep2a/app/main.py`
- `services/iep2a/app/detect.py`

Done when:
- IEP2A returns valid layout responses

**Packet 6.2 — IEP2A postprocessing**

Implement:
- `services/iep2a/app/postprocess.py`

Done when:
- output is canonical and postprocessed

**Packet 6.3 — IEP2B service shell and detect path**

Implement:
- `services/iep2b/app/main.py`
- `services/iep2b/app/detect.py`

Done when:
- IEP2B returns valid layout responses

**Packet 6.4 — IEP2B canonical class mapping and postprocessing**

Implement:
- `services/iep2b/app/class_mapping.py`
- `services/iep2b/app/postprocess.py`

Done when:
- canonical mapping is enforced strictly

**Packet 6.5 — layout consensus gate**

Implement:
- `services/eep/app/gates/layout_gate.py`

Done when:
- agreement, disagreement, and single-model fallback logic work

**Packet 6.6 — layout integration tests**

Implement:
- dual-model agreement test
- disagreement test
- single-model fallback test
- verification that layout does not execute for pages in `ptiff_qa_pending`
- IEP2A contract tests (request/response schema and error behavior)
- IEP2B contract tests (request/response schema and error behavior)

Done when:
- end-to-end layout mode is covered
- PTIFF QA gate is enforced before layout
- IEP2A and IEP2B contract tests pass

#### Phase definition of done

- layout mode works end-to-end
- IEP2B unavailable routes to review
- canonical class mapping is enforced
- layout JSON artifact written and linked
- layout does not start unless PTIFF QA stage is completed or skipped
- tests cover dual-model agreement, disagreement, single-model fallback, and PTIFF QA enforcement
- IEP2A and IEP2B contract tests pass

---

### Phase 7 — Auth, RBAC, admin/user APIs, lineage

#### Goals

Implement secured API surface for users and admins.

#### Files / modules

- `services/eep/app/auth.py`
- `services/eep/app/admin_api.py`
- `services/eep/app/users_api.py`
- `services/eep/app/lineage_api.py`
- `services/eep/app/jobs/list.py`

#### Required endpoints

- `POST /v1/auth/token`
- `GET /v1/jobs`
- `GET /v1/admin/dashboard-summary`
- `GET /v1/admin/service-health`
- `GET /v1/lineage/{job_id}/{page_number}`
- `POST /v1/users`
- `GET /v1/users`
- `PATCH /v1/users/{user_id}/deactivate`

#### Work packets

**Packet 7.1 — auth and JWT issuance**

Implement:
- `POST /v1/auth/token`
- JWT support

Done when:
- authentication works

**Packet 7.2 — RBAC helpers and enforcement**

Implement:
- role checks for user/admin access

Done when:
- RBAC works across protected endpoints

**Packet 7.3 — job list endpoint**

Implement:
- `GET /v1/jobs`

Done when:
- user-scoped and admin-scoped behavior works

**Packet 7.4 — admin dashboard endpoints**

Implement:
- `GET /v1/admin/dashboard-summary`
- `GET /v1/admin/service-health`

Done when:
- admin summaries work

**Packet 7.5 — lineage endpoint**

Implement:
- `GET /v1/lineage/{job_id}/{page_number}`

Done when:
- lineage response is complete and correct

**Packet 7.6 — user management endpoints**

Implement:
- `POST /v1/users`
- `GET /v1/users`
- `PATCH /v1/users/{user_id}/deactivate`

Done when:
- admin user management works

#### Phase definition of done

- JWT works
- RBAC works
- job list is scoped for users and global for admins
- lineage response is correct and complete

---

### Phase 8 — MLOps plumbing

#### Goals

Implement shadow pipeline, promotion/rollback metadata, retraining triggers, policy endpoints.

#### Files / modules

- `services/eep/app/shadow_enqueue.py`
- `services/eep/app/promotion_api.py`
- `services/eep/app/retraining_webhook.py`
- `services/eep/app/policy_api.py`
- `services/shadow_worker/app/main.py`
- `services/shadow_worker/app/task.py`
- `services/shadow_recovery/app/main.py`
- `services/shadow_recovery/app/reconcile.py`
- `services/retraining_worker/app/main.py`
- `services/retraining_worker/app/task.py`
- `services/retraining_recovery/app/main.py`
- `services/retraining_recovery/app/reconcile.py`

#### Required tables

The below MLOps tables must be introduced in a separate migration created and applied in Phase 8. Phase 1 must not pre-create these tables. They belong to the Phase 8 migration only.

- `shadow_results`
- `model_versions`
- `policy_versions`
- `task_retry_states`
- `retraining_triggers`
- `retraining_jobs`
- `slo_audit_samples`

#### Work packets

**Packet 8.1 — Phase 8 migration**

Implement:
- migration for all MLOps tables only

Done when:
- Phase 8 tables exist and Phase 1 tables remain separate

**Packet 8.2 — policy endpoints**

Implement:
- `services/eep/app/policy_api.py`

Done when:
- policy read/update works

**Packet 8.3 — shadow enqueue path**

Implement:
- `services/eep/app/shadow_enqueue.py`

Done when:
- eligible pages can enqueue shadow tasks

**Packet 8.4 — shadow worker and recovery service**

Implement:
- `services/shadow_worker/app/main.py`
- `services/shadow_worker/app/task.py`
- `services/shadow_recovery/app/main.py`
- `services/shadow_recovery/app/reconcile.py`

Done when:
- shadow tasks are processed and recorded
- shadow recovery can reconcile abandoned shadow work safely
- shadow worker contract tests validate request/response and error behavior

**Packet 8.5 — promotion / rollback API**

Implement:
- `services/eep/app/promotion_api.py`
- promotion and rollback execution wiring for model-version state changes

Done when:
- promote/rollback metadata path works
- rollback can change the active deployed model/version state without schema changes

**Packet 8.6 — retraining webhook and trigger recording**

Implement:
- `services/eep/app/retraining_webhook.py`

Done when:
- retraining triggers can be stored correctly

**Packet 8.7 — retraining worker and recovery service**

Implement:
- `services/retraining_worker/app/main.py`
- `services/retraining_worker/app/task.py`
- `services/retraining_recovery/app/main.py`
- `services/retraining_recovery/app/reconcile.py`

Done when:
- retraining tasks can be executed asynchronously
- retraining recovery can reconcile abandoned retraining work safely
- retraining worker contract tests validate request/response and error behavior

#### Phase definition of done

- shadow tasks enqueue and process
- shadow recovery service works
- promote/rollback endpoints work
- retraining triggers can be recorded
- retraining worker and recovery service work
- policy endpoints work
- shadow worker and retraining worker contract tests pass

---

### Phase 9 — Metrics, policy loading, drift skeleton, hardening, and golden-dataset testing

#### Goals

Finish observability, remove hardcoded thresholds, and implement golden-dataset tests.

#### Files / modules

- `shared/metrics.py`
- `monitoring/baselines.json`
- `monitoring/drift_detector.py`
- `monitoring/prometheus/prometheus.yml`
- `monitoring/alertmanager/alertmanager.yml`
- `monitoring/alert_rules/libraryai-alerts.yml`
- `monitoring/grafana/provisioning/`
- `monitoring/grafana/dashboards/`

#### Must implement

- all metrics from spec
- policy-driven thresholds from config
- drift detector skeleton
- trigger writes on simulated drift
- Prometheus scrape configuration
- Alertmanager configuration
- alert rules for meaningful operational failures
- Grafana dashboards for service, queue, and ML signals
- golden-dataset tests

Because real IEP1A/IEP1B model evaluation is deferred, `monitoring/baselines.json` must initially be created as a placeholder file with synthetic baseline means and standard deviations for all monitored metrics required by the drift detector.

These placeholder values must allow the drift detector and retraining trigger pipeline to execute without crashing.

The file must be explicitly marked as temporary and requiring replacement after real baseline evaluation from trained models.

#### Work packets

**Packet 9.1 — metrics registration**

Implement:
- `shared/metrics.py`

Done when:
- metrics required by the spec are defined

**Packet 9.2 — policy loading and threshold wiring**

Implement:
- config-driven threshold loading in gates and relevant modules

Done when:
- no gate relies on hardcoded thresholds

**Packet 9.3 — drift detector skeleton**

Implement:
- `monitoring/drift_detector.py`

Done when:
- drift detector can run against baseline values

**Packet 9.4 — placeholder baselines file**

Implement:
- `monitoring/baselines.json`

Done when:
- file is loadable
- synthetic values exist for required monitored metrics
- file is clearly marked temporary

**Packet 9.5 — observability hardening and golden-dataset tests**

Implement:
- metric wiring into real flows
- simulated drift trigger path
- golden-dataset tests for:
  - IEP1C normalization outputs against known-good reference data
  - geometry gate routing outcomes for fixed synthetic cases
  - artifact validation decisions for fixed synthetic cases
  - lineage write expectations for known scenarios
  - page state transitions for known scenarios

Done when:
- observability reflects real events
- simulated drift can write trigger records
- golden-dataset tests pass for all listed deterministic paths

**Packet 9.6 — Prometheus and alerting configuration**

Implement:
- `monitoring/prometheus/prometheus.yml`
- `monitoring/alertmanager/alertmanager.yml`
- `monitoring/alert_rules/libraryai-alerts.yml`

Done when:
- Prometheus can scrape all services
- alert rules are defined for meaningful failures
- Alertmanager configuration is valid
- alert rules include explicit retraining-trigger and rollback-trigger categories
- Alertmanager routes retraining-trigger alerts to retraining webhook
- Alertmanager routes rollback-trigger alerts to rollback endpoint

**Packet 9.7 — Grafana dashboards**

Implement:
- `monitoring/grafana/provisioning/`
- `monitoring/grafana/dashboards/`

Done when:
- Grafana dashboards exist for API, workers, queue, gate decisions, and model signals

#### Phase definition of done

- `/metrics` reflects real events
- no gate code contains magic threshold literals
- drift detector runs against baseline file
- placeholder `monitoring/baselines.json` exists and is loadable
- file is clearly marked as temporary
- Prometheus scrape configuration exists
- Alertmanager configuration and alert rules exist
- Grafana dashboards exist
- golden-dataset tests pass

---

### Phase 10 — Frontend

#### Goals

Implement admin and user UI on top of stable APIs.

#### Must include

- login
- job submission
- my jobs
- job detail
- PTIFF QA review screen
- admin dashboard
- correction queue
- correction workspace
- lineage page
- shadow models page
- retraining page
- policy page
- users page

#### Correction workspace interaction requirements

The correction workspace must provide an interactive human-review UI for preprocessing correction.

It must support at minimum:

- displaying the raw OTIFF-derived page preview
- displaying the currently selected best available output
- displaying available branch outputs (`IEP1A`, `IEP1B`, `IEP1C`, and `IEP1D` when present)
- switching between source/branch views
- interactive crop editing with draggable handles
- interactive split-line editing when split is relevant
- deskew adjustment using both numeric input and a direct manipulation control
- synchronized form fields for:
  - crop bounds
  - deskew angle
  - split position
- reviewer notes entry
- correction submit action
- correction reject action
- visual indication of review reason
- page metadata display relevant to correction review
- if no derived artifact is available for a page, the workspace must display the original raw/displayable page preview only
- if one or more derived artifacts are available, the workspace must display the original preview plus all available branch/edited outputs
- the reviewer must be able to switch the active editing/view source between the original image and any available derived output
- the original image must always remain available as a reference source

#### Work packets

**Packet 10.1 — auth and base app shell**

Implement:
- login
- routing shell
- permission-aware layout

Done when:
- authenticated app shell works

**Packet 10.2 — user job flow screens**

Implement:
- job submission
- my jobs
- job detail
- frontend upload flow using `POST /v1/uploads/jobs/presign`
- user selects:
  - `preprocess` or `layout`
  - PTIFF QA mode: `manual` or `auto_continue`

Done when:
- user job flow works
- frontend uploads raw OTIFF input through the presigned upload path before job submission

**Packet 10.2a — PTIFF QA review screen**

Implement:
- job-level QA screen after preprocessing
- options:
  - approve all pages
  - review pages individually
  - edit individual pages (routes to correction workspace)
- integration with correction workspace for edit flow

Done when:
- reviewer can finalize PTIFF stage
- reviewer can choose bulk approval or detailed inspection
- edit flow routes through correction workspace and returns to QA
- bulk approval affects only pages currently awaiting PTIFF QA

**Packet 10.3 — correction UI and interactive correction workspace**

Implement:
- correction queue
- correction workspace
- interactive crop editing
- interactive split-line editing
- deskew editing controls
- branch output switching
- raw vs selected-output compare mode
- reviewer notes entry
- correction submission and rejection actions

Done when:
- correction payloads can be submitted correctly
- reviewer can visually inspect the raw page and available branch outputs
- reviewer can modify crop bounds, deskew angle, and split position using both direct manipulation tools and form inputs
- correction workspace supports correction submit and rejection without requiring manual JSON editing

**Packet 10.3a — correction workspace UX hardening**

Implement:
- zoom controls
- rotation controls for review convenience
- compare mode between raw and corrected/selected outputs
- auto-populate of editable fields from workspace payload when available
- synchronization between canvas interactions and numeric input fields

Done when:
- reviewer can efficiently inspect and edit page corrections without leaving the workspace
- visual tools and form fields remain synchronized
- workspace interaction supports realistic human review throughput

**Packet 10.4 — admin operational screens**

Implement:
- admin dashboard
- lineage page
- users page

Done when:
- core admin operational UI works

**Packet 10.5 — MLOps admin screens**

Implement:
- shadow models page
- retraining page
- policy page

Done when:
- MLOps/admin configuration screens work

**Packet 10.6 — frontend hardening**

Implement:
- polling behavior
- preprocess-only UI suppression
- permission matrix enforcement

Done when:
- UI behavior matches spec details

#### Phase definition of done

- permission matrix enforced
- job detail polling works
- correction workspace submits valid payloads
- PTIFF QA review screen works for both bulk approval and individual page workflows
- preprocess-only jobs suppress layout-specific UI where appropriate

---

### Phase 11 — Cloud deployment, Kubernetes, Runpod, CI/CD, observability stack

#### Goals

Deploy LibraryAI as a publicly reachable cloud system using Kubernetes, Runpod CPU and on-demand GPU execution, PostgreSQL, Redis with AOF persistence, Prometheus, Alertmanager, Grafana, and an S3-compatible object storage provider.

This phase is deployment and platformization only. It must not change schemas, orchestrator logic, service contracts, state machine behavior, or gate semantics.

#### Files / modules

- `kubernetes/namespaces/`
- `kubernetes/deployments/`
- `kubernetes/services/`
- `kubernetes/configmaps/`
- `kubernetes/secrets/`
- `kubernetes/ingress/`
- `kubernetes/hpa/`
- `kubernetes/jobs/`
- `kubernetes/network-policies/`
- `kubernetes/configmaps/libraryai-policy.yaml`
- `.github/workflows/`
- deployment Dockerfiles or production image definitions as needed for all services
- Runpod backend configuration for CPU and on-demand GPU execution
- production storage configuration for an S3-compatible object storage provider

#### Required deployment architecture

The cloud deployment must include these independently deployable workloads:

- `eep-api`
- `eep-worker`
- `eep-recovery`
- `shadow-worker`
- `shadow-recovery`
- `retraining-worker`
- `retraining-recovery`
- `artifact-cleanup`
- `iep1a`
- `iep1b`
- `iep1d`
- `iep2a`
- `iep2b`
- PostgreSQL
- Redis
- Prometheus
- Alertmanager
- Grafana
- MLflow

#### Required platform behavior

- Kubernetes is required
- Docker Compose remains the local development environment only
- production secrets must not be hardcoded
- production config must be supplied through ConfigMaps and Secrets
- object storage provider may be any provider, but it must be S3-compatible
- Runpod must be supported as the production execution backend:
  - CPU endpoints where configured
  - on-demand GPU endpoints where configured
- Redis must run with AOF persistence enabled
- ingress must expose the public EEP API
- HPA must exist for at least:
  - `eep-worker`
  - `shadow-worker`
- service-to-service communication and health checks must work in cluster
- Prometheus, Alertmanager, and Grafana must run in cluster
- CI/CD must build images, run tests, and deploy manifests
- production image path must support model-weight baking where needed
- deployment must preserve DB-first artifact write semantics
- deployment must preserve recovery/reconciliation loops
- persistent volumes must be configured for at least:
  - PostgreSQL
  - Redis
  - any stateful monitoring components that require persistence

#### Work packets

**Packet 11.1 — Kubernetes base manifests**

Implement:
- namespaces
- deployments
- services
- configmaps
- secrets
- ingress
- HPA
- network policies
- persistent volume manifests or volume claim templates for stateful workloads

Done when:
- all required workloads have Kubernetes manifests
- EEP API is externally reachable through ingress
- ConfigMaps and Secrets are used correctly
- stateful workloads have persistent storage configured where required

**Packet 11.2 — Runpod production backend**

Implement:
- production GPUBackend for Runpod
- configuration for CPU endpoints
- configuration for on-demand GPU endpoints

Done when:
- EEP can route to Runpod-backed internal services without schema changes
- timeout/error classification remains correct

**Packet 11.3 — production storage and Redis durability**

Implement:
- production S3-compatible storage configuration
- Redis AOF persistence configuration
- deployment wiring for DB-first artifact semantics

Done when:
- object storage works in production
- Redis durability is configured
- artifact flow preserves required semantics

**Packet 11.4 — production image path and model-weight baking**

Implement:
- production image build strategy
- model-weight baking path for services that require bundled weights

Done when:
- services can be deployed using production-ready images
- model-loading strategy is defined and implemented where needed

**Packet 11.5 — CI/CD pipelines**

Implement:
- GitHub Actions workflows in `.github/workflows/`

Done when:
- CI runs linting and tests
- images are built automatically
- deployment pipeline exists for Kubernetes manifests

**Packet 11.6 — in-cluster observability stack**

Implement:
- Prometheus deployment
- Alertmanager deployment
- Grafana deployment
- wiring to the configs created in Phase 9

Done when:
- monitoring stack runs in cluster
- dashboards and alerts function against live deployment

**Packet 11.7 — deployment validation**

Implement:
- end-to-end deployment verification
- failure-path verification for worker restart, Redis restart, and service unavailability

Done when:
- public deployment works end-to-end
- recovery and reconciliation loops still function correctly under failure scenarios

#### Phase definition of done

- LibraryAI is deployed in Kubernetes
- the EEP API is publicly reachable
- Runpod CPU and on-demand GPU backend support is implemented
- production object storage is configured through an S3-compatible provider
- Redis AOF persistence is enabled in production
- CI/CD pipelines exist and run
- Prometheus, Alertmanager, and Grafana run in cluster
- dashboards and alerts work against the live system
- production deployment preserves recovery, reconciliation, and DB-first artifact semantics

---

### Phase 12 — Later model swap

#### Goals

Replace mock IEP1A/IEP1B inference with real trained models.

#### Rule

This phase must require:

- no schema changes
- no EEP redesign
- no gate redesign
- no worker redesign

Only inference internals and Docker/model loading should change.

#### Work packets

**Packet 12.1 — IEP1A real inference replacement**

Implement:
- real YOLOv8-seg inference in place of the mock

Done when:
- IEP1A contract remains unchanged and real inference runs

**Packet 12.2 — IEP1B real inference replacement**

Implement:
- real YOLOv8-pose inference in place of the mock

Done when:
- IEP1B contract remains unchanged and real inference runs

**Packet 12.3 — real TTA integration**

Implement:
- actual TTA execution for IEP1A and IEP1B

Done when:
- true TTA-derived outputs replace stubbed ones

**Packet 12.4 — Docker / model loading update**

Implement:
- weight loading
- image/runtime changes needed for real models

Done when:
- services boot with real models

**Packet 12.5 — swap validation pass**

Implement:
- regression verification that EEP, schemas, gates, and worker required no redesign

Done when:
- real model swap is confirmed as drop-in only

#### Phase definition of done

- real IEP1A and IEP1B replace mocks without schema or orchestration redesign
- only inference internals and deployment/runtime details changed

---

## 5. Risk Register

**Risk 1 — IEP1C geometry math bugs**
A bug in homography, crop logic, corner ordering, or deskew can make every downstream result wrong.
Mitigation: test with synthetic cases and real sample images early.

**Risk 2 — Wrong `failed` routing**
The biggest semantic bug is sending content failures to `failed` instead of `pending_human_correction`.
Mitigation: centralize failure classification; test every exception path.

**Risk 3 — Leaf-page status derivation bugs**
Split-parent vs leaf-page counting is easy to get wrong and can break job status.
Mitigation: central helper for job status derivation; dedicated tests.

**Risk 4 — Split idempotency bugs**
Retries or crashes may create duplicate children.
Mitigation: enforce DB uniqueness and idempotent creation logic.

**Risk 5 — Mock-to-real dependency leak**
If the system relies on fixed mock confidence distributions, it may break when real models arrive.
Mitigation: vary mock outputs during tests.

**Risk 6 — IEP2 canonical mapping bugs**
If non-canonical classes leak through, the consensus gate becomes incorrect.
Mitigation: strict tests on output vocabulary.

**Risk 7 — DB-first artifact write inconsistencies**
S3 write and DB confirmation can diverge.
Mitigation: implement recovery handling early.

**Risk 8 — Hidden threshold hardcoding**
Hardcoded numbers in gates will diverge from policy.
Mitigation: all thresholds must come from a config object.

**Risk 9 — Queue ownership and recovery bugs**
Incorrect queue claim/reclaim behavior can cause duplicate processing, stuck work, or silent task loss.
Mitigation: implement processing-list ownership, dead-letter handling, reconnect-safe recovery loops, and restart simulation tests.

---

## 6. Checklist Contract

After completing a phase, the agent must:

- Mark that phase complete in `docs_pre_implementation/implementation_checklist.md`
- Add a one-line summary of what was implemented
- Add a one-line `Blocked/blocking:` note if anything remains blocked
- List any spec constraints that were especially relevant in that phase
- Never mark a phase complete if any item in that phase's Definition of Done remains unmet

---

## 7. Required Evaluation and Test Tracks

The following tracks are mandatory and must not be omitted from implementation even if some model internals are initially mocked.

### 7.1 Contract tests

Contract tests must validate request/response compatibility, error behavior, and readiness behavior for:

- EEP API → Phase 1 (Packets 1.8, 1.9)
- EEP worker-facing queue contract → Phase 4 (Packet 4.8)
- IEP1A → Phase 2 (Packet 2.1)
- IEP1B → Phase 2 (Packet 2.3)
- IEP1D → Phase 4 (Packet 4.8)
- IEP2A → Phase 6 (Packet 6.6)
- IEP2B → Phase 6 (Packet 6.6)
- shadow worker paths → Phase 8 (Packet 8.4)
- retraining worker paths → Phase 8 (Packet 8.7)

### 7.2 Simulation tests

Simulation tests must cover controlled failure and routing scenarios including:

- first-pass disagreement
- second-pass disagreement
- service timeout
- service cold-start timeout
- malformed model response
- Redis reconnect
- worker crash during processing
- split retry/idempotency scenarios

All simulation tests are assigned to Phase 4, Packet 4.8.

### 7.3 Golden-dataset tests

Golden-dataset tests must exist for deterministic or partially deterministic paths, including at minimum:

- IEP1C normalization outputs
- geometry gate routing outcomes for fixed synthetic cases
- artifact validation decisions for fixed synthetic cases
- lineage write expectations
- page state transitions

All golden-dataset tests are assigned to Phase 9, Packet 9.5.

### 7.4 Eligibility and rectification evaluation hooks

If IEP1D internals are mocked initially, the codebase must still include explicit evaluation hooks and replacement points for:

- rectification eligibility logic
- rectification quality evaluation
- future UVDoc-backed inference replacement

---

## 8. Enforcement Appendix (Legacy-Checklist Strength)

This appendix is mandatory. It defines non-negotiable implementation invariants and acceptance gates that must be satisfied before phase completion claims.

### 8.1 Database Invariants (Must Hold)

1. `pgcrypto` extension must be created before any `gen_random_uuid()` default is used.
2. `job_pages` must enforce split-safe uniqueness so unsplit parent rows cannot duplicate and split children cannot duplicate.
3. Split parent rows must be retained as lineage/provenance records and must not be deleted after child creation.
4. `page_lineage` must enforce one-row identity per logical page key.
5. Durable artifact URIs must be authoritative in `page_lineage`; `job_pages` must not become a duplicate source for durable output URIs.
6. `job_pages.updated_at` must be maintained by DB trigger on all updates.
7. Attempt fencing fields must exist and be enforced for safe retry/recovery ownership transitions.
8. Terminal-state rows must not retain active-attempt ownership fields.
9. `service_invocations` must support per-attempt/per-phase uniqueness to preserve retry and post-rectification provenance.
10. `quality_gate_log` must record gate decisions and route decisions for all gate executions.
11. Phase 1 migration must contain only Phase 1 core tables; Phase 8 MLOps tables must be introduced only in Phase 8 migration.
12. Schema and constraints required by PTIFF QA (`ptiff_qa_mode`, `ptiff_qa_pending`) must exist before worker PTIFF routing is implemented.

### 8.2 Queue and Recovery Invariants (Must Hold)

1. Queue move must use `BLMOVE` when available, with `BRPOPLPUSH` fallback where needed.
2. Processing-list ownership is required; no task may be considered in-flight without ownership state.
3. Retry must preserve task identity semantics required for idempotent reconciliation.
4. Dead-letter path is mandatory after retry-budget exhaustion.
5. DB is authoritative for reconciliation after Redis faults; Redis task hashes/lists are advisory.
6. Recovery services (`eep_recovery`, `shadow_recovery`, `retraining_recovery`) must be independent processes, not embedded in API/worker loops.
7. On Redis reconnect/epoch change, workers must pause dequeue until recovery marks control plane ready.
8. Reconciliation must detect and repair abandoned, stuck, and orphaned tasks without duplicate terminalization.
9. Queue control-plane behavior must be shared and consistent across page, shadow, and retraining domains.
10. No phase may claim done while restart/reconnect simulations for its queue domain are failing.
11. Background workers and recovery services must expose lightweight HTTP probe endpoints (`/health`, `/ready`, `/metrics`) for liveness, readiness, and observability; these endpoints must not block or interfere with main processing loops.

### 8.3 Failure-Routing Matrix (Authoritative)

| Failure class | Examples | Retry path | Terminal/routing outcome |
|---|---|---|---|
| Transient infrastructure dependency failure | timeout, cold-start timeout, temporary service unavailability, circuit-open | Yes, bounded retry/requeue | `failed` only after retry-budget exhaustion with infra metadata |
| Content/quality preprocessing failure | geometry disagreement after required passes, invalid artifact quality | No infra retry loop | `pending_human_correction` |
| Layout single-model fallback or consensus failure | IEP2B unavailable, low-consensus layout result | No auto-accept | `review` or `pending_human_correction` per spec route contract |
| Data integrity unrecoverable failure | unreadable/corrupt input, hash mismatch, irrecoverable data integrity break | No | `failed` |
| PTIFF QA manual partial approval | some pages approved, gate not yet fully satisfied | N/A | remain `ptiff_qa_pending` until gate release; gate release requires all pages to be QA-resolved (approved or returned from correction) and no page remains in correction flow |
| Human correction reject | reviewer rejects correction | N/A | `review` |


### 8.4 Must-Pass Test Matrix (Phase Completion Gates)

| Test ID | Scope | Assigned phase/packet | Pass requirement |
|---|---|---|---|
| CT-API-01 | `POST /v1/jobs`, `GET /v1/jobs/{job_id}`, `POST /v1/uploads/jobs/presign` contracts | Phase 1 (1.8, 1.9, 1.7b) | schema + error behavior + auth/scoping pass |
| CT-IEP1-01 | IEP1A/IEP1B contract tests | Phase 2 (2.1, 2.3) | request/response/error/readiness pass |
| CT-WKR-01 | EEP worker queue contract + IEP1D contract | Phase 4 (4.8) | ownership/fencing/recovery contract pass |
| CT-IEP2-01 | IEP2A/IEP2B contract tests | Phase 6 (6.6) | schema/error/readiness + canonical class mapping pass |
| CT-MLOPS-01 | shadow/retraining worker contract tests | Phase 8 (8.4, 8.7) | async execution + recovery contract pass |
| SIM-01 | first-pass disagreement behavior | Phase 4 (4.8) | low-trust path, no invalid fail routing |
| SIM-02 | second-pass disagreement behavior | Phase 4 (4.8) | routes to `pending_human_correction` |
| SIM-03 | timeout/cold-start timeout handling | Phase 4 (4.8) | bounded retry + correct classification |
| SIM-04 | malformed model response | Phase 4 (4.8) | no silent success, correct route/logging |
| SIM-05 | Redis reconnect recovery | Phase 4 (4.8) | pause/reconcile/resume behavior correct |
| SIM-06 | worker crash mid-task | Phase 4 (4.8) | no task loss, no duplicate terminalization |
| SIM-07 | split retry/idempotency | Phase 4 (4.8) | no duplicate children, parent lifecycle preserved |
| GOLD-01 | IEP1C deterministic normalization references | Phase 9 (9.5) | outputs match known-good tolerances |
| GOLD-02 | geometry gate deterministic routing cases | Phase 9 (9.5) | expected route outcomes match fixtures |
| GOLD-03 | artifact validation deterministic cases | Phase 9 (9.5) | threshold and hard-fail behavior match fixtures |
| GOLD-04 | lineage write expectations | Phase 9 (9.5) | required lineage/provenance records present |
| GOLD-05 | page-state transition expectations | Phase 9 (9.5) | transitions match `shared/state_machine.py` |

Rule: A phase cannot be marked complete if any mandatory test assigned to that phase is failing or missing.

### 8.5 Hard Rules (Must Never Be Violated)

1. No single-model auto-acceptance in IEP1 final acceptance.
2. No single-model auto-acceptance in IEP2.
3. Confidence must never override structural agreement requirements.
4. Content failures must never be routed to `failed`.
5. `failed` is only for unrecoverable infrastructure/data integrity failures.
6. No silent page loss is permitted.
7. No destructive overwrite of OTIFF is permitted.
8. All gate decisions must be logged to `quality_gate_log`; no silent gate bypass.
9. DB-first artifact write protocol must be preserved.
10. PTIFF QA manual mode is a job-level gate; partial approval must not bypass gate release rules.
11. State transitions must be enforced by one shared transition contract (`shared/state_machine.py`).
12. Recovery services must remain independent from API and worker loops.
13. Policy thresholds must be loaded from policy/config, not hardcoded literals.
14. Switching local HTTP backend ↔ Runpod backend must require no schema or orchestrator contract changes.
15. Phase 12 model swap must be inference-internal only and must not redesign schemas, gates, or worker orchestration.
