# Architecture

## Purpose

LibraryAI is a multi-service document preprocessing and AI-assisted pipeline. Its central API (**EEP**, Execution Engine Pipeline) coordinates jobs and quality decisions; separate **worker** and **inference (IEP)** services perform page-level processing. The design aims to produce corrected, layout-aware page artifacts and metadata that support downstream OCR and long-term digital preservation workflows.

## Architecture Evidence Policy

This document reflects the **current repository state** (application code, `docker-compose.yml`, Dockerfiles, GitHub Actions workflows, ECS task-definition JSON under `k8s/ecs/`, and optional Kubernetes YAML under `k8s/*.yaml` — see `k8s/README.md`). Capabilities are described as **implemented** only when that support appears in those artifacts or in importable service code. Items that are optional at build or runtime, stubbed, or only partially wired into Compose or CI are labeled. Components **not** present in this branch (no Dockerfile in Compose, no CI build step, no ECS task definition) are listed separately—even if mentioned in `README.md` or older planning documents.

## High-Level Architecture

The implemented control plane is:

1. **Clients** use the **Next.js frontend** (`frontend/`), which calls the **EEP FastAPI** service (`services/eep/app/main.py`) using `NEXT_PUBLIC_API_BASE_URL` (see `docker-compose.yml` and `.env.example`).
2. **EEP** persists authoritative state in **PostgreSQL** (SQLAlchemy models in `services/eep/app/db/models.py`; schema changes via Alembic migrations in `services/eep/migrations/versions/`). It enqueues per-page work to **Redis** using the custom queue implementation in `services/eep/app/queue.py` (list-based queues with processing lists, claims, retries, and dead-letter handling—not Celery or RQ).
3. **EEP worker** (`services/eep_worker/`) runs a Redis consumer loop (`services/eep_worker/app/worker_loop.py`). It loads inputs and writes outputs through **`shared/io/storage.py`**, which supports `s3://` URIs via **boto3** (AWS S3 or S3-compatible endpoints). Locally, **MinIO** provides object storage (`docker-compose.yml`; credentials and endpoint in `.env.example`).
4. The worker invokes **IEP** HTTP services (e.g. geometry, rectification, layout, semantic normalization) using URLs from environment variables such as `IEP1A_URL`, `IEP1B_URL`, `IEP0_URL`, etc. (defaults align with `docker-compose.yml` service names and ports).
5. **Recovery** and auxiliary processes: **`eep_recovery`** (`services/eep_recovery/`) and **`shadow_worker`** (`services/shadow_worker/`) are defined as separate uvicorn apps in Compose and interact with Redis/Postgres per their modules. **`retraining_worker`** (`services/retraining_worker/`) depends on **MLflow** in Compose and participates in retraining flows exposed via EEP routes (see `services/eep/app/main.py` router includes).
6. **Observability (local stack):** **Prometheus**, **Alertmanager**, and **Grafana** are included in `docker-compose.yml` with configuration under `monitoring/`.

**Deployment-related configuration:** `.github/workflows/deploy.yml` builds and pushes images to **Amazon ECR** for **AWS ECS**-style deployment. The **`k8s/`** directory holds **two** paths: **`k8s/ecs/*.json`** — Fargate/EC2 ECS task definitions used by the CI-automated deploy (placeholders such as `ACCOUNT_ID` and `REGION` appear in those files); and **`k8s/*.yaml`** — Kubernetes **Deployment** / **Service** / **Ingress** / **Job** manifests for a generic cluster option, with **`k8s/README.md`** (`kubectl apply`) instructions. The automated staging deployment in this repository uses the **ECS** path. Additional workflows (for example `scale-up.yml`, `scale-down.yml`, `observability-up.yml`) automate ECS-oriented operations.

**Processing cadence (cloud):** **`PROCESSING_START_MODE`** on the EEP task selects **`immediate`** (scale processing capacity after enqueue — default in **`k8s/ecs/eep-task-def.json`**, suited to **demos**) vs **`scheduled_window`** (enqueue-time scale-up **disabled**; **`scheduled-window.yml`** starts work **inside cron windows** — **batch-style**, **cost-aware**, aligned with digitization loads that **need not** finish immediately). See **`services/eep/app/scaling/normal_scaler.py`**.

**Text diagram:**

```text
User / Admin
   ↓
Frontend (Next.js)
   ↓
EEP API (FastAPI)
   ↓
Postgres (metadata, lineage, gates) + Redis (page task queue) + S3-compatible storage (artifacts)
   ↓
EEP Worker (+ HTTP calls to IEP services)
   ↓
Updated artifacts, job/page records, queue lifecycle
   ↓
Frontend / Admin UI (via EEP REST API)
```

Auxiliary processes running alongside this path in `docker-compose.yml` include **eep-recovery**, **shadow-worker**, **retraining-worker**, optional **dataset-builder** (Compose profile `dataset-build`), and optional observability (**prometheus**, **alertmanager**, **grafana**) and **mlflow**.

---

### 1. Implemented and evidenced in code

| Area | Evidence |
|------|----------|
| Monorepo layout | `services/` (EEP, workers, IEPs), `shared/`, `frontend/`, root `docker-compose.yml` |
| EEP API | `services/eep/app/main.py` — FastAPI app, multiple routers (jobs, uploads, auth, admin, correction, PTIFF QA, lineage, policy, retraining, artifacts) |
| Redis queue contract | `services/eep/app/queue.py`; enqueue on job creation in `services/eep/app/jobs/create.py` |
| Worker runtime | `services/eep_worker/app/worker_loop.py` — claims tasks, invokes IEP HTTP endpoints, updates DB lineage/page state |
| PostgreSQL models & migrations | `services/eep/app/db/models.py`, `services/eep/migrations/versions/*.py` |
| S3-compatible I/O | `shared/io/storage.py` (`S3Backend`, env vars `S3_ENDPOINT_URL`, `S3_ACCESS_KEY` / `S3_ACCESS_KEY_ID`, etc.) |
| Local orchestration | `docker-compose.yml` — postgres, redis, minio, eep, eep-worker, iep0–iep2b, frontend, recovery/worker services listed above |
| Frontend app structure | `frontend/src/app/` — App Router pages for jobs, queue/correction workspace, PTIFF QA, admin dashboard, users, policy, retraining, deployment info |
| CI | `.github/workflows/ci.yml` — Python tests via `uv sync` / pytest |
| CD image build | `.github/workflows/deploy.yml` — matrix of Dockerfiles pushed to ECR |
| ECS task definition templates | `k8s/ecs/*-task-def.json` for eep, eep-worker, eep-recovery, IEP services, frontend, mlflow, prometheus, grafana, migration, dataset-builder, drain-monitor, etc. |

### 2. Configured for deployment

| Area | Evidence |
|------|----------|
| AWS ECS Fargate | JSON task definitions under `k8s/ecs/` (network mode `awsvpc`, placeholder ARNs and image URIs) |
| Secrets / env from AWS | Example: `k8s/ecs/eep-task-def.json` references Secrets Manager ARNs for `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET_KEY`, S3 keys, etc. |
| GitHub Actions deploy pipeline | `deploy.yml` — OIDC `id-token: write`, ECR registry from `vars.AWS_ACCOUNT_ID` / `vars.AWS_REGION`, conditional rebuild paths |
| Scaling / observability automation | `.github/workflows/scale-up.yml`, `scale-down.yml`, `observability-up.yml`, `observability-down.yml`, `scheduled-window.yml` |

The project uses Docker Compose for local multi-service development. For cloud, the **primary automated path** is **AWS ECS/Fargate** via `k8s/ecs/` and `deploy.yml`. **Kubernetes** manifests in **`k8s/*.yaml`** provide an **alternate** deployment option and are **not** driven by that CI pipeline. `docker-compose.yml` explicitly notes that some production mounts (e.g. Google Document AI credentials) are applied by operators outside this repo.
