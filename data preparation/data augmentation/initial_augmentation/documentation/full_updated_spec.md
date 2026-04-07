# LibraryAI — Full Updated Product Specification

**Version:** 2.0
**Date:** 2025-07-15

This is the authoritative single-source-of-truth specification for the LibraryAI processing system.

---

## 1. System Overview

LibraryAI processes raw scanned images (OTIFF) into corrected, layout-annotated pages suitable for archival ingestion. The system automates page-geometry detection, deskewing, cropping, page splitting, and layout detection while preserving reliability, traceability, and recoverability.

The pipeline is organized around independent processing stages orchestrated by a central execution engine (EEP). Pages progress through preprocessing, optional rectification, and layout detection. Quality gates own all routing decisions. Incorrect ingestion is treated as more costly than unnecessary human review.

**Hard Rule:** No pipeline stage may auto-accept a page when quality or agreement is insufficient.

**Access policy:** Authentication is required. All API endpoints except `POST /v1/auth/token` require a valid JWT bearer token. RBAC (roles: `user`, `admin`) is enforced at API endpoints — `require_user` guards user-scoped endpoints and `require_admin` guards admin-only endpoints. Stability controls, queue limits, concurrency limits, and rate limits are also enforced.

**Global measurement rule:** `processing_time_ms` wherever it appears in any schema is wall-clock elapsed time from request receipt to response serialization, measured in milliseconds using a monotonic clock.

**Threshold derivation rule:** All preprocessing quality thresholds (e.g., IoU, skew residual) are fixed conservative values in the absence of a downstream quality measurement system. Thresholds are validated and may be adjusted only through human auditor assessment using statistically significant sampling during shadow mode and SLO audit sampling. No dependency on OCR or external text-extraction systems is assumed.

---

## 2. Product Intent

LibraryAI exists to automate the AUB Library's scanning workflow while guaranteeing that no incorrect page is silently accepted into the archival system.

The system must:

- convert raw OTIFF scans into clean, deskewed, cropped PTIFFs
- detect page splits in multi-page spreads and produce independent sub-page artifacts
- detect and classify document layout regions (text blocks, titles, tables, images, captions)
- route pages to human review when automation cannot guarantee correctness
- preserve a complete audit trail (lineage) for every page

These goals must hold across heterogeneous library collections: books, newspapers, and documents (including microfilm-captured materials).

The system produces structurally annotated pages (layout regions, column structure) without performing character-level text recognition. Full-text extraction is explicitly out of scope and may be implemented as a separate downstream system.

---

## 3. Core Concepts

**OTIFF:** Raw scanner output. Immutable. The authoritative input. Never overwritten.

**PTIFF:** Processed TIFF. The output of the preprocessing pipeline. Deskewed, cropped, split if necessary.

**IEP (Image-processing Execution Pipeline):** A named processing service. IEP1 handles preprocessing. IEP2 handles layout detection.

**EEP (Execution Engine Pipeline):** The central orchestrator. Owns job management, page routing, quality gates, artifact persistence, lineage recording, and all acceptance decisions.

**Page:** A single processing unit. Corresponds to one OTIFF file. May be a single-page scan or a two-page spread.

**Sub-page:** A child page created when a spread is split. Identified by `(page_number, sub_page_index)`. Left child: index 0. Right child: index 1.

**Page status:** The current processing state of a page. Worker-terminal states are `accepted`, `pending_human_correction`, `review`, `failed`, `split`. Of these, `accepted`, `review`, and `failed` are leaf-final states (permanent terminal outcomes for a final page). `pending_human_correction` is worker-terminal but NOT leaf-final: automated workers stop processing, but explicit human action may requeue the page and transition it to a leaf-final state. `split` is a routing-terminal state for a parent spread page only, never a leaf-page outcome.

**Lineage:** The complete audit record for a page. Includes every service invocation, geometry selection result, artifact URI, and human correction event.

**Job:** A batch of pages submitted together with shared metadata (collection, material type, pipeline mode, policy version).

**Material type:** One of `book`, `newspaper`, `archival_document`. Supplied by the caller; used as metadata for preprocessing heuristics, aspect-ratio sanity bounds, and layout model behavior. Describes the physical type of the document being processed, not the means by which it was captured.

**Capture modality:** The physical scanning method used to digitize the document (e.g., book scanner, microfilm scanner). Capture modality is NOT part of `material_type`. It is collection-level metadata and does not appear in per-page processing schemas. A newspaper captured via microfilm has `material_type="newspaper"`; the microfilm capture is collection metadata recorded separately.

**Pipeline mode:** `preprocess` (preprocessing only) or `layout` (preprocessing + layout detection).

**Shadow mode:** A flag on a job that enables async candidate model evaluation alongside live processing.
**Leaf page:** A page record that is not a split parent. Includes:
- original pages that were not split
- all sub-pages (sub_page_index IS NOT NULL)

Excludes:
- parent pages with status='split'

### PTIFF-stage QA checkpoint

LibraryAI must implement a PTIFF-stage quality assurance checkpoint between preprocessing and any downstream stages.

This checkpoint is modeled as a **page-level non-terminal state**:

- `ptiff_qa_pending`

It is not a terminal state and must not be included in `TERMINAL_PAGE_STATES`.

For every job:
- preprocessing produces PTIFF-equivalent output artifacts
- each successfully preprocessed page enters `ptiff_qa_pending`
- downstream progression depends on the job's configured PTIFF QA mode

QA behavior supports two modes, selected at job creation and persisted on the job:

1. **Manual QA mode (`ptiff_qa_mode="manual"`):**
   - pages remain in `ptiff_qa_pending` until reviewer action
   - reviewers may approve pages individually
   - reviewers may approve all remaining pages currently in `ptiff_qa_pending`
   - reviewers may route individual pages back into correction/edit flow
   - for `pipeline_mode="layout"`, layout must not begin for a page until that page exits `ptiff_qa_pending`

2. **Auto-continue mode (`ptiff_qa_mode="auto_continue"`):**
   - pages automatically transition through `ptiff_qa_pending` without waiting for manual review
   - pages that were auto-accepted by preprocessing and pages that were previously human-corrected are both eligible for automatic transition
   - final routing after automatic transition is:
     - `pipeline_mode="preprocess"` → `accepted`
     - `pipeline_mode="layout"` → `layout_detection`

Approve-all semantics:
- approve-all applies only to pages currently in `ptiff_qa_pending`
- in `ptiff_qa_mode="manual"`, approve-all records approval intent only and must not immediately transition page state out of `ptiff_qa_pending`
- pages already approved, already terminal, or currently in correction must not be altered
- once the PTIFF QA gate is fully satisfied, a controlled gate-release step transitions approved pages to:
  - `accepted` for `pipeline_mode="preprocess"`
  - `layout_detection` for `pipeline_mode="layout"`

Correction return semantics:
- when a page is edited through human correction, it returns to `ptiff_qa_pending`
- from there, the job's `ptiff_qa_mode` determines whether it waits for manual approval or auto-continues

---

## 4. Architecture Overview

### 4.1 Service Inventory

| Service | Port | Role | Compute | Production Deployment |
|---------|------|------|---------|----------------------|
| EEP | 8000 | Orchestration, quality gates, geometry selection, job management, acceptance policy | CPU | Continuous CPU |
| IEP1A | 8001 | YOLOv8-seg instance segmentation — primary page geometry model | GPU (inference) | Scale-to-zero GPU |
| IEP1B | 8002 | YOLOv8-pose keypoint regression — secondary page geometry model | GPU (inference) | Scale-to-zero GPU |
| IEP1C | — | Deterministic normalization: applies selected geometry to full-res image | CPU | Shared module (invoked by EEP, not a network service) |
| IEP1D | 8003 | UVDoc rectification fallback for warped/distorted pages | GPU | Scale-to-zero GPU |
| IEP2A | 8004 | Detectron2 Faster R-CNN layout detection (primary, high accuracy) | GPU | Scale-to-zero GPU |
| IEP2B | 8005 | DocLayout-YOLO layout detection (fast second opinion, document-trained) | GPU (minimal) | Scale-to-zero GPU |

Every service exposes: `GET /health` → 200, `GET /ready` → 200/503, `GET /metrics` → Prometheus text.

IEP1 is a staged cascade with quality gates, not a peer-consensus preprocessing system. IEP1A and IEP1B are both always-on geometry models. Both run on every page. Their structural outputs are compared as a safety check. In the first pass, disagreement between models reduces geometry trust and triggers rescue flow (rectification and a mandatory second geometry pass) rather than immediately halting preprocessing. In the second pass, both models must agree — this is the authoritative safety gate. Agreement is a structural corroboration signal, not a voting or averaging mechanism.

IEP1C is CPU-only deterministic image math. It is invoked as a shared module within EEP, not as a separate network service. A separate service boundary adds latency and operational failure modes without adding model diversity.

IEP1D is a GPU service invoked when artifact validation fails after normalization or when first-pass geometry trust is insufficient. It is a rectification rescue stage, not a geometry model.

### 4.2 Data Flow

```text
RAW OTIFF
   │
   ▼
┌──────────────────────────────┐
│  Parallel geometry inference │
│  IEP1A (YOLOv8-seg) with TTA│
│  IEP1B (YOLOv8-pose) with TTA│
└──────────────┬───────────────┘
               │
               ▼
  Geometry selection (EEP)
    structural agreement
    → sanity checks
    → split confidence
    → TTA variance
    → page area preference
    → select by confidence
               │
               ▼
  IEP1C — deterministic normalization
          (shared module, full-res)
               │
               ▼
  Artifact validation (EEP)
               │
        ┌──────┴──────┐
        │              │
      valid         invalid
        │              │
        │              ▼
        │      IEP1D — UVDoc rectification
        │              │
        │              ▼
        │      Second geometry pass
        │      (IEP1A + IEP1B, parallel)
        │              │
        │              ▼
        │      Second geometry selection (EEP)
        │              │
        │              ▼
        │      Second normalization (IEP1C)
        │              │
        │              ▼
        │      Final validation (EEP)
        │              │
        └──────┬───────┘
               │
        ┌──────┴──────┐
        ▼              ▼
   ACCEPTED     pending_human_correction
   (preprocess
    mode stops
    here)
        │
        ▼ (layout mode only)
   IEP2A layout detection
        │
        ▼ (if IEP2A returns plausible output)
   IEP2B layout detection
        │
        ▼
   Layout consensus gate (EEP)
        │
   ┌────┴────┐
   ▼         ▼
ACCEPTED   review
```

### 4.3 Internal Endpoint Summary

| Service | Route | Request Schema | Response Schema | Timeout |
|---------|-------|---------------|-----------------|---------|
| IEP1A | `POST /v1/geometry` | GeometryRequest | GeometryResponse or PreprocessError | 30s |
| IEP1B | `POST /v1/geometry` | GeometryRequest | GeometryResponse or PreprocessError | 30s |
| IEP1C | (shared module) | NormalizeRequest (internal) | PreprocessBranchResponse (internal) | — |
| IEP1D | `POST /v1/rectify` | RectifyRequest | RectifyResponse | 60s |
| IEP2A | `POST /v1/layout-detect` | LayoutDetectRequest | LayoutDetectResponse (detector_type="detectron2") | 60s |
| IEP2B | `POST /v1/layout-detect` |LayoutDetectRequest | LayoutDetectResponse (detector_type="doclayout_yolo") | 30s |

IEP1A and IEP1B expose identical endpoint schemas. The same GeometryRequest/GeometryResponse contract serves both.

### 4.4 Repository Structure

```text
libraryai/
├── README.md
├── pyproject.toml
├── Makefile
├── docker-compose.yml
├── .pre-commit-config.yaml
├── docs/
│   ├── full_updated_spec.md          ← this file
│   ├── updated_spec.md
│   ├── training_data.md
│   ├── architecture.md
│   └── ...
├── services/
│   ├── eep/
│   │   └── app/
│   │       ├── main.py
│   │       ├── jobs/
│   │       ├── worker/
│   │       │   └── task.py
│   │       ├── gates/
│   │       │   ├── geometry_selection.py      ← structural agreement + selection
│   │       │   ├── artifact_validation.py     ← hard + soft artifact checks
│   │       │   └── layout_gate.py
│   │       ├── db/
│   │       ├── shadow_enqueue.py
│   │       ├── shadow_worker.py
│   │       ├── promotion_api.py
│   │       ├── retraining_webhook.py
│   │       └── lineage_api.py
│   ├── iep1a/        — YOLOv8-seg geometry service
│   ├── iep1b/        — YOLOv8-pose geometry service
│   ├── iep1d/        — UVDoc rectification service
│   ├── iep2a/        — Detectron2 layout detection
│   └── iep2b/        — DocLayout-YOLO layout detection
├── shared/
│   ├── schemas/
│   │   ├── ucf.py
│   │   ├── preprocessing.py
│   │   ├── geometry.py          ← IEP1A/IEP1B geometry request/response
│   │   ├── normalization.py     ← IEP1C normalize request (internal)
│   │   ├── iep1d.py             ← rectification request/response
│   │   ├── layout.py
│   │   └── eep.py
│   ├── normalization/           ← IEP1C shared module
│   │   ├── __init__.py
│   │   └── normalize.py
│   ├── io/storage.py
│   ├── gpu/backend.py
│   ├── shadow/
│   ├── ml_metrics/
│   └── lineage/
├── training/
│   ├── preprocessing/     — IEP1A (seg) + IEP1B (pose) training
│   ├── layout_detection/
│   ├── doclayout_yolo/
│   └── rectification/
├── monitoring/
└── tests/
```

---

## 5. IEP Framework

### 5.1 Design Principle

The IEP framework structures processing into named services, each with a single well-defined responsibility. EEP orchestrates all IEP calls and owns all routing decisions.

**No IEP service may make routing decisions.** Each service returns its result; EEP decides what to do with it.

### 5.2 IEP1 Staged Cascade and Structural Safety Check

IEP1 is a quality-gated staged processing cascade, not a peer-consensus system. Two architecturally diverse geometry models — IEP1A (YOLOv8-seg, instance segmentation) and IEP1B (YOLOv8-pose, keypoint regression) — run in parallel on every page. Their structural outputs are compared as a safety check before any preprocessing is accepted. Agreement between them is a safety signal — a binary verification that both models independently reach the same structural conclusion — not a voting or averaging mechanism.

The IEP1 staged cascade:

1. Two always-on geometry models (IEP1A and IEP1B) predict page structure independently.
2. A structural safety check verifies both agree on `page_count` and `split_required`. However, agreement is not required for initial processing. When disagreement occurs in the first pass, the system proceeds with a provisional geometry hypothesis and attempts recovery via normalization and rectification. Final acceptance requires structural agreement after rectification (second pass).

If agreement cannot be achieved at that stage, the page is routed to pending_human_correction.
3. A deterministic geometry selection cascade chooses the best geometry from the available candidates.
4. A deterministic normalization module (IEP1C) applies the selected geometry to the full-resolution image.
5. Artifact validation evaluates the produced output.
6. A rectification fallback (IEP1D) rescues pages where normalization produces poor quality.
7. After rectification, a second geometry pass + normalization + validation cycle runs.
8. Pages that fail all validation routes go to `pending_human_correction`.

The IEP2 layout detection pipeline uses consensus agreement between two layout detectors (IEP2A + IEP2B). Consensus is an IEP2-only concept.

### 5.3 Shared Invariant

**Agreement between independent models is the primary safety mechanism across both IEP1 and IEP2.**

For IEP1: structural agreement between IEP1A and IEP1B is the authoritative safety condition for final auto-acceptance. In the first pass, disagreement lowers geometry trust and triggers rescue flow; in the second pass, agreement is mandatory. The treatment of disagreement is stage-dependent:
- **First pass:** disagreement reduces geometry trust to low. The pipeline does not halt; instead, the best available provisional candidate is selected and the page proceeds through rectification and a mandatory second geometry pass.
- **Second pass (post-rectification):** both models must return valid outputs and agree on `page_count` and `split_required`. Disagreement at this stage is terminal and routes to `pending_human_correction`. No exceptions.

High confidence from a single model is never sufficient for final auto-acceptance. This is not consensus voting; it is a two-stage quality gate requiring structural corroboration before final acceptance.

For IEP2: consensus agreement between IEP2A and IEP2B on layout regions after both services map their native classes to the canonical LibraryAI layout ontology. Single-model auto-acceptance is prohibited. Consensus is the appropriate term for IEP2 because layout region matching involves a similarity-scored comparison across region sets, not a binary structural verification. Agreement between structurally diverse detectors reduces the probability of error but does not eliminate it; agreement is used as a gating signal, not as a guarantee of correctness.

For all cases requiring rectification: the second geometry pass is mandatory. If rectification is unavailable or fails, the artifact is routed to `pending_human_correction`. Skipping the second geometry pass is not permitted for any low-trust or rescue-required artifact.

---

## 6. IEP1 Specification

### 6.1 Design Principle

IEP1 is structure-first and geometry-first. The preprocessing problem is primarily a page-structure and page-geometry problem, not a deskew-heuristic problem.

IEP1:

- learns explicit page geometry using two independent models with different output representations
- requires structural agreement between both models for final auto-acceptance; first-pass disagreement reduces geometry trust and triggers rectification and second-pass verification rather than immediate rejection
- applies geometry deterministically on the full-resolution image
- rescues difficult pages with a geometric rectification fallback (UVDoc)
- routes to human review when uncertainty is unresolved

The IEP1A + IEP1B pair provides the primary defense against the most dangerous failure mode: a high-confidence wrong prediction that gets auto-accepted. When IEP1A is confidently wrong (e.g., fails to detect a spread), IEP1B independently disagrees. This disagreement lowers geometry trust and forces the page into rescue flow with mandatory rectification and second-pass verification. If structural trust is not restored, the page routes to human review.

When two architecturally diverse models agree, a third rarely disagrees. A third always-on model is not warranted.

### 6.2 IEP1A — YOLOv8-seg Instance Segmentation

**Port:** 8001
**Compute:** GPU (inference on proxy image)

IEP1A is the primary page geometry model. It uses YOLOv8-seg (instance segmentation) to predict page regions as segmentation masks, from which quadrilateral geometry is derived via mask contour fitting.

**Endpoint:** `POST /v1/geometry`

**Request:** GeometryRequest

**Response:** GeometryResponse or PreprocessError

**Model:** YOLOv8-seg fine-tuned on AUB data with page instance segmentation annotations. Predicts per-page instance masks; geometry (quadrilateral corners, bounding box, page area) is derived from mask contours.

**Expected outputs:**

- `page_count` — number of detected page instances (1 or 2)
- `pages[]` — one or two page regions, each with:
  - quadrilateral corners derived from mask contour
  - bounding box
  - instance confidence
  - `page_area_fraction` — detected page area as fraction of full image area
- `split_required` — True when `page_count == 2`
- `split_x` — split coordinate derived from the boundary between two detected page instances
- `geometry_confidence` — overall confidence (minimum across detected instances)
- TTA-derived uncertainty fields (see below)
- warnings and uncertainty flags

**Uncertainty estimation — TTA (Test-Time Augmentation):**

Both IEP1A and IEP1B use TTA for uncertainty estimation. MC dropout is not used. Both models use BatchNorm throughout the YOLO backbone. MC dropout — enabling dropout at inference time while BatchNorm remains in eval mode — produces inconsistent uncertainty estimates because stochastic dropout changes the activation distribution that BatchNorm was calibrated for. TTA does not have this problem because it applies augmentations to the input, not to the network internals.

**TTA procedure:**

1. Apply N augmentations to the proxy image (horizontal flip, small rotation ±2–3°, small scale ±5–10%).
2. Run model inference on each augmented input.
3. Map predictions back to original image coordinates (invert the augmentation).
4. Compute mode of `page_count` and `split_required` across passes → structural prediction.
5. `tta_structural_agreement_rate` = fraction of passes matching the mode prediction.
6. `tta_prediction_variance` = inter-pass variance of geometry predictions (corner coordinates or mask IoU).
7. Report the mode prediction as the primary output.

**Design constraints:**

- IEP1A must run on a proxy (downscaled) image for speed. It must not persist durable artifacts.
- Proxy resolution is an implementation-critical parameter. It must be calibrated empirically on a held-out AUB validation set. The system must not assume one fixed downscale ratio is equally safe for books, newspapers, and microfilm frames.
- IEP1A absorbs the online single-page vs spread decision: split detection emerges from instance count. No separate classifier is needed.

**Readiness check:** YOLOv8-seg model loaded AND CUDA available.

### 6.3 IEP1B — YOLOv8-pose Keypoint Regression

**Port:** 8002
**Compute:** GPU (inference on proxy image)

IEP1B is the secondary page geometry model. It uses YOLOv8-pose (keypoint regression) to predict page corners directly as coordinate keypoints, providing geometry from a fundamentally different output representation than IEP1A's segmentation masks.

**Endpoint:** `POST /v1/geometry`

**Request:** GeometryRequest

**Response:** GeometryResponse or PreprocessError

**Model:** YOLOv8-pose fine-tuned on AUB data with page corner keypoint annotations. Predicts 4 corner keypoints per page instance; geometry (quadrilateral corners, bounding box, page area) is derived directly from keypoint coordinates.

**Expected outputs:** Same schema as IEP1A — GeometryResponse. Both models produce identical output types despite using different internal representations.

**Uncertainty estimation:** Same TTA procedure as IEP1A (Section 6.2). Both models use the same number of TTA passes and the same augmentation strategy.

**Why IEP1B uses a different representation from IEP1A:**

IEP1A (segmentation) predicts pixel-level masks and derives corners from contours. IEP1B (keypoint) predicts corner coordinates directly. Their error modes are different:

- IEP1A may produce noisy mask boundaries that affect contour-derived corners, especially for small pages where mask resolution is limited.
- IEP1B may produce imprecise keypoint locations on pages with ambiguous corners, but is more robust on small pages where mask resolution degrades.

This architectural diversity is the reason their errors are less correlated than two models of the same type, and why structural agreement between them is a stronger safety signal than single-model confidence.

**Page area preference:** When `page_area_fraction < config.page_area_preference_threshold` (default 0.30), IEP1B geometry is preferred over IEP1A because IEP1A mask resolution degrades for small pages relative to the full image.

**Design constraints:** Same as IEP1A — runs on proxy image, must not persist durable artifacts.

**Readiness check:** YOLOv8-pose model loaded AND CUDA available.

### 6.4 IEP1C — Deterministic Normalization Module

**Port:** None (shared module, not a network service)
**Compute:** CPU

IEP1C applies deterministic geometry normalization on the full-resolution image using the geometry selected by EEP from the IEP1A/IEP1B outputs.

**Invocation:** Called as a shared Python module by EEP, not via HTTP.

**Input:** NormalizeRequest (internal schema)

**Output:** PreprocessBranchResponse

**Responsibilities:**

- crop per detected page region using the selected geometry
- derive deskew from predicted quadrilateral corners (perspective correction when available, affine rotation otherwise)
- apply perspective correction when quadrilateral geometry is available
- generate the normalized page artifact
- compute quality metrics on the produced artifact (`blur_score`, `border_score`, `foreground_coverage`, `skew_residual`)

**Split handling:** When `split_required=True`, IEP1C splits the full-resolution image at `split_x` and normalizes each half independently. Each half produces a separate PreprocessBranchResponse.

**Outputs:** normalized page image artifact, TransformRecord, crop_box, deskew information, `split_required` / `split_x` copied from the selected geometry, quality metrics and warnings, `source_model` indicating which geometry model was selected.

**Why not a service:** IEP1C is deterministic image math — crop, affine transform, perspective warp. A separate service boundary adds network latency, serialization overhead, and an additional failure mode without adding model diversity or compute isolation value. A dedicated CPU service remains an option only if later scaling or isolation needs justify it.

### 6.5 IEP1D — UVDoc Rectification Fallback

**Port:** 8003
**Compute:** GPU

IEP1D is a rectification-only service. Its role is to rescue difficult pages when deterministic normalization produces a normalized artifact that fails validation.

**Endpoint:** `POST /v1/rectify`

**Request:** RectifyRequest

**Response:** RectifyResponse

**Typical trigger cases:**

- warped bound pages
- strong page curl
- perspective-heavy captures
- microfilm frames with severe distortion

**Recommended model:** UVDoc as the first fallback rectifier candidate.

**Critical constraints:**

- IEP1D does not decide split. Split ownership remains with the original full-image geometry from the initial geometry pass.
- IEP1D does not replace IEP1A/IEP1B as the geometry source.
- IEP1D improves an already selected page artifact; it does not redefine the page structure of the original raw scan.
- When the source image is a spread, IEP1D may improve a child page artifact but must not redefine `split_required` or `split_x`.
- Rectification is attempted at most once per page.

**Readiness check:** UVDoc model loaded AND CUDA available.

**Deployment gate:** Before IEP1D is enabled in production, the system must measure the baseline performance of IEP1A + IEP1B + IEP1C + artifact validation on a held-out validation set. IEP1D should be added only after that baseline is established, so its gain from rectification can be measured rather than assumed.

**Post-rectification flow:** After IEP1D rectifies the artifact, a second geometry pass (IEP1A + IEP1B again) runs on the rectified image to produce refined page boundaries. This second pass is authoritative: both IEP1A and IEP1B must return valid outputs and agree on `page_count` and `split_required`. Disagreement or any model failure in the second pass is terminal and routes to `pending_human_correction`. The rationale: rectification changes the image geometry (dewarps the page), so the original geometry predictions made on the warped image are no longer accurate. Re-running geometry on the rectified image provides better boundary detection for the final normalization.

### 6.6 IEP1 Schemas

#### GeometryRequest

| Field | Type | Constraint |
|-------|------|-----------|
| job_id | str | none |
| page_number | int | ge=1 |
| image_uri | str | URI of proxy/downscaled image |
| material_type | Literal["book", "newspaper", "archival_document"] | none |

#### PageRegion

| Field | Type | Notes |
|-------|------|-------|
| region_id | str | e.g. "page_0", "page_1" |
| geometry_type | Literal["quadrilateral", "mask_ref", "bbox"] | |
| corners | list[tuple[float,float]] \| None | 4 corners if quadrilateral |
| bbox | tuple[int,int,int,int] \| None | bounding box (always present) |
| confidence | float | ge=0, le=1 — per-instance confidence |
| page_area_fraction | float | ge=0, le=1 — detected page area / full image area |

#### GeometryResponse

| Field | Type | Constraint |
|-------|------|-----------|
| page_count | int | ge=1, le=2 |
| pages | list[PageRegion] | 1 or 2 entries |
| split_required | bool | none |
| split_x | int \| None | ge=0 if not None |
| geometry_confidence | float | ge=0, le=1 — min confidence across instances |
| tta_structural_agreement_rate | float | ge=0, le=1 — fraction of TTA passes agreeing on page_count + split_required |
| tta_prediction_variance | float | ge=0 — inter-pass variance of geometry |
| tta_passes | int | ge=1 — number of TTA passes performed |
| uncertainty_flags | list[str] | none |
| warnings | list[str] | none |
| processing_time_ms | float | ge=0 |

Both IEP1A and IEP1B return this identical schema. The `geometry_type` field within PageRegion indicates the representation origin (IEP1A typically returns "quadrilateral" or "mask_ref"; IEP1B typically returns "quadrilateral").

#### NormalizeRequest (internal — used by IEP1C shared module)

| Field | Type | Constraint |
|-------|------|-----------|
| job_id | str | none |
| page_number | int | ge=1 |
| image_uri | str | full-resolution OTIFF or rectified artifact URI |
| material_type | Literal["book", "newspaper",  "archival_document"] | none |
| selected_geometry | GeometryResponse | from whichever model was selected |
| source_model | Literal["iep1a", "iep1b"] | which model produced the selected geometry |

#### PreprocessBranchResponse (canonical post-normalization output)

This schema is the output of IEP1C and is the input to the EEP artifact validation gate. It is also the canonical preprocessing result stored in lineage.

| Field | Type | Constraint |
|-------|------|-----------|
| processed_image_uri | str | none |
| deskew | DeskewResult | none |
| crop | CropResult | none |
| split | SplitResult | none |
| quality | QualityMetrics | none |
| transform | TransformRecord | from ucf.py |
| source_model | Literal["iep1a", "iep1b"] | which geometry model was selected |
| processing_time_ms | float | ge=0 |
| warnings | list[str] | none |

**DeskewResult:** `angle_deg`, `residual_deg` (ge=0), `method` (e.g. "geometry_quad", "geometry_bbox")

**CropResult:** `crop_box` (x_min, y_min, x_max, y_max), `border_score` (ge=0, le=1), `method` (e.g. "geometry_quad", "geometry_bbox")

**SplitResult:** `split_required`, `split_x`, `split_confidence`, `method` (e.g. "instance_boundary")

`split_confidence` is computed as: `min(weakest_instance_confidence, tta_structural_agreement_rate)` — derived from the selected model's TTA passes and instance detection confidence. No separate classification head is needed because split detection emerges from instance count.

**QualityMetrics:** `skew_residual` (ge=0), `blur_score` (ge=0, le=1), `border_score` (ge=0, le=1), `split_confidence`, `foreground_coverage` (ge=0, le=1)

#### PreprocessError

| Field | Type |
|-------|------|
| error_code | Literal["INVALID_IMAGE", "UNSUPPORTED_FORMAT", "TIMEOUT", "INTERNAL", "GEOMETRY_FAILED"] |
| error_message | str |
| fallback_action | Literal["RETRY", "ESCALATE_REVIEW"] |

**fallback_action semantics** (EEP interpretation — these are service-advisory signals, not EEP directives):

- **RETRY** — EEP may retry the service call within the configured retry budget.
- **ESCALATE_REVIEW** — EEP must record a `pending_human_correction` outcome with appropriate `review_reasons`. This signal never causes silent data loss. Every page receiving this signal must be traceable in the job record with a terminal outcome.

#### RectifyRequest

| Field | Type | Constraint |
|-------|------|-----------|
| job_id | str | none |
| page_number | int | ge=1 |
| image_uri | str | URI of the normalized artifact to rectify |
| material_type | Literal["book", "newspaper",  "archival_document"] | none |

#### RectifyResponse

| Field | Type | Constraint |
|-------|------|-----------|
| rectified_image_uri | str | none |
| rectification_confidence | float | ge=0, le=1 |
| skew_residual_before | float | ge=0 |
| skew_residual_after | float | ge=0 |
| border_score_before | float | ge=0, le=1 |
| border_score_after | float | ge=0, le=1 |
| processing_time_ms | float | ge=0 |
| warnings | list[str] | none |
### **6.7 IEP1 Data Flow (EEP Steps)**

---

#### **Step 1 — Raw TIFF Intake**

EEP receives the raw OTIFF via a storage URI.

The system does not accept large image payloads directly in API requests.
Clients must upload images to an external or managed storage location and provide a URI.

Supported ingestion patterns include:

- presigned S3 upload (preferred)
- cloud storage reference (e.g., Google Drive, Dropbox, OneDrive)
- internal storage URI

Steps:

- Resolve and download the OTIFF from the provided URI
- Compute SHA-256 hash and store in `page_lineage.input_image_hash`
- If previous lineage exists with a different hash, raise `ValueError` (corruption detected)
- If `reference_ptiff_uri` is provided, store it for offline evaluation only (must not affect routing)

---

#### **Step 2 — First Parallel Geometry Inference**

EEP updates page status to `"preprocessing"` (compare-and-set from `"queued"`).

EEP derives a proxy image from the raw OTIFF.

EEP invokes both geometry models in parallel:

- IEP1A `POST /v1/geometry` (YOLOv8-seg, with TTA)
- IEP1B `POST /v1/geometry` (YOLOv8-pose, with TTA)

Each model attempts to return a `GeometryResponse` including:

- `page_count`
- `split_required`
- page regions / geometry
- `geometry_confidence`
- uncertainty signals (e.g., TTA agreement, variance)

**Failure handling:**

- If one model fails, the other remains a valid candidate
- If both models fail, no reliable geometry is available

Model failure or disagreement does not trigger immediate human correction.
Uncertainty is handled through validation and rescue stages.

---

#### **Step 3 — First Geometry Selection**

EEP constructs the set of available geometry candidates from Step 2.

1. **Candidate construction**

   - Include all valid model outputs
   - Exclude failed or malformed outputs

2. **Sanity filtering**

   - Apply sanity checks (Section 6.8) independently to each candidate
   - Remove any candidate that fails

3. **Candidate availability**

   - If at least one valid candidate remains → select provisional geometry candidate; proceed to Step 4 (normalization)
   - If no valid candidate remains after sanity, split-confidence, and variance filtering → route to pending_human_correction

4. **Confidence and uncertainty evaluation**

   - Structural agreement between models increases confidence but is not required at this stage
   - Disagreement is treated as uncertainty
   - Consider:
     - `geometry_confidence`
     - `tta_structural_agreement_rate`
     - `tta_prediction_variance`
     - `split_confidence` (if applicable)

5. **Page area preference**

   - Prefer IEP1B (keypoint) when `page_area_fraction` is small

6. **Selection**

   - Select the candidate with highest effective confidence

The selected geometry is treated as the **initial structure hypothesis**.

---

#### **Step 4 — First Deterministic Normalization**

EEP invokes IEP1C with the full-resolution OTIFF and selected geometry.

IEP1C produces:

- normalized page artifact(s)
- deskew / perspective-corrected output
- `TransformRecord`
- quality metrics:
  - `blur_score`
  - `border_score`
  - `foreground_coverage`
  - `skew_residual`

If `split_required=True`:

- split at `split_x`
- process each child independently

---

#### **Step 5 — First Validation and Rectification Decision (per artifact)**

Each artifact is evaluated independently along two dimensions:

##### **(A) Artifact validity and quality**

**Hard requirements (must pass):**

- artifact exists and is decodable
- dimensions are valid
- crop is within bounds
- geometry matches output

**Quality signals:**

- skew residual
- blur score
- border score
- foreground coverage

---

##### **(B) Geometry trustworthiness**

Evaluate whether the selected geometry is reliable enough for auto-ingestion:

- `geometry_confidence`
- `tta_structural_agreement_rate`
- `split_confidence` (if applicable)
- model agreement vs disagreement
- whether geometry came from one or both models

---

##### **Decision**

An artifact proceeds directly to Step 8 only if **both conditions are satisfied**:

- artifact quality is acceptable
- geometry trust is high (i.e., first-pass structural agreement was achieved)

Otherwise, the artifact proceeds to rectification (Step 6).

This includes cases where:

- only one model produced valid geometry
- models disagreed structurally
- geometry confidence or stability is insufficient

Rectification is the mandatory rescue stage before any human correction.

---

##### **Split handling**

When `split_required=True`:

- evaluate each child independently
- only failing children proceed to Step 6

---

#### **Step 6 — Rectification Fallback (IEP1D)**

For each artifact requiring rescue:

EEP invokes IEP1D `POST /v1/rectify`.

IEP1D returns:

- rectified image
- `rectification_confidence`
- quality improvement metrics

**Failure handling:**

- If IEP1D fails or is unavailable:
  - route artifact to `pending_human_correction` (`review_reasons=["rectification_failed"]`)
  - stop further processing for that artifact

Rectification operates on the artifact produced in Step 4.
It improves visual quality only and does not redefine page structure.

---

#### **Step 6.5 — Second Geometry Pass (after rectification)**

For each successfully rectified artifact:

EEP reruns geometry inference:

- IEP1A on rectified proxy
- IEP1B on rectified proxy

Expected behavior:

- already-split child → `page_count=1`, `split_required=False`

If re-splitting occurs:

- route to `pending_human_correction` (`geometry_unexpected_split_on_child`)

---

##### **Second-pass geometry selection (authoritative stage)**

- Both IEP1A and IEP1B must return valid outputs
- Both models must agree on `page_count` and `split_required`

If either condition fails:

- route to `pending_human_correction`

This requirement is strict and non-bypassable.
Second-pass agreement is required to restore trust in geometry.

---

#### **Step 7 — Second Normalization and Final Validation**

EEP:

- reruns IEP1C normalization using second-pass geometry
- reruns validation (same logic as Step 5)

Final decision:

- If validation passes → proceed to Step 8
- If validation fails → route to `pending_human_correction`

This is the final automated rescue attempt.
No further automated rescue is attempted after this step.

---

#### **Step 8 — Split Handling and Downstream Routing**

##### **When `split_required=True`**

After all children exit validation:

- Parent → `status="split"` (terminal routing state)
- Children:
  - assigned `sub_page_index`
  - independent artifacts
  - independent lifecycle

The parent transitions to `split` only after all child artifacts have completed their validation paths.

Each valid child:

- enqueued as a new Redis task

Each failed child:

- `pending_human_correction`

---

##### **When `split_required=False`**

- proceed with single artifact

---

#### **Downstream Routing**

If `pipeline_mode == "preprocess"`:

- store artifact
- set `status="accepted"`
- stop

If `pipeline_mode == "layout"`:

- enqueue for layout detection

#### Step 9 — Layout Detection (IEP2A)

Update page status to "layout_detection".

Invoke IEP2A via GPU backend:
`iep2a_result = gpu_backend.invoke(component="iep2a", ...)`

If IEP2A fails or returns unusable output: status="review", review_reasons=["layout_detection_failed"], return.

**Shadow enqueue (best effort):**

Apply all three conditions:

1. Sampling: `sha256(f"{job_id}:{page_number}") % 100 < shadow_fraction × 100` (deterministic per page)
2. `shadow_mode == True`
3. Staging candidate exists (from in-memory background-refreshed cache)

If all three pass: push shadow task to `libraryai:shadow_tasks`. Failure to enqueue must not affect live routing.

#### Step 10 — Layout Detection (IEP2B)

If IEP2A returned plausible output, invoke IEP2B:
`iep2b_result = gpu_backend.invoke(component="iep2b", ...)`

If IEP2B unavailable: `iep2b_result = None` (single-model mode).

#### Step 11 — Layout Consensus Gate

`result = evaluate_layout_consensus(iep2a, iep2b_or_none, config)`

#### Step 12 — Route After Layout Consensus

If `layout_consensus.agreed == False`: status="review", review_reasons=["layout_consensus_failed"], return.

If `layout_consensus.consensus_confidence < config.layout.min_consensus_confidence`: status="review", review_reasons=["layout_consensus_low_confidence"], return.

#### Step 13 — Persist Artifacts

DB-first write order. For each artifact type (preprocessed, layout):

1. BEGIN transaction → set artifact_state='pending' → COMMIT
2. Write artifact to S3 at deterministic path
3. UPDATE: set output_*_uri, set artifact_state='confirmed'

Update `eep_auto_accept_rate` Gauge (observability only).

Update status to "accepted", routing_path="preprocessing_layout".

### 6.8 Geometry Selection Logic

**File:** `services/eep/app/gates/geometry_selection.py`

The geometry selection cascade receives GeometryResponse from both IEP1A and IEP1B and produces either a selected geometry or a routing decision to human review.

#### Structural Agreement (mandatory, non-negotiable)

```python
def check_structural_agreement(iep1a: GeometryResponse, iep1b: GeometryResponse) -> bool:
    return (iep1a.page_count == iep1b.page_count
            and iep1a.split_required == iep1b.split_required)
```

If `check_structural_agreement(...)` returns `True`, structural trust is initially high, subject to sanity, split-confidence, and variance filtering.

If `check_structural_agreement(...)` returns `False`, structural trust is low. The page does not immediately route to pending_human_correction if at least one usable candidate remains after filtering. Instead, EEP selects the lowest-risk provisional candidate, marks the artifact as rescue-required, and enforces rectification followed by a mandatory second geometry pass. Final auto-acceptance is prohibited unless second-pass structural agreement is achieved. If no usable candidate remains after filtering, route to `pending_human_correction`.

#### Sanity Checks (per model)

Six hard sanity checks applied to each model's prediction. A model fails sanity if any check fails.

| Check | Rule |
|-------|------|
| Page region within image bounds | all corner coordinates ≥ 0 and within proxy dimensions |
| Non-degenerate geometry | quadrilateral area > 0; bounding box width > 0, height > 0 |
| Page area fraction plausible | `config.geometry_sanity_area_min_fraction` ≤ `page_area_fraction` ≤ `config.geometry_sanity_area_max_fraction` |
| Aspect ratio plausible | page region aspect ratio within `config.preprocessing.aspect_ratio_bounds[material_type]` (see Section 8.4); fails if width/height ratio is outside the configured [min, max] for the submitted material_type |
| Corner ordering valid | corners form a convex (or near-convex) quadrilateral; no self-intersecting edges |
| Page regions non-overlapping | when page_count == 2, IoU between two page regions < 0.1 |

If both models fail sanity: route to `pending_human_correction` with `review_reasons=["geometry_sanity_failed"]`.

If one model fails sanity and the other passes: the passing model is retained as a provisional candidate with low geometry trust. Final acceptance requires second-pass agreement after rectification.

#### Split Confidence Filter

When `split_required=True`:

```python
split_confidence = min(weakest_instance_confidence, tta_structural_agreement_rate)
```

If `split_confidence < config.split_confidence_threshold`: remove that model from candidates. Split is the highest-risk structural decision and requires higher confidence than single-page geometry.

If both models fall below the split confidence threshold: route to `pending_human_correction` with `review_reasons=["split_confidence_low"]`.

#### TTA Variance Filter

Remove any model whose `tta_prediction_variance > config.tta_variance_ceiling`.

High TTA variance means the model's predictions are unstable across augmented inputs — the geometry is not reliable even if the primary prediction has high confidence.

If both models exceed the variance ceiling: route to `pending_human_correction` with `review_reasons=["tta_variance_high"]`.

#### Page Area Preference

When `page_area_fraction < config.page_area_preference_threshold` (default 0.30) for any detected page region: prefer IEP1B.

Rationale: IEP1A derives geometry from segmentation masks. At low page-area fractions, the mask occupies relatively few pixels, and contour-derived corners become noisy. IEP1B predicts corners directly as keypoints and is more robust at small page sizes.

This preference is applied only as a tiebreaker when both models pass all preceding filters.

#### Confidence Selection

Among remaining candidates: select the model with higher `geometry_confidence`.

If only one model remains: select it.

#### Selection Result

The output of geometry selection is one of:

- A GeometryResponse from the selected model plus `source_model: Literal["iep1a", "iep1b"]`
- A routing decision to `pending_human_correction` with a specific `review_reason`

The selection decision and all intermediate filter results are logged to `quality_gate_log` for auditability.

### 6.9 Artifact Validation

**File:** `services/eep/app/gates/artifact_validation.py`

Artifact validation evaluates the output of IEP1C normalization. It runs twice in the pipeline: once after the initial normalization (Step 5) and once after post-rectification normalization (Step 7).

#### Hard Requirements

Any failure → artifact is invalid, no scoring.

| Requirement | Check |
|-------------|-------|
| File exists | artifact URI resolves to a readable file |
| Valid image | file decodes as a valid TIFF/image without error |
| Non-degenerate | width > 0, height > 0 |
| Bounds consistency | crop box coordinates within original image bounds |
| Dimension consistency | artifact dimensions match expected crop box dimensions (within rounding tolerance) |

#### Soft Signals

Weighted and combined into a single validation score.

| Signal | Weight | Good range | Suspicious range |
|--------|--------|-----------|-----------------|
| skew_residual | configurable | < 1.0° | > 5.0° |
| blur_score | configurable | < 0.4 | > 0.7 |
| border_score | configurable | > 0.5 | < 0.3 |
| foreground_coverage | configurable | 0.2–0.9 | < 0.1 or > 0.95 |
| geometry_confidence | configurable | > 0.8 | < 0.5 |
| tta_structural_agreement_rate | configurable | > 0.9 | < 0.7 |

Skew residual threshold is fixed at 5° as a conservative operational bound in the absence of a downstream quality measurement system. Adjustment is permitted  through auditor-reviewed sampling and must not increase bad auto-accept rate beyond defined SLO limits.

The combined validation score is computed as a weighted sum of normalized signal values. The validation threshold is configurable via `libraryai-policy`.

This is a soft ensemble over quality signals, not over geometry predictions. It combines information from multiple measurement sources to make a single accept/reject decision on the artifact.

**Optional extension:** Statistical calibration such as conformal prediction is the statistically rigorous way to set the combined score threshold and may be added later, but is not required for the first implementation.

### 6.10 Acceptance Philosophy

**Incorrect ingestion is more costly than unnecessary review.**

Review volume reduction must come from better geometry understanding across two architecturally diverse models, deterministic normalization, and geometric rescue — not from weakening the gate.

The dual-model structural safety check operates in two stages. In the first pass, disagreement between IEP1A and IEP1B reduces geometry trust and triggers rescue flow (rectification and mandatory second-pass verification) rather than immediate rejection. In the second pass, structural agreement is mandatory — disagreement is terminal and routes to `pending_human_correction`. This two-stage design ensures that no page can be auto-accepted based on a single model's confident-but-wrong prediction. It is not peer-consensus preprocessing: IEP1 uses quality-gated staged routing where structural checks are gates in a cascade. Consensus as a concept applies to IEP2 layout detection only.

Threshold adjustments must not weaken safety guarantees. Any threshold change must be validated against SLO audit sampling and must not increase the bad auto-accept rate.

---

## 7. IEP2 — Layout Detection

### 7.1 IEP2A — Detectron2 Layout Detection

**Port:** 8004
**Compute:** GPU

**Endpoint:** `POST /v1/layout-detect`
**Request:** LayoutDetectRequest
**Response:** LayoutDetectResponse with `detector_type="detectron2"`

**Model:** Detectron2 Faster R-CNN, ResNet-50-FPN backbone, pretrained PubLayNet weights — no fine-tuning required for initial deployment. Detectron2’s two-stage refinement improves localization and classification in overlapping or ambiguous regions, but its ability to detect very small regions depends on training priors and may be limited when using pretrained weights such as PubLayNet without fine-tuning.

- `NUM_CLASSES = 5`
- `SCORE_THRESH_TEST = 0.3`
- Class mapping: 0→text_block, 1→title, 2→table, 3→image, 4→caption
- `advertisement` and `column_separator` classes removed (no public training data; column structure inferred algorithmically via DBSCAN on text_block x-centroids)

**Postprocessing:**

- Merge overlapping same-type regions (IoU > 0.5, preserve higher-confidence ID)
- Recalibrate confidence: small regions (<1% page) × 0.8, edge regions × 0.9
- Infer column structure via DBSCAN on text_block x-centroids (eps = `config.layout.dbscan_eps_fraction` × page_width; default 0.08)

**Readiness check:** Detectron2 production model loaded AND CUDA available.

IEP2A serves live layout detection using the production model only. Candidate model evaluation for promotion is handled by the asynchronous shadow evaluation pipeline.

### 7.2 IEP2B — DocLayout-YOLO Layout Detection

**Port:** 8005
**Compute:** GPU (minimal)

**Endpoint:** `POST /v1/layout-detect`
**Request:** LayoutDetectRequest
**Response:** LayoutDetectResponse with `detector_type="doclayout_yolo"`

**Model:** DocLayout-YOLO with pretrained document-layout weights (DocStructBench-aligned class vocabulary), used without fine-tuning for initial deployment. IEP2B maps its native output classes to LibraryAI’s canonical 5-class schema before returning `LayoutDetectResponse`. DocLayout-YOLO improves initial deployment suitability through document-specific training and multi-scale handling, but it does not imply semantic understanding of reading order or layout hierarchy.

**Purpose:** Fast second opinion. Provides a document-trained, architecturally distinct counterpoint to IEP2A. It is used to catch gross structural errors and to make the layout consensus gate meaningful through lower error correlation with Detectron2.

**Postprocessing:** Apply native-to-canonical class mapping, exclude non-canonical classes, merge overlapping same-type canonical regions (IoU > 0.5, preserve higher-confidence ID), and compute canonical histograms and confidence summary.

### 7.3 Layout Schemas

#### RegionType enum

```python
class RegionType(str, Enum):
    text_block = "text_block"
    title = "title"
    table = "table"
    image = "image"
    caption = "caption"
```

#### Region

| Field | Type | Constraint |
|-------|------|-----------|
| id | str | regex `^r\d+$`; unique within page |
| type | RegionType | none |
| bbox | BoundingBox | from ucf.py |
| confidence | float | ge=0, le=1 |

Region IDs assigned sequentially (r1, r2, r3, …) after final postprocessing. IDs do not need to remain stable across different model runs.

#### LayoutDetectRequest

| Field | Type | Constraint |
|-------|------|-----------|
| job_id | str | none |
| page_number | int | ge=1 |
| image_uri | str | none |
| material_type | Literal["book", "newspaper", "archival_document"] | none |

#### LayoutDetectResponse

| Field | Type | Constraint |
|-------|------|-----------|
| region_schema_version | Literal["v1"] | none |
| regions | list[Region] | unique IDs |
| layout_conf_summary | LayoutConfSummary | mean_conf, low_conf_frac |
| region_type_histogram | dict[str, int] | none |
| column_structure | ColumnStructure \| None | column_count, column_boundaries |
| model_version | str | none |
| detector_type | Literal["detectron2", "doclayout_yolo"] | none |
| processing_time_ms | float | ge=0 |
| warnings | list[str] | none |

#### LayoutConfSummary

| Field | Type | Constraint | Description |
|-------|------|-----------|-------------|
| mean_conf | float | ge=0, le=1 | Mean confidence across all detected regions in this result |
| low_conf_frac | float | ge=0, le=1 | Fraction of regions with confidence < 0.5 |

#### ColumnStructure

| Field | Type | Constraint | Description |
|-------|------|-----------|-------------|
| column_count | int | ge=1 | Number of inferred text columns |
| column_boundaries | list[float] | length == column_count − 1; values ge=0, le=1; sorted ascending | x-coordinates of column dividers as fractions of page width |

`column_boundaries` has exactly `column_count − 1` entries. A single-column page has `column_count=1` and `column_boundaries=[]`.

### 7.4 Layout Consensus Gate

**Consensus principle:** detector agreement is the primary decision signal. `consensus_confidence` summarizes how strongly the detectors agree but is never used to override disagreement.

**Dual-model mode:**

- Match regions between IEP2A and IEP2B using greedy one-to-one matching by descending IoU. Matching is performed on canonical regions after native-to-canonical class mapping inside each service. A match requires IoU ≥ `config.match_iou_threshold` (default 0.5) AND same canonical `RegionType`.
- `total = max(len(iep2a_regions), len(iep2b_regions))`
- `match_ratio = matched_regions / total`
- `type_histogram_match`: for every region type in either histogram, absolute count difference ≤ `config.max_type_count_diff` (default 1)
- `agreed = match_ratio >= config.min_match_ratio (0.7) AND type_histogram_match`
- When agreed: use IEP2A (Detectron2) regions as canonical layout.

**Single-model fallback (IEP2B unavailable):** `agreed = False` unconditionally. Single-model auto-acceptance is prohibited.

#### LayoutConsensusResult schema:

| Field | Type | Notes |
|-------|------|-------|
| iep2a_region_count | int | none |
| iep2b_region_count | int | none |
| matched_regions | int | none |
| unmatched_iep2a | int | none |
| unmatched_iep2b | int | none |
| mean_matched_iou | float | none |
| type_histogram_match | bool | none |
| agreed | bool | none |
| consensus_confidence | float | 0.6\*match_ratio + 0.2\*mean_iou + 0.2\*histogram_match |
| single_model_mode | bool | none |

---

## 8. Execution Model

### 8.1 EEP Worker

**File:** `services/eep/app/worker/task.py`

The EEP worker is a standalone process (separate from the API server). Each worker process generates a unique `worker_id` (UUID4) at startup. This ID is stable for the lifetime of the process.

**Concurrency control:** Redis semaphore with key `libraryai:worker_slots` (initialized to `config.max_concurrent_pages`, default 20). Before processing each task: DECR slots; if negative, INCR and wait with exponential backoff (1s, 2s, 4s, 8s max). On task completion (success or error): release via LREM ACK → DEL task data → DEL worker lease → INCR slots. Release must happen in `try/finally`.

**Circuit breaker:** Wraps every external IEP call (IEP1A, IEP1B, IEP1D, IEP2A, IEP2B). Opens after `config.circuit_breaker_failure_threshold` (default 5) consecutive failures. Allows one probe call after `config.circuit_breaker_reset_timeout_seconds` (default 60s). State stored in-process per-worker.

### 8.2 Full Process — process_page()

```text
0.  Download OTIFF, compute SHA-256, store hash in page_lineage.
    If reference_ptiff_uri provided: store for offline evaluation only.

1.  Update page status to "preprocessing" (CAS from "queued").

2.  Derive proxy image from OTIFF.
    Call IEP1A POST /v1/geometry AND IEP1B POST /v1/geometry IN PARALLEL
    (both with TTA; timeout: 30s each, circuit breaker on each).
    If both models fail to return a valid GeometryResponse (timeout, error, malformed response):
      → status="pending_human_correction"
      → review_reasons=["geometry_failed"]
      → return
    If one model fails or models disagree:
      → proceed with provisional geometry
      → mark geometry trust as low
      → enforce second-pass agreement after rectification

3.  Run geometry selection cascade (Section 6.8):
    3a. Structural agreement check:

    If both models agree:
        → geometry trust = high

    If one model fails or models disagree:
        → proceed with provisional geometry
        → geometry trust = low
        → enforce rectification + second-pass agreement

    3b. Sanity check filtering (6 hard checks per model).
    3c. Split confidence filtering (when split_required=True).
    3d. TTA variance filtering.
    3e. Page area preference (IEP1B when page_area_fraction < 0.30).
    3f. Select by geometry_confidence among remaining candidates.
    If no candidates survive:
      → status="pending_human_correction"
      → review_reasons=["geometry_selection_failed"]
      → return
    Log selection decision to quality_gate_log (gate_type="geometry_selection").

4.  Invoke IEP1C shared module: normalize full-res OTIFF using selected geometry.
    If split_required: split at split_x, normalize each half independently.
    IEP1C produces: normalized artifact(s), TransformRecord, quality metrics.
    If IEP1C fails:
      → status="pending_human_correction"
      → review_reasons=["normalization_failed"]
      → return

5.  Run artifact validation (Section 6.9):
    Hard requirements + soft signal scoring.
    Log validation result to quality_gate_log (gate_type="artifact_validation").
    When split_required=False:
      If valid AND geometry trust is high → skip to Step 8.
      Otherwise (invalid OR geometry trust is low) → proceed to Step 6.
    When split_required=True (IEP1C produced left and right child artifacts):
      Run validation independently for each child artifact.
      For each child: if valid AND geometry trust is high → mark ready for Step 8.
      For each child: if invalid OR geometry trust is low → proceed to Step 6 for that child only.
      A child proceeding directly to Step 8 must not carry low geometry trust. Both children must resolve before Step 8.

6.  [Per invalid child artifact] Call IEP1D POST /v1/rectify (timeout: 60s).
    (Update page status to "rectification" before first IEP1D call.)
    When split_required=True: invoke IEP1D only for the child(ren) that failed Step 5.
    If IEP1D fails or is unavailable for a given artifact:
      → route that artifact to pending_human_correction
      → review_reasons=["rectification_failed"]
      → stop further processing for that artifact.

6.5 [Per rectified artifact — only if IEP1D succeeded] Second geometry pass:
    Derive proxy from rectified artifact.
    Call IEP1A POST /v1/geometry AND IEP1B POST /v1/geometry IN PARALLEL.
    Expected output on already-split child artifacts: page_count=1, split_required=False.
    If either model returns page_count=2 or split_required=True on an already-split child:
      → route that child to pending_human_correction
      → review_reasons=["geometry_unexpected_split_on_child"]
      → continue with other children/artifacts.
    Run geometry selection cascade (same logic as Step 3).
    If structural disagreement:
      → route that child to pending_human_correction
      → review_reasons=["structural_disagreement_post_rectification"]
      → continue with other children/artifacts.
    Invoke IEP1C: normalize rectified artifact using second-pass selected geometry.
    Log second-pass selection to quality_gate_log (gate_type="geometry_selection_post_rectification").

7.  [Per artifact that went through Steps 6–6.5] Final artifact validation.
    Same logic as Step 5. Log to quality_gate_log (gate_type="artifact_validation_final").
    If valid → that artifact proceeds to Step 8.
    If invalid:
      → route that artifact to pending_human_correction
      → review_reasons=["artifact_validation_failed"]
      → (other children/artifacts continue independently)

[Split handling — if split_required=True]
8.  After both child artifacts have exited their validation paths:
    - parent: status="split" (terminal routing state, set now)
    - left child: sub_page_index=0, own output_image_uri
    - right child: sub_page_index=1, own output_image_uri
    Split must be idempotent: (parent_page_id, side) is unique.
    Each valid child is independently enqueued as a new task in Redis libraryai:page_tasks.
    Children routed to pending_human_correction are NOT enqueued.

8.5 After successful preprocessing artifact resolution:
    Transition page to "ptiff_qa_pending".

    If job.ptiff_qa_mode == "auto_continue":
      - if pipeline_mode == "preprocess":
          update status to "accepted", routing_path="preprocessing_only"
          return
      - if pipeline_mode == "layout":
          update status to "layout_detection"
          continue to Step 9

    If job.ptiff_qa_mode == "manual":
      - stop automated processing for this page at "ptiff_qa_pending"
      - wait for reviewer action via PTIFF QA endpoints
      - return

9.  Layout processing begins only for pages that have exited "ptiff_qa_pending".
    Update page status to "layout_detection".

10. Invoke IEP2A via GPU backend:
    iep2a_result = gpu_backend.invoke(component="iep2a", ...)
    If IEP2A fails or returns unusable output:
      → status="review", review_reasons=["layout_detection_failed"], return.

    Shadow enqueue (best effort):
    Apply all three conditions:
    (a) Sampling: sha256(f"{job_id}:{page_number}") % 100 < shadow_fraction×100
    (b) shadow_mode == True
    (c) Staging candidate exists (from in-memory background-refreshed cache)
    If all three pass: push shadow task to libraryai:shadow_tasks.
    Failure to enqueue must not affect live routing.

11. If IEP2A returned plausible output, invoke IEP2B:
    iep2b_result = gpu_backend.invoke(component="iep2b", ...)
    If IEP2B unavailable: iep2b_result = None (single-model mode).

12. Run layout consensus gate:
    result = evaluate_layout_consensus(iep2a, iep2b_or_none, config)

13. Route after layout consensus:
    If layout_consensus.agreed == False:
      → status="review", review_reasons=["layout_consensus_failed"], return
    If layout_consensus.consensus_confidence < config.layout.min_consensus_confidence:
      → status="review", review_reasons=["layout_consensus_low_confidence"], return

14. Persist artifacts (DB-first write order):
    For each artifact type (preprocessed, layout):
    (1) BEGIN transaction → set artifact_state='pending' → COMMIT
    (2) Write artifact to S3 at deterministic path
    (3) UPDATE: set output_*_uri, set artifact_state='confirmed'
    Update eep_auto_accept_rate Gauge (observability only).

    Update status to "accepted", routing_path="preprocessing_layout".
```

### 8.3 GPU Invocation Policy

EEP must distinguish:

- **cold-start budget:** time for GPU infrastructure activation, container startup, model loading
- **execution budget:** time for actual inference once ready

Cold-start latency must not be treated as inference failure. Supported strategies: direct warm service call, explicit warm-up before first use, async submission to scale-to-zero with polling.

Circuit breaker must distinguish: startup timeout, inference timeout, and true service failure. Cold-start delay alone must not count as a hard failure unless the cold-start budget is exceeded.

### 8.4 Acceptance Policy Configuration

ConfigMap `libraryai-policy`:

```yaml
preprocessing:
  split_confidence_threshold: 0.75        # higher than geometry — split is highest-risk decision
  tta_variance_ceiling: 0.15              # TTA prediction variance above this → model unstable
  page_area_preference_threshold: 0.30    # below this → prefer IEP1B (keypoint)
  structural_agreement_required: true     # non-negotiable for final acceptance — first-pass disagreement triggers rescue flow; second-pass agreement is mandatory
  geometry_sanity_area_min_fraction: 0.15 # page region must be ≥ 15% of image area
  geometry_sanity_area_max_fraction: 0.98
  artifact_validation_threshold: 0.60    # combined soft score threshold for artifact acceptance
  quality_blur_score_max: 0.7            # soft signal: above this → quality poor
  quality_border_score_min: 0.3          # soft signal: below this → quality poor
  threshold_adjustment_requires_audit: true
  threshold_adjustment_requires_slo_validation: true
  # Aspect ratio sanity bounds per material_type (width/height ratio, portrait < 1.0 < landscape).
  # A page failing outside these bounds is rejected by the aspect ratio sanity check.
  aspect_ratio_bounds:
    book:       [0.5, 2.5]   # portrait to slight landscape
    newspaper:  [0.3, 5.0]   # tall columns to wide tabloid
    archival_document:   [0.5, 3.0]

layout:
  min_consensus_confidence: 0.6
  match_iou_threshold: 0.5
  min_match_ratio: 0.7
  max_type_count_diff: 1
  dbscan_eps_fraction: 0.08  # eps = dbscan_eps_fraction × page_width for column-boundary inference

safety:
  max_auto_accept_rate: 0.90   # alerting baseline only — NOT a routing gate

shadow:
  shadow_fraction: 0.10

gpu_startup:
  cold_start_timeout_seconds: 300   # budget for GPU container activation + model loading
  warm_timeout_seconds: 60
  startup_probe_interval_seconds: 5
  enable_gpu_warmup_on_job_create: true
  # Warmup scope depends on pipeline_mode. Only warm services on the likely execution path.
  warmup_services_preprocess: ["iep1a", "iep1b"]
  warmup_services_layout:     ["iep1a", "iep1b", "iep2a", "iep2b"]
  warmup_iep1d: false   # IEP1D is a fallback; warmed lazily when rectification is actually triggered

circuit_breaker:
  failure_threshold: 5
  reset_timeout_seconds: 60
  # Circuit breaker state is stored in-process per worker (not shared across workers).
  # This is the intentional design: each worker maintains independent failure tracking.
  # At typical worker counts (< 10), the operational impact of independent state is acceptable.

timeouts:
  iep1a: 30
  iep1b: 30
  iep1d_rectify: 60
  iep2a: 60
  iep2b: 30

retry:
  iep1a: 1
  iep1b: 1
  iep1d: 0
  iep2a: 2
  iep2b: 1

stability:
  max_task_retries: 3
  max_concurrent_pages: 20
  max_queue_depth: 5000
  rate_limit_tokens_per_minute: 100
  # task_timeout_seconds covers warm-service execution time including all retries.
  # It does NOT include cold-start time. Cold-start is separately bounded by
  # cold_start_timeout_seconds (300s). Worst-case warm execution:
  # IEP1A/IEP1B parallel (30s) + IEP1C (2s) + IEP1D (60s) + second geometry pass (30s) +
  # IEP1C (2s) + IEP2A with 2 retries (180s) + IEP2B (30s) ≈ 334s. 450s provides ~35% margin.
  task_timeout_seconds: 900
  watchdog_check_interval_seconds: 30
  dead_letter_warning_threshold: 100
  artifact_cleanup_grace_hours: 24
```

All thresholds and fallback triggers are runtime-tunable. The geometry selection and artifact validation gates are implemented as policy-driven routing logic, not hardcoded thresholds scattered across service code.

`threshold_adjustment_requires_audit` and `threshold_adjustment_requires_slo_validation` are policy guardrails enforced by the policy update workflow. A threshold change must be rejected unless supporting audit evidence and SLO validation are recorded.

### 8.5 Review Reasons (canonical values)

| Value | Set by | Meaning |
|-------|--------|---------|
| "geometry_failed" | EEP step 2 | one or both geometry models failed to produce valid output |
| "geometry_sanity_failed" | EEP step 3b | both models failed sanity checks |
| "split_confidence_low" | EEP step 3c | both models below split confidence threshold |
| "tta_variance_high" | EEP step 3d | both models exceed TTA variance ceiling |
| "geometry_selection_failed" | EEP step 3f | no candidate survived the selection cascade |
| "normalization_failed" | EEP step 4 | IEP1C normalization failed |
| "rectification_failed" | EEP step 6 | IEP1D rectification failed or is unavailable for a rescue-required artifact |
| "structural_disagreement_post_rectification" | EEP step 6.5 | IEP1A and IEP1B disagree after rectification |
| "geometry_unexpected_split_on_child" | EEP step 6.5 | second geometry pass returned split_required=True on an already-split child artifact |
| "artifact_validation_failed" | EEP step 7 | final artifact validation failed |
| "layout_detection_failed" | EEP step 10 | IEP2A failed |
| "layout_consensus_failed" | EEP step 13 | layout detectors did not agree |
| "layout_consensus_low_confidence" | EEP step 13 | layout consensus confidence below threshold |
| "human_correction_rejected" | human QC endpoint | human reviewer rejected the page |
| "geometry_failed_post_rectification" | EEP step 6.5 | one or both geometry models failed to produce valid output during the second geometry pass after rectification |

### 8.6 Human Correction Workflow

When a page reaches `status="pending_human_correction"`, it enters a human correction flow. This is a recoverable path — the pipeline resumes after manual intervention.

**Trigger conditions:** Any geometry selection failure, structural disagreement, or artifact validation failure routes here. Layout disagreement routes to `status="review"` (terminal, not correctable via this path).

**`GET /v1/correction-queue`:** Returns pages in `pending_human_correction` with preprocessing outputs.

**`POST /v1/jobs/{job_id}/pages/{page_number}/correction`:**

```json
{
  "crop_box": [100, 80, 2400, 3200],
  "deskew_angle": 1.5,
  "split_x": null
}
```

**When `split_x` is null (single-page correction):**

Steps:

1. Generate corrected PTIFF using submitted parameters applied to OTIFF
2. Update `output_image_uri` to corrected PTIFF URI
3. Transition the page from `pending_human_correction` → `ptiff_qa_pending`
4. Route based on `ptiff_qa_mode`:
   - If `ptiff_qa_mode == "manual"`: remain in `ptiff_qa_pending` until reviewer approval
   - If `ptiff_qa_mode == "auto_continue"`:
     - if `pipeline_mode == "preprocess"`: transition `ptiff_qa_pending` → `accepted`, `routing_path="preprocessing_only"`
     - if `pipeline_mode == "layout"`: transition `ptiff_qa_pending` → `layout_detection`

**When `split_x` is non-null (reviewer submits a split):**

Steps:

1. The original parent page remains the retained lineage record for the original OTIFF.
2. Two child sub-pages are created (or reused if they already exist for idempotency):
   - Left child: sub_page_index = 0, cropped from x_min to split_x
   - Right child: sub_page_index = 1, cropped from split_x to x_max
3. Each child receives its own page_number (derived from parent) and sub_page_index.
4. Each child is linked to the parent via `parent_page_id`. Parent-child lineage must remain explicit in `page_lineage`.
5. The parent page remains in pending_human_correction until both child sub-pages reach a worker-terminal state (`accepted`, `pending_human_correction`, `review`, or `failed`). The parent must not transition to split until both children are complete. Once both children reach worker-terminal states, the parent transitions to `split` to close out its role as the lineage anchor for the original OTIFF. This differs intentionally from automated split handling: in the automated pipeline, the parent may transition to `split` immediately after gated child creation because the split decision has already passed automated quality gates; in the human-correction path, the parent remains open until both manually introduced children stabilize.
6. Each child sub-page is independently enqueued for further processing.
7. After correction output is produced for a child, that child transitions to `ptiff_qa_pending`.
8. Routing from `ptiff_qa_pending` depends on `ptiff_qa_mode`:
   - If `ptiff_qa_mode == "manual"`: child remains in `ptiff_qa_pending` until reviewer approval
   - If `ptiff_qa_mode == "auto_continue"`:
     - if `pipeline_mode == "preprocess"`: child transitions to `accepted`
     - if `pipeline_mode == "layout"`: child transitions to `layout_detection`

Corrected artifacts stored at: `s3://{bucket}/jobs/{job_id}/corrected/{page_number}_{sub_page_index}.tiff`

**`POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject`:**

Steps:

1. Transition: `pending_human_correction` → `review`
2. `review_reasons = ["human_correction_rejected"]`
3. `page_lineage.human_corrected = FALSE`

---

## 9. Execution Guarantees

### 9.1 Terminal States

All five states below stop automated worker processing. They are subdivided by purpose:

**Worker-terminal states** (automated processing halts in these states only):

- **accepted** — preprocessing and layout processing completed successfully; all required artifacts finalized.
- **pending_human_correction** — automated processing could not guarantee correctness; requires manual intervention before further processing. Worker-terminal but NOT leaf-final: automated workers must not process this page further, but explicit human action (submit correction or reject) may requeue it, causing a transition to `layout_detection` (or `accepted` in preprocess mode) or to `review`.
- **review** — leaf-final. Automated processing is permanently stopped; page cannot re-enter the normal pipeline. Set by: layout disagreement, unresolvable preprocessing failures, or human rejection via correction-reject.
- **failed** — leaf-final. Unrecoverable infrastructure or system failure; no automated retry will be attempted.
- **split** — routing-terminal state for the parent page of a spread; child sub-pages are created and independently processed.

`ptiff_qa_pending` is not a worker-terminal state and not a leaf-final state.
However, in `ptiff_qa_mode="manual"`, `ptiff_qa_pending` is an automatic-processing stop point: workers must not progress the page further until reviewer action occurs and the PTIFF QA gate is released.
A job with any leaf page in `ptiff_qa_pending` remains `running`, not `done`
It is a non-terminal page state representing the PTIFF-stage QA checkpoint between preprocessing and downstream routing. Pages in `ptiff_qa_pending` do not count as terminal for job completion.

**Leaf-final states** (permanent terminal outcomes — no further transitions possible):
`accepted`, `review`, `failed`

`pending_human_correction` is worker-terminal but not leaf-final. It can transition to `layout_detection`, `accepted`, or `review` via human action.

**Routing-terminal state** (terminal for the parent record only, not a page outcome):
`split` — the parent transitions to `split` to close its role as the lineage anchor for the original OTIFF. For automated pipeline splits (Step 8 in process_page), the parent transitions to `split` immediately upon child creation and enqueueing. For human-correction splits (reviewer submits `split_x`), the parent remains in `pending_human_correction` until both child sub-pages reach worker-terminal states, then transitions to `split`. In both cases the parent lineage record is retained for traceability to the original OTIFF but never counts as an accepted, failed, or corrected page. Job completion is evaluated on leaf pages only; split-parent routing records are excluded.

```python
TERMINAL_PAGE_STATES: frozenset[str] = frozenset({
    "accepted", "pending_human_correction", "review", "failed", "split"
})
```

`ptiff_qa_pending` must not be included in `TERMINAL_PAGE_STATES`.

This constant must be exported from `shared/schemas/eep.py` and imported by all components. Never redefined inline.

Jobs become terminal (`done` or `failed`) only when all leaf pages reach a worker-terminal state. Pages in `pending_human_correction` count toward this check (they stop automated processing), even though they are not leaf-final. Split-parent routing records are excluded from this check once their child pages exist.

### 9.2 No Silent Data Loss

Every uploaded OTIFF must remain visible in the job record and must produce a traceable outcome. A page must end in one of the five terminal states above. No page may disappear from the job record. No page may be silently dropped, skipped, or omitted from lineage due to any automated routing decision. Every content-quality failure, infrastructure failure, or uncertain outcome must resolve to a named terminal state with a recorded reason.

### 9.3 No Incorrect Auto-Accept

The system must never accept a page when preprocessing quality or agreement is insufficient.

- The IEP1 structural agreement gate: first-pass disagreement between IEP1A and IEP1B on page count or split decision does not halt preprocessing; the page proceeds with provisional low-trust geometry through rectification and a mandatory second geometry pass. Final acceptance requires structural agreement in the second pass. If the second pass also produces disagreement, the page routes to `pending_human_correction`. No exceptions. High confidence from either model alone is never sufficient for final acceptance.
- The IEP1 artifact validation gate: high geometry confidence alone is never sufficient if artifact quality is implausible.
- The IEP2 layout gate: single-model output alone (IEP2B unavailable) is never sufficient for acceptance.

### 9.4 No Destructive Overwrites

Raw OTIFF files are immutable. All derived artifacts must be written as new files with lineage tracking.

### 9.5 No Partial Job Completion

A job cannot be marked complete while pages remain in non-terminal states.

### 9.6 No Orphan Artifacts

Every generated artifact must be linked to a `page_lineage` record.

### 9.7 Retry Policy

**Infrastructure failures (must retry automatically):**

- service timeout
- network failure
- temporary container crash
- storage upload failure

Rules: retries bounded, exponential backoff, retry parameters configurable. If retries exhausted: escalate to `failed`.

**Content failures (must NOT retry automatically):**

- structural disagreement between geometry models
- geometry sanity or quality gate failure
- invalid geometry outputs
- layout detection conflicts

Rules: no automatic retry; route to `pending_human_correction` or `review`.

This separation must be explicit in error handling code.

### 9.8 Database as Source of Truth

The database page state is authoritative. After any failure, reconnect, or worker restart:

- reload page state from the database
- verify artifact existence on storage
- resume from latest persisted state

In-memory state must never be trusted after faults. The original request payload must never be replayed blindly.

### 9.9 Artifact Verification Before State Advance

Before advancing any page state, the system must verify the produced artifact. Verification rules are artifact-type-specific and must be applied to every artifact class produced in that step. Applying image-only checks to a layout artifact or vice versa is an error. Rules depend on artifact type:

**Image artifacts (PTIFF/preprocessed page):**

- file exists on storage
- file is readable and decodable as a valid image
- image dimensions match the expected transformation (crop_box dimensions within original image bounds)

**Layout artifacts (JSON):**

- file exists on storage
- JSON parses successfully without error
- schema validation passes (all required fields present, correct types)
- all region bounding-box coordinates are valid within the page's canonical dimensions

State must never advance without successful artifact verification for all artifacts produced in that step.

### 9.10 Split Idempotency

Page splitting must be idempotent. Repeating the split operation must never create duplicate child pages. Children are uniquely identifiable by `(parent_page_id, side)`.

### 9.11 pending_human_correction Semantics

This state is worker-terminal: automated workers must never process or retry pages in this state. Only explicit human action via the correction workflow may resume the page. Human action may transition the page to `layout_detection`, `accepted` (in preprocess-only mode), or `review` (via correction-reject). Until such action occurs, the page remains in this state indefinitely. `acceptance_decision` remains NULL while the page is in this state.

### 9.12 failed Semantics

`failed` means the page cannot proceed due to an unrecoverable infrastructure or system failure. No automated retry will be attempted. Manual intervention outside the normal correction workflow is required.

`failed` must only be used for:

- corrupted or unreadable OTIFF input
- persistent storage failure
- retry budget exhausted after infrastructure faults
- unrecoverable service error (not a content-quality failure)

`failed` must never be used for content-quality failures. Content-quality failures (structural disagreement, geometry sanity failure, artifact validation failure, layout disagreement) must route to `pending_human_correction` or `review`, not `failed`.

Model or algorithmic failure (e.g., geometry detection failure, normalization failure,
rectification failure, or layout disagreement) MUST NOT result in `failed` status.

If the OTIFF is valid and can be displayed, the page MUST be routed to
`pending_human_correction` or `review`, not `failed`.

### 9.13 Fault Tolerance

- Any worker can resume processing any page.
- All tasks are idempotent.
- Crashes cannot corrupt page state.
- Queue recovery does not duplicate work.
- All processing must be restart-safe.

---

## 10. Training Data and Model Behavior

### 10.1 Preprocessing Model Training (IEP1A and IEP1B)

**Training data:** OTIFF/PTIFF pairs from AUB Library collections. Labels derived from PTIFF (human-processed pages):

- Page masks (for IEP1A segmentation training) and quadrilateral corner keypoints (for IEP1B pose training)
- Page count / split labels
- Quality / review labels
- Paired corrected artifacts as evaluation support

Explicit geometry supervision is preferred over pure image-to-image pairs because:

- explicit geometry is more auditable
- deterministic normalization is easier to trust
- final artifacts remain reproducible from stored geometry
- failure analysis is much easier than with a black-box image-to-image model

**IEP1A-specific training:** Instance segmentation annotations — page masks with instance IDs. Training uses YOLOv8-seg with appropriate augmentations. Geometry (quadrilateral corners, page area) is derived at inference time from predicted mask contours.

**IEP1B-specific training:** Keypoint annotations — 4 corner coordinates per page instance. Training uses YOLOv8-pose. Geometry is read directly from predicted keypoint coordinates.

Both models are trained on the same underlying dataset with different annotation formats derived from the same source labels (page regions).

**Dataset manifest** (`training/preprocessing/dataset_manifest.json`):

```json
{
  "dataset_version": "aub_v1",
  "dataset_pages": 2700,
  "dataset_checksum": "sha256:...",
  "dataset_collections": [
    "aub_aco003575",
    "mic_06",
    "na121_al-moqatam",
    "na246_sada-nahda"
  ]
}
```

`dataset_version` and `dataset_checksum` MUST be logged to MLflow in every training run. A run that does not log these params is invalid and must not promote a model.

### 10.2 Evaluation Framework

The system has access to four collections:

| Collection | material_type | Capture modality | Approximate size |
|-----------|--------------|-----------------|-----------------|
| aub_aco003575 | book | book scanner | ~125 files |
| mic_06 | archival_document | microfilm | ~2000 files |
| na121_al-moqatam | newspaper | microfilm | ~300 files |
| na246_sada-nahda | newspaper | microfilm | ~275 files |

Two distinct evaluation types are required:

#### Development Evaluation — Stratified Within-Collection Splits

Within each collection, split at the file level: 70% train / 15% validation / 15% test. Split applied independently per collection. Resulting sets each contain examples from all four material types.

This measures: does the model learn the task across all material types it was trained on?

Use this for architecture decisions, threshold calibration, and comparing IEP1A vs IEP1B model families.

#### Generalization Evaluation — Leave-One-Collection-Out

Run four experiments, each holding out one entire collection from training:

| Experiment | Train on | Test on |
|-----------|----------|---------|
| 1 | mic_06 + na121 + na246 | aub_aco003575 |
| 2 | aub_aco003575 + na121 + na246 | mic_06 |
| 3 | aub_aco003575 + mic_06 + na246 | na121_al-moqatam |
| 4 | aub_aco003575 + mic_06 + na121 | na246_sada-nahda |

This measures: how badly does performance drop when the model encounters a collection it has never seen?

The gap between within-collection test accuracy and leave-one-out accuracy is the **generalization gap**. This determines how much to trust the system when a new collection arrives in production.

**Important:** na121 and na246 are both microfilm newspapers but from different years and sources and must be treated as separate collections in leave-one-out experiments. na121 had no splitting while na246 required it. Treating them separately gives two separate data points: if leaving out na121 causes a small accuracy drop but leaving out na246 causes a large one, split detection is the fragile capability.

Do not use leave-one-out test sets for threshold setting. Those are reserved for measuring generalization only.

#### Class Imbalance Handling

mic_06 has ~2000 files vs ~125 for books. Two approaches:

1. **Stratified batch sampling (preferred):** During training, sample batches so each collection contributes equally. Simplest fix and usually sufficient.
2. **Per-collection sample weighting:** Assign higher loss weight to underrepresented collections.

Do not oversample by duplicating minority examples — the book collection is small enough that heavy duplication causes overfitting.

### 10.3 Required Evaluation Metrics

Do not report a single aggregate accuracy number. Report separately for each collection type:

| Metric | Description |
|--------|-------------|
| Geometry accuracy | IoU between predicted page region and ground truth region derived from OTIFF→PTIFF pairs |
| Split accuracy | Precision and recall on `split_required` prediction |
| Structural agreement rate | Fraction of pages where IEP1A and IEP1B agree on page_count and split_required |
| Review rate | Fraction of cases routed to human review |
| Bad auto-accept rate | Fraction of auto-accepted cases where output diverges from PTIFF beyond the accepted similarity threshold, with divergence operationalized using IoU < 0.90 unless a later auditor-approved threshold revision is adopted |

The **bad auto-accept rate** is the most important metric for library trust. A model that sends 40% of cases to human review but never auto-accepts a bad output is more trustworthy than one that auto-accepts 95% but gets 5% wrong.

**IoU threshold** for determining divergence from PTIFF is fixed at 0.90. This value is conservative and does not rely on downstream OCR correlation. It may be adjusted only through auditor-validated sampling during shadow mode, subject to SLO constraints on bad auto-accept rate.

The **structural agreement rate** measures how often the dual-model safety mechanism is exercised. A low structural agreement rate means many pages go to human review even if both models are individually accurate — this indicates the models are not correlated enough in their correct predictions, or that the task is genuinely ambiguous.

### 10.4 Practical Build Sequence

1. Train IEP1A (YOLOv8-seg) and IEP1B (YOLOv8-pose) on AUB data within Ultralytics ecosystem.
2. Implement IEP1C deterministic normalization as shared module.
3. Implement geometry selection logic (structural agreement, sanity checks, TTA variance, page area preference, confidence selection).
4. Implement artifact validation (hard requirements + soft signal scoring).
5. Evaluate IEP1A + IEP1B + IEP1C + geometry selection + artifact validation on held-out validation set. Establish baseline per-collection accuracy, structural agreement rate, review rate, and bad auto-accept rate.
6. Integrate UVDoc in IEP1D only for pages where artifact validation fails.
7. Tune artifact validation thresholds against review-rate and error-rate targets.

### 10.5 Production Monitoring and Active Learning

Once deployed:

- Measure per-collection-type accuracy, structural agreement rate, and review rate on real production traffic.
- Treat every new collection that arrives as a new leave-one-out test.
- When performance drops on a new collection, route those cases to active learning and prioritize them for labeling.

### 10.6 Layout Detection Training (IEP2A and IEP2B)

Both models use pretrained weights directly for initial deployment (PubLayNet for Detectron2, document-layout pretrained weights for DocLayout-YOLO with a DocStructBench-aligned class vocabulary). Fine-tuning is deferred unless post-deployment evaluation shows unacceptable accuracy on AUB scans.

**IEP2A promotion gates:** mAP ≥ current_production − 0.02, p95 latency ≤ 3s, zero critical golden dataset failures, all per-class AP > 0.5.

**IEP2B promotion gates:** mAP ≥ current_production − 0.03 (looser — secondary detector), p95 latency ≤ 100ms, zero regression in canonical class mapping behavior on the golden dataset.

### 10.7 Calibration Sequence

**Phase 1:** Baseline within-collection evaluation. Train IEP1A and IEP1B. Measure per-collection accuracy and structural agreement rate.

**Phase 2:** Generalization evaluation. Run four leave-one-out experiments. Measure the generalization gap per collection.

**Phase 3:** Calibration. Use within-collection validation set for temperature scaling. Use within-collection test set to set gate thresholds (split confidence, TTA variance ceiling, artifact validation threshold).

**Phase 4:** Production monitoring (see 10.5 above).

### 10.8 Honest Data Volume Assessment

With ~125 book files, ~300 na121 files, and ~275 na246 files, the non-microfilm-document data is small. The leave-one-out results for the book collection will have high variance — 125 test examples is not enough to be confident in aggregate numbers, and stratifying by operation type (split vs no split, heavy warp vs clean) will produce even smaller subcategories.

The book collection leave-one-out result should be treated as a qualitative indicator — does the model completely fail on books, or does it degrade gracefully — rather than as a precise accuracy number.

The mic_06 collection is large enough that leave-one-out results on it will be reliable. This is the strongest generalization test.

---

## 11. Frontend and UI

### 11.1 Role Definitions

#### Regular User

A library staff member or job submitter.

**Capabilities:**

- Submit new processing jobs
- View status and outputs of their own jobs
- Access correction queue scoped to their own jobs
- Submit corrections or reject pages for their own jobs

**Restricted from:** other users' jobs, admin dashboard, shadow evaluation, retraining management, lineage inspection, policy configuration, user management.

#### Admin

System overseer or MLOps engineer with full access.

**Additional capabilities:**

- View and manage all jobs across all users
- Access global correction queue
- Inspect full page lineage
- Shadow model evaluation, promotion, force promotion, rollback
- Retraining trigger history and monitoring
- System policy read and edit
- User account management
- Operational dashboard with system-wide KPIs

### 11.2 Permission Matrix

| Screen / Action | Regular User | Admin |
|----------------|-------------|-------|
| Login | Yes | Yes |
| Submit new job | Yes | Yes |
| View own job list | Yes | Yes |
| View own job detail and outputs | Yes | Yes |
| Download output artifact | Yes (own jobs) | Yes (all jobs) |
| View correction queue (own jobs) | Yes | Yes |
| View correction queue (all jobs) | No | Yes |
| Open correction workspace (own job page) | Yes | Yes |
| Open correction workspace (other user's page) | No | Yes |
| Submit correction | Yes (own jobs) | Yes (all) |
| Reject page | Yes (own jobs) | Yes (all) |
| Admin dashboard | No | Yes |
| Global jobs list | No | Yes |
| Lineage page | No | Yes |
| Shadow models page | No | Yes |
| Retraining page | No | Yes |
| Settings / policy | No | Yes |
| User management | No | Yes |

### 11.3 Screen Specifications

#### Admin Dashboard

Real-time pipeline snapshot: throughput, error rates, correction backlog, worker activity, pipeline-stage health.

**Widgets:** Throughput (pages/hour, 1-hour rolling), Auto Accept Rate, Structural Agreement Rate (fraction of pages where IEP1A and IEP1B agree), Pending Corrections count, Active Jobs count, Shadow Evaluations count, Pipeline Health bars per stage, Quick Links, Recent Activity.

**Backend:** `GET /v1/admin/dashboard-summary`, `GET /v1/admin/service-health`.

#### Admin Jobs Page

Searchable, filterable, paginated list of all jobs. Columns: Job ID, Submitted, Submitted by, Pipeline mode, Shadow mode, Pages, Status summary, Actions.

**Backend:** `GET /v1/jobs` (with filter/pagination params).

#### Correction Queue

Pages in `pending_human_correction`. Admins see all; regular users see own-job pages only. Filterable by review reason, material type, job ID. Sortable by waiting duration.

**Backend:** `GET /v1/correction-queue` (with filter/pagination params).

#### PTIFF QA Review Screen

A job-level PTIFF QA screen used after preprocessing and before downstream progression.

**Capabilities:**
- show all pages currently in `ptiff_qa_pending`
- bulk approval of all pages currently in `ptiff_qa_pending`
- individual page approval
- route individual pages into correction workspace for edit/review
- display current PTIFF QA mode (`manual` or `auto_continue`)

**Behavior:**
- available only for jobs with `ptiff_qa_mode="manual"`
- in `ptiff_qa_mode="manual"`, page approval records reviewer intent only and does not immediately change page state
- once the PTIFF QA gate is fully satisfied, approved pages are released in a controlled batch:
  - to `accepted` for `pipeline_mode="preprocess"`
  - to `layout_detection` for `pipeline_mode="layout"`
- approve-all affects only pages currently in `ptiff_qa_pending`

**Backend:**
- `GET /v1/jobs/{job_id}/ptiff-qa`
- `POST /v1/jobs/{job_id}/ptiff-qa/approve-all`
- `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve`
- `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit`

#### Correction Workspace

Single-page interface for inspecting a page routed to correction, comparing geometry outputs, comparing available preprocessing artifacts, applying corrections, and submitting or rejecting.

**Controls:** image viewer (zoomable, pannable), crop box overlay with draggable handles, deskew angle slider/input, split_x input with visual line, reviewer notes free-text field.

**Actions:** Submit Correction → transitions `pending_human_correction` → `ptiff_qa_pending`. From there:
- if `ptiff_qa_mode == "manual"`, page remains in PTIFF QA until reviewer approval
- if `ptiff_qa_mode == "auto_continue"`:
  - `pipeline_mode == "preprocess"` → `accepted`
  - `pipeline_mode == "layout"` → `layout_detection`

Reject Page → transitions to `review`.
 Layout-related controls (deskew, crop) are displayed for both modes; the split_x control is available for both modes. Layout detection fields are suppressed for preprocess-only jobs.

**Backend:** `GET /v1/correction-queue/{job_id}/{page_number}`, `POST .../correction`, `POST .../correction-reject`.

`GET /v1/correction-queue/{job_id}/{page_number}` response:

```json
{
  "job_id": "string",
  "page_number": 1,
  "sub_page_index": null,
  "material_type": "book",
  "review_reasons": ["structural_disagreement_post_rectification"],
  "original_otiff_uri": "s3://...",
  "best_output_uri": "s3://...",
  "branch_outputs": {
    "iep1a_geometry": { "page_count": 2, "split_required": true, "geometry_confidence": 0.87 },
    "iep1b_geometry": { "page_count": 1, "split_required": false, "geometry_confidence": 0.91 },
    "iep1c_normalized": "s3://...",
    "iep1d_rectified": null
  },
  "current_crop_box": [100, 80, 2400, 3200],
  "current_deskew_angle": 1.3,
  "current_split_x": null
}
```

#### Lineage Page

Full audit trail: every service invocation, geometry selection result, artifact validation result, artifact URI, human correction event. Admin only.

**Backend:** `GET /v1/lineage/{job_id}/{page_number}` (admin only).

#### Shadow Models Page

Monitor shadow evaluation pipeline, inspect gate results, manage promotion/rollback lifecycle.

**Gate results:** Quality (mean confidence delta ≥ −0.02), Latency (p95 ≤ 1.25× production), Reliability (error rate < 5%), Golden dataset (zero critical failures).

**Actions:** Refresh stats, Run gate evaluation, Promote with gate check, Force promote (with confirmation), Manual rollback.

#### Retraining Page

Trigger history, active/queued jobs, completed jobs, cooldown status per pipeline type.

#### Regular User Portal

Subtree: Job Submission, My Jobs, Job Detail/Output screen. No admin screens rendered or linked.

**Job Detail large-batch display requirements:** Display queued/running progress indicator while non-terminal pages exist. Update page statuses incrementally as pages complete. Never display "processing complete" immediately after upload. Show time elapsed since submission. Poll `GET /v1/jobs/{job_id}` on 5–10 second interval while non-terminal pages exist.

### 11.4 New Backend Endpoints Required

**`GET /v1/jobs`** — paginated job list. Auth: `require_user` (scoped). Params: `search`, `status`, `pipeline_mode`, `created_by` (admin only), `from_date`, `to_date`, `page`, `page_size`. Response: pagination envelope with job rows.

**`GET /v1/admin/dashboard-summary`** — aggregate KPIs. Auth: `require_admin`. Fields: `throughput_pages_per_hour`, `auto_accept_rate`, `structural_agreement_rate`, `pending_corrections_count`, `active_jobs_count`, `active_workers_count`, `shadow_evaluations_count`.

**`GET /v1/admin/service-health`** — per-pipeline-stage success rates. Auth: `require_admin`. Fields: `preprocessing_success_rate`, `rectification_success_rate`, `layout_success_rate`, `human_review_throughput_rate`, `structural_agreement_rate`, `window_hours`.

**`GET /v1/correction-queue/{job_id}/{page_number}`** — workspace detail. Auth: `require_user` (scoped). Optional query param `sub_page_index` (required when multiple sub-pages pending, else 422).

---

## 12. Shared Infrastructure

### 12.1 Shared Schemas (shared/schemas/)

**ucf.py:** `Dimensions`, `BoundingBox` (validators: x_min < x_max, y_min < y_max), `TransformRecord` (validators: crop_box within original dimensions), `ProcessingContext` (canonical_dimensions == transform.post_preprocessing_dimensions), `validate_bbox_in_context()`.

**preprocessing.py:** `DeskewResult`, `CropResult`, `SplitResult`, `QualityMetrics`, `PreprocessBranchResponse`, `PreprocessError`. (`PreprocessRequest` has been removed — it is not part of the pipeline; the external entry point is `JobCreateRequest` and internal normalization uses `NormalizeRequest`.)

**geometry.py:** `GeometryRequest`, `PageRegion`, `GeometryResponse`.

**normalization.py:** `NormalizeRequest` (internal schema used by IEP1C shared module).

**iep1d.py:** `RectifyRequest`, `RectifyResponse`.

**layout.py:** `RegionType`, `Region`, `LayoutConfSummary` (mean_conf: float ge=0 le=1; low_conf_frac: float ge=0 le=1), `ColumnStructure` (column_count: int ge=1; column_boundaries: list[float] sorted ascending, length == column_count−1), `LayoutDetectRequest`, `LayoutDetectResponse`.

**eep.py:** `PageInput`, `JobCreateRequest`, `JobCreateResponse`, `QualitySummary`, `PageStatus`, `JobStatusSummary`, `JobStatusResponse`, `TERMINAL_PAGE_STATES`. `JobCreateRequest` must include `ptiff_qa_mode`. `PageStatus` must include `ptiff_qa_pending`, and `ptiff_qa_pending` must not be part of `TERMINAL_PAGE_STATES`.

### 12.2 Storage (shared/io/storage.py)

**Protocol:** `StorageBackend` with `get_bytes(uri)` and `put_bytes(uri, data)`.

**Implementations:** `LocalFileBackend` (`file://` URIs), `S3Backend` (`s3://` URIs, custom endpoint via env vars).

**Function** `get_backend(uri)` selects backend from URI scheme.

**Config env vars:** `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET_NAME`.

### 12.3 Metrics (shared/metrics.py)

**Core request metrics per service:** latency (Histogram), errors (Counter), requests (Counter).

**EEP metrics:** `eep_geometry_selection_route` (Counter, labels: route=[accepted/review/structural_disagreement/sanity_failed/split_confidence_low/tta_variance_high]), `eep_artifact_validation_route` (Counter, labels: route=[valid/invalid/rectification_triggered]), `eep_layout_consensus_confidence` (Histogram), `eep_consensus_route` (Counter), `eep_auto_accept_rate` (Gauge — observability only, MUST NOT influence routing), `eep_structural_agreement_rate` (Gauge — observability only), `eep_requests_total` (Counter).

**Per-service domain metrics:**

- **IEP1A:** `iep1a_geometry_confidence` (Histogram), `iep1a_page_count` (Histogram), `iep1a_split_detection_rate` (Counter), `iep1a_tta_structural_agreement_rate` (Histogram), `iep1a_tta_prediction_variance` (Histogram), `iep1a_gpu_inference_seconds` (Histogram)
- **IEP1B:** `iep1b_geometry_confidence` (Histogram), `iep1b_page_count` (Histogram), `iep1b_split_detection_rate` (Counter), `iep1b_tta_structural_agreement_rate` (Histogram), `iep1b_tta_prediction_variance` (Histogram), `iep1b_gpu_inference_seconds` (Histogram)
- **IEP1C:** `iep1c_blur_score` (Histogram), `iep1c_border_score` (Histogram), `iep1c_skew_residual` (Histogram), `iep1c_foreground_coverage` (Histogram)
- **IEP1D:** `iep1d_rectification_confidence` (Histogram), `iep1d_rectification_triggered` (Counter), `iep1d_gpu_inference_seconds` (Histogram)
- **IEP2A:** `iep2a_region_confidence` (Histogram), `iep2a_mean_page_confidence` (Histogram), `iep2a_regions_per_page` (Histogram), `iep2a_gpu_inference_seconds` (Histogram)
- **IEP2B:** `iep2b_region_confidence` (Histogram), `iep2b_mean_page_confidence` (Histogram), `iep2b_regions_per_page` (Histogram), `iep2b_gpu_inference_seconds` (Histogram)
- **Shadow Worker:** `shadow_tasks_enqueued` (Counter), `shadow_tasks_processed` (Counter), `shadow_tasks_failed` (Counter), `shadow_conf_delta` (Histogram)

### 12.4 GPU Backend (shared/gpu/backend.py)

```python
class GPUBackend(Protocol):
    async def invoke(
        self,
        component: str,
        payload: Dict[str, Any],
        cold_start_timeout_seconds: int,
        execution_timeout_seconds: int,
    ) -> Dict[str, Any]: ...
```

Implementations target: Runpod Serverless, Knative HTTP services, KEDA-backed workers.

### 12.5 Health and Logging

**shared/health.py:** `GET /health` → 200 (liveness), `GET /ready` → 200/503 (readiness).

**shared/logging_config.py:** structlog with JSON rendering and ISO timestamps. Logger bound with `correlation_id`, `job_id`, `page_number`.

**shared/middleware.py:** Extracts `X-Correlation-ID` (generates UUID4 if missing) and `X-Job-ID`, binds to structlog context, adds correlation ID to response headers.

---

## 13. Database Schema

PostgreSQL. Exact table definitions.

### jobs

```sql
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL,
    material_type TEXT NOT NULL CHECK (material_type IN ('book', 'newspaper',  'archival_document')),
    pipeline_mode TEXT NOT NULL CHECK (pipeline_mode IN ('preprocess','layout')) DEFAULT 'layout',
    ptiff_qa_mode TEXT NOT NULL CHECK (ptiff_qa_mode IN ('manual','auto_continue')) DEFAULT 'manual',
    policy_version TEXT NOT NULL,
    -- status derivation (exact, deterministic):
    --   queued:  all leaf pages in 'queued' state (no page has started processing)
    --   running: at least one leaf page is in a non-worker-terminal state
    --            ('queued', 'preprocessing', 'rectification', 'ptiff_qa_pending', 'layout_detection')
    --   done:    all leaf pages are worker-terminal AND at least one is not 'failed'
    --   failed:  all leaf pages are worker-terminal AND all are 'failed'
    -- Leaf pages = all job_pages where status != 'split' and no children exist,
    --              PLUS all sub-pages (sub_page_index IS NOT NULL).
    -- Split-parent records (status='split') are excluded from all counts.
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    -- page_count: number of pages submitted in JobCreateRequest (immutable after creation).
    page_count INTEGER NOT NULL,
    -- Counter semantics: leaf-page outcomes only. Split parents never counted.
    -- Split children (sub_page_index IS NOT NULL) count as leaf pages.
    -- Reconciliation when all leaf pages are terminal:
    --   accepted_count + review_count + failed_count + pending_human_correction_count = total leaf pages
    accepted_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    pending_human_correction_count INTEGER NOT NULL DEFAULT 0,
    shadow_mode BOOLEAN NOT NULL DEFAULT FALSE,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
```

### job_pages

```sql
CREATE TABLE job_pages (
    page_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    page_number INTEGER NOT NULL,
    sub_page_index INTEGER,
    status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'preprocessing', 'rectification',
                      'ptiff_qa_pending', 'layout_detection',
                      'pending_human_correction',
                      'accepted', 'review', 'failed', 'split')),
    routing_path TEXT,
    escalated_to_gpu BOOLEAN NOT NULL DEFAULT FALSE,
    input_image_uri TEXT NOT NULL,
    output_image_uri TEXT,
    quality_summary JSONB,
    -- layout_consensus_result: stores LayoutConsensusResult JSONB from IEP2 consensus gate.
    -- NULL for pages that never reach layout detection or in preprocess-only mode.
    -- Not related to preprocessing (preprocessing uses structural agreement, not consensus).
    layout_consensus_result JSONB,
    -- acceptance_decision is set only when the page reaches a leaf-final state
    -- (accepted, review, or failed). It remains NULL while the page is in
    -- pending_human_correction, as that state is not yet a resolved outcome.
    acceptance_decision TEXT CHECK (acceptance_decision IN ('accepted', 'review', 'failed')),
    review_reasons JSONB,
    processing_time_ms REAL,
    status_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    output_layout_uri TEXT,
    UNIQUE (job_id, page_number, sub_page_index)
);
CREATE INDEX idx_job_pages_job_id ON job_pages(job_id);
CREATE INDEX idx_job_pages_status_updated ON job_pages(status_updated_at) WHERE status_updated_at IS NOT NULL;
```

### page_lineage

```sql
CREATE TABLE page_lineage (
    lineage_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    sub_page_index INTEGER,
    correlation_id TEXT NOT NULL,
    input_image_uri TEXT NOT NULL,
    input_image_hash TEXT,
    otiff_uri TEXT NOT NULL,
    reference_ptiff_uri TEXT,
    -- ptiff_ssim: offline-only metric. Computed post-hoc when reference_ptiff_uri is available
    -- by comparing the produced PTIFF against the reference. NOT computed during live pipeline
    -- processing and MUST NOT influence any routing decisions. NULL during normal operation.
    ptiff_ssim FLOAT,
    iep1a_used BOOLEAN NOT NULL DEFAULT FALSE,
    iep1b_used BOOLEAN NOT NULL DEFAULT FALSE,
    selected_geometry_model TEXT,  -- 'iep1a' or 'iep1b'
    structural_agreement BOOLEAN,
    iep1d_used BOOLEAN NOT NULL DEFAULT FALSE,
    material_type TEXT NOT NULL,
    routing_path TEXT,
    policy_version TEXT NOT NULL,
    acceptance_decision TEXT,
    acceptance_reason TEXT,
    gate_results JSONB,
    total_processing_ms REAL,
    shadow_eval_id TEXT,
    cleanup_retry_count INT NOT NULL DEFAULT 0,
    -- Artifact state defaults to 'pending'. The DB-first write protocol sets 'pending' before
    -- writing to S3, then 'confirmed' after a successful write. 'recovery_failed' is set when
    -- cleanup_retry_count >= 3 and age exceeds 3× grace period. A newly created lineage record
    -- has not yet had any artifact written; 'pending' correctly reflects this state.
    preprocessed_artifact_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (preprocessed_artifact_state IN ('pending', 'confirmed', 'recovery_failed')),
    layout_artifact_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (layout_artifact_state IN ('pending', 'confirmed', 'recovery_failed')),
    output_image_uri TEXT,
    parent_page_id TEXT,
    split_source BOOLEAN NOT NULL DEFAULT FALSE,
    human_corrected BOOLEAN NOT NULL DEFAULT FALSE,
    human_correction_timestamp TIMESTAMPTZ,
    human_correction_fields JSONB,
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    reviewer_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    UNIQUE (job_id, page_number, sub_page_index)
);
CREATE INDEX idx_lineage_job ON page_lineage(job_id, acceptance_decision, created_at);
```

### service_invocations

```sql
CREATE TABLE service_invocations (
    id SERIAL PRIMARY KEY,
    lineage_id TEXT NOT NULL REFERENCES page_lineage(lineage_id),
    service_name TEXT NOT NULL,
    service_version TEXT,
    model_version TEXT,
    model_source TEXT,
    invoked_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    processing_time_ms REAL,
    status TEXT NOT NULL CHECK (status IN ('success', 'error', 'timeout', 'skipped')),
    error_message TEXT,
    metrics JSONB,
    config_snapshot JSONB
);
CREATE INDEX idx_invocations_lineage ON service_invocations(lineage_id, service_name);
```

### quality_gate_log

```sql
CREATE TABLE quality_gate_log (
    gate_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    gate_type TEXT NOT NULL CHECK (gate_type IN (
        'geometry_selection',
        'geometry_selection_post_rectification',
        'artifact_validation',
        'artifact_validation_final',
        'layout'
    )),
    iep1a_geometry JSONB,           -- IEP1A GeometryResponse summary
    iep1b_geometry JSONB,           -- IEP1B GeometryResponse summary
    structural_agreement BOOLEAN,
    selected_model TEXT,            -- 'iep1a' or 'iep1b' or NULL if routed to review
    selection_reason TEXT,          -- which filter/preference determined selection
    sanity_check_results JSONB,    -- per-model sanity check pass/fail
    split_confidence JSONB,        -- per-model split confidence when applicable
    tta_variance JSONB,            -- per-model TTA variance
    artifact_validation_score FLOAT, -- combined soft score when applicable
    -- route_decision maps to the actual page status transition triggered by this gate:
    --   accepted            → page validated, proceeds downstream
    --   rectification       → artifact invalid, IEP1D fallback triggered
    --   pending_human_correction → page routed to human review (geometry/preprocessing failures)
    --   review              → page routed to permanent review (layout consensus failures)
    route_decision TEXT NOT NULL CHECK (route_decision IN (
        'accepted', 'rectification', 'pending_human_correction', 'review'
    )),
    review_reason TEXT,
    processing_time_ms REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_quality_gate_job ON quality_gate_log(job_id, page_number);
CREATE INDEX idx_quality_gate_route ON quality_gate_log(route_decision);
CREATE INDEX idx_quality_gate_agreement ON quality_gate_log(structural_agreement);
```

### shadow_results

```sql
-- shadow_id, candidate_tag, timestamp, production_latency_ms, candidate_latency_ms,
-- candidate_error, production_quality, candidate_quality, quality_delta,
-- structural_match, comparison_detail, shadow_retry_count, created_at
```

### model_versions

```sql
-- model_id, service_name, version_tag, mlflow_run_id, dataset_version,
-- stage (experimental/staging/shadow/production/archived), promoted_at, notes
```

### policy_versions

```sql
-- version, config_yaml, applied_at, applied_by, justification
```

### task_retry_states

```sql
-- task_id, page_id, job_id, retry_count, last_error, final_error,
-- last_attempted_at, created_at
```

### retraining_triggers

```sql
-- trigger_id, trigger_type, metric_name, metric_value, threshold_value,
-- persistence_hours, fired_at, cooldown_until, status, retraining_job_id,
-- mlflow_run_id, resolved_at, notes
```

### retraining_jobs

```sql
-- job_id, trigger_id, pipeline_type (layout_detection/doclayout_yolo/rectification/preprocessing),
-- status, mlflow_experiment, mlflow_run_id, dataset_version, started_at, completed_at,
-- result_model_version, result_mAP, promotion_decision, error_message, created_at
```

### slo_audit_samples

```sql
-- audit_id, job_id, page_number, audit_week, auditor_id, auto_accepted,
-- auditor_would_flag, disagreement_reason, audited_at
```

### users

```sql
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## 14. EEP API Endpoints

### Core Job Endpoints

**`POST /v1/jobs`**

- Request: `JobCreateRequest` (collection_id, material_type, pages[1-1000], pipeline_mode, ptiff_qa_mode, policy_version, shadow_mode)
- Generate UUID4 job_id, create job record, enqueue pages to Redis `libraryai:page_tasks`
- Response 201: `JobCreateResponse`
- Response 422: Pydantic validation error

**`GET /v1/jobs/{job_id}`**

- Response 200: `JobStatusResponse`
- Response 404: Job not found
- Job-level status derivation (exact, deterministic — matches jobs.status definition):
  - `queued`: all leaf pages in `queued` state; no processing has started.
- `running`: at least one leaf page is in a non-worker-terminal state (`queued`, `preprocessing`, `rectification`, `ptiff_qa_pending`, `layout_detection`).
  - `done`: all leaf pages are in worker-terminal states (`accepted`, `pending_human_correction`, `review`, `failed`) AND at least one is not `failed`. Mixed outcomes (some accepted, some in review or correction) resolve to `done`.
  - `failed`: all leaf pages are in worker-terminal states AND all are `failed`.
  - Leaf pages: all `job_pages` records where `status != 'split'` AND either `sub_page_index IS NOT NULL` OR no child records exist with this record's `page_id` as `parent_page_id`. Split-parent records (`status='split'`) are excluded.

### Auth Endpoints

**`POST /v1/auth/token`**

- Accepts username/password, returns JWT token
- No auth required

### Lineage

**`GET /v1/lineage/{job_id}/{page_number}`**

- Returns full page_lineage with joined service_invocations and quality_gate_log
- Admin only (`require_admin`)

### Correction

**`GET /v1/correction-queue`** — list pending pages (auth: `require_user`, server-scoped)

**`GET /v1/correction-queue/{job_id}/{page_number}`** — workspace detail (auth: `require_user`, scoped)

**`POST /v1/jobs/{job_id}/pages/{page_number}/correction`** — submit human correction

**`POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject`** — reject page
### PTIFF QA

**`GET /v1/jobs/{job_id}/ptiff-qa`**
- Returns PTIFF QA status for the job, including per-page QA status
- Auth: `require_user` (scoped), admin unrestricted

**`POST /v1/jobs/{job_id}/ptiff-qa/approve-all`**
- Applies only to pages currently in `ptiff_qa_pending` for this job
- In `ptiff_qa_mode="manual"`, records approval intent only and must not immediately transition page state out of `ptiff_qa_pending`
- Must not alter pages already approved, already terminal, or currently in correction
- If this approval satisfies the job-level PTIFF QA gate, a controlled gate-release step transitions approved pages to:
  - `accepted` for preprocess jobs
  - `layout_detection` for layout jobs
- Auth: `require_user` (scoped), admin unrestricted

**`POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve`**
- Applies only to a single page currently in `ptiff_qa_pending`
- In `ptiff_qa_mode="manual"`, this records approval intent only; the page remains in `ptiff_qa_pending`
- Release to `accepted` or `layout_detection` occurs only at job-level gate release
- Auth: `require_user` (scoped), admin unrestricted

**`POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit`**
- Routes a page from `ptiff_qa_pending` back into correction workflow
- After correction is applied, the page returns to `ptiff_qa_pending`
- Auth: `require_user` (scoped), admin unrestricted

### Shadow / MLOps

**`GET /v1/shadow/stats?candidate_tag={tag}`** — shadow stats for candidate

**`POST /v1/shadow/evaluate`** — evaluate 4 promotion gates

**`POST /v1/shadow/promote`** — promote candidate (with or without force)

**`POST /v1/shadow/rollback`** — rollback (automated or manual)

**`POST /v1/retraining/webhook`** — Alertmanager webhook

**`GET /v1/retraining/status`** — retraining job status

### Policy

**`GET /v1/policy`** — read current policy (admin only)

**`PATCH /v1/policy`** — update policy (admin only)

### User Management

**`POST /v1/users`**, **`GET /v1/users`**, **`PATCH /v1/users/{user_id}/deactivate`** — admin only

### Rate Limiting

Sliding window token bucket in Redis. Key: `libraryai:rate_limit:{caller_id}`. `caller_id` is derived from the verified JWT token: `sub` claim (or `user_id` if `sub` is absent) for authenticated requests; client IP as fallback for unauthenticated requests (applies only to `POST /v1/auth/token`). Return HTTP 429 if exhausted.

Request size limit: 50 MB.

---

## 15. Artifact Storage

### 15.1 Artifact Path Conventions

```text
input OTIFF:
  s3://{bucket}/jobs/{job_id}/input/otiff/{page_number}.tiff

reference PTIFF (offline evaluation only):
  s3://{bucket}/jobs/{job_id}/input/ptiff_reference/{page_number}.tiff

preprocessed PTIFF (IEP1C output):
  s3://{bucket}/jobs/{job_id}/preprocessed/{page_number}.tiff

rectified page (IEP1D output):
  s3://{bucket}/jobs/{job_id}/rectified/{page_number}.tiff

split sub-page:
  s3://{bucket}/jobs/{job_id}/split/{page_number}_{sub_page_index}.tiff

corrected page (human correction, single-page):
  s3://{bucket}/jobs/{job_id}/corrected/{page_number}.tiff

corrected split child (human-submitted split, left or right):
  s3://{bucket}/jobs/{job_id}/corrected/{page_number}_{sub_page_index}.tiff

layout regions JSON (unsplit pages, sub_page_index IS NULL):
  s3://{bucket}/jobs/{job_id}/layout/{page_number}.json

layout regions JSON (split child, sub_page_index = 0 or 1):
  s3://{bucket}/jobs/{job_id}/layout/{page_number}_{sub_page_index}.json

lineage/debug artifacts:
  s3://{bucket}/jobs/{job_id}/debug/{page_number}/...
```

For unsplit pages, `sub_page_index` is NULL; path templates that include `sub_page_index` use the single-file variant (no `_sub_page_index` suffix). For split children: sub_page_index 0 (left), 1 (right).

Local development mirrors the same relative structure under `file://` URIs.

### 15.2 Artifact Write Protocol (DB-first)

For each artifact type:

1. BEGIN transaction → set `artifact_state='pending'` → COMMIT (marks intent)
2. Write artifact to S3
3. UPDATE: set `output_uri` and `artifact_state='confirmed'`

If step 2 fails: state shows 'pending' (no orphan on S3).
If step 3 fails: artifact exists on S3 with state 'pending' → cleanup process re-attempts the DB update.

`cleanup_retry_count`: When ≥ 3 and age exceeds 3× grace period, state transitions to 'recovery_failed' and ERROR is logged. Artifact is NOT deleted.

### 15.3 Deployment Architecture

System is cloud-agnostic. All inter-service communication via HTTP APIs. Compute, storage, and state layers are separately deployed.

**Reference deployment:**

- Runpod for GPU inference (IEP1A, IEP1B, IEP1D, IEP2A, IEP2B, Shadow Worker)
- S3-compatible object storage for artifacts
- CPU compute nodes for EEP, IEP1C (shared module), Redis, PostgreSQL, Prometheus, Grafana, MLflow

**GPU deployment modes:**

- Development/local: standard FastAPI services
- Production: scale-to-zero via Runpod Serverless, Knative Serving with KPA, or KEDA

Model artifacts must be baked into container images or mounted from low-latency storage. GPU containers must not download model weights from remote object storage during cold start.

**Minimum GPU configuration if co-scheduled:** IEP1A YOLOv8-seg (~1GB) + IEP1B YOLOv8-pose (~1GB) + IEP1D UVDoc (~2GB) + IEP2A Detectron2 production (~3GB) + IEP2A Detectron2 candidate (~3GB) + IEP2B DocLayout-YOLO (~0.5–1.0GB, model-size-dependent) ≈ 10.5–11.0GB. Recommend 16GB GPU.

**Docker service ports:**

| Service | Port | Base Image |
|---------|------|-----------|
| EEP | 8000 | python:3.11-slim |
| IEP1A (YOLOv8-seg) | 8001 | nvidia/cuda:11.8.0 + ultralytics |
| IEP1B (YOLOv8-pose) | 8002 | nvidia/cuda:11.8.0 + ultralytics |
| IEP1C | — | (shared module within EEP, no separate container) |
| IEP1D (UVDoc) | 8003 | nvidia/cuda:11.8.0 + PyTorch |
| IEP2A (Detectron2) | 8004 | nvidia/cuda:11.8.0 + Detectron2 |
| IEP2B (DocLayout-YOLO) | 8005 |  nvidia/cuda:11.8.0 + PyTorch + DocLayout-YOLO dependencies |
---


## 16. MLOps: Shadow Evaluation and Auto-Retraining

### 16.1 Shadow Evaluation Pipeline

Asynchronous: candidate model runs in parallel with production on sampled pages, results compared offline.

**Shadow task enqueue conditions (all three must be true):**
1. Sampling: `sha256(f"{job_id}:{page_number}") % 100 < shadow_fraction * 100` (deterministic per page)
2. `job.shadow_mode == True`
3. Staging candidate exists in MLflow (background-refreshed cache, updated every 60 seconds)

Shadow tasks pushed to Redis queue `libraryai:shadow_tasks`. Failure to enqueue must not affect live routing.

**Shadow Worker** processes tasks asynchronously: dequeues task, runs candidate inference (IEP2A only — shadow evaluation is layout-only), computes quality delta, writes to `shadow_results`.

**Scope limitation:** The shadow pipeline evaluates IEP2A (layout) candidates only. IEP1A and IEP1B (preprocessing geometry models) are never run as shadow candidates on the live pipeline. Their promotion uses an offline evaluation path (see Section 16.4).

**Promotion gates (after 50 shadow samples):**
1. Quality: candidate mean_conf ≥ production mean_conf − 0.02
2. Latency: candidate p95 ≤ production p95 × 1.25
3. Reliability: candidate error rate < 5%
4. Golden dataset: zero critical failures

**`POST /v1/shadow/promote`:**
- `force=false`: re-checks all 4 gates; blocked with 409 if any fail
- `force=true`: promotes without gate check (logged as forced)
- On success: updates `model_versions`, transitions MLflow stages (staging→production, production→archived), publishes reload signal to Redis `libraryai:model_reload:iep2a`

**`POST /v1/shadow/rollback`:**
- Automated path (Alertmanager `PostPromotionAcceptRateCollapse`): acts only within 2h of promotion
- Manual path: `{"reason": "manual"}`; no window check
- Restores most recent archived model version

### 16.2 Auto-Retraining Triggers

| Trigger | Condition | Persistence |
|---------|-----------|-------------|
| `layout_confidence_degradation` | Median IEP2A confidence drops >15% from baseline | 48h |
| `drift_alert_persistence` | Any drift detector alert firing | 48h |
| `escalation_rate_anomaly` | Rectification fallback rate exceeds 25% | 24h |
| `auto_accept_rate_collapse` | Auto-accept rate drops below 40% | 24h |
| `structural_agreement_degradation` | IEP1A/IEP1B structural agreement rate drops >20% from baseline | 48h |

Cooldown: 7 days per trigger type after firing.

Pipeline mapping per trigger type:
- `layout_confidence_degradation` → [layout_detection, doclayout_yolo]
- `drift_alert_persistence` → [layout_detection]
- `escalation_rate_anomaly` → [rectification, preprocessing]
- `auto_accept_rate_collapse` → [layout_detection, doclayout_yolo, rectification, preprocessing]
- `structural_agreement_degradation` → [preprocessing]

### 16.3 Drift Detection

`DriftDetector` class with sliding window (default size 200) and baseline comparison. Returns True if current window mean deviates more than `threshold_std` standard deviations from baseline.

Monitored metrics:
- IEP1A: `geometry_confidence`, `split_detection_rate`, `tta_structural_agreement_rate`, `tta_prediction_variance`
- IEP1B: `geometry_confidence`, `split_detection_rate`, `tta_structural_agreement_rate`, `tta_prediction_variance`
- IEP1C: `blur_score`, `border_score`, `foreground_coverage`
- IEP1D: `rectification_confidence`
- IEP2A: `mean_page_confidence`, `region_count`, per-class fractions
- IEP2B: `mean_page_confidence`, `region_count`, per-class fractions
- EEP: `geometry_selection_route` distribution, `structural_agreement_rate`, `artifact_validation_route` distribution, `layout_consensus_confidence`

Baselines loaded from `monitoring/baselines.json` at startup.

**Initial baselines.json creation:** Baselines are generated from the Phase 4 baseline evaluation (Section 10.4). After completing the baseline evaluation on the held-out validation set, compute the mean and standard deviation of each monitored metric across the validation set and write them to `monitoring/baselines.json`. This file must exist before the first production deployment. Procedure:

1. Complete Phase 4 baseline evaluation (Section 10.4, Step 5).
2. Run `training/scripts/compute_baselines.py --split validation` to generate the file.
3. Commit `monitoring/baselines.json` to version control.

**Baselines refresh:** After each successful retraining run, re-run the baseline computation script on the updated validation set and commit the result. Alternatively, an operator may refresh baselines from a recent production window by running `training/scripts/compute_baselines.py --source production --window 200`. The file is reloaded by EEP at startup; a rolling restart is required to apply refreshed baselines without downtime.

---

### 16.4 Preprocessing Model Promotion (Offline Path)

IEP1A and IEP1B (preprocessing geometry models) are not evaluated via the live shadow pipeline. Candidate geometry models are promoted through an offline evaluation-gated manual process.

**Candidate training:** Train candidate IEP1A or IEP1B model on updated dataset (extended annotations, active learning additions, or corrected labels). Log `dataset_version` and `dataset_checksum` to MLflow. A run without these params is invalid and must not promote.

**Offline evaluation:** Run the full evaluation suite (Section 10.3) on the candidate model against the held-out test set:
- Geometry accuracy (IoU) per collection
- Split accuracy (precision + recall) per collection
- Structural agreement rate (candidate vs. production IEP1A/IEP1B pair)
- Review rate and bad auto-accept rate

**Promotion gates:**
- Geometry IoU ≥ current production − 0.02 on all collections
- Split precision ≥ current production − 0.03 (split is highest-risk decision)
- Structural agreement rate ≥ current production − 0.05
- Zero regression on golden test set (pre-defined cases that must not degrade)
- p95 inference latency ≤ current production × 1.25

**Promotion process:** All gate results reviewed by admin. Promotion is manual: admin invokes `POST /v1/shadow/promote` with `service=iep1a` or `service=iep1b` and `force=true` (since no live shadow data exists for preprocessing candidates). Promotion is logged with justification in `model_versions`. A post-promotion monitoring window of 48h is required; rollback is available if structural agreement rate or review rate degrades.

---

## 17. Operational Constraints

### 17.1 Capacity

- `max_concurrent_pages`: 20 (configurable)
- `max_queue_depth`: 5000
- `rate_limit_tokens_per_minute`: 100 per caller
- `max_task_retries`: 3 (infrastructure failures)
- `task_timeout_seconds`: 900 — provides a conservative upper bound covering worst-case execution under GPU contention, degraded performance, large-image processing, and retry overhead. The nominal warm-service execution path is ~334s, but additional headroom is intentionally allocated to prevent premature termination during transient slowdowns or resource contention.

### 17.2 Observability

- Prometheus metrics on every service at `GET /metrics`
- Grafana dashboards for pipeline health
- Alertmanager configured for retraining and rollback triggers
- `eep_auto_accept_rate` Gauge updated after each terminal page (observability only; never influences routing)
- `eep_structural_agreement_rate` Gauge updated after each geometry selection (observability only)

### 17.3 Infrastructure Portability

Deployment must never depend on a single cloud vendor. All components must remain portable through Docker containers, HTTP APIs, S3-compatible storage, PostgreSQL, and Redis.

### 17.4 SLO Audit Sampling

Weekly human audit sampling of auto-accepted pages is required and must cover all material types represented in production traffic. Audit results are stored in `slo_audit_samples`.

SLO audit sampling is the mechanism for detecting shared blind spots that the consensus gate cannot detect. Dual-model agreement reduces the probability of error but does not guarantee correctness, especially on domains absent or underrepresented in both training datasets. If both models share the same blind spot, they may agree on an incorrect output; only human audit can reveal this failure mode.

Audit results must therefore be reviewed not only for overall bad auto-accept rate, but also stratified by material type, collection, and capture modality where available.

Any proposed adjustment to IoU, skew, or consensus parameters must be supported by statistically significant audit evidence and must demonstrate no increase in bad auto-accept rate beyond defined SLO limits.

### 17.5 Threshold Conservativeness Risk

IoU and skew thresholds are fixed conservative values (IoU ≥ 0.90, skew ≤ 5°) set without a downstream quality measurement system. This may increase human review rate.

Mitigation:
- Use SLO audit sampling to evaluate real-world acceptance quality
- Adjust thresholds only through auditor-validated sampling
- Enforce no increase in bad auto-accept rate during any threshold change

---

## 18. Edge Cases and Failure Handling

### 18.1 One Geometry Model Fails

If both IEP1A and IEP1B fail (timeout, error, malformed response) in the first pass: route immediately to `pending_human_correction` with `review_reasons=["geometry_failed"]`.
If one model fails in the first pass:
    - proceed with provisional geometry from the surviving model
    - mark geometry trust as low
    - enforce rectification and second-pass agreement
If either IEP1A or IEP1B fails (timeout, error, malformed response) during the second pass: route immediately to `pending_human_correction` with `review_reasons=["geometry_failed_post_rectification"]`.
### 18.2 Structural Disagreement
If IEP1A and IEP1B disagree on `page_count` or `split_required` in first pass:
    → proceed with provisional geometry
    → mark geometry as low-trust
    → enforce rectification and second-pass agreement

If disagreement persists after second pass:
    → route to pending_human_correction. No exceptions. No override by confidence. This is the primary safety gate.

### 18.3 Both Models Fail Sanity Checks

If both models' predictions fail one or more sanity checks: route to `pending_human_correction` with `review_reasons=["geometry_sanity_failed"]`.

### 18.4 IEP1C Normalization Failure

If IEP1C fails: route to `pending_human_correction` with `review_reasons=["normalization_failed"]`.

### 18.5 IEP1D Rectification Unavailable

If IEP1D is unavailable (circuit breaker open, timeout, error) for an artifact that requires rescue: log warning and route that artifact directly to `pending_human_correction` with `review_reasons=["rectification_failed"]`. Step 6.5 (second geometry pass) and Step 7 (final validation) are not executed for that artifact. No further automated acceptance is allowed for rescue-required artifacts when IEP1D is unavailable.

### 18.6 Structural Disagreement After Rectification

If IEP1A and IEP1B disagree after the second geometry pass on the rectified image: route to `pending_human_correction` with `review_reasons=["structural_disagreement_post_rectification"]`. This routing is terminal and non-bypassable. No confidence override is permitted.

### 18.7 IEP2A Failure

If IEP2A returns unusable output: route to `review` with `review_reasons=["layout_detection_failed"]`. Do not invoke IEP2B.

### 18.8 IEP2B Unavailable

If IEP2B is unavailable: `single_model_mode=True`, `agreed=False`, page routes to `review`. Single-model layout auto-acceptance is prohibited.

### 18.9 Split Across Rectification

When IEP1D runs on a split sub-page: IEP1D may improve the artifact but must not redefine `split_required` or `split_x`. Split geometry is owned by the initial geometry pass on the original full-image OTIFF.

### 18.10 Corrupt OTIFF

If OTIFF is corrupted: hash mismatch raises ValueError, page routes to `failed` (not retried).

### 18.11 Worker Crash During Processing

On restart: worker reloads page state from database. Artifact existence is verified on storage. Processing resumes from latest persisted state. Tasks are idempotent.

### 18.12 New Collection Type

A new collection arriving in production that was not in training is treated as a new leave-one-out test case. Performance on it is monitored. Cases with high uncertainty, low structural agreement, or high review rate are routed to active learning for labeling.

### 18.13 Page Area Below Preference Threshold

When `page_area_fraction < 0.30` for a detected page region: IEP1B geometry is preferred in the selection cascade because IEP1A mask resolution degrades at small page sizes. This is a preference tiebreaker, not an override — IEP1B must still pass all sanity and quality checks.

### 18.14 Preprocess-Only Human Correction Routing

When `pipeline_mode == "preprocess"` and a page in `pending_human_correction` receives a correction submission:

**Single-page correction (`split_x` is null):**
1. Generate corrected PTIFF from submitted parameters applied to OTIFF.
2. Update `output_image_uri` to corrected PTIFF URI.
3. Transition: `pending_human_correction` → `ptiff_qa_pending`.
4. Then apply `ptiff_qa_mode`:
   - if `ptiff_qa_mode == "manual"`: remain in `ptiff_qa_pending` until reviewer approval
   - if `ptiff_qa_mode == "auto_continue"`: transition to `accepted`, `routing_path="preprocessing_only"`
5. No layout detection is performed in `pipeline_mode="preprocess"`.

**Human-submitted split (`split_x` is non-null):**
1. Create left and right child sub-pages as in the normal split correction workflow (Section 8.6).
2. Each child is enqueued to produce its corrected PTIFF artifact only (no layout detection).
3. Each child is enqueued to produce its corrected PTIFF artifact only (no layout detection).
4. After corrected artifact generation, each child transitions to `ptiff_qa_pending`.
5. Then apply `ptiff_qa_mode`:
   - if `ptiff_qa_mode == "manual"`: child remains in `ptiff_qa_pending`
   - if `ptiff_qa_mode == "auto_continue"`: child transitions to `accepted`

The correction workspace UI must not display or enable layout-related fields for preprocess-only jobs.

A page MUST be routed to human correction if a valid visual representation of the OTIFF can be displayed, regardless of model failures.

A page MUST be routed to `failed` only if:
- the OTIFF cannot be decoded or displayed
- the OTIFF cannot be retrieved after retry policy exhaustion
- a lineage hash mismatch is detected (data integrity violation)

---

## 19. Implementation Roadmap (Phase -> Work Packet)

This section supersedes the previous high-level acceptance checklist and aligns execution with the current phase/packet roadmap used by implementation agents.

### 19.1 Scope and Execution Rules

- Implementation is phase-based and packet-based.
- One packet is executed at a time unless explicitly instructed otherwise.
- `docs_pre_implementation/implementation_checklist.md` is mandatory:
  - create in Phase 0
  - update after each completed phase
  - never mark phase complete unless all packet done criteria and phase DoD are satisfied.

### 19.2 Non-Negotiable Roadmap Constraints

- Preserve architecture and contracts:
  - EEP orchestrator
  - IEP1A/IEP1B geometry services (mock inference internals allowed pre-swap)
  - IEP1C deterministic normalization
  - IEP1D UVDoc rectification fallback
  - IEP2A/IEP2B dual layout consensus model
  - DB-first artifact write protocol
  - Redis queue + semaphore model
  - PostgreSQL source of truth
  - S3-compatible artifact storage
  - JWT + RBAC
- Preserve safety semantics:
  - no single-model auto-accept in IEP1 or IEP2
  - post-rectification structural disagreement routes to `pending_human_correction`
  - content failures do not route to `failed`
  - no silent page loss
  - no destructive OTIFF overwrite
  - no silent gate/lineage bypass.

### 19.3 PTIFF QA Gate (Authoritative)

- `ptiff_qa_pending` is a non-terminal page state.
- After successful preprocessing (including accepted correction), pages enter `ptiff_qa_pending`.
- `ptiff_qa_mode`:
  - `manual`: job-level gate before downstream stages.
  - `auto_continue`: automatic transition through QA to downstream terminal/next stage.
- Manual-mode approval semantics:
  - single-page approval records approval intent only.
  - transition to `layout_detection` (layout jobs) or `accepted` (preprocess jobs) occurs at gate release.
  - gate release requires QA-resolved pages and no in-flight correction paths that must return to QA.

### 19.4 Phase Order

1. Phase 0 — repo, containers, service/process skeletons, shared foundations.
2. Phase 1 — schemas, DB core migration, storage, Redis queue contract, presigned upload, core job API.
3. Phase 2 — IEP1A/B mock services + IEP1C normalization.
4. Phase 3 — geometry selection + artifact validation gates.
5. Phase 4 — full IEP1 worker orchestration + recovery integration.
6. Phase 5 — correction workflow + PTIFF QA workflow.
7. Phase 6 — IEP2 services + layout consensus gate.
8. Phase 7 — auth/RBAC/admin-user APIs + lineage.
9. Phase 8 — MLOps plumbing (shadow/retraining workers + recovery + policy/promote/rollback/retraining hooks).
10. Phase 9 — metrics, policy-threshold wiring, drift skeleton, observability hardening, golden-dataset tests.
11. Phase 10 — frontend (user/admin/PTIFF QA/correction/MLOps screens).
12. Phase 11 — cloud deployment (Kubernetes, Runpod backend support, CI/CD, in-cluster observability).
13. Phase 12 — real IEP1A/B model swap (inference internals only; no orchestration/schema redesign).

### 19.5 Mandatory Work-Packet Highlights

- Phase 0 includes separate process skeletons:
  - `eep_worker`, `eep_recovery`, `shadow_worker`, `shadow_recovery`, `retraining_worker`, `retraining_recovery`, `artifact_cleanup`.
- Phase 1 includes:
  - `shared/state_machine.py` authoritative transition contract
  - `POST /v1/uploads/jobs/presign`
  - reliable Redis queue contract with `BLMOVE` + `BRPOPLPUSH` fallback and reconciliation hooks
  - `ptiff_qa_mode` job config + `ptiff_qa_pending` state support.
- Phase 4 includes:
  - full preprocess + rescue + split + PTIFF QA routing
  - watchdog + recovery reconciliation.
- Phase 5 includes PTIFF QA endpoints:
  - `GET /v1/jobs/{job_id}/ptiff-qa`
  - `POST /v1/jobs/{job_id}/ptiff-qa/approve-all`
  - `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve`
  - `POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit`.
- Phase 11 includes production architecture requirements:
  - Kubernetes manifests/workloads
  - Runpod backend support
  - Redis AOF in production
  - MLflow workload presence
  - CI/CD pipelines
  - in-cluster Prometheus/Alertmanager/Grafana.

### 19.6 Required Test Tracks (Phase-Gated)

- Contract tests:
  - EEP API (Phase 1)
  - IEP1A/B (Phase 2)
  - EEP worker queue + IEP1D (Phase 4)
  - IEP2A/B (Phase 6)
  - shadow/retraining workers (Phase 8).
- Simulation tests (Phase 4):
  - first/second-pass disagreement
  - timeout/cold-start timeout
  - malformed response
  - Redis reconnect
  - worker crash
  - split retry/idempotency.
- Golden-dataset tests (Phase 9):
  - normalization outputs
  - gate routing/validation determinism
  - lineage expectations
  - state transition expectations.

### 19.7 Enforcement Invariants (Mandatory)

- Database invariants:
  - split-safe uniqueness
  - split-parent lineage retention
  - trigger-maintained timestamps
  - attempt fencing + terminal attempt-null enforcement
  - per-attempt/per-phase invocation uniqueness
  - phase-separated migrations (Phase 1 core vs Phase 8 MLOps).
- Queue/recovery invariants:
  - reliable claim/move/processing ownership
  - bounded retries + dead-letter
  - DB-authoritative reconciliation after Redis faults
  - independent recovery processes
  - pause/reconcile/resume semantics after reconnect.
- Failure-routing invariants:
  - transient infra failures retry first; terminal infra `failed` only after budget exhaustion
  - content/quality failures route to `pending_human_correction`
  - manual PTIFF QA partial approvals do not bypass gate release.

### 19.8 Legacy Checklist Replacement Note

The earlier checklist block in this section is deprecated. The authoritative execution ledger is:

- `docs_pre_implementation/implementation_checklist.md`

and must mirror the current phase/packet roadmap and its phase definitions of done.
