# Cloud infrastructure


## AWS services

### Amazon ECS

**Task definition sources:** one JSON file per service under **`k8s/ecs/`** (e.g. **`eep-task-def.json`**, **`iep0-task-def.json`**). **`.github/workflows/deploy.yml`** substitutes **`ACCOUNT_ID`** / **`REGION`** / **`CLUSTER_NAME`** and many `*_VALUE` placeholders, then **`aws ecs register-task-definition`** and **`aws ecs update-service`**.

**Launch type / capacity (from `requiresCompatibilities` in each JSON):**

| Pattern | Task-definition examples | Notes |
|---------|---------------------------|--------|
| **FARGATE** | **`eep`**, **`frontend`**, **`eep-worker`**, **`iep1d`**, **`iep1e`**, **`iep2a`**, **`iep2b`**, **`mlflow`**, **`dataset-builder`**, most workers | Serverless Fargate tasks. |
| **EC2** | **`iep0-task-def.json`**, **`iep1a-task-def.json`**, **`iep1b-task-def.json`** | ECS tasks intended for **EC2** capacity (`"requiresCompatibilities": ["EC2"]`). |

**Operational note (application code):** **`services/eep/app/scaling/normal_scaler.py`** documents that **GPU ECS services** (**`libraryai-iep0`**, **`libraryai-iep1a`**, **`libraryai-iep1b`**) are expected to stay at **`desiredCount == 0`** as placeholders while **GPU inference runs on RunPod** pods; URLs are injected into **`eep-worker`** via a refreshed task definition. This is **implementation evidence**, not generic AWS doctrine.

**ECS Service Connect:** **`deploy.yml`** and **`scale-up.yml`** pass **`serviceConnectConfiguration`** with namespace **`libraryai.local`** and **short DNS aliases** (e.g. **`iep1d`**, **`iep2a-v2`**, **`eep-worker`**) matching **`services/eep/app/scaling/normal_scaler.py`** **`_SERVICE_CONNECT_CONFIGS`**. Internal URLs such as **`http://mlflow.libraryai.local:5000`** appear in **`k8s/ecs/eep-task-def.json`** and related files.

**GPU Auto Scaling Group (ASG) variables:** **`k8s/ecs/eep-task-def.json`** sets **`GPU_ASG_NAME`** and **`GPU_ASG_DESIRED`**, and **`k8s/iam/eep-scaler-policy.json`** grants **`autoscaling:SetDesiredCapacity`** on an ASG resource pattern. **However:** a repository-wide search shows **no Python or workflow implementation** that calls **`SetDesiredCapacity`**—only the IAM policy and env placeholders. Treat ASG as **configured for IAM/env**, **not verified as driven by this codebase’s scale-up path**. Primary GPU orchestration in **`normal_scaler._do_scale_up`** is **RunPod** (see **Service scaling strategy**).

---

### ECS services (names, ports, “always on”)

Service names match **`deploy.yml`** helpers (e.g. **`libraryai-iep2a-v2`** for **`iep2a`**). Ports match **`portMappings`** / Service Connect aliases in **`k8s/ecs/*-task-def.json`** and **`deploy.yml`**.

| ECS service (evidence) | Container port / alias | Typically always on? | Evidence |
|------------------------|-------------------------|----------------------|----------|
| **`libraryai-eep`** | **8000** (`eep`) | **Yes** — API must accept jobs; deploy does **not** set this service’s desired count to **0** | **`deploy.yml`** (processing loop excludes **`eep`**) |
| **`libraryai-frontend`** | **3000** | **Yes** — same | **`deploy.yml`** |
| **`libraryai-mlflow`** | **5000** | **Not forced off** by deploy’s “processing → 0” loops | **`deploy.yml`** loops list workers/IEPs, not **`mlflow`** |
| **`libraryai-eep-worker`** | **9100** (health) | **No** — scaled by **`normal_scaler`** / **`scale-up.yml`** / **`scale-down.yml`** | **`normal_scaler.py`**, workflows |
| **`libraryai-eep-recovery`** | **9101** | **No** | same |
| **`libraryai-shadow-worker`** | **9102** | **No** — starts only if a **`stage=shadow`** **`ModelVersion`** exists (**`normal_scaler._has_shadow_candidate`**) | **`normal_scaler.py`** |
| **`libraryai-retraining-worker`** | **9104** | **No** — excluded from **`normal_scaler`**; deploy sets desired **0** after register | **`normal_scaler.py`** **`_EXCLUDED_SERVICES`**, **`deploy.yml`** |
| **`libraryai-dataset-builder`** | batch (no HTTP port in task def) | **No** — batch / manual | **`dataset-builder-task-def.json`**, **`deploy.yml`** |
| **`libraryai-iep0`** | **8006** | **No** — ECS **EC2** task def exists; **`normal_scaler`** treats GPU ECS as placeholders (**RunPod** carries live traffic) | **`iep0-task-def.json`**, **`normal_scaler.py`** comments |
| **`libraryai-iep1a`** | **8001** | **No** — same | **`iep1a-task-def.json`** |
| **`libraryai-iep1b`** | **8002** | **No** — same | **`iep1b-task-def.json`** |
| **`libraryai-iep1d`** | **8003** | **No** — **Fargate**; pre-warmed to desired **1** on scale-up per **`normal_scaler`** | **`iep1d-task-def.json`**, **`normal_scaler.py`** |
| **`libraryai-iep1e`** | **8007** | **No** | **`iep1e-task-def.json`** |
| **`libraryai-iep2a-v2`** | **8004** | **No** | **`deploy.yml`** ecs_service_name for **`iep2a`** |
| **`libraryai-iep2b`** | **8005** | **No** | **`iep2b-task-def.json`** |

After **`deploy-staging`**, processing services are explicitly set to **`desired-count 0`** (**`deploy.yml`** “Ensure staging processing services are stopped”) while **`eep`** / **`frontend`** remain the stable “core” surface for traffic.

---

### Task definitions and secrets

- **Location:** **`k8s/ecs/*.json`** — one file per logical service (same basename pattern as ECR image **`libraryai-<service>`**).
- **Registration:** **`deploy.yml`** registers a new revision on each deploy with image tags tied to **`${{ github.sha }}`** / **`latest`**.
- **Non-secret config:** many literals and **`sed`**-substituted **`_VALUE`** placeholders (RunPod, Google Document AI toggles, etc.) are plain **`environment`** entries.
- **Secrets:** sensitive values use ECS **`secrets`** with **`valueFrom`** pointing at **AWS Secrets Manager** ARNs with the naming pattern **`libraryai/<SECRET_NAME>`** (see **`eep-task-def.json`**: **`DATABASE_URL`**, **`REDIS_URL`**, **`JWT_SECRET_KEY`**, **`RUNPOD_API_KEY`**, **`RETRAINING_CALLBACK_SECRET`**, S3 keys). **`mlflow-task-def.json`** uses **`libraryai/MLFLOW_BACKEND_STORE_URI`** for the backend store.

Exact ARNs in committed JSON use placeholders **`REGION`** and **`ACCOUNT_ID`**; real deployments substitute real account/region.

---

### Amazon S3

- **Bucket name** **`libraryai2`** appears as a **default environment value** in multiple ECS task definitions (**`eep-task-def.json`**, **`eep-worker-task-def.json`**, **`mlflow-task-def.json`**, etc.) and in application defaults (e.g. **`services/eep/app/service_status.py`**).
- **Artifact layout examples** in-repo: **`s3://libraryai2/mlflow-artifacts`** (**`MLFLOW_ARTIFACT_ROOT`** in **`mlflow-task-def.json`**), **`s3://libraryai2/retraining/dataset_registry.json`**, **`s3://libraryai2/ops/runpod-pods.json`** (read/written by **`scale-up.yml`** / **`scale-down.yml`** for RunPod bookkeeping).
- **Region defaults** in code/task-def templates often line up with **`eu-central-1`** (e.g. **`AWS_REGION`** / **`S3_REGION`** defaults in **`services/eep/app/service_status.py`** and **`eep-runpod-task-def.json`** — the latter is a **non-template** artifact with frozen literals **and may embed real account IDs, ARNs, and hostnames** — see **Evidence policy** above). Operators may use other regions; nothing in-repo proves a single global region for all accounts.

**Not evidenced here:** IAM bucket policy JSON, Terraform bucket resource, or mandatory single-region enforcement beyond defaults and examples.

---

### RDS / PostgreSQL

- **Connection string:** application and migration tasks pull **`DATABASE_URL`** from **Secrets Manager** (**`libraryai/DATABASE_URL`**) per **`migration-task-def.json`** and **`eep-task-def.json`**.
- **Example placeholder only:** **`k8s/README.md`** mentions an RDS-style hostname pattern (**`libraryai.xxxx.rds.amazonaws.com`**) as documentation for operators—**not** a committed production endpoint in the templated **`k8s/ecs/*.json`** files.
- **MLflow metadata DB:** **`MLFLOW_BACKEND_STORE_URI`** is a **separate** secret (**`mlflow-task-def.json`**). Whether it shares an RDS instance with the app is **deployment-specific** and **not** stated in task-definition JSON beyond using Secrets Manager.

---

### Redis

- **Connection:** **`REDIS_URL`** from **Secrets Manager** **`libraryai/REDIS_URL`** (**`eep-task-def.json`**).
- **Uses evidenced in code:** task queues and orchestration keys—e.g. **`libraryai:normal_scale:lock`** (scale-up lock, TTL defaults via **`NORMAL_SCALE_LOCK_TTL_SECONDS`**, default **600** s in **`normal_scaler.py`**), and **`libraryai:model_reload:{service}`** channels documented in **`services/eep/app/promotion_api.py`** / IEP **`main.py`** files.

The repository does **not** name an AWS **ElastiCache** cluster resource directly; it only requires a **Redis URL** secret.

---

### Amazon CloudWatch Logs

- ECS task definitions use **`logConfiguration`** with **`logDriver: awslogs`**, **`awslogs-group`** names such as **`/ecs/libraryai-eep`**, **`awslogs-region`**, and **`awslogs-stream-prefix`** (see **`eep-task-def.json`**).
- Application stdout: **`shared/logging_config.py`** documents **newline-delimited JSON** logs to stdout (picked up by the container log driver).

---

### AWS IAM / GitHub Actions OIDC

- **OIDC:** **`.github/workflows/deploy.yml`** (and scale/observability workflows) use **`aws-actions/configure-aws-credentials`** with **`role-to-assume: arn:aws:iam::${{ vars.AWS_ACCOUNT_ID }}:role/github-actions-deploy`** (pattern—exact role name from workflows).
- **Sample policy:** **`k8s/iam/eep-scaler-policy.json`** illustrates ECS + ASG permissions bound to a **specific account/region/cluster** in that file’s literals—treat as **an example binding**, not a generic guarantee for every fork.

---

### GitHub Actions deployment (staging path)

**Workflow:** **`.github/workflows/deploy.yml`**.

**High-level pipeline (job order in file):** CI tests → **build/push** images to **ECR** (and optional **Docker Hub** for matrix entries with **`push_dockerhub: true`**) → **`migrate-staging`** (one-shot Fargate task from **`k8s/ecs/migration-task-def.json`**) → **`deploy-staging`** (register task defs, **`update-service`**, Service Connect) → **`bootstrap-admin-staging`** → **`e2e-tests`** → **`rollback-staging`** on failure.

**Docker Hub mirror:** the build matrix sets **`push_dockerhub: true`** for a **subset** of services (e.g. **`retraining-worker`**, **`iep0`**, **`iep1a`**, **`iep1b`**, **`iep1e`**, **`iep2a`**, **`iep2b`**—see **`deploy.yml`** matrix); images are tagged under the **`gma51/libraryai-*`** namespace when enabled.

**Model weights:** **`deploy.yml`** documents downloading IEP weight archives from **S3** during image build when GitHub **Variables** such as **`IEP1A_WEIGHTS_URI`** are set (comments in the workflow file list expected **`s3://libraryai2/...`** layout examples).

---

## Service scaling strategy

### API and UI layer

**`libraryai-eep`** and **`libraryai-frontend`** are the stable HTTP entrypoints for job submission and UI; **`deploy.yml`** does **not** scale them to **0** in the “stop processing” steps. **`normal_scaler`** likewise does not shut down the API when bringing workers up.

### Normal processing scale-up (`PROCESSING_START_MODE=immediate`)

**Modes:** The EEP task sets **`PROCESSING_START_MODE`** (see **`k8s/ecs/eep-task-def.json`**). In code, **`immediate`** means **`maybe_trigger_scale_up`** runs after **durable enqueue**; **`scheduled_window`** means **no** enqueue-time scale-up — only **`scheduled-window.yml`** may raise capacity **inside** its window when there is work (`normal_scaler.py` module docstring, lines ~10–17). **Batch / cost-aware operations** favor **`scheduled_window`** because digitization throughput **need not be immediate** after upload. **`immediate`** is appropriate for **demonstrations** and **interactive** staging where low queue latency matters.

**Implementation:** **`services/eep/app/scaling/normal_scaler.py`** **`_do_scale_up`** (see docstring at **`_do_scale_up`** — order below reflects **code**, not the older module banner alone).

1. **RunPod** — If **`RUNPOD_API_KEY`** is set, create or reuse **RunPod** GPU pods for **iep0 / iep1a / iep1b** and derive proxy URLs (`*.proxy.runpod.net`). If no API key / no URLs, scale-up **aborts** (workers would point at stale GPU endpoints).
2. **`libraryai-eep-worker`** — Register a new task definition revision with **`IEP0_URL` / `IEP1A_URL` / `IEP1B_URL`** set to the RunPod URLs, then **`update-service`** with **`WORKER_DESIRED_COUNT`** (default **2** from **`eep-task-def.json`** **`WORKER_DESIRED_COUNT`**).
3. **CPU IEPs** — **`libraryai-iep1d`**, **`libraryai-iep1e`**, **`libraryai-iep2a-v2`**, **`libraryai-iep2b`** → desired **1** each.
4. **Other workers** — **`libraryai-eep-recovery`** → **1**; **`libraryai-shadow-worker`** → **`WORKER_DESIRED_COUNT`** only if a shadow **`ModelVersion`** exists.

**Redis lock:** **`libraryai:normal_scale:lock`** with NX + TTL (**`NORMAL_SCALE_LOCK_TTL_SECONDS`**, default **600**) prevents duplicate concurrent AWS/RunPod bursts.

**Explicitly not started by `normal_scaler`:** **`libraryai-retraining-worker`**, **`libraryai-dataset-builder`**, **`libraryai-prometheus`**, **`libraryai-grafana`** (**`_EXCLUDED_SERVICES`**).

### Scheduled / workflow scaling

- **`scheduled-window.yml`** only drives processing when **`PROCESSING_START_MODE=scheduled_window`** (see workflow header comments).
- **`scale-up.yml`** / **`scale-down.yml`** / **`scale-down-auto.yml`** implement time-window and idle-drain behavior using **`k8s/ecs/drain-monitor-task-def.json`** and ECS APIs (details in **`docs/05_DEPLOYMENT.md`**).

**Not evidenced:** native **ECS Application Auto Scaling** policies (CPU/memory/queue depth) in YAML/JSON in this repository.

### Scale-down and drain

Processing services are stopped when **`deploy.yml`** sets **desired 0** for the worker/IEP set, and when **scale-down** workflows run after **`drain_monitor`** checks (**`scale-down.yml`**, **`scale-down-auto.yml`**). RunPod termination and **`s3://…/ops/runpod-pods.json`** updates are handled in those workflows and **`normal_scaler`** helpers—see workflow bodies for exact commands.

### Retraining and batch services

**`normal_scaler`** does **not** start **`libraryai-retraining-worker`** or **`libraryai-dataset-builder`**. **`eep-task-def.json`** includes **`RETRAINING_WORKER_START_MODE`** and RunPod image settings; retraining paths are **mode-dependent** and documented at the API level in **`docs/04_API_CONTRACTS.md`**. This infrastructure doc does **not** assert a single production mode beyond **“not part of normal scale-up.”**

---

## What is not defined in this repository

| Topic | Status |
|-------|--------|
| VPC, subnets, ALB, security groups as IaC | **Not found** — workflows reference **`vars.ECS_SUBNETS`**, **`vars.ECS_SECURITY_GROUPS`**, **`vars.ALB_DNS_STAGING`**, etc. |
| **`ECS_CLUSTER_PROD`** | **Not referenced** in workflows found |
| **GPU ASG automation** | **IAM + env present**; **no `SetDesiredCapacity` implementation** located in Python/YAML |
| **ElastiCache cluster name** | **Not stated** — only **`REDIS_URL`** secret |
| **Single canonical production hostname** | **Not committed** (operators use variables / AWS console) |

---

## Related documentation

- **End-to-end deployment procedures and verification:** **`docs/05_DEPLOYMENT.md`**
- **HTTP API contracts:** **`docs/04_API_CONTRACTS.md`**
- **ECS task definitions:** **`k8s/ecs/*.json`** — **ECS** task definitions (Fargate/EC2) used by **`deploy.yml`**.
- **Kubernetes manifests:** **`k8s/*.yaml`** + **`k8s/README.md`** — optional **non-CI** cluster path; not the same files as **`k8s/ecs/`**.
