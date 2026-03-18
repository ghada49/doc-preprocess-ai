# LibraryAI

Automated archival document processing pipeline for the AUB Library.

## What it does

LibraryAI processes raw scanned images (OTIFF) into corrected, layout-annotated pages
suitable for archival ingestion. It automates page-geometry detection, deskewing, cropping,
page splitting, and layout detection while preserving reliability, traceability, and
recoverability.

**No incorrect page is silently auto-accepted. No page is silently lost.**

## Architecture

| Service   | Port | Role |
|-----------|------|------|
| EEP       | 8000 | Central orchestrator — job management, quality gates, routing |
| IEP1A     | 8001 | YOLOv8-seg geometry service (mock → real in Phase 12) |
| IEP1B     | 8002 | YOLOv8-pose geometry service (mock → real in Phase 12) |
| IEP1C     | —    | Deterministic normalization (shared module, not a network service) |
| IEP1D     | 8003 | UVDoc rectification fallback |
| IEP2A     | 8004 | Detectron2 layout detection |
| IEP2B     | 8005 | DocLayout-YOLO layout detection |

Background processes: `eep_worker`, `eep_recovery`, `shadow_worker`, `shadow_recovery`,
`retraining_worker`, `retraining_recovery`, `artifact_cleanup`.

## Prerequisites

- Docker + Docker Compose v2
- Python 3.11
- [uv](https://github.com/astral-sh/uv) — `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Local setup

```bash
# Copy and edit environment config
cp .env.example .env
# Fill in secrets in .env before starting

# Install dev tooling
uv sync

# Start all services
make up

# Run health checks
make health

# Run tests
make test
```

## Repository structure

```
services/                  — FastAPI service entrypoints and business logic
  eep/                     — EEP API server
  eep_worker/              — EEP page-processing worker
  eep_recovery/            — EEP recovery and reconciliation service
  shadow_worker/           — Shadow evaluation worker
  shadow_recovery/         — Shadow recovery service
  retraining_worker/       — Retraining trigger worker
  retraining_recovery/     — Retraining recovery service
  artifact_cleanup/        — Artifact lifecycle cleanup service
  iep1a/                   — IEP1A YOLOv8-seg geometry service
  iep1b/                   — IEP1B YOLOv8-pose geometry service
  iep1d/                   — IEP1D UVDoc rectification service
  iep2a/                   — IEP2A Detectron2 layout detection service
  iep2b/                   — IEP2B DocLayout-YOLO layout detection service
shared/                    — Shared Python modules (schemas, normalization, storage, GPU)
training/                  — Model training pipelines
monitoring/                — Prometheus, Alertmanager, and Grafana configuration
tests/                     — Contract, simulation, integration, and golden-dataset tests
docs/                      — Implementation documentation and architecture notes
docs_pre_implementation/   — Authoritative specification and roadmap (do not edit)
```

## Authoritative documents

- `docs_pre_implementation/full_updated_spec.md` — system specification (source of truth)
- `docs_pre_implementation/implementation_roadmap.md` — implementation execution plan
- `docs_pre_implementation/implementation_checklist.md` — phase completion ledger
- `docs_pre_implementation/outcome_spec.md` — project outcome spec and acceptance tests

## Development

```bash
make lint        # ruff + black --check + isort --check
make format      # black + isort + ruff --fix
make typecheck   # mypy
make test        # pytest tests/
make pre-commit  # pre-commit run --all-files
```
