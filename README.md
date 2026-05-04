# LibraryAI

AI-assisted archival document processing pipeline for the AUB Library.

LibraryAI processes raw scanned images through geometry correction, quality gating, layout detection, and human review — producing corrected, traceable page artifacts ready for archival ingestion.

**No incorrect page is silently auto-accepted. No page is silently lost.**

---

## Table of contents

- [What it does](#what-it-does)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Service URLs](#service-urls)
- [Testing the deployed staging app](#testing-the-deployed-staging-app)
- [Running tests](#running-tests)
- [Development commands](#development-commands)
- [Frontend development](#frontend-development)
- [Optional features](#optional-features)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)

---

## What it does

Pages submitted to the API are processed through a multi-stage worker pipeline:

1. **Material classification** — identifies book, newspaper, manuscript, or document
2. **Geometry detection** — two competing YOLOv8 models detect page boundaries
3. **Normalization** — deterministic deskew, crop, perspective warp, and quality scoring
4. **Quality gates** — accept clean results; uncertain pages go to rectification rescue or human review
5. **Layout detection** — two layout detectors identify regions; results are adjudicated (with optional Google Document AI fallback)
6. **Human correction** — librarians review and correct pages via the web UI
7. **Training export** — accepted corrections are exported as training datasets for model improvement

---

## Prerequisites

| Tool | Minimum version | Install |
|------|----------------|---------|
| **Docker** | 24+ | [docs.docker.com](https://docs.docker.com/get-docker/) |
| **Docker Compose v2** | bundled with Docker Desktop | included above |
| **Python** | 3.11 | [python.org](https://www.python.org/) — only needed to run tests locally |
| **uv** | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **make** | any | optional — wraps common commands |
| **Node.js** | 18+ | only needed for frontend development outside Docker |

---

## Quick start

### 1. Clone the repository

```bash
git clone <repo-url>
cd doc-preprocess-ai
```

### 2. Create the local environment file

```bash
cp .env.example .env
```

The default `.env` is designed for local Docker Compose development. Docker Compose also loads `docker-compose.override.yml` automatically. That override is local-only: it exposes the frontend/API on localhost, uses the local MinIO bucket, sets browser-reachable upload URLs, and enables immediate processing without changing the cloud deployment files.

For a local demo, set an admin account in `.env` before the first start:

```env
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=yourpassword
```

### 3. Add model files if you want real inference

The stack can start in mock mode, but real model inference requires downloaded model files. Download the trained models from:

https://drive.google.com/drive/folders/1lGB0ZF9BsQPF25X364uO-d5PM_xGJZoG

Place the downloaded files under `models/` in the matching IEP folders, following the Drive structure:

```text
models/
  iep0/
  iep1a/
  iep1b/
  iep2a/
  iep2b/
```

Model weights are not committed to Git. Keep them local or in managed model storage.

### 4. Install Python dev dependencies

This is needed for running tests and developer commands from the host machine:

```bash
uv sync
```

### 5. Start the local stack

```bash
docker compose up -d
# or: make up
```

The first run pulls images and builds containers; allow a few minutes. The stack starts Postgres, Redis, MinIO, MLflow, monitoring, the frontend, EEP API, EEP worker, and IEP inference services.

If uploads fail because the MinIO bucket was not created yet, run:

```bash
docker compose run --rm minio-init
```

### 6. Check health

```bash
# EEP API (main orchestrator)
curl -sf http://localhost:8888/v1/status

# Quick IEP health subset from the Makefile
make health
```

Expected output from the first command: `{"status":"ok","service":"eep"}`

> **Windows PowerShell note:** if `curl -sf` fails because PowerShell aliases `curl`, use `curl.exe -sf http://127.0.0.1:8888/v1/status`.

> **Note:** `make health` checks the legacy local health subset in the Makefile. EEP runs on port **8888** and should be checked separately. The full local service list, including IEP0 on **8006** and IEP1E on **8007**, is in [Service URLs](#service-urls).

### 7. Open the UI

Go to **http://localhost:3000** in your browser and log in with the admin account.

If `localhost` behaves unexpectedly on Windows, use **http://127.0.0.1:3000**.

If you did not set `BOOTSTRAP_ADMIN_PASSWORD`, create the account manually:

```bash
docker compose exec eep python -m scripts.create_admin \
  --username admin --password yourpassword --email admin@example.com
```

### 8. Test with sample files

If you want to test the system with sample scans, use the `testing dataset/` folder. It contains sample inputs that can be uploaded through the frontend for local smoke tests, validation, and demonstrations.

### 9. Get an API token (optional, for direct API use)

```bash
curl -s -X POST http://localhost:8888/v1/auth/token \
  -d "username=admin&password=yourpassword"
```

This returns a JWT access token. Pass it as `Authorization: Bearer <token>` on subsequent requests.

---

## Service URLs

| URL | What it is | Default credentials |
|-----|-----------|-------------------|
| http://localhost:3000 | Frontend (jobs, correction queue, admin) | admin / your bootstrap password |
| http://localhost:8888/v1/status | EEP API health check | — |
| http://localhost:8888/docs | EEP FastAPI interactive docs | — |
| http://localhost:9001 | MinIO web console | minioadmin / minioadmin |
| http://localhost:5000 | MLflow experiment tracking | — |
| http://localhost:9090 | Prometheus metrics | — |
| http://localhost:3001 | Grafana dashboards | admin / admin |
| http://localhost:9093 | Alertmanager | — |

**IEP inference services (internal, accessible on host for debugging):**

| URL | Service |
|-----|---------|
| http://localhost:8001/health | IEP1A — geometry segmentation |
| http://localhost:8002/health | IEP1B — geometry pose |
| http://localhost:8003/health | IEP1D — rectification rescue |
| http://localhost:8004/health | IEP2A — layout detection |
| http://localhost:8005/health | IEP2B — layout detection |
| http://localhost:8006/health | IEP0 — material classifier |
| http://localhost:8007/health | IEP1E — semantic normalization |

---

## Testing the deployed staging app

If you want to test the deployed staging version instead of running everything locally, open:

http://libraryai-staging-alb-520535967.eu-central-1.elb.amazonaws.com/

Use this link for hosted demos, quick frontend checks, or validating the deployed environment. For local development and upload testing, use the Quick start steps above.

---

## Running tests

Tests run against the host Python environment using `uv`. Make sure you have run `uv sync` first.

If you want to test the system with sample files, use the `testing dataset/` folder. It includes sample inputs for local smoke tests, validation, and demonstrations.

```bash
# Full test suite
uv run pytest tests/ -v
# or: make test

# Single file
uv run pytest tests/test_worker_loop_preprocessing.py -v

# With coverage
uv run pytest tests/ --cov=services --cov=shared --cov-report=term-missing
```

**Tests excluded from CI** (require external services — run manually when needed):

| File | Reason |
|------|--------|
| `tests/test_p1_migration.py` | Needs a live PostgreSQL connection |
| `tests/test_google_document_ai.py` | Needs Google Cloud credentials |
| `tests/test_p2_2_google_worker_config.py` | Needs Google Cloud credentials |

```bash
# Run migration tests against local postgres
uv run pytest tests/test_p1_migration.py -v
```

---

## Development commands

```bash
make up          # docker compose up -d
make down        # docker compose down
make build       # docker compose build
make logs        # docker compose logs -f
make restart     # docker compose restart
make test        # pytest tests/ -v
make lint        # check style: ruff + black + isort
make format      # auto-fix: black + isort + ruff --fix
make typecheck   # mypy type checking
make pre-commit  # run all pre-commit hooks
make health      # curl /health on IEP ports 8000–8005
```

### Code style

```bash
# Check
make lint

# Auto-fix
make format
```

Tooling: **ruff** (linting), **black** (formatting), **isort** (import order), **mypy** (types).

---

## Frontend development

The frontend runs inside Docker by default with hot-reload enabled.

To run it locally outside Docker:

```bash
cd frontend
npm install
npm run dev        # http://localhost:3000 with hot-reload
```

Available scripts (defined in `frontend/package.json`):

```bash
npm run dev        # development server
npm run build      # production build
npm run lint       # ESLint via Next.js
npm run type-check # TypeScript type check (tsc --noEmit)
```

> There is no `npm test` — frontend unit tests are not part of this project.

---

## Optional features

### Trained model files

The trained models used by the system are available in Google Drive:

https://drive.google.com/drive/folders/1lGB0ZF9BsQPF25X364uO-d5PM_xGJZoG

The Drive folder contains the model files used for classification, segmentation, pose estimation, and layout detection. To run the system locally with real models, download the files and place them under the repository's `models/` directory. Each model should go in its corresponding IEP folder, following the same structure as the Drive folder, for example:

```text
models/
  iep0/   # material classification
  iep1a/  # geometry segmentation
  iep1b/  # geometry pose estimation
  iep2a/  # layout detection
  iep2b/  # alternative layout detection
```

Model weights are intentionally not committed to Git. Keep downloaded model files local or store them in managed model storage for deployment.

### Real layout model weights (IEP2A, IEP2B)

IEP services start in **mock mode** by default. To use real model weights:

```bash
# In .env:
IEP2A_ENABLE_REAL_MODEL=true
IEP2B_ENABLE_REAL_MODEL=true

# Mount local weight files
IEP2A_LOCAL_WEIGHTS_PATH=./models/iep2a/model_final.pth
IEP2B_LOCAL_WEIGHTS_PATH=./models/iep2b/doclayout_yolo_docstructbench_imgsz1024.pt
```

Then rebuild:

```bash
docker compose build iep2a iep2b
docker compose up -d iep2a iep2b
```

See `models/iep2a/README.txt` and `models/iep2b/README.txt` for expected file names.

### IEP1D rectification weights

```bash
# In .env:
IEP1D_LOCAL_WEIGHTS_PATH=./models/iep1d/best_model.pkl
```

### Paddle PP-DocLayoutV2 backend (IEP2A alternative)

```bash
# In .env:
IEP2A_ENABLE_REAL_MODEL=true
IEP2A_ENABLE_PADDLE_BACKEND=true
IEP2A_LAYOUT_BACKEND=paddleocr
IEP2A_PADDLE_LOCAL_MODEL_DIR=./models/iep2a/paddle/PP-DocLayoutV2
```

### Google Document AI (layout fallback)

Disabled by default. To enable:

1. Obtain a Google Cloud service account key JSON file.
2. Set in `.env`:

```bash
GOOGLE_ENABLED=true
GOOGLE_PROJECT_ID=your-gcp-project-id
GOOGLE_PROCESSOR_ID_LAYOUT=your-layout-processor-id
GOOGLE_CREDENTIALS_HOST_PATH=/absolute/path/to/service-account-key.json
```

The file at `GOOGLE_CREDENTIALS_HOST_PATH` is bind-mounted into the container at `/var/secrets/google/key.json` by `docker-compose.yml`.

### Live retraining

The retraining worker runs in **stub mode** by default (training is simulated, no actual training runs). To enable real training:

```bash
# In .env:
LIBRARYAI_RETRAINING_TRAIN=live
LIBRARYAI_RETRAINING_GOLDEN_EVAL=live
RETRAINING_TRAIN_MANIFEST=/path/to/manifest.json
```

The manifest JSON shape:

```json
{
  "iep0": {"data_root": "/abs/ImageFolder"},
  "iep1a": {"book": "/abs/book/data.yaml", "newspaper": "/abs/newspaper/data.yaml"},
  "iep1b": {"book": "/abs/book/data.yaml", "newspaper": "/abs/newspaper/data.yaml"}
}
```

See `.env.example` for all available retraining env vars.

### Dataset builder

The dataset builder is excluded from the default Compose stack. Run it on demand:

```bash
docker compose --profile dataset-build up dataset-builder
```

---

## Project structure

```
services/
  eep/                  FastAPI orchestration API (port 8888→8000)
  eep_worker/           Async page processing worker
  eep_recovery/         Task recovery and reconciliation
  shadow_worker/        Shadow model evaluation worker
  retraining_worker/    Retraining trigger handler
  dataset_builder/      Training dataset export (Compose profile: dataset-build)
  iep0/                 Material classifier — YOLOv8-cls (port 8006)
  iep1a/                Geometry segmentation — YOLOv8-seg (port 8001)
  iep1b/                Geometry pose — YOLOv8-pose (port 8002)
  iep1d/                Rectification rescue — UVDoc (port 8003)
  iep1e/                Semantic normalization — mock default (port 8007)
  iep2a/                Layout detection — Detectron2 or Paddle (port 8004)
  iep2b/                Layout detection — DocLayout-YOLO (port 8005)
shared/                 Shared schemas, DB models, storage, metrics, normalization
frontend/               Next.js admin UI
monitoring/             Prometheus, Grafana, Alertmanager config
training/               Training scripts and golden evaluation
tests/                  Pytest test suite (104 test files)
k8s/
  ecs/                  AWS ECS task definitions (cloud deployment)
  *.yaml                Kubernetes manifests (alternative deployment path)
.github/workflows/      CI/CD, scale-up/down, observability workflows
docker-compose.yml
.env.example
pyproject.toml
Makefile
```

---

## Troubleshooting

**`make up` fails with `.env` missing**

```bash
cp .env.example .env
```

**EEP fails to start — database connection refused**

Postgres may still be starting. Wait a few seconds and restart EEP:

```bash
docker compose restart eep
```

Or check Postgres health:

```bash
docker compose ps postgres
```

**MinIO bucket missing — uploads fail**

The `minio-init` service creates the bucket on first start. If it failed, recreate it:

```bash
docker compose run --rm minio-init
```

Or create the bucket manually via the MinIO console at http://localhost:9001 (login: minioadmin / minioadmin).

**Worker is not processing jobs**

```bash
docker compose logs eep-worker -f
```

Check Redis is reachable:

```bash
docker compose exec redis redis-cli ping
# Expected: PONG
```

**Port already in use**

Find the conflict and stop the service, then:

```bash
docker compose down
docker compose up -d
```

**Tests fail with import errors**

```bash
uv sync   # re-install all dependencies
```

**IEP service returning mock data unexpectedly**

IEP services run in mock mode by default. Check the service logs:

```bash
docker compose logs iep1a
```

Look for `mock_mode: true` in the startup log. To use real models, see [Optional features](#optional-features).

---

## Documentation

| File | Contents |
|------|---------|
| `docs/02_ARCHITECTURE.md` | Service architecture, components, evidence |
| `docs/03_AI_PIPELINE.md` | Pipeline stages, IEP roles, gates |
| `docs/04_API_CONTRACTS.md` | All API endpoints, auth, request/response fields |
| `docs/05_DEPLOYMENT.md` | Local and cloud deployment guide |
| `docs/08_HUMAN_REVIEW_AND_RETRAINING.md` | Correction workflow and retraining pipeline |
| `docs/09_MLOPS_OBSERVABILITY.md` | MLflow, Prometheus, Grafana |
| `docs/10_QA_TESTING_VALIDATION.md` | Test suite structure and CI |
