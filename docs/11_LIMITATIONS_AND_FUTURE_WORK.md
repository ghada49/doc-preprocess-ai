# Limitations and future work


## Current limitations

| Limitation | Why it matters | Current mitigation | Future direction |
|------------|----------------|--------------------|------------------|
| **1. Difficult scans may still require human review** | Skew, low contrast, damage, complex spreads, and newspaper-style layouts are inherently risky to auto-accept. | The pipeline **routes uncertain outcomes** to **`rectification`** or **`pending_human_correction`** via geometry and artifact gates (`services/eep/app/gates/`); integration tests document constrained **`accepted`** paths (`tests/test_p3_gate_integration.py`). User-initiated review exists (`services/eep/app/correction/send_to_review.py`). | Continue tuning thresholds/material policies while preserving conservative routing. |
| **2. Retraining depends on enough accepted corrections** | Training signal improves when there are enough **trusted** labels; sparse corrections slow dataset export. | **`dataset_builder`** selects **`human_corrected = TRUE`**, **`acceptance_decision = 'accepted'`**, non-null **`human_correction_fields`** (`services/dataset_builder/app/main.py`); **`min_samples_not_met`** when per-family/material counts fall below env thresholds (`RETRAINING_MIN_CORRECTED_*`). `dataset_registry` prefers **approved** registry datasets when present (`services/retraining_worker/app/dataset_registry.py`). | Grow curated corrections over time; widen material coverage before expecting stable live training runs. |
| **3. GPU and cloud cost constrain always-on inference** | GPU inference (RunPod + ECS patterns) and large IEP images have real cost if left running 24/7. | **Workflow-based** scale-up/scale-down and **`normal_scaler`** orchestration (`services/eep/app/scaling/normal_scaler.py`; `.github/workflows/scale-up.yml`, `scale-down.yml`, `scale-down-auto.yml`); **`deploy.yml`** leaves many processing services at **desired 0** after deploy until scale-up. **`PROCESSING_START_MODE=scheduled_window`** avoids enqueue-time scale-up so jobs **batch** into **planned windows** — **cost-aware** for real digitization loads (finishing work **later** is acceptable). Demo-oriented configs often use **`immediate`** for responsiveness. API/frontend paths remain separable from batch workers. | Tune schedules and RunPod/ECS settings per budget; optional queue-driven autoscaling (not in-repo today). |
| **4. Evaluation strength depends on benchmark / golden coverage** | Small or unrepresentative benchmarks limit confidence in **`gate_results`** and promotions. | **`training/scripts/evaluate_golden_dataset.py`** implements offline evaluation; **`tests/test_p9_golden_dataset.py`** exercises related logic; **`LIBRARYAI_RETRAINING_GOLDEN_EVAL=live`** path in **`services/retraining_worker/app/task.py`** requires explicit enablement. Default CI (**`.github/workflows/ci.yml`**) does **not** run the golden script. | Expand curated golden cases (materials, edge scans); optionally wire selective golden runs into CI when infra allows. |
| **5. OCR / readability depend on upstream preprocessing** | If crop, deskew, split, layout, or reading-order stages are wrong, downstream OCR quality suffers. | Multi-stage worker pipeline with gates and rescue paths (`services/eep_worker/app/`); human correction updates artifacts before later stages resume (`services/eep/app/correction/apply.py`). | Stronger layout/newspaper validation (below) and clearer reading-order metrics where the pipeline exposes them. |
| **6. Newspaper and complex layouts need sustained validation** | Columns, mixed content, and degraded newsprint stress geometry and adjudication. | Newspaper-specific logic and tests exist (`tests/test_newspaper_review_policy.py`, `tests/test_rectification_policy.py`); uncertain routes still land in review/correction (`services/eep/app/gates/geometry_selection.py`). | More fixtures and regression tests for newspaper edge cases; optional policy tuning per collection. |
| **7. Cloud deployment remains operator-sensitive** | Correct AWS/GitHub configuration (OIDC, subnets, secrets, ALB DNS) is required even with automation. | **`deploy.yml`** documents staging deploy + **partial E2E** (health, status, auth, optional job submit); **`docs/05_DEPLOYMENT.md`** catalogs variables and artifacts. | Expand automated verification (see **Future work**); keep runbooks for secrets and first-time ECS service creation. |

---

## What these limitations do not mean

These limits are **normal** for an AI-assisted digitization system aimed at **library/archives quality**. The project **deliberately** favors **safe routing**, **human review for uncertain pages**, **traceable lineage**, and **gated promotion** over maximizing raw throughput or blind automation. Partial automation with strong oversight is the **intended** engineering posture—not an admission that the system lacks value.

---

## Future work

| Future work | Expected benefit | Priority | Related limitation |
|-------------|------------------|----------|---------------------|
| **Larger benchmark / golden dataset** | Fairer **`gate_results`** and promotion decisions; fewer false passes on narrow data | **High** | §4 evaluation coverage |
| **Stronger end-to-end regression** | Protect upload → processing → review → output; staging deploy E2E currently polls job until **`queued`/`running` only—full completion coverage is explicitly marked **TODO** in **`.github/workflows/deploy.yml`** (`e2e-tests` job comment block) | **High** | §7 deployment verification |
| **More robust newspaper handling** | Better multi-column and degraded-newsprint behavior | **Medium** | §6 |
| **Better reading-order evaluation** | Quantify **IEP1e** / reading-order quality for RTL/LTR and columns | **Medium** | §5 |
| **Queue-depth autoscaling** | Reduce reliance on cron/GitHub dispatch for capacity; **no** KEDA/HPA or ECS Application Auto Scaling policies are present in-repo today | **Medium** | §3 |
| **Librarian-centered UX testing** | Validate correction/review flows with real operators (no formal UX study artifacts in repository) | **Medium–High** (process) | §1 human review |
| **Broader scan-quality coverage** | Generalize beyond tested fixtures and dev datasets | **Medium** | §1, §4 |
| **Expanded observability & alerting** | Faster detection of backlog, drift, failed retrains; Grafana/Prometheus on ECS is **optional** workflows (`observability-up.yml`) | **Medium** | §3, §7 |
| **Deeper MLOps wiring** | Stronger candidate comparison; **`version_tag`** correlation on **`/model-info`** (**TODO** in **`services/iep1a/app/inference.py`**, **`iep1b`**) — promotion-time MLflow transition is **implemented** with graceful degradation (`promotion_api.py`) | **Medium** | §4 |
| **EKS or extending Kubernetes manifests** | **`k8s/*.yaml`** already provides a generic cluster deployment option (**`k8s/README.md`**). Future work is **HPA/KEDA** queue-depth autoscaling, richer ingress/IAM wiring, or **EKS** as a managed control plane — deferred pending operational need vs **ECS + workflows** (`docs/07_TRADEOFFS_AND_DESIGN_DECISIONS.md`) | **Low / later** | §3 |

---

## Prioritized roadmap

Priorities reflect **repository gaps** (explicit TODOs, CI vs offline scripts) and **risk reduction** for demos and production-like staging—not a judgment that optional items are unimportant.

| Priority | Improvement | Reason |
|----------|-------------|--------|
| **High** | Extend deploy **E2E** toward full job completion + artifact checks | **`deploy.yml`** TODO calls out full upload→process→download cycle; current poll stops at **`queued`/`running`** |
| **High** | Grow **golden / benchmark** coverage and run selectively in CI when feasible | Golden script exists but is **not** in default **`ci.yml`**; strengthens promotions |
| **High** | Keep **conservative gates** + documentation aligned as features evolve | Protects quality while scope grows (`tests/test_p3_gate_integration.py`) |
| **Medium** | **Newspaper** and complex-layout regression packs | Reduces surprises on archival newsprint (`tests/test_newspaper_review_policy.py` baseline) |
| **Medium** | **Queue-aware scaling** research (e.g. ECS target tracking or external autoscaler)—replace or augment workflow-only scaling | **`normal_scaler`** + schedules are explicit; queue-depth policies **not** in YAML |
| **Medium** | **Observability** on when workloads warrant always-on Prometheus/Grafana on ECS | **`observability-up.yml`** exists but is manual |
| **Medium** | **User workflow validation** with librarians/archivists | Improves adoption of correction UX beyond automated tests |
| **Low / later** | **HPA/KEDA**, richer **`k8s/*.yaml`** ops, or **EKS** if portability/autoscaling requirements dominate **ECS** simplicity | Plain K8s YAML **exists**; Helm/Kustomize **not** in repo |

---

## Engineering rationale

The limitations above are treated as **engineering constraints**: bounded datasets, finite infrastructure budgets, and preservation-grade quality targets. **LibraryAI** emphasizes **traceability** (**`page_lineage`**, **`quality_gate_log`**), **human review for uncertainty**, **gated model changes**, and **cost-aware scaling**—not unchecked automation across every scan condition.

---
