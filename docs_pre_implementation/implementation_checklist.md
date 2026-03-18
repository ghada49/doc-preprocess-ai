# LibraryAI Implementation Checklist

This file is the execution ledger for the project.
It must be updated after every completed phase.
A phase must never be marked complete if any item in its Definition of Done remains unmet.

## Legend
- [ ] Not started
- [~] In progress
- [x] Complete

## Phases

### ☐ Phase 0 — Repo, containers, skeletons

- ☐ Packet 0.1 — repository structure and root files
- ☐ Packet 0.2 — docker-compose and service bootstrapping
- ☐ Packet 0.3 — shared health, metrics, logging, middleware
- ☐ Packet 0.4 — API and model service skeleton entrypoints
- ☐ Packet 0.5 — GPU backend local HTTP stub
- ☐ Packet 0.6 — service skeletons for worker, recovery, and maintenance processes
- ☐ Packet 0.7 — checklist creation

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

### ☐ Phase 1 — Schemas, DB, storage, Redis, job API

- ☐ Packet 1.1 — UCF and preprocessing schemas
- ☐ Packet 1.2 — geometry, normalization, iep1d, layout schemas
- ☐ Packet 1.3 — EEP schemas and terminal page states
- ☐ Packet 1.3a — page state machine contract
- ☐ Packet 1.4 — storage backends
- ☐ Packet 1.5 — core DB migration
- ☐ Packet 1.6 — ORM / DB model layer
- ☐ Packet 1.7 — Redis queue setup
- ☐ Packet 1.7a — reliable Redis queue contract
- ☐ Packet 1.7b — presigned upload endpoint
- ☐ Packet 1.8 — job creation endpoint
- ☐ Packet 1.9 — job status endpoint

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

### ☐ Phase 2 — IEP1A/B mocks + IEP1C

- ☐ Packet 2.1 — IEP1A mock service shell
- ☐ Packet 2.2 — IEP1A TTA mock behavior
- ☐ Packet 2.3 — IEP1B mock service shell
- ☐ Packet 2.4 — IEP1B TTA mock behavior
- ☐ Packet 2.5 — normalization core
- ☐ Packet 2.6 — split handling
- ☐ Packet 2.7 — quality metrics

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

### ☐ Phase 3 — Geometry selection + artifact validation

- ☐ Packet 3.1 — structural agreement and sanity checks
- ☐ Packet 3.2 — split confidence, variance, page area preference
- ☐ Packet 3.3 — final selection and route-to-human logic
- ☐ Packet 3.4 — artifact hard requirements
- ☐ Packet 3.5 — artifact soft score and threshold logic
- ☐ Packet 3.6 — gate test suite

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

### ☐ Phase 4 — Full IEP1 worker orchestration

- ☐ Packet 4.1 — worker concurrency and circuit breaker
- ☐ Packet 4.2 — page state and lineage DB helpers
- ☐ Packet 4.3a — intake, hash, proxy image derivation
- ☐ Packet 4.3b — parallel geometry invocation and selection wiring
- ☐ Packet 4.4 — normalization and first validation
- ☐ Packet 4.5 — rescue flow (rectification, second geometry pass, second normalization, final validation)
- ☐ Packet 4.6 — split handling, PTIFF QA routing, and preprocess-only stop path
- ☐ Packet 4.7 — watchdog and recovery service
- ☐ Packet 4.8 — worker integration tests

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

### ☐ Phase 5 — Human correction workflow + PTIFF QA

- ☐ Packet 5.0 — correction workspace response schema and data assembly
- ☐ Packet 5.0a — PTIFF QA workflow (job-level review gate)
- ☐ Packet 5.1 — correction queue read endpoints
- ☐ Packet 5.2 — single-page correction apply path
- ☐ Packet 5.3 — split correction apply path
- ☐ Packet 5.4 — correction reject path
- ☐ Packet 5.5 — correction and PTIFF QA tests

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

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

### ☐ Phase 7 — Auth, RBAC, admin/user APIs, lineage

- ☐ Packet 7.1 — auth and JWT issuance
- ☐ Packet 7.2 — RBAC helpers and enforcement
- ☐ Packet 7.3 — job list endpoint
- ☐ Packet 7.4 — admin dashboard endpoints
- ☐ Packet 7.5 — lineage endpoint
- ☐ Packet 7.6 — user management endpoints

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

### ☐ Phase 8 — MLOps plumbing

- ☐ Packet 8.1 — Phase 8 migration
- ☐ Packet 8.2 — policy endpoints
- ☐ Packet 8.3 — shadow enqueue path
- ☐ Packet 8.4 — shadow worker and recovery service
- ☐ Packet 8.5 — promotion / rollback API
- ☐ Packet 8.6 — retraining webhook and trigger recording
- ☐ Packet 8.7 — retraining worker and recovery service

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

### ☐ Phase 9 — Metrics, policy loading, drift skeleton, hardening

- ☐ Packet 9.1 — metrics registration
- ☐ Packet 9.2 — policy loading and threshold wiring
- ☐ Packet 9.3 — drift detector skeleton
- ☐ Packet 9.4 — placeholder baselines file
- ☐ Packet 9.5 — observability hardening and golden-dataset tests
- ☐ Packet 9.6 — Prometheus and alerting configuration
- ☐ Packet 9.7 — Grafana dashboards

- **Summary:**
- **Blocked/blocking:**
- **Relevant spec constraints:**

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