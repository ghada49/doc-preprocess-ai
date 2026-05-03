# QA, testing, and validation



## Test suite summary

| Test type | Purpose | Evidence | Command (from repo) | Status |
|-----------|---------|----------|---------------------|--------|
| Backend unit + integration | Broad **`tests/`** coverage (worker, gates, correction, API, IEP contracts) | `tests/*.py` (**104** Python test files); `[tool.pytest.ini_options]` in **`pyproject.toml`** | **`uv run pytest tests/`** with CI ignores — see **Exact commands** | **Implemented** |
| Migration / Alembic | DB migration correctness | `tests/test_p1_migration.py`; `services/eep/migrations/` | **`uv run pytest tests/test_p1_migration.py`** | **Implemented** |
| API / FastAPI | Routers, auth, admin | e.g. `tests/test_p7_auth_token.py`, `tests/test_admin_infra_endpoints.py` | **`pytest tests/`** subsets | **Implemented** |
| Golden dataset evaluation | Script + tests for golden eval helpers | `training/scripts/evaluate_golden_dataset.py`; `tests/test_p9_golden_dataset.py` | **CLI:** `python training/scripts/evaluate_golden_dataset.py ...` (see script docstring) | **Implemented** |
| Prometheus / metrics wiring | **`/metrics`** content | `tests/test_p9_prometheus_metrics.py` | **`pytest tests/test_p9_prometheus_metrics.py`** | **Implemented** |
| Frontend lint | ESLint via Next | `frontend/package.json` **`scripts.lint`** | **`npm run lint`** (from **`frontend/`**) | **Implemented** |
| Frontend typecheck | TypeScript **`tsc --noEmit`** | `frontend/package.json` **`scripts.type-check`** | **`npm run type-check`** | **Implemented** |
| Lint / format (repo root) | Ruff, Black, isort | **`pyproject.toml`** `[tool.ruff]`, `[tool.black]`; **`Makefile`** | **`make lint`** → **`ruff check .`**, **`black --check .`**, **`isort --check .`** | **Implemented** |
| Typecheck (Python) | mypy strict | **`pyproject.toml`** `[tool.mypy]`; **`Makefile`** | **`make typecheck`** → **`mypy .`** | **Implemented** |
| Pre-commit | Hooks bundle | `.pre-commit-config.yaml` | **`make pre-commit`** | **Implemented** |
| Deploy smoke / E2E | Post-deploy HTTP checks | `.github/workflows/deploy.yml` job **`e2e-tests`** | **CI only** (curl **`/health`**, **`/v1/status`**, auth, optional job post) | **Implemented** (workflow) |

---

## Exact commands

Commands below match **`Makefile`**, **`ci.yml`**, and **`package.json`**. From repo root unless noted.

```bash
# Backend tests (matches CI unit/integration job — note ignores)
uv sync
uv run pytest tests/ \
  --ignore=tests/test_p1_migration.py \
  --ignore=tests/test_google_document_ai.py \
  --ignore=tests/test_p2_2_google_worker_config.py \
  -q --tb=short

# Migration tests (separate CI job with Postgres service)
uv run pytest tests/test_p1_migration.py -q --tb=short

# Backend tests via Makefile (runs full tests/ — does not mirror CI ignores)
make test
# equivalent: pytest tests/ -v   (requires dev env / uv sync)

# Lint / format / typecheck (repo root)
make lint
make format
make typecheck

# Frontend (cd frontend first)
cd frontend
npm install
npm run lint
npm run type-check

# Local stack (requires .env — Makefile enforces)
make up
# or: docker compose up -d
```

**Note:** **`make test`** uses **`pytest tests/ -v`** without CI’s **`--ignore`** flags; local runs may hit optional/skipped suites unless you mirror **`ci.yml`**.

---

## Validation and request constraints

| Area | Mechanism | Evidence |
|------|-----------|----------|
| HTTP API bodies | **Pydantic** models on routers | `services/eep/app/**/*.py` (e.g. **`CorrectionApplyRequest`** in `services/eep/app/correction/apply.py`) |
| Geometry / pipeline config | Policy **`PreprocessingGateConfig`** thresholds | `services/eep/app/gates/geometry_selection.py` (defaults e.g. **`split_confidence_threshold`**, **`artifact_validation_threshold`**) |
| Artifact soft scoring | Material-aware thresholds | `services/eep/app/gates/artifact_validation.py` |
| Job/page state machine | **`advance_page_state`** CAS transitions | `services/eep/app/db/page_state.py`; tests **`tests/test_p1_state_machine.py`** |
| Auth / RBAC | JWT + role checks | `services/eep/app/auth.py`; **`tests/test_p7_rbac.py`** |

API surface contracts are summarized in **`docs/04_API_CONTRACTS.md`** (secondary index).

---

## Errors, timeouts, retries, fallbacks

| Concern | Behavior | Evidence |
|---------|----------|----------|
| Worker task retries | **`max_task_retries`**, then dead-letter path | `services/eep_worker/app/worker_loop.py`; **`shared/schemas/queue.py`** |
| Transient S3 / IO | Retries in intake / worker | `services/eep_worker/app/intake.py`; worker loop retry logs |
| Circuit breaker | Opens on failures; routes per **`circuit_breaker.py`** | `services/eep_worker/app/circuit_breaker.py`; **`tests/test_p4_circuit_breaker.py`** |
| IEP / geometry timeouts | Documented in rescue/normalization modules | `services/eep_worker/app/rescue_step.py`; **`tests/test_p4_rescue_step.py`** |
| Golden dataset S3 reads | Retry env vars in script header | `training/scripts/evaluate_golden_dataset.py` (docstring) |

---

## Regression and validation strategy

| Strategy | What it guards | Evidence |
|----------|----------------|----------|
| Gate integration regression | Routing never lands in illegal **`route_decision`** combinations | `tests/test_p3_gate_integration.py` |
| Promotion gate regression | Failed **`gate_results`** block promote | `tests/test_p8_promotion_api.py` |
| Correction workflow | Apply/reject/workspace | `tests/test_p5_correction_apply.py`, **`test_p5_correction_reject.py`**, **`test_p5_correction_queue.py`** |
| Dataset registry | Corrected hybrid selection | `tests/test_dataset_registry.py` |
| Retraining worker stub/live branches | Task execution paths | `tests/test_p8_retraining_worker.py`, **`test_retraining_live_train.py`** |
| Worker integration | End-to-end worker steps | `tests/test_p4_worker_integration.py`, **`test_worker_loop_integration_real.py`** |

---

## Human review and acceptance (testing angle)

Quality routing and human-review paths are covered by integration tests (e.g. **`test_p3_gate_integration.py`**, **`test_p5_ptiff_qa.py`**, correction suites). See **`docs/08_HUMAN_REVIEW_AND_RETRAINING.md`** for product behavior.

---

## CI and deployment verification

| Job | What runs | Evidence |
|-----|-----------|----------|
| **`ci.yml`** **`unit-tests`** | **`uv run pytest tests/`** with three **`--ignore`** patterns | `.github/workflows/ci.yml` |
| **`ci.yml`** **`migration-tests`** | Postgres service + **`test_p1_migration.py`** | `.github/workflows/ci.yml` |
| **`deploy.yml`** **`test`** | Calls reusable **`ci`** workflow | `.github/workflows/deploy.yml` |
| **`deploy.yml`** **`e2e-tests`** | **`curl`** **`/health`**, **`/v1/status`**, token, **`/v1/admin/queue-status`**, optional **`POST /v1/jobs`** | `.github/workflows/deploy.yml` |

---
