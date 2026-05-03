# Human-Corrected Auto-Retrain Plan (Active-When-Ready)

## 1) Decision and Objective

We will build the full corrected-data retraining system now, but keep it **safe/inactive** until enough human-corrected pages exist.

Behavior policy:

- If corrected data is insufficient: builder returns `min_samples_not_met`, no training starts.
- If corrected data is sufficient: builder exports dataset artifacts and retraining runs automatically.

This lets us deploy the flow now without forcing bad/empty retraining runs.

---

## 2) What This Plan Uses as Ground Truth

Source table: `page_lineage`

Required filters:

- `human_corrected = true`
- `human_correction_fields IS NOT NULL`
- `acceptance_decision = 'accepted'`

Training source fields:

- `human_correction_fields.source_artifact_uri` (raw page image for training input)
- `human_correction_fields.quad_points` OR `human_correction_fields.crop_box`
- `human_correction_fields.selection_mode` (`quad` or `rect`)
- width/height from:
  - `human_correction_fields.image_width/image_height`, else
  - `gate_results.downsample.downsampled_width/downsampled_height`

Important:

- Use `source_artifact_uri` as training image.
- Do not use `output_image_uri` as training input.

---

## 3) System Behavior (Simple End-to-End)

1. Trigger arrives.
2. Worker starts retraining job.
3. Dataset selector runs corrected-export builder mode.
4. Builder checks corrected sample counts.
5. If below threshold: output `min_samples_not_met`, stop safely.
6. If threshold met:
   - export images + YOLO labels
   - generate `data.yaml`
   - generate `retraining_train_manifest.json`
   - register version/checksum in `dataset_registry.json`
7. Worker trains IEP1A/IEP1B.
8. Golden evaluation runs.
9. Promotion eligibility is decided as usual.

---

## 4) Prerequisites

## A) Must-have now (to build flow)

- Access to codebase and DB schema.
- Working dataset builder runtime (Python + DB + object store access).
- Retraining worker path already wired to dataset selector.

## B) Must-have later (to actually train from corrected data)

- Enough corrected pages in DB.
- Reachable source image URIs.
- Minimum sample thresholds satisfied.

If B is not available, flow stays inactive and returns `min_samples_not_met`.

---

## 5) Label Conversion Rules

## A) Corners

- `quad` mode:
  - `corners_abs = quad_points` (absolute pixels).
  - normalize with image dims: `corners_norm = [[x/W, y/H] for (x, y) in corners_abs]`.
- `rect` mode:
  - derive 4 corners from `crop_box = [x_min, y_min, x_max, y_max]`.
  - if `deskew_angle != 0`, rotate corners around bbox center.
  - normalize with image dims: `corners_norm = [[x/W, y/H] for (x, y) in corners_abs_or_rotated]`.
- Canonicalize order to: `TL, TR, BR, BL`.

## B) Pose label format

`class x_c y_c w h kp1_x kp1_y v kp2_x kp2_y v kp3_x kp3_y v kp4_x kp4_y v`

- class = `0`
- visibility `v = 2` for valid corrected points
- bbox rule:
  - `rect` mode: use `crop_box` directly, then convert to normalized `x_c, y_c, w, h`.
  - `quad` mode: compute axis-aligned bbox from `quad_points` first, then normalize.

## C) Seg label format (if used)

`class x1 y1 x2 y2 x3 y3 x4 y4`

All coordinates normalized.

---

## 6) Threshold / Inactive Gate Design

Define minimum counts:

- IEP1A: `book >= 10`
- IEP1A: `newspaper >= 10`
- IEP1A: `microfilm >= 10`
- IEP1B: `book >= 10`
- IEP1B: `newspaper >= 10`
- IEP1B: `microfilm >= 10`

Each service/material is evaluated independently. A retraining run exports and trains only the combinations that meet the threshold; missing or below-threshold combinations do not block eligible combinations.

Builder output contract:

- `status = ok` if all thresholds pass
- `status = min_samples_not_met` if any threshold fails
- include counts by model/material and reason

Worker contract:

- on `min_samples_not_met`, do not train; log clear reason and exit gracefully.

---

## 7) Implementation Phases

## Phase 0 - Contract freeze (0.5-1 day) — **complete**

- Freeze corrected-only query filters.
- Freeze corner ordering and normalization rules.
- Freeze minimum threshold policy.

## Phase 1 - Builder exporter core (2-3 days) — **complete**

In `services/dataset_builder/app/main.py` (or helpers), implement:

- corrected-row query
- URI image fetch/export
- quad/rect conversion
- corner canonicalization
- YOLO line writers
- dataset directory + split writer
  - use `train/val` split with deterministic seed
  - stratify by `job_id` (keep pages from the same job together to reduce leakage)
- `data.yaml` generation
- manifest generation

## Phase 2 - Registry and selector integration (1 day) — **complete**

- checksum calculation
- registry append
- ensure selector consumes new manifests in `corrected_hybrid` / `corrected_only`.

## Phase 3 - Worker path (1 day) — **complete (inactive); active path wired**

- verify corrected-export success path to training (implemented; full run awaits enough corrected rows + images).
- verify no-train inactive path (verified in deployment logs: `min_samples_not_met`).

## Phase 4 - Tests + docs (1-2 days) — **complete**

- unit tests for conversion and ordering (`tests/test_dataset_builder.py`)
- threshold tests (`min_samples_not_met` subprocess test)
- threshold gate validation (including `min_samples_not_met` contract)
- selector tests (`tests/test_dataset_registry.py`)
- contract doc: `docs/retraining_runtime_contract.md`
- reproducible verification: **§13** below

---

## 8) File-Level Change Plan

- `services/dataset_builder/app/main.py`
  - add corrected-export mode and conversion pipeline.
- `services/retraining_worker/app/dataset_registry.py`
  - ensure builder output is accepted and status handled.
- `services/retraining_worker/app/task.py`
  - ensure `min_samples_not_met` exits cleanly without training.
- `docs/retraining_runtime_contract.md`
  - document corrected-only source and inactive gate behavior.
- `tests/test_dataset_builder.py`
  - conversion + threshold + manifest tests.
- Reproducible checks for operators: **§13** (same stack as local retraining worker tests; no separate “step” naming required).

---

## 9) Commands / Runtime Contract

Builder command example:

- `python services/dataset_builder/app/main.py --mode corrected-export --source-window 2026W17`

`--source-window` should filter on `page_lineage.human_correction_timestamp` (not `created_at`).

Required stdout JSON fields:

- `status` (`ok` | `min_samples_not_met` | `error`)
- `dataset_version`
- `dataset_checksum`
- `manifest_path`
- `registry_path`
- `counts`
- `skipped_counts`

---

## 10) Definition of Done

- [x] Corrected-only query enforced.
- [x] Uses `source_artifact_uri` (not corrected output) for training images.
- [x] Corner order deterministic (`TL,TR,BR,BL`).
- [x] YOLO labels valid and normalized.
- [x] Manifest + registry entries generated.
- [x] Minimum threshold gate works and blocks training when unmet.
- [x] Worker supports both:
  - inactive no-train path
  - active train path
- [x] Golden evaluation: merge contract and gate aggregation covered by `tests/test_golden_gate_merge.py`; worker can run `evaluate_golden_dataset.py` when `LIBRARYAI_RETRAINING_GOLDEN_EVAL=live` (requires golden manifest + S3/read access — ops checklist when those are available).
- [x] Reproducible verification steps documented in **§13** (inactive path evidence captured; active path follows same compose services when thresholds pass).

**Not covered by this document alone (needs real data + ops):** one full production-style run of §3 steps 6–9 (export at scale → train → **live** golden on hosted assets → promotion) on a database that actually has enough accepted human-corrected pages and reachable image URIs. That is the same as §12 item 5 — not “undone code,” but **pending runtime validation**.

---

## 11) Risks and Mitigations

1. Not enough corrected samples
   - Use inactive gate (`min_samples_not_met`), no bad training.
2. Geometry inconsistencies
   - Canonical ordering + strict validation.
3. Missing image dimensions
   - fallback to downsample metadata.
4. S3/object fetch failures
   - retries + skip accounting + fail threshold.

---

## 12) Immediate Next Steps (Actionable) — **status**

1. ~~Implement corrected-export mode in dataset builder.~~ **Done**
2. ~~Implement threshold gate + status contract.~~ **Done**
3. ~~Add tests for corner normalization and gate behavior.~~ **Done** (`tests/test_dataset_builder.py`, `tests/test_dataset_registry.py`, golden merge tests)
4. ~~Run dry-run and confirm `min_samples_not_met` if data is sparse.~~ **Done** (see §13)
5. **Ongoing:** keep services deployed; when corrected row counts pass thresholds, the same path exports and trains without code changes.

---

## 13) Reproducible verification (inactive path, local Docker)

Prereqs: Docker Desktop running; repo at project root; `.env` for containers uses `DATABASE_URL=...@postgres:5432/...` (service name `postgres` inside Compose).

1. Start the minimal MLOps slice (adjust compose files if your repo uses a local overlay for live train flags):

   `docker compose up -d postgres redis minio mlflow eep retraining-worker`

2. Confirm corrected dataset mode inside the worker (use **single quotes** in PowerShell so `$VAR` is evaluated inside the container):

   `docker compose exec retraining-worker /bin/sh -lc 'printenv RETRAINING_DATASET_MODE RETRAINING_IEP0_MODE'`

3. Fire a retraining webhook (example trigger type; pick one not in cooldown — see `services/eep/app/retraining_webhook.py`):

   - POST `http://127.0.0.1:8000/v1/retraining/webhook` with header `X-Webhook-Secret` matching `RETRAINING_WEBHOOK_SECRET`, body shaped like Alertmanager (`alerts[].labels.trigger_type`, `alerts[].status=firing`).

4. Expected with **no** human-corrected rows: worker logs contain `min_samples_not_met` and counts for all IEP1A/IEP1B materials plus `rows_total`.

   `docker compose logs --since 30m retraining-worker`

5. If a **host** script polls Postgres, set `DATABASE_URL` to `127.0.0.1:5432` (not `postgres`), e.g.  
   `postgresql+psycopg2://libraryai:changeme@127.0.0.1:5432/libraryai`

This section is the evidence trail for “safe when empty”; when corrected data exists, steps 1–5 are the same, and logs should show export + training instead of deferral.
