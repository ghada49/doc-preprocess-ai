# LibraryAI — Kubernetes Manifests

This directory contains the Kubernetes manifests for deploying LibraryAI to a cluster.
They cover the user-owned application services. Infrastructure dependencies (PostgreSQL,
Redis, MinIO, MLflow, Prometheus, Grafana, Alertmanager) are expected to be provided
externally — either as managed cloud services or via their own Helm charts.

---

## Files

| File | What it creates |
|---|---|
| `namespace.yaml` | `libraryai` namespace |
| `configmap.yaml` | Non-secret config shared across all services |
| `secret.yaml` | Secret template — **fill before applying** |
| `frontend.yaml` | Next.js frontend — Deployment (2 replicas) + ClusterIP Service |
| `eep.yaml` | EEP API — Deployment (2 replicas, Alembic init-container) + ClusterIP Service |
| `eep-worker.yaml` | EEP worker (2 replicas) + EEP recovery (1 replica) Deployments |
| `ingress.yaml` | nginx-ingress routing frontend and API on separate subdomains |
| `admin-bootstrap-job.yaml` | One-shot Job that seeds the initial admin account |

The other services (iep1a, iep1b, iep1d, iep2a, iep2b, shadow-worker, retraining-worker, etc.)
follow the same Deployment + Service pattern as `eep.yaml`. Add a file per service using
`eep.yaml` as a template.

---

## Before you apply — everything that must be changed

### 1. Container image registry

Every `image:` field contains the placeholder `your-registry`. Replace with your actual
registry path before building or deploying.

**Files:** `frontend.yaml`, `eep.yaml`, `eep-worker.yaml`, `admin-bootstrap-job.yaml`

```yaml
# Change this:
image: your-registry/libraryai-eep:latest

# To something like:
image: ghcr.io/your-org/libraryai-eep:1.0.0
# or:
image: myacr.azurecr.io/libraryai-eep:1.0.0
```

Build and push each image before deploying:
```bash
# Production frontend build (NEXT_PUBLIC_API_BASE_URL baked in at build time)
docker build \
  --target runner \
  --build-arg NEXT_PUBLIC_API_BASE_URL=https://api.libraryai.example.com \
  -t ghcr.io/your-org/libraryai-frontend:1.0.0 \
  ./frontend

docker build -t ghcr.io/your-org/libraryai-eep:1.0.0        -f services/eep/Dockerfile .
docker build -t ghcr.io/your-org/libraryai-eep-worker:1.0.0 -f services/eep_worker/Dockerfile .
docker build -t ghcr.io/your-org/libraryai-eep-recovery:1.0.0 -f services/eep_recovery/Dockerfile .

docker push ghcr.io/your-org/libraryai-frontend:1.0.0
docker push ghcr.io/your-org/libraryai-eep:1.0.0
# ... etc
```

---

### 2. Domain names

All occurrences of `libraryai.example.com` are placeholders.

**File:** `ingress.yaml`, `configmap.yaml`

```yaml
# ingress.yaml — change both hosts:
host: app.libraryai.example.com   → host: app.yourdomain.com
host: api.libraryai.example.com   → host: api.yourdomain.com

# configmap.yaml — change CORS_ALLOWED_ORIGINS:
CORS_ALLOWED_ORIGINS: "https://app.libraryai.example.com"
→ CORS_ALLOWED_ORIGINS: "https://app.yourdomain.com"
```

The frontend image must also be rebuilt with the matching API URL:
```bash
--build-arg NEXT_PUBLIC_API_BASE_URL=https://api.yourdomain.com
```

---

### 3. Secrets (`secret.yaml`)

**Do not commit real secrets to git.**

Every value in `secret.yaml` is a base64-encoded placeholder (`CHANGE_ME`).
Replace each one before applying. Encode with:
```bash
echo -n 'your-actual-value' | base64
```

| Key | What to put |
|---|---|
| `POSTGRES_PASSWORD` | Your PostgreSQL password |
| `S3_ACCESS_KEY_ID` | MinIO / S3 access key |
| `S3_SECRET_ACCESS_KEY` | MinIO / S3 secret key |
| `JWT_SECRET_KEY` | Long random string (min 32 chars) — `openssl rand -hex 32` |
| `GF_ADMIN_PASSWORD` | Grafana admin password |
| `BOOTSTRAP_ADMIN_USERNAME` | First admin account username (e.g. `admin`) |
| `BOOTSTRAP_ADMIN_PASSWORD` | First admin account password |

The Google Document AI Secret is separate — create it with:
```bash
kubectl create secret generic google-documentai-sa \
  --from-file=key.json=/path/to/your/sa-key.json \
  -n libraryai
```

---

### 4. Database and Redis connection strings (`configmap.yaml`)

The ConfigMap assumes services named `postgres` and `redis` exist in the cluster
(e.g. deployed via Helm into the same namespace, or via ExternalName Services pointing
at managed cloud databases).

If you use managed services (AWS RDS, Azure Database, etc.), update these:
```yaml
POSTGRES_HOST: "postgres"       → your RDS endpoint, e.g. libraryai.xxxx.rds.amazonaws.com
REDIS_HOST:    "redis"          → your ElastiCache endpoint
S3_ENDPOINT_URL: "http://minio:9000"  → remove or set to "" for real AWS S3
```

Also add `DATABASE_URL` to `secret.yaml` if your password contains special characters:
```
DATABASE_URL: <base64 of postgresql+psycopg2://libraryai:PASSWORD@HOST:5432/libraryai>
```

---

### 5. Ingress TLS

TLS is commented out in `ingress.yaml`. To enable HTTPS:

**Option A — cert-manager (recommended):**
```bash
# Install cert-manager
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --set installCRDs=true

# Create a ClusterIssuer for Let's Encrypt (see cert-manager docs)
```
Then uncomment the `tls:` block in `ingress.yaml` and the annotation:
```yaml
cert-manager.io/cluster-issuer: "letsencrypt-prod"
```

**Option B — manual certificate:**
```bash
kubectl create secret tls libraryai-frontend-tls \
  --cert=path/to/tls.crt --key=path/to/tls.key -n libraryai
```

---

### 6. Resource limits

The `requests` and `limits` in each Deployment are conservative starting points.
Tune them based on actual usage after observing Grafana dashboards.

Particularly: ML inference services (iep1a, iep2a, etc.) need GPU node selectors
and higher memory limits when running real models. Add to those Deployments:
```yaml
resources:
  limits:
    nvidia.com/gpu: 1
    memory: "8Gi"
nodeSelector:
  accelerator: nvidia-gpu
```

---

## Apply order (first deploy)

```bash
# 1. Namespace first
kubectl apply -f k8s/namespace.yaml

# 2. Config and secrets
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml        # fill in real values first

# 3. Application services
kubectl apply -f k8s/eep.yaml           # runs Alembic migrations as init-container
kubectl apply -f k8s/eep-worker.yaml
kubectl apply -f k8s/frontend.yaml

# 4. Ingress
kubectl apply -f k8s/ingress.yaml

# 5. Seed the first admin account (once only)
kubectl apply -f k8s/admin-bootstrap-job.yaml
kubectl logs -n libraryai job/admin-bootstrap -f

# Or apply everything at once (namespace + config must exist first):
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml k8s/secret.yaml
kubectl apply -f k8s/eep.yaml k8s/eep-worker.yaml k8s/frontend.yaml k8s/ingress.yaml
kubectl apply -f k8s/admin-bootstrap-job.yaml
```

## Verify

```bash
kubectl get pods -n libraryai
kubectl get svc  -n libraryai
kubectl get ingress -n libraryai

# Tail logs
kubectl logs -n libraryai deploy/eep -f
kubectl logs -n libraryai deploy/eep-worker -f
kubectl logs -n libraryai deploy/frontend -f
```
