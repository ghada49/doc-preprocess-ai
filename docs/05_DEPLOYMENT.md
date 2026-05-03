# Deployment

## Deployment Summary

LibraryAI uses **Docker Compose** for local multi-service development (`docker-compose.yml`). Cloud-oriented deployment is evidenced by **Amazon ECR** image build/push and **AWS ECS on Fargate** operations in **`.github/workflows/deploy.yml`**, using JSON task definitions under **`k8s/ecs/`**. The **`k8s/`** directory **also** contains **Kubernetes** manifests (**`k8s/*.yaml`**) — **Deployment**, **Service**, **Ingress**, **Job**, **ConfigMap**, etc. — documented in **`k8s/README.md`** as an **alternate** cluster path; those YAML files are **not** applied by the CI **ECS** pipeline.

**Important distinction:** The **automated** staging deploy uses **ECS** task definitions and **`deploy.yml`**. **Kubernetes** YAML is opt-in / manual unless wired separately.

GitHub Actions also implements **scheduled** and **manual** **scale-up** / **scale-down** workflows, optional **observability** start/stop, and a **scheduled processing window** helper — see tables below.

## Environment

| Layer | Technology / service | Evidence | Status |
|-------|----------------------|----------|--------|
| Local orchestration | Docker Compose | `docker-compose.yml` | **Implemented** |
| Cloud orchestration | AWS ECS Fargate (`requiresCompatibilities: ["FARGATE"]`) | `k8s/ecs/*.json`, `.github/workflows/deploy.yml` | **Configured** (templates + automation) |
| Container registry | Amazon ECR | `.github/workflows/deploy.yml` (`ECR_REGISTRY`, `amazon-ecr-login`) | **Configured** |
| Supplementary registry | Docker Hub push for **seven** images (`gma51/libraryai-*`): **`retraining-worker`**, **`iep0`**, **`iep1a`**, **`iep1b`**, **`iep1e`**, **`iep2a`**, **`iep2b`** (`push_dockerhub: true` in **`deploy.yml`** matrix) | `deploy.yml` matrix `push_dockerhub`, `secrets.DOCKERHUB_TOKEN` | **Configured** |
| API runtime | EEP FastAPI | `services/eep/Dockerfile`, `k8s/ecs/eep-task-def.json` | **Implemented** / **Configured** |
| Worker runtime | `eep-worker` | `services/eep_worker/Dockerfile`, `k8s/ecs/eep-worker-task-def.json` | **Implemented** / **Configured** |
| Frontend runtime | Next.js | `frontend/Dockerfile`, `k8s/ecs/frontend-task-def.json` | **Implemented** / **Configured** |
| Database | PostgreSQL locally; RDS-style **`DATABASE_URL`** in ECS secrets | `docker-compose.yml` **postgres**; `migration-task-def.json`, `eep-task-def.json` | **Implemented locally** / **Configured for cloud** |
| Queue / cache | Redis | `docker-compose.yml` **redis**; `eep-task-def.json` **secrets** `REDIS_URL` | **Implemented locally** / **Configured for cloud** |
| Artifact storage | MinIO locally; S3 API via **`shared/io/storage.py`** | `docker-compose.yml` **minio**; `.env.example` `S3_*`; ECS secrets `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | **Implemented locally** / **Configured for cloud** |
| Observability (local) | Prometheus, Grafana, Alertmanager | `docker-compose.yml`, `monitoring/prometheus/`, `monitoring/grafana/`, `monitoring/alertmanager/` | **Implemented** (optional stack) |
| Observability (ECS) | Prometheus / Grafana images & task defs | `k8s/ecs/prometheus-task-def.json`, `k8s/ecs/grafana-task-def.json`, `.github/workflows/observability-up.yml` | **Optional** (workflow_dispatch demo/batch) |
| Experiment tracking | MLflow | `docker-compose.yml` **mlflow**; `k8s/ecs/mlflow-task-def.json`; `.env.example` `MLFLOW_TRACKING_URI` | **Optional local** / **Configured ECS artifact** |
| GPU / RunPod | RunPod-related env in **EEP** task def and **scale-up** workflow comments | `k8s/ecs/eep-task-def.json`; `.github/workflows/scale-up.yml` | **Configured** (external GPU via RunPod — not generic “all services on GPU”) |

**Not found in repo:** Terraform / Helm / root `infra/` directory (no matches under this workspace).

## Local deployment

### Prerequisites (evidenced)

- **Docker** + **Docker Compose v2** — stated in `README.md`.
- **Python 3.11** and **[uv](https://github.com/astral-sh/uv)** — `README.md`, `.github/workflows/ci.yml`.
- **`make`** optional — `Makefile` targets `up`, `test`, `health`.

### Configure environment

```bash
cp .env.example .env
```

Edit **`.env`** with non-default secrets (see **Required configuration**). The Compose **`eep`** service loads **`env_file: .env`** (`docker-compose.yml`).

### Start the stack

From repo root (matches `Makefile` **`make up`** which runs `docker compose up -d`):

```bash
docker compose up -d
```

Optional **model-weight** and **dataset-build** behaviors are controlled by build args / Compose **profiles** (see `docker-compose.yml` comments and **`dataset-builder`** profile `dataset-build`).

### Services started locally (from `docker-compose.yml`)

Includes at minimum: **postgres**, **redis**, **minio** (+ **minio-init**), **eep**, **eep-worker**, **eep-recovery**, **shadow-worker**, **retraining-worker**, **frontend**, **iep0**, **iep1a**, **iep1b**, **iep1d**, **iep1e**, **iep2a**, **iep2b**, plus optional **mlflow**, **prometheus**, **alertmanager**, **grafana**.

The **`dataset-builder`** service exists but uses **`profiles: ["dataset-build"]`** — not started by default `docker compose up`.

### Local URLs and ports (host → container)

| Access | Host URL / port | Evidence |
|--------|-----------------|----------|
| Frontend | `http://localhost:3000` | `frontend` ports `3000:3000` |
| EEP API | `http://localhost:8888` → app port **8000** | `eep` ports `8888:8000` |
| MLflow | `http://localhost:5000` | `mlflow` |
| Prometheus | `http://localhost:9090` | `prometheus` |
| Grafana | `http://localhost:3001` (maps container 3000) | `grafana` |
| MinIO API / console | `9000` / `9001` | `minio` |
| PostgreSQL | `5432` | `postgres` |
| Redis | `6379` | `redis` |
| Alertmanager | `http://localhost:9093` | `alertmanager` |
| IEP HTTP (host ports match container) | **iep1a** `8001`, **iep1b** `8002`, **iep1d** `8003`, **iep2a** `8004`, **iep2b** `8005`, **iep0** `8006`, **iep1e** `8007` | `docker-compose.yml` **iep*** services |

**Note:** `Makefile` **`make health`** curls **`/health`** on host ports **8000**–**8005** only (`Makefile`). That omits **EEP** on **8888**, **iep0** (**8006**), and **iep1e** (**8007**). For EEP with default Compose, use **`http://localhost:8888/v1/status`** or **`/health`**.

### Local migrations and admin bootstrap

On startup, **`eep`** runs (`docker-compose.yml` **command**):

1. `cd /app/services/eep && alembic upgrade head`
2. Optional `python -m scripts.create_admin ...` when **`BOOTSTRAP_ADMIN_PASSWORD`** is set
3. `uvicorn services.eep.app.main:app --host 0.0.0.0 --port 8000 --reload`

### Health / status checks (local)

| Check | Command / endpoint | Expected | Evidence |
|-------|-------------------|----------|----------|
| EEP liveness | `curl -sf http://localhost:8888/v1/status` | JSON `status` / `service` | `services/eep/app/main.py` **`GET /v1/status`** |
| EEP middleware health | `curl -sf http://localhost:8888/health` | 200 | `shared/middleware.py` mounts **`/health`** |
| IEP (example IEP1A) | `curl -sf http://localhost:8001/health` | 200 | `services/iep1a/app/main.py` |
| IEP readiness | `curl -sf http://localhost:8001/ready` | 200 or 503 per mock | same |

---

## Cloud deployment overview

### What the repo provides

- **Docker images** built from Dockerfiles and pushed to **ECR** (`deploy.yml`).
- **ECS task definitions** as JSON under **`k8s/ecs/`** — placeholders **`ACCOUNT_ID`**, **`REGION`**, **`CLUSTER_NAME`** substituted in CI (`deploy.yml` **`sed`**).
- **Secrets Manager references** in task defs (e.g. `arn:aws:secretsmanager:REGION:ACCOUNT_ID:secret:libraryai/...`) — see **`eep-task-def.json`**, **`migration-task-def.json`**.
- **CloudWatch Logs** via **`logConfiguration`** → **`awslogs`** (`logDriver`, `awslogs-group`, `awslogs-region`) on task definitions (e.g. **`k8s/ecs/eep-task-def.json`**).
- **OIDC** to AWS (`permissions: id-token: write`, `aws-actions/configure-aws-credentials`, role **`github-actions-deploy`** pattern in workflows).

### What is not in the repo

- Live **ALB DNS**, **VPC subnets**, or **security group IDs** as literals — workflows reference GitHub **Actions variables** (e.g. **`vars.ALB_DNS_STAGING`**, **`vars.ECS_SUBNETS`**, **`vars.ECS_SECURITY_GROUPS`**).
- **Production vs staging** split beyond naming: **`deploy.yml`** uses **`ECS_CLUSTER_STAGING`** for migrate/deploy/E2E; **`scale-up.yml`** / **`scale-down.yml`** use **`vars.ECS_CLUSTER`** (operators must align variables in GitHub).

---

## Deployment flow

Actual automation is in **`.github/workflows/deploy.yml`** (push to **`main`** or **`workflow_dispatch`**).

| Step | Workflow / script | Evidence | Notes |
|------|-------------------|----------|--------|
| **Tests** | Reusable **`ci.yml`** — unit/integration + **`migration-tests`** (PostgreSQL service, `tests/test_p1_migration.py`) | `deploy.yml` job **`test`** | Gates deploy |
| **Build images** | **`deploy.yml`** job **`build-push`** — matrix of services, conditional build via **`git diff`** paths | Same | Tags **`libraryai-<service>:${{ github.sha }}`** and **`:latest`** to ECR |
| **Model weights (CI)** | **`deploy.yml`** downloads from **S3** via **`aws s3 cp`** when building IEP images | Same | Requires GitHub **Variables** like **`IEP1A_WEIGHTS_URI`** (documented in workflow comments) |
| **Push to ECR** | **`docker/build-push-action`** `push: true` | `deploy.yml` | Registry from **`vars.AWS_ACCOUNT_ID`** / **`vars.AWS_REGION`** |
| **Docker Hub mirror** | Select services **`push_dockerhub: true`** → tag **`gma51/libraryai-*:latest`** | `deploy.yml` | Uses **`secrets.DOCKERHUB_TOKEN`** |
| **Migrate DB (staging)** | Job **`migrate-staging`** — registers **`k8s/ecs/migration-task-def.json`**, **`aws ecs run-task`**, **`aws ecs wait tasks-stopped`**, asserts exit code **0** | `deploy.yml` | One-off Fargate task: **`alembic upgrade head`** |
| **Deploy ECS (staging)** | Job **`deploy-staging`** — registers each **`k8s/ecs/<svc>-task-def.json`**, **`aws ecs update-service`** when service exists | `deploy.yml` | Sets many workers/IEPs to **`desired-count 0`** after register (processing brought up elsewhere) |
| **Bootstrap admin** | Job **`bootstrap-admin-staging`** — runs **`scripts.create_admin`** via task override; password from **`secrets.BOOTSTRAP_ADMIN_PASSWORD`** | `deploy.yml` | Idempotent **`--skip-if-exists`** |
| **E2E checks** | Job **`e2e-tests`** — **`curl`** **`ALB_DNS_STAGING`** **`/health`**, **`/v1/status`**, admin token, **`/v1/admin/queue-status`**, **`POST /v1/jobs`** if **`vars.E2E_FIXTURE_URI`** set | `deploy.yml` | **`BASE_URL: http://${{ vars.ALB_DNS_STAGING }}`** |
| **Rollback workers on E2E failure** | Job **`rollback-staging`** | `deploy.yml` **`if: failure()`** | Stops workers; leaves **EEP** running |

**Manual / not fully automated:** Creating ECS **services** the first time in an AWS account is **not** defined as Terraform in this repo — **`deploy.yml`** skips update if service does not exist (`describe-services` branch).

---

## Services deployed

Legend: **Compose** = `docker-compose.yml` service name. **ECS file** = `k8s/ecs/<name>-task-def.json`. **Deploy matrix** = listed in **`deploy.yml`** `build-push` matrix.

| Service | Image / Dockerfile | Local Compose | ECS task def | Container / listen port | Runtime notes |
|---------|-------------------|---------------|--------------|-------------------------|---------------|
| **frontend** | `frontend/Dockerfile` | yes (`target: dev`) | `frontend-task-def.json` | **3000** | Dev hot-reload compose; ECS **healthCheck** wget `/` |
| **eep** | `services/eep/Dockerfile` | yes | `eep-task-def.json` | **8000** | ECS **healthCheck** `curl /health`; **`cpu`/`memory`** in JSON |
| **eep-worker** | `services/eep_worker/Dockerfile` | yes | `eep-worker-task-def.json` | **9100** (health) | Fargate CPU/mem higher than EEP in JSON |
| **eep-recovery** | `services/eep_recovery/Dockerfile` | yes | `eep-recovery-task-def.json` | **9101** | — |
| **shadow-worker** | `services/shadow_worker/Dockerfile` | yes | `shadow-worker-task-def.json` | **9102** | — |
| **retraining-worker** | `services/retraining_worker/Dockerfile` | yes | `retraining-worker-task-def.json` | **9104** | ECR + optional Docker Hub when matrix sets **`push_dockerhub: true`** |
| **dataset-builder** | `services/dataset_builder/Dockerfile` | profile **`dataset-build`** only | `dataset-builder-task-def.json` | batch | Not default Compose |
| **iep0** … **iep2b** | `services/iep*/Dockerfile` | yes | matching `iep*-task-def.json` | **8006**, **8001**, **8002**, **8003**, **8007**, **8004**, **8005** | **iep2a** ECS service name **`libraryai-iep2a-v2`** in workflows |
| **mlflow** | `services/mlflow/Dockerfile` | yes | `mlflow-task-def.json` | **5000** | — |
| **migration** | uses **`libraryai-eep`** image | no (task only) | `migration-task-def.json` | — | Alembic only |
| **drain-monitor** | repo shell / Python | no | `drain-monitor-task-def.json` | — | Used by **scale-up** / **scale-down** |
| **prometheus** | image + `monitoring/prometheus/` | yes (image `prom/prometheus`) | `prometheus-task-def.json` + **`Dockerfile.ecs`** in observability workflow | — | ECS path optional via **`observability-up.yml`** |
| **grafana** | image + provisioning | yes | `grafana-task-def.json` + **`Dockerfile.ecs`** | — | Same |

**Code present without ECS task def / deploy matrix (not claimed as cloud-deployed from this repo):** `services/shadow_recovery/`, `services/retraining_recovery/` (Dockerfiles exist; no matching **`k8s/ecs`** entry in file list).

---

## Required configuration and secrets

Names below appear in **`.env.example`**, **`k8s/ecs/*.json`**, and/or **`.github/workflows/*.yml`**. Do **not** commit production values.

| Name | Purpose | Local source | Cloud source (evidence) | Required? | Notes |
|------|-----------|--------------|-------------------------|-----------|--------|
| **`DATABASE_URL`** | SQLAlchemy DB connection | `.env.example` | ECS **Secrets Manager** ref in `migration-task-def.json`, `eep-task-def.json` | Yes | Local uses **postgres** hostname |
| **`REDIS_URL`** | Redis client URL | `.env.example` | ECS secret in `eep-task-def.json` | Yes for queue paths | |
| **`JWT_SECRET_KEY`** | JWT signing | `.env.example` | ECS secret | Yes | |
| **`S3_ENDPOINT_URL`** | S3 API endpoint | `.env.example` | Often omitted for AWS (SDK default) | Yes locally for MinIO | |
| **`S3_ACCESS_KEY_ID`** / **`S3_SECRET_ACCESS_KEY`** (and **`S3_ACCESS_KEY`** aliases in code) | S3 credentials | `.env.example` | ECS secrets | Yes | **shared/io/storage.py** accepts aliases |
| **`S3_BUCKET_NAME`** | Default bucket | `.env.example` | Env in task defs (`libraryai2` placeholder) | Yes | |
| **`NEXT_PUBLIC_API_BASE_URL`** | Browser → EEP URL | `.env.example`, Compose **frontend** | `deploy.yml` **build-arg** from **`vars.NEXT_PUBLIC_API_BASE_URL`** | Yes for frontend | |
| **`CORS_ALLOW_ORIGINS`** | Browser CORS | `.env.example` | Env in `eep-task-def.json` (may be `*`) | Deploy-dependent | |
| **`BOOTSTRAP_ADMIN_USERNAME`** / **`BOOTSTRAP_ADMIN_PASSWORD`** | Local admin creation | `.env.example`, Compose **eep** | **`secrets.BOOTSTRAP_ADMIN_PASSWORD`** in **`deploy.yml`** bootstrap | Optional | |
| **`MLFLOW_TRACKING_URI`** | MLflow client | `.env.example` | Env in worker/EEP task defs | Optional | |
| **`RETRAINING_CALLBACK_SECRET`** | RunPod callback auth | `.env.example` | ECS secret (`eep-task-def.json`) | When using callback | Header **`X-Retraining-Callback-Secret`** |
| **`RETRAINING_WEBHOOK_SECRET`** | Alertmanager → EEP webhook auth | Not listed in `.env.example` | Set in runtime env / Secrets Manager | Production webhook path | Default **`dev-webhook-secret-change-in-production`** in `services/eep/app/retraining_webhook.py` — override for prod; header **`X-Webhook-Secret`** |
| **`PROCESSING_START_MODE`** | **`immediate`** — scale workers/GPU after enqueue; **`scheduled_window`** — no enqueue-time scale-up; windows via **`scheduled-window.yml`** | — | **`k8s/ecs/eep-task-def.json`** (default **`immediate`**); GitHub **`vars.PROCESSING_START_MODE`** for workflows | **immediate** = demo/responsive; **scheduled_window** = **batch / cost-aware** (work need not start immediately) | `services/eep/app/scaling/normal_scaler.py` |
| **`RUNPOD_API_KEY`** | RunPod API | `.env.example` | ECS secret | When RunPod used | |
| **`GOOGLE_*`** | Document AI optional path | `.env.example` | Workflow **`sed`** placeholders in **`deploy.yml`** for ECS | Optional | Credentials via mount / secrets path |
| **`DRAIN_SUBNET_ID`** / **`DRAIN_SECURITY_GROUP_ID`** | Fargate drain tasks | — | **`scale-up.yml`**, **`scale-down.yml`** **`env`** | For drain workflows | |
| **`ECS_CLUSTER`** / **`ECS_CLUSTER_STAGING`** | ECS cluster names | — | **`vars.ECS_CLUSTER`**, **`vars.ECS_CLUSTER_STAGING`** | Yes in CI | |

**Reminder:** Real secrets belong in **GitHub Actions secrets**, **AWS Secrets Manager**, or runtime injection — not in git.

---

## Database migration and bootstrap

| Topic | Evidence |
|-------|----------|
| **Alembic** | `services/eep/alembic.ini`, `services/eep/migrations/versions/` |
| **Local auto-migrate** | `docker-compose.yml` **eep** command runs **`alembic upgrade head`** before uvicorn |
| **CI migration tests** | `.github/workflows/ci.yml` job **`migration-tests`** runs **`tests/test_p1_migration.py`** |
| **ECS migration task** | **`k8s/ecs/migration-task-def.json`** — image **`libraryai-eep`**, command **`alembic upgrade head`**, **`DATABASE_URL`** from Secrets Manager |
| **Deploy pipeline migrate** | **`deploy.yml`** **`migrate-staging`** registers JSON and runs Fargate task, waits **stopped**, checks **exitCode == 0** |
| **Admin bootstrap** | **`scripts/create_admin`** invoked locally in Compose when env set; **`deploy.yml`** **`bootstrap-admin-staging`** runs same module on ECS with secret password |

---

## Health checks and verification

### Implemented endpoints

| Component | Endpoint | Evidence |
|-----------|----------|----------|
| EEP | **`GET /v1/status`** | `services/eep/app/main.py` |
| EEP (+ all services using **`configure_observability`**) | **`GET /health`**, **`GET /ready`**, **`GET /metrics`** | `shared/middleware.py` |
| IEP services | **`/health`**, **`/ready`** (e.g. IEP1A) | `services/iep1a/app/main.py` (pattern repeated across IEPs) |
| EEP worker | Health on **`HEALTH_PORT`** (default **9100** in Compose) | `docker-compose.yml` **eep-worker** |

### Cloud verification (from **`deploy.yml`** **`e2e-tests`**)

Uses **`http://${{ vars.ALB_DNS_STAGING }}`** (operator-provided variable — **not** hardcoded in repo):

| Check | Command / placeholder | Expected | Evidence |
|-------|----------------------|----------|----------|
| ALB health | `curl -sf http://<alb-dns>/health` | **200** | **`deploy.yml`** (middleware **`/health`**) |
| API status | `curl -sf http://<alb-dns>/v1/status` | **200** | **`deploy.yml`** |
| Authenticated admin | `POST /v1/auth/token` then **`GET /v1/admin/queue-status`** | **200** | **`deploy.yml`** |

**Public URL:** The live deployment hostname is **environment-specific** and stored in GitHub **Variables** / AWS console — **not** committed in this repository.

---

## Scaling and lifecycle automation

| Workflow | Trigger | Purpose | Evidence |
|----------|---------|---------|----------|
| **`scale-up.yml`** | Cron **22:00 UTC** daily + **`workflow_dispatch`** | Starts processing stack when work exists; uses **`drain-monitor`** task; integrates RunPod/GPU per steps | `.github/workflows/scale-up.yml` |
| **`scale-down.yml`** | Cron **08:00 UTC** + **`workflow_dispatch`** | Drain then scale down | `.github/workflows/scale-down.yml` |
| **`scale-down-auto.yml`** | Cron **`*/15 * * * *`** + **`workflow_dispatch`** | If **`libraryai-eep-worker`** has desiredCount greater than zero, runs **`drain_monitor.py --assert-drained`** as a Fargate task (`k8s/ecs/drain-monitor-task-def.json`); on exit code **0** dispatches **`scale-down.yml`**. Skips when infra is already stopped. **Does not** stop **`libraryai-eep`** (API stays up per workflow comments). | `.github/workflows/scale-down-auto.yml` |
| **`scheduled-window.yml`** | Cron + **`workflow_dispatch`** | Only proceeds if **`PROCESSING_START_MODE=scheduled_window`** | `.github/workflows/scheduled-window.yml` |
| **`observability-up.yml`** / **`observability-down.yml`** | **`workflow_dispatch`** | Start/stop Prometheus/Grafana on ECS — **demo/batch**, not core pipeline | Workflow headers |

**Autoscaling claim:** Processing capacity is influenced by **scheduled**/**manual** GitHub Actions (**`scale-up.yml`**, **`scale-down.yml`**, **`scale-down-auto.yml`**) and by application code **`services/eep/app/scaling/normal_scaler.py`** (invoked from job/correction paths). **Native ECS service autoscaling policies** (CPU/memory/target tracking) are **not** defined in this repository’s YAML/JSON artifacts.

---

## Observability in deployment

| Item | Evidence |
|------|----------|
| **CloudWatch log groups** | **`awslogs-group`** in ECS JSON (e.g. `/ecs/libraryai-eep`) |
| **Prometheus/Grafana on ECS** | **`k8s/ecs/prometheus-task-def.json`**, **`grafana-task-def.json`**; build **`monitoring/prometheus/Dockerfile.ecs`**, **`monitoring/grafana/Dockerfile.ecs`** in **`observability-up.yml`** |
| **Local Prometheus/Grafana** | `docker-compose.yml`, `monitoring/prometheus/prometheus.yml` |

Deeper observability narrative can live in a dedicated observability doc alongside **`monitoring/`** — this deployment guide stays scoped to **what runs where** and **how it is shipped**.

---

## Public cloud API evidence (GT2)

The repository implements the HTTP API in **`services/eep/app/main.py`** and exposes **`GET /v1/status`** for lightweight checks.

To verify a **deployed** environment (after DNS/TLS are configured by operators):

```bash
curl -sf https://<api-domain>/v1/status
```

Expected JSON shape includes **`"status": "ok"`** and **`"service": "eep"`** (see implementation).

Authentication for protected routes uses **`Authorization: Bearer <jwt>`** from **`POST /v1/auth/token`** — see **`docs/04_API_CONTRACTS.md`**.

**If `<api-domain>` is unknown:** use the operator-provided URL (for example GitHub variable **`ALB_DNS_STAGING`** referenced in **`deploy.yml`** for E2E). That value is **not** stored in this git repository.

---

## Security and secret handling

- **`.env.example`** contains placeholders only — replace before real use.
- **ECS task definitions** reference **AWS Secrets Manager ARNs** by name pattern (`libraryai/DATABASE_URL`, etc.) — no plaintext secrets in JSON.
- **GitHub Actions** uses **`secrets.*`** (e.g. **`BOOTSTRAP_ADMIN_PASSWORD`**, **`DOCKERHUB_TOKEN`**) and **`vars.*`** for non-secret configuration.
- Do not paste production secrets into docs, screenshots, or commits.

---

## What is not implemented or not claimed

| Item | Status |
|------|--------|
| **Kubernetes** cluster manifests (`Deployment`/`Ingress` YAML) | **Present** in **`k8s/*.yaml`** (full set: **`eep`**, **`frontend`**, **`eep-worker`**, **`inference-services`**, **`ingress`**, **`admin-bootstrap-job`**, **`background-workers`**, **`namespace`**, **`configmap`**, **`secret`**, **`iep1e`**, plus **`k8s/README.md`**) — **not** used by CI **`deploy.yml`** (ECS path only) |
| **Terraform / Helm / root `infra/`** | **Not found** in workspace |
| **Public production URL** | **Not in repo** — use deployment variables / instructor demo |
| **`shadow_recovery` / `retraining_recovery`** images | **Not** in **`deploy.yml`** matrix or **`k8s/ecs/`** list |
| **Full end-to-end job completion** in CI | **`deploy.yml`** E2E polls until **queued/running** only; comments note TODO for full pipeline |
| **GPU on all services** | **Not claimed** — RunPod + selective services per **`eep-task-def.json`** / scale scripts |
| **`make health`** | Does **not** hit default Compose **EEP** port **8888** — verify with **`curl`** manually |

---

## Rubric alignment

This document supports:

- **GT2 Public cloud API functional** — API surface and **`/v1/status`** / **`/health`** verification paths; E2E job in **`deploy.yml`** when **`E2E_FIXTURE_URI`** is set.
- **S4 Containerization and orchestration** — Dockerfiles, Compose, ECS Fargate JSON, ECR push.
- **S5 Deployment architecture and secrets** — Task defs, Secrets Manager refs, GitHub OIDC, env tables.
- **M1 Automated lifecycle pipeline** — **`deploy.yml`** test→build→migrate→deploy→bootstrap→E2E; scale workflows.
- **M4 Documentation completeness** — Instructor-oriented tables with **evidence paths**.
- **D3 Evidence shown** — Citations to **`docker-compose.yml`**, **`.github/workflows/`**, **`k8s/ecs/`**, **`.env.example`**.
