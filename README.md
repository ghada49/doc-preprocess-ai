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

## Model serving

`IEP2A` and `IEP2B` keep their existing HTTP contracts and detector semantics:

- `IEP2A` serves Detectron2-based PubLayNet layout detection by default and
  can optionally run PaddleOCR PP-DocLayoutV2 behind
  `IEP2A_LAYOUT_BACKEND=paddleocr`.
- `IEP2B` serves DocLayout-YOLO layout detection.

Production serving is local-artifact only:

- candidate or staging artifacts may live in MLflow or S3 during the MLOps lifecycle
- once a model version is approved, build a versioned inference image that contains the exact checkpoint
- bake the matching artifact version sidecar alongside the checkpoint as `<weights>.version`
- runtime startup loads weights only from local in-image paths under `/opt/models`
- `/ready` depends only on successful local model load, never on remote downloads

Default real-mode artifact locations:

- `IEP2A_WEIGHTS_PATH=/opt/models/iep2a/model_final.pth`
- `IEP2A_PADDLE_MODEL_DIR=/opt/models/iep2a/paddle/PP-DocLayoutV2`
- `IEP2B_WEIGHTS_PATH=/opt/models/iep2b/doclayout_yolo_docstructbench_imgsz1024.pt`
- `IEP2A_CONFIG_PATH` is optional; if unset, `IEP2A` uses the packaged
  Detectron2 `faster_rcnn_R_50_FPN_3x` config shipped inside the image

Local development overrides remain available:

- `docker-compose.yml` mounts `./models/iep2a` to `/dev-models/iep2a`
- `docker-compose.yml` mounts `./models/iep2b` to `/dev-models/iep2b`
- set `IEP2A_LOCAL_WEIGHTS_PATH`, `IEP2A_PADDLE_LOCAL_MODEL_DIR`, or `IEP2B_LOCAL_WEIGHTS_PATH`
  to use those mounted files in real mode
- local Paddle model loads disable Paddle's model-source connectivity check by
  default; override with `IEP2A_PADDLE_DISABLE_MODEL_SOURCE_CHECK=false` only
  if you intentionally want that upstream probe

Model version metadata:

- production images must place a `<weights>.version` file next to each baked checkpoint
- that sidecar becomes the authoritative `model_version` logged at startup and returned by `/v1/layout-detect`
- `IEP2A_MODEL_VERSION` and `IEP2B_MODEL_VERSION` are validation or dev-override inputs only; if they disagree with a sidecar, startup fails
- PaddleOCR uses the same rule, except the sidecar is next to the baked model
  directory: `PP-DocLayoutV2.version`

Typical production image build flow:

```bash
# Place approved artifacts in the local build context without committing them.
# See models/iep2a/README.txt and models/iep2b/README.txt for expected names.

docker compose build iep2a iep2b
docker compose up -d iep2a iep2b
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
