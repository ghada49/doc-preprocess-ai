# LibraryAI Specification Update
## Authoritative Architectural Revision with Migration Gap Analysis

**Date:** April 1, 2026
**Status:** SPECIFICATION CORRECTION + MIGRATION PLAN
**Scope:** IEP0 document-type classification (NEW), IEP2A backend change, IEP2 adjudication redesign, IEP1 rescue addition, IEP1D recommendation

---

# 1. Summary of Intended Architecture

The updated LibraryAI architecture introduces **IEP0 for automated document-type classification** and maintains **IEP1 as internally authoritative** for page geometry while shifting **IEP2 from consensus-only to authoritative external adjudication**:

**IEP0 (Document-Type Classification) — Upstream Metadata:**
- Automatic classification of document type (book, newspaper, archival_document)
- Runs immediately on upload using lightweight model on proxy image
- Replaces manual material_type selection
- Informs IEP1/IEP2 threshold adaptation
- **Output:** predicted_material_type + confidence

**IEP1 (Preprocessing) — Internally Authoritative:**
- Two local geometry models (IEP1A: YOLOv8-seg, IEP1B: YOLOv8-pose) with structural agreement gate
- First-pass disagreement → rescue flow (rectification + mandatory second-pass verification)
- Second-pass disagreement → human review (non-bypassable)
- **NEW:** External assist for difficult/warped pages: mandatory external OCR/cleanup service as rescue aid before second geometry pass
- **Final acceptance:** High-trust local geometry (both models agree) OR rectification succeeds and second-pass agrees

**IEP2 (Layout Detection) — Externally Adjudicated:**
- IEP2A: **PaddleOCR PP-DocLayoutV2** (local candidate, replaces Detectron2)
- IEP2B: DocLayout-YOLO (local candidate, fast second opinion)
- **Local agreement path:** IEP2A + IEP2B agree → accept (fast path)
- **Disagreement/failure path:** IEP2A and IEP2B disagree OR one/both fail → consult Google Document AI as final adjudicator
- **Final acceptance:** IEP2A + IEP2B agree locally → accept; OR Google Document AI resolves agreement → accept; OR all fail → human review
- **Terminology shift:** "Consensus" → "Authoritative External Adjudication"

**IEP1D Rectification — UVDoc (Recommended):**
- Geometric rescue for warped, curved, distorted pages
- Operates on preprocessed artifact; does not redefine page structure
- Second-pass geometry required after rectification

**Key Design Intent:**
- IEP0 automates material-type classification (no user selection)
- IEP1 requires internal agreement (two local models)
- IEP2 delegates layout authority to Google Document AI on disagreement
- External adjudication is a safety mechanism, not a primary method
- Arabic document handling is supported via Google Document AI's multi-language capability
- No fine-tuning is assumed or required

---

# 2. Sections With No Changes / Sections With Extensions

The following sections are **minimally affected** or **have new subsections added**:

- **Section 1: System Overview** — Product intent, access policy, global measurement rules remain valid
- **Section 2: Product Intent** — System purpose unchanged
- **Section 3: Core Concepts** — Page, job, lineage, material type definitions (now applies to IEP0 output, not user input)
- **Section 6.0-6.2: IEP0 Document-Type Classification** — **NEW SECTION** (upstream of IEP1)
- **Section 6.3 (was 6.1): IEP1 Design Principle** — IEP1 structure-first, dual-model architecture unchanged
- **Section 6.4 (was 6.2): IEP1A — YOLOv8-seg** — Model, endpoint, schema unchanged
- **Section 6.5 (was 6.3): IEP1B — YOLOv8-pose** — Model, endpoint, schema unchanged
- **Section 6.4: IEP1C — Deterministic Normalization Module** — Unchanged
- **Section 6.6: IEP1 Schemas** — All geometry request/response schemas unchanged
- **Section 6.8: Geometry Selection Logic** — Selection cascade, sanity checks unchanged
- **Section 6.9: Artifact Validation** — Hard requirements, soft signals unchanged
- **Section 6.10: Acceptance Philosophy** — Two-stage safety design still valid
- **Section 7.3: Layout Schemas (RegionType, Region, LayoutDetectRequest)** — Basic schemas unchanged
- **Section 8.1: EEP Worker Concurrency** — Unchanged
- **Section 9: Execution Guarantees** — Terminal states, no silent data loss, retry policy unchanged
- **Section 8.5: Review Reasons (partial)** — Many review reasons remain; new ones added

---

# 3. Changed Sections — Full Replacement Text

## Section 4.1 Service Inventory

**ACTION:** REPLACE (expand to include Google Document AI, change IEP2A)

```markdown
### 4.1 Service Inventory

| Service | Port | Role | Compute | Production Deployment |
|---------|------|------|---------|----------------------|
| **IEP0** | **8010** | **Document-type classification (book, newspaper, archival_document)** | **GPU or lightweight CPU** | **Scale-to-zero** |
| EEP | 8000 | Orchestration, quality gates, geometry selection, job management, acceptance policy | CPU | Continuous CPU |
| IEP1A | 8001 | YOLOv8-seg instance segmentation — primary page geometry model | GPU (inference) | Scale-to-zero GPU |
| IEP1B | 8002 | YOLOv8-pose keypoint regression — secondary page geometry model | GPU (inference) | Scale-to-zero GPU |
| IEP1C | — | Deterministic normalization: applies selected geometry to full-res image | CPU | Shared module (invoked by EEP, not a network service) |
| IEP1D | 8003 | UVDoc rectification fallback for warped/distorted pages | GPU | Scale-to-zero GPU |
| IEP2A | 8004 | **PaddleOCR PP-DocLayoutV2 layout detection (primary local candidate, document-trained)** | GPU | Scale-to-zero GPU |
| IEP2B | 8005 | DocLayout-YOLO layout detection (fast second local candidate, document-trained) | GPU (minimal) | Scale-to-zero GPU |
| **Google Document AI** | — (async HTTP to Google Cloud API) | **Final adjudicator for layout disagreement/failure (external, authoritative fallback)** | Managed (Google Cloud) | Asynchronous, configurable timeout |

**Processing order:** IEP0 (document classification) → IEP1 (preprocessing) → IEP2 (layout detection)

Every service exposes: `GET /health` → 200, `GET /ready` → 200/503, `GET /metrics` → Prometheus text.

**IEP0 (Document Type Classification)** is invoked first, on upload, on a proxy/preview image to determine document class (book, newspaper, archival_document). The predicted material_type and confidence are stored and used by downstream IEP1/IEP2 thresholds and routing. IEP0 is a lightweight model inference step, not a user selection step.

IEP1 is a staged cascade with quality gates, not a peer-consensus preprocessing system. IEP1A and IEP1B are both always-on geometry models. Both run on every page. Their structural outputs are compared as a safety check. In the first pass, disagreement between models reduces geometry trust and triggers rescue flow (rectification and a mandatory second geometry pass) rather than immediately halting preprocessing. In the second pass, both models must agree — this is the authoritative safety gate. Agreement is a structural corroboration signal, not a voting or averaging mechanism.

IEP1C is CPU-only deterministic image math. It is invoked as a shared module within EEP, not as a separate network service. A separate service boundary adds latency and operational failure modes without adding model diversity.

IEP1D is a GPU service invoked when artifact validation fails after normalization or when first-pass geometry trust is insufficient. It is a rectification rescue stage, not a geometry model.

**IEP2 is now an authoritative external adjudication system, not a consensus system.** IEP2A (PaddleOCR PP-DocLayoutV2) and IEP2B (DocLayout-YOLO) are local layout region candidates. When they agree, the result is accepted (fast path). When they disagree or either fails, Google Document AI is consulted as the final authoritative adjudicator. If Google also fails, the page routes to human review. Single-model output from either IEP2A or IEP2B alone (without agreement or Google adjudication) is never sufficient for acceptance.

**IEP2A backend change:** IEP2A was previously Detectron2 Faster R-CNN. It is now **PaddleOCR PP-DocLayoutV2**, a document-layout-specific detector with native support for multi-language documents and stronger performance on Arabic text. PP-DocLayoutV2 provides both region detection and logical reading-order hints, improving layout understanding for non-Latin scripts.
```

---

## Section 4.2 Data Flow

**ACTION:** UPDATE (add Google Document AI fallback path)

Replace the data flow diagram section with:

```markdown
### 4.2 Data Flow

```text
upload
   │
   ▼
┌──────────────────────────────┐
│  IEP0 — document-type        │
│  classification              │
│  (proxy image)               │
└──────────────┬───────────────┘
               │
     predicted_material_type
     + confidence
               │
               ▼
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
   IEP2A layout detection (PaddleOCR)
        │
        ▼ (if IEP2A returns plausible output)
   IEP2B layout detection (DocLayout-YOLO)
        │
        ▼
   Layout agreement check (EEP)
        │
    ┌───┴────────────────┐
    │                    │
  agree            disagree or fail
    │                    │
    │                    ▼
    │           Google Document AI
    │           (external adjudicator)
    │                    │
    │            ┌───────┴────────┐
    │            │                 │
    │         success           failure
    │            │                 │
    └────┬───────┘                 │
         │                         │
         ▼                         ▼
      ACCEPTED                  review
```

**Decision logic:**
- Boxes "ACCEPTED" (local agreement path, fast) vs Google Document AI consultation (fallback path):
- Local agreement between IEP2A and IEP2B → accept (canonical layout from IEP2A)
- Local disagreement OR one/both fail → call Google Document AI
- Google success → accept (Google result becomes canonical layout)
- Google failure → review (human decision)
```

---

## Section 4.3 Internal Endpoint Summary

**ACTION:** UPDATE (add Google Document AI, change IEP2A)

```markdown
### 4.3 Internal Endpoint Summary

| Service | Route | Request Schema | Response Schema | Timeout |
|---------|-------|---------------|-----------------|---------|
| **IEP0** | **`POST /v1/classify`** | **DocumentClassificationRequest** | **DocumentClassificationResponse** | **30s** |
| IEP1A | `POST /v1/geometry` | GeometryRequest | GeometryResponse or PreprocessError | 30s |
| IEP1B | `POST /v1/geometry` | GeometryRequest | GeometryResponse or PreprocessError | 30s |
| IEP1C | (shared module) | NormalizeRequest (internal) | PreprocessBranchResponse (internal) | — |
| IEP1D | `POST /v1/rectify` | RectifyRequest | RectifyResponse | 60s |
| **IEP2A** | **`POST /v1/layout-detect`** | **LayoutDetectRequest** | **LayoutDetectResponse (detector_type="paddleocr_pp_doclayout_v2")** | **60s** |
| IEP2B | `POST /v1/layout-detect` |LayoutDetectRequest | LayoutDetectResponse (detector_type="doclayout_yolo") | 30s |
| **Google Document AI** | **Async HTTP to Google Cloud API `documentai.googleapis.com`** | **LayoutAdjudicationRequest** | **LayoutAdjudicationResult** | **90s** |

**IEP0** runs first on upload with a low-resolution proxy image. It predicts material_type (book, newspaper, archival_document) and stores confidence.

IEP1A and IEP1B expose identical endpoint schemas. The same GeometryRequest/GeometryResponse contract serves both.

IEP2A is now **PaddleOCR PP-DocLayoutV2**. The response schema remains `LayoutDetectResponse` but `detector_type` field is now `"paddleocr_pp_doclayout_v2"`.

**Google Document AI** is a synchronous HTTP API call to Google Cloud (not a standalone service managed by LibraryAI). The EEP worker makes the direct HTTP call when layout disagreement or IEP2 failure occurs. No separate microservice is deployed.
```

---

## Section 5.2 — IEP1 Staged Cascade and Structural Safety Check

**ACTION:** UPDATE (clarify IEP1 internal authority, remove consensus-only language)

```markdown
### 5.2 IEP1 Staged Cascade and Structural Safety Check

IEP1 is a quality-gated staged processing cascade with a mandatory three-pass rescue policy. Two architecturally diverse geometry models — IEP1A (YOLOv8-seg, instance segmentation) and IEP1B (YOLOv8-pose, keypoint regression) — run in parallel on every page. Their structural outputs are compared as a safety check before any preprocessing is accepted. Agreement between them is a safety signal — a binary verification that both models independently reach the same structural conclusion — not a voting or averaging mechanism.

The IEP1 staged cascade with mandatory rescue escalation:

**Pass 1 (Initial geometry):**
1. IEP1A and IEP1B predict page structure independently (parallel)
2. Geometry selection cascade chooses the best candidate
3. Deterministic normalization (IEP1C) applies selected geometry to full-resolution image
4. Artifact validation evaluates the normalized output

**If Pass 1 artifact validation succeeds:** Continue to next stage (layout detection or acceptance). **IEP1 is done.**

**If Pass 1 artifact validation fails:** Proceed to rescue cascade (mandatory).

**Rescue Escalation Level 1 (Geometric rectification):**
5. IEP1D (UVDoc) rectifies warped/distorted artifacts
6. If rectification fails → **proceed immediately to Level 2 (external cleanup); do NOT route to human review**
7. If rectification succeeds → run **Pass 2 geometry** (IEP1A + IEP1B again on rectified artifact)
8. Pass 2 artifact validation: assess normalized output from rectified artifact

**If Pass 2 validation succeeds:** Continue. **IEP1 is done.**

**If Pass 2 validation fails OR IEP1D rectification failed:** Proceed to Level 2 (mandatory).

**Rescue Escalation Level 2 (Image quality cleanup):**
9. External OCR/cleanup service (Google Document AI) performs image enhancement (denoising, contrast, binarization, compression cleanup)
10. If external cleanup fails → **proceed immediately to human review** (no further rescue available)
11. If external cleanup succeeds → run **Pass 3 geometry** (IEP1A + IEP1B again on cleaned artifact)
12. Pass 3 artifact validation: assess normalized output from cleaned artifact

**If Pass 3 validation succeeds:** Continue. **IEP1 is done.**

**If Pass 3 validation fails:** Route to `pending_human_correction`. **No further rescue available.**

**Critical principle:** IEP1 remains internally authoritative. No external system predicts or overrides geometry. External services (IEP1D rectification, external cleanup) are **mandatory recovery aids** in sequence: they improve the artifact, then the internal geometry models (IEP1A + IEP1B) re-analyze and decide.
- **Rectification failure does NOT bypass external cleanup:** must go to Google cleanup
- **Cleanup failure is the last resort:** routes to human review
- The final two-model agreement gate (mandatory in all three passes) is inviolable

**IEP2 is now an authoritative external adjudication system.** IEP2A and IEP2B are local layout region candidates. When they agree, the layout is accepted without further consultation (fast path). When they disagree or either fails, Google Document AI is consulted as the authoritative final adjudicator. Single-model auto-acceptance is prohibited; the system must either achieve local agreement or obtain external adjudication before acceptance.
```

---

## Section 5.3 — Shared Invariant

**ACTION:** UPDATE (expand to clarify IEP2 adjudication)

```markdown
### 5.3 Shared Invariant

**For IEP1: Agreement between independent internal models is the primary safety mechanism. External services are recovery aids, not authorities.**

**For IEP2: External adjudication (Google Document AI) is the safety mechanism for unresolved local disagreement or failure.**

**For IEP1:** Structural agreement between IEP1A and IEP1B is the authoritative safety condition for final auto-acceptance. In the first pass, disagreement lowers geometry trust and triggers rescue flow; in the second pass, agreement is mandatory. The treatment of disagreement is stage-dependent:
- **First pass:** disagreement reduces geometry trust to low. The pipeline does not halt; instead, the best available provisional candidate is selected and the page proceeds through rescue (rectification, potentially external cleanup) and a mandatory second geometry pass.
- **Second pass (post-rescue):** both models must return valid outputs and agree on `page_count` and `split_required`. Disagreement at this stage is terminal and routes to `pending_human_correction`. No exceptions.

External rescue services (IEP1D, external OCR/cleanup) are **recovery aids:**
- They improve artifacts that first-pass validation rejected
- They do NOT predict or override geometry
- After external rescue: internal IEP1A + IEP1B re-analyze the improved artifact
- The final two-model agreement gate remains mandatory

High confidence from a single model OR from external services is never sufficient for final auto-acceptance. This is not consensus voting; it is a two-stage quality gate requiring internal structural corroboration before final acceptance.

**For IEP2:** Local agreement between IEP2A and IEP2B on layout regions is a fast-path acceptance signal. When they agree (after both services map their native classes to the canonical LibraryAI layout ontology), the layout is accepted immediately without further consultation. When they disagree or either fails, **Google Document AI is consulted as the authoritative final adjudicator**. Google's result, when successful, becomes the canonical layout and the page is accepted. Only if Google also fails does the page route to human review.

IEP2 is not a consensus system: it is an **authoritative external adjudication system** where local agreement enables a fast path, and external adjudication provides a fallback for disagreement or failure. Single-model auto-acceptance is prohibited. The system requires either local agreement OR successful external adjudication before accepting a page's layout.

**For all cases requiring rescue (rectification + geometry pass or external cleanup + geometry pass):** the second geometry pass is mandatory. If rescue is unavailable or fails, the artifact is routed to `pending_human_correction`. Skipping the second geometry pass is not permitted for any low-trust or rescue-required artifact.
```

---

# 6. IEP0 — Document-Type Classification

## Section 6.0 Overview

**IEP0** is an automated document-type classification stage that runs immediately after upload, before any IEP1 preprocessing. It replaces manual material-type selection with a learned model that predicts document class (book, newspaper, archival_document) and stores confidence metadata. Downstream IEP1/IEP2 processing uses the predicted material_type for threshold adaptation and routing decisions.

**Architecture:**
- **Input:** Proxy image (low-res preview of first page, ~512px)
- **Model:** Lightweight CNN or Vision Transformer (ViT) trained to classify documents into 3 material types
- **Output:** `predicted_material_type` + `confidence score` (0.0-1.0)
- **Persistence:** Store prediction + confidence in job metadata; used by IEP1/IEP2 routing

**Rationale:**
- Removes manual user selection; automatic classification
- Informs IEP1 geometry thresholds (books use stricter split validation; newspapers allow looser regions)
- Informs IEP2 layout region expectations (books expect dense text blocks; newspapers expect columns with whitespace)
- Confidence can guide review triage (low confidence → flag for manual verification)

---

## Section 6.1 — IEP0 Service

**Port:** 8010
**Compute:** GPU or lightweight CPU (inference only, no training)
**Model:** Lightweight Vision Transformer or EfficientNet trained on LibraryAI document corpus

**Endpoint:** `POST /v1/classify`

**Request:** DocumentClassificationRequest

```json
{
  "job_id": "...",
  "page_image_uri": "...",
  "image_format": "png",
  "image_width": 512,
  "image_height": 512
}
```

**Response:** DocumentClassificationResponse

```json
{
  "predicted_material_type": "book",
  "confidence": 0.92,
  "class_scores": {
    "book": 0.92,
    "newspaper": 0.07,
    "archival_document": 0.01
  },
  "processing_time_ms": 45
}
```

**Classes (mutually exclusive):**
- `"book"` — Bound volumes, monographs (single-density text layout)
- `"newspaper"` — Print newspapers, journals, magazines (multi-column layout with headers)
- `"archival_document"` — Archival materials (historical, mixed layouts, variable quality)

**Readiness check:**
- Model loaded AND weights available
- CUDA accessible (if GPU deployment)

**Failure handling:**
- If IEP0 times out or returns error → use default material_type (fallback: "book") + confidence = 0.0 (unknown)
- Continue processing with fallback; do NOT halt pipeline

**Configuration:**
- `iep0.enabled: bool` (enable/disable IEP0; default true)
- `iep0.timeout_seconds: float` (max time for classification; default 30s)
- `iep0.default_material_type: string` (fallback if classification fails; default "book")
- `iep0.confidence_threshold: float` (minimum confidence for using prediction; below this → use default; default 0.5)

**Observability metrics:**
- Counter: `iep0_classifications_total` (by predicted_material_type)
- Gauge: `iep0_classification_confidence_avg` (moving average confidence)
- Histogram: `iep0_classification_latency_ms` (p50, p95, p99)
- Counter: `iep0_fallback_used_total` (count of timeout/error fallbacks)

---

## Section 6.2 — IEP0 Training and Model Updates

**Model responsibility:** Trained on LibraryAI's historical job corpus (stratified by actual material types, labels assigned during model review phase or via expert curation).

**Training strategy:**
- No fine-tuning required for deployment (transfer learning from pretrained ViT/EfficientNet)
- Baseline: measure baseline accuracy (per-class F1) on held-out test corpus
- Updates: periodic retraining as new documents accumulate (quarterly or on-demand if accuracy drifts)

**Monitoring signal:** If predicted_material_type disagrees significantly with human-reviewed material_type post-processing, signal for model refresh.

---

## Section 6.5 — IEP1D — Geometric Rectification and Rescue

**ACTION:** UPDATE (recommend UVDoc officially, clarify external rescue)

```markdown
### 6.5 IEP1D — Geometric Rectification and Rescue

**Port:** 8003
**Compute:** GPU

IEP1D provides two complementary rescue mechanisms for difficult preprocessing cases:

#### A. Geometric Rectification Fallback (UVDoc)

**Recommended model:** **UVDoc** (Uniform VocabuLary Document Dewarping)

**Why UVDoc:**
- Specialized for document dewarping and perspective correction
- Handles curved, warped, and distorted pages (common in bound books, rolled microfilm, hand-held captures)
- Does not require fine-tuning; pretrained weights work across diverse document styles
- Provides confidence/quality estimates for post-rectification validation
- Computationally affordable for scale-to-zero deployment
- Supports diverse capture scenarios (Arabic documents included in standard training)
- Complements geometric rescue: fixes physical page curl and distortion, allowing second-pass geometry to find true page boundaries

**Alternative candidates (not recommended for initial deployment):**
- DocTR Geometric Rectification (text-OCR coupled; unnecessary complexity for layout-only; requires text modeling)
- Simple perspective/affine fallbacks (insufficient for curved/rolled pages)

**Endpoint:** `POST /v1/rectify`

**Request:** RectifyRequest

**Response:** RectifyResponse

**Typical trigger cases:**

- warped bound pages (spine distortion)
- strong page curl (book pages)
- perspective-heavy captures (handheld scanning)
- microfilm frames with severe distortion

**Critical constraints:**

- IEP1D does not decide split. Split ownership remains with the original full-image geometry from the initial geometry pass.
- IEP1D does not replace IEP1A/IEP1B as the geometry source.
- IEP1D improves an already selected page artifact; it does not redefine the page structure of the original raw scan.
- When the source image is a spread, IEP1D may improve a child page artifact but must not redefine `split_required` or `split_x`.
- Rectification is attempted at most once per page.

**Readiness check:** UVDoc model loaded AND CUDA available.

**Deployment gate:** Before IEP1D is enabled in production, the system must measure the baseline performance of IEP1A + IEP1B + IEP1C + artifact validation on a held-out validation set. IEP1D should be added only after that baseline is established, so its gain from rectification can be measured rather than assumed.

#### B. External OCR/Cleanup Rescue (Image Readability Recovery)

For pages where artifact validation fails after normalization AND geometric rectification (IEP1D) is either unavailable or insufficient, an external OCR or document cleanup service may be consulted as a **readability recovery aid** — not as a geometry authority — before the second geometry pass.

**What external OCR/cleanup does:**
- Analyzes the low-quality normalized image for readability problems (blur, noise, poor contrast, compression artifacts)
- Applies optical cleanup: denoising, contrast enhancement, binarization, artifact removal
- Returns a cleaned image artifact

**What external OCR/cleanup does NOT do:**
- Does not predict or replace page geometry
- Does not alter the split decision or page boundaries
- Does not provide geometry candidates
- Is purely an image enhancement step

**When used (mandatory cascade):**
1. First-pass artifact validation: **FAIL** → proceed to IEP1D rectification
2. IEP1D rectification: **unavailable OR fails** → proceed IMMEDIATELY to external cleanup (do NOT skip to human review)
3. IEP1D rectification: **succeeds** → run second geometry pass (IEP1A + IEP1B on rectified artifact)
4. Second-pass artifact validation: **FAIL** → proceed IMMEDIATELY to external cleanup (do NOT route to human review)
5. External cleanup: **succeeds** → run third geometry pass (IEP1A + IEP1B on cleaned artifact)
6. Third-pass artifact validation: **FAIL** → route to `pending_human_correction` (no further rescue available)

**Exception:** If external cleanup is completely unavailable/disabled in the deployment:
- After rectification fails → can skip to human review
- But this is not the normal path; external cleanup should be available

**Critical design principle:**
- Rectification failure is **NOT** a terminal state; it MUST escalate to external cleanup
- The external service assists image quality, not geometry authority
- The structured agreement check (IEP1A + IEP1B must agree in all passes) is mandatory and inviolable
- Only after both geometric rescue (IEP1D) AND image quality rescue (external cleanup) have been attempted or exhausted does a page go to human review

**IEP1 remains internally authoritative:** No external system can predict or override the final multi-pass geometry agreement gate.

#### IEP1D Configuration and Monitoring

**Configuration:**
- `iep1d.enabled: bool` (enable/disable rectification; default true)
- `iep1d.retry_budget: int` (max rectification attempts per page; default 1)
- `iep1d.timeout_seconds: float` (max time for rectification; default 60s)

**Readiness check:** UVDoc model loaded AND CUDA available.

**Observability metrics:**
- Counter: `iep1d_rectification_invocations_total` (by status: success, timeout, error)
- Gauge: `iep1d_rectification_success_rate`
- Histogram: `iep1d_rectification_latency_ms` (p50, p95, p99)
- Counter: `iep1_second_pass_geometries_total` (tracking second-pass invocations after rectification)

#### C. External Cleanup Service (Image Quality Recovery via Google Document AI)

**Service:** Google Document AI (used for OCR-driven cleanup, not layout detection)

**Endpoint:** `https://documentai.googleapis.com/v1/projects/{project_id}/locations/{location}/processors/{processor_id}:process`

**Role:** When IEP1D rectification fails OR passes but second-pass geometry validation still fails, external cleanup performs image enhancement before a third geometry pass.

**What external cleanup does (via Google Document AI):**
- Extracts text and layout information from the image (as a byproduct of document understanding)
- Applies optical cleanup based on that analysis: denoising, contrast enhancement, binarization, compression artifact removal
- Returns a cleaned/enhanced image (TIFF or similar) for re-processing by internal geometry models

**What external cleanup does NOT do:**
- Does not provide geometry predictions or layout to IEP1
- Does not alter page structure or split decisions
- Is purely image enhancement to improve geometry detectability

**Configuration:**
- `external_cleanup.enabled: bool` (enable/disable external cleanup; default true)
- `external_cleanup.provider: string` (currently "google_document_ai")
- `external_cleanup.timeout_seconds: float` (max time for cleanup; default 120s)
- `external_cleanup.retry_budget: int` (max retries on transient failure; default 2)
- Uses same Google credentials as IEP2 adjudication (shared `google.project_id`, `google.location`, etc.)

**Readiness check:** Google Document AI credentials available AND processor accessible.

**Observability metrics:**
- Counter: `iep1_external_cleanup_invocations_total` (by status: success, timeout, error)
- Gauge: `iep1_external_cleanup_success_rate`
- Histogram: `iep1_external_cleanup_latency_ms` (p50, p95, p99)
- Counter: `iep1_third_pass_geometries_total` (tracking third-pass invocations after cleanup)

**Failure handling:**
- If external cleanup times out or returns error: proceed directly to `pending_human_correction` (no further rescue available)

---

## Section 6.6 — IEP1 Architecture Summary

**ACTION:** ADD (new summary section)

**The IEP1 rescue cascade ensures robust geometry detection through multiple attempts and fallbacks:**

```
First-pass geometry  →  Validation PASS
      ↓
   Validation FAIL
      ↓
    IEP1D (UVDoc)  ──→  Rectification FAIL  ───┐
  Rectification       Rectification SUCCESS     │
                            ↓                   │
                      Second-pass geometry      │
                            ↓                   │
                        Validation PASS         │
                            ↓                   ↓
                        Continue            [MANDATORY]
                                           External Cleanup
                                                ↓
                                          Cleanup SUCCESS
                                                ↓
                                         Third-pass geometry
                                                ↓
                                           Validation PASS
                                                ↓
                                             Continue
                                           (or fail →
                                           human review)
```

**Key invariant:** Rectification failure is **never** a direct route to human review. It **must** escalate to external cleanup first. Both rescue mechanisms use Google Document AI (UVDoc is a specialized model; external cleanup uses general Document AI service). After each rescue, IEP1A + IEP1B re-analyze the improved artifact and must agree for acceptance. The two-model agreement gate is the final arbiter.

---

## Section 7.1 — IEP2A — PaddleOCR PP-DocLayoutV2 Layout Detection

**ACTION:** REPLACE (change from Detectron2 to PaddleOCR)

```markdown
### 7.1 IEP2A — PaddleOCR PP-DocLayoutV2 Layout Detection

**Port:** 8004
**Compute:** GPU

**Endpoint:** `POST /v1/layout-detect`

**Request:** LayoutDetectRequest

**Response:** LayoutDetectResponse with `detector_type="paddleocr_pp_doclayout_v2"`

**Model:** PaddleOCR PP-DocLayoutV2, a document-layout-specific detector trained on diverse document types and multi-language content. PP-DocLayoutV2 provides both region detection and native support for Arabic and other non-Latin scripts. No fine-tuning is required for initial deployment.

**Advantages over Detectron2:**
- Document-layout-specific training (stronger baseline on document regions vs. general object detection)
- Native multi-language support (including Arabic)
- Logical reading-order prediction (provides hints for document structure, valuable for downstream OCR/NLP)
- Faster inference on typical document pages
- No pretrain-specific configuration (PubLayNet weights) needed
- Designed for production document processing

**Native class set (7 classes, mapped to LibraryAI canonical 5 classes):**
- "text" → canonical "text_block"
- "title" → canonical "title"
- "table" → canonical "table"
- "figure" → canonical "image"
- "caption" → canonical "caption"
- (discard: "header", "footer", "page_number"—optional; may map to text_block if needed)

**Postprocessing:**

- Map native classes to canonical LibraryAI classes (text→text_block, title→title, table→table, figure→image, caption→caption)
- Exclude non-canonical classes or map them conservatively (headers/footers→text_block)
- Merge overlapping same-type canonical regions (IoU > 0.5, preserve higher-confidence ID)
- Recalibrate confidence: small regions (<1% page) × 0.8, edge regions × 0.9
- Infer column structure via DBSCAN on text_block x-centroids (eps = `config.layout.dbscan_eps_fraction` × page_width; default 0.08)

**Readiness check:** PaddleOCR PP-DocLayoutV2 production model loaded AND CUDA available.

IEP2A serves live layout detection using the production model only. Candidate model evaluation for promotion is handled by the asynchronous shadow evaluation pipeline.
```

---

## NEW: Section 7.1.5 — Google Document AI (Authoritative Adjudicator)

**ACTION:** ADD (new section)

```markdown
### 7.1.5 Google Document AI — Authoritative Adjudicator for Layout Disagreement

**Service:** Google Document AI (external, managed service)

**Endpoint:** `https://documentai.googleapis.com/v1/projects/{project_id}/locations/{location}/processors/{processor_id}:process`

**Role:** Final adjudicator when local layout detectors (IEP2A and IEP2B) disagree or either fails.

**Invocation trigger:** Only when:
1. IEP2A and IEP2B disagreement on layout regions (after agreement check fails)
2. IEP2A fails or returns unusable output (AND IEP2B is unavailable/also fails)
3. IEP2B fails (and IEP2A returned plausible output but low confidence)

**Request:** LayoutAdjudicationRequest

```json
{
  "job_id": "...",
  "page_number": 15,
  "image_uri": "s3://..../processed_image.tiff",
  "material_type": "book",
  "iep2a_result": {...},
  "iep2b_result": null,
  "reason": "iep2a_iep2b_disagreement"
}
```

**Response:** LayoutAdjudicationResult

```json
{
  "status": "done",
  "layout_decision_source": "google_document_ai",
  "fallback_used": true,
  "iep2a_result": {...},
  "iep2b_result": null,
  "google_document_ai_result": {...},
  "final_layout_result": {...}
}
```

**Processing:**

1. EEP submits the processed page image to Google Document AI via async HTTP call
2. Google processes the image and returns detected layout regions in its native format
3. EEP maps Google's native classes to canonical LibraryAI layout ontology
4. Canonical regions from Google become the final layout result
5. Page is accepted with `layout_decision_source="google_document_ai"`, `fallback_used=true`

**Google's native class mapping to canonical:**
- "text" → canonical "text_block"
- "heading" → canonical "title"
- "table" → canonical "table"
- "image" / "figure" → canonical "image"
- "caption" → canonical "caption"
- (other classes mapped conservatively or discarded)

**Failure handling:**

- If Google Document AI times out or returns an error: route page to `status="review"`, `review_reasons=["layout_adjudication_google_failed"]`
- If Google succeeds but returns implausible regions (e.g., empty result, malformed response): route to review with `review_reasons=["layout_adjudication_google_implausible"]`
- Timeout: configurable, default 90s (includes network latency and Google processing time)

**Configuration:**

- `google.project_id`: Google Cloud project ID
- `google.location`: Google Document AI processor location (e.g., "us")
- `google.processor_id`: Deployed Document AI processor ID for layout detection
- `google.credentials_path`: Path to Google Cloud service account JSON credentials
- `google.timeout_seconds`: Adjudication timeout (default 90s)
- `google.retry_budget`: Number of retries on transient Google API failures (default 2)

**Observability:**

- Metric: `layout_adjudication_google_invocations_total` (counter)
- Metric: `layout_adjudication_google_success_rate` (gauge)
- Metric: `layout_adjudication_google_latency_ms` (histogram)
- Log: every Google Document AI call with request/response summary

**Cost and SLA considerations:**

- Google Document AI is a metered managed service; each invocation incurs API cost
- It is invoked only on disagreement/failure (not on every page), so cost is minimized
- SLA is Google's standard (typically 99.9% availability)
- Latency is variable (typically 2-10s depending on load); the 90s timeout provides margin
```

---

## Section 7.2 — IEP2B — DocLayout-YOLO Layout Detection

**ACTION:** UPDATE (minor clarification; no major change)

```markdown
### 7.2 IEP2B — DocLayout-YOLO Layout Detection

**Port:** 8005
**Compute:** GPU (minimal)

**Endpoint:** `POST /v1/layout-detect`
**Request:** LayoutDetectRequest
**Response:** LayoutDetectResponse with `detector_type="doclayout_yolo"`

**Model:** DocLayout-YOLO with pretrained document-layout weights (DocStructBench-aligned class vocabulary), used without fine-tuning for initial deployment. IEP2B maps its native output classes to LibraryAI's canonical 5-class schema before returning `LayoutDetectResponse`. DocLayout-YOLO improves initial deployment suitability through document-specific training and multi-scale handling, but it does not imply semantic understanding of reading order or layout hierarchy.

**Purpose:** Fast second opinion. Provides a document-trained, architecturally distinct counterpoint to IEP2A. It is used to detect gross structural errors through disagreement checking and to make the layout adjudication decision meaningful (when agreement cannot be achieved locally, external adjudication is consulted).

**Postprocessing:** Apply native-to-canonical class mapping, exclude non-canonical classes, merge overlapping same-type canonical regions (IoU > 0.5, preserve higher-confidence ID), and compute canonical histograms and confidence summary.

**Readiness check:** DocLayout-YOLO model loaded AND CUDA available.

**Note:** IEP2B is optional from an infrastructure perspective. If IEP2B is unavailable (timeout, service failure), single-model mode activates: EEP proceeds with IEP2A result and consults Google Document AI for confirmation (no local adjudication possible without a second candidate). If both IEP2A and IEP2B fail, Google is consulted immediately.
```

---

## Section 7.4 — Layout Adjudication Gate (Formerly "Layout Consensus Gate")

**ACTION:** REPLACE (major revision: consensus → adjudication)

```markdown
### 7.4 Layout Adjudication Gate

**Decision principle:** Authoritative adjudication via local agreement or external consultation. Google Document AI is the final arbiter for unresolved local disagreement or failure.

**Decision logic:**

```python
def evaluate_layout_adjudication(iep2a, iep2b_or_none, google_cred, config):
    # Try local agreement first (fast path)
    if iep2b_or_none and iep2a and iep2b_agree(iep2a, iep2b_or_none):
        return LayoutAdjudicationResult(
            agreed=True,
            consensus_confidence=...,
            layout_decision_source="local_agreement",
            fallback_used=False,
            final_layout_result=iep2a.regions  # IEP2A is canonical when local agreement
        )

    # Local agreement not achieved; consult Google Document AI
    google_result = call_google_document_ai(page_image, google_cred, config.google.timeout_s)

    if google_result.success:
        return LayoutAdjudicationResult(
            agreed=False,  # local agreement not achieved
            consensus_confidence=0.0,  # not applicable
            layout_decision_source="google_document_ai",
            fallback_used=True,
            iep2a_result=iep2a,
            iep2b_result=iep2b_or_none,
            google_document_ai_result=google_result.regions,
            final_layout_result=map_google_to_canonical(google_result.regions)
        )
    else:
        # All methods failed; must route to human review
        return LayoutAdjudicationResult(
            agreed=False,
            layout_decision_source="none",
            fallback_used=True,
            status="failed",
            error="all_layout_detection_methods_failed"
        )
```

#### Local Agreement (Fast Path)

- Match regions between IEP2A and IEP2B using greedy one-to-one matching by descending IoU. Matching is performed on canonical regions after native-to-canonical class mapping inside each service. A match requires IoU ≥ `config.match_iou_threshold` (default 0.5) AND same canonical `RegionType`.
- `total = max(len(iep2a_regions), len(iep2b_regions))`
- `match_ratio = matched_regions / total`
- `type_histogram_match`: for every region type in either histogram, absolute count difference ≤ `config.max_type_count_diff` (default 1)
- `agreed = match_ratio >= config.min_match_ratio (0.7) AND type_histogram_match`
- When agreed: use IEP2A (PP-DocLayoutV2) regions as canonical layout. **Accept immediately (fast path). No external consultation needed.**

#### Disagreement or Failure Path (External Adjudication)

When agreed == False OR either IEP2A/IEP2B fails:

- Invoke Google Document AI synchronously
- Google processes the page and returns layout regions in its native format
- Map Google's classes to canonical LibraryAI ontology
- Google's result becomes the final canonical layout
- **Accept with `layout_decision_source="google_document_ai"`, `fallback_used=true`**

#### All Methods Failed (No Acceptance Path)

If Google Document AI also fails:

- Route page to `status="review"`, `review_reasons=["layout_adjudication_failed"]`
- Human reviewer must decide the layout manually

#### LayoutAdjudicationResult schema

| Field | Type | Notes |
|-------|------|-------|
| agreed | bool | True if local IEP2A + IEP2B agreement achieved (fast path) |
| consensus_confidence | float | 0.6\*match_ratio + 0.2\*mean_iou + 0.2\*histogram_match (only if agreed=True) |
| layout_decision_source | Literal["local_agreement", "google_document_ai", "none"] | Which system made the final decision |
| fallback_used | bool | True if Google Document AI was consulted |
| iep2a_result | LayoutDetectResponse \| None | IEP2A output (always present when called) |
| iep2b_result | LayoutDetectResponse \| None | IEP2B output (None if unavailable) |
| google_document_ai_result | dict \| None | Google Document AI raw response (if consulted) |
| final_layout_result | list[Region] | Final canonical regions (IEP2A if local agreement; mapped from Google if Google consulted) |
| status | Literal["done", "failed"] | "done" if successful acceptance path; "failed" if all methods failed |
| error | str \| None | Error description if status="failed" |

#### Single-model fallback semantics (IEP2B unavailable)

When IEP2B is unavailable or times out:

- `iep2b_result = None`
- `agreed = False` (no local agreement possible with only one detector)
- Immediately consult Google Document AI as final adjudicator
- Google's result becomes the canonical layout (no comparison to IEP2A required; Google is authoritative)
- Accept with `layout_decision_source="google_document_ai"`, `fallback_used=true`
- If Google also fails, route to review

This ensures **no single local detector can achieve acceptance without external confirmation**.
```

---

## Section 8.2 — Full Process — process_page()

**ACTION:** UPDATE (Steps 9-13 completely rewritten for adjudication)

Replace Steps 9-13 with:

```markdown
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

10. Invoke IEP2A (PaddleOCR PP-DocLayoutV2) via GPU backend:
    iep2a_result = gpu_backend.invoke(component="iep2a", ...)
    If IEP2A fails or returns unusable output:
      → mark iep2a_result = None
      → proceed to Step 11 (attempt IEP2B)
      → (if IEP2B also fails, skip to Step 12 for Google adjudication)

    Shadow enqueue (best effort):
    Apply all three conditions:
    (a) Sampling: sha256(f"{job_id}:{page_number}") % 100 < shadow_fraction×100
    (b) shadow_mode == True
    (c) Staging candidate exists (from in-memory background-refreshed cache)
    If all three pass: push shadow task to libraryai:shadow_tasks.
    Failure to enqueue must not affect live routing.

11. Attempt IEP2B (DocLayout-YOLO) if IEP2A returned plausible output:
    If iep2a_result is valid:
        iep2b_result = gpu_backend.invoke(component="iep2b", ...)
    Else:
        iep2b_result = None  (skip IEP2B; will proceed directly to Google)
    If IEP2B unavailable/fails: iep2b_result = None (single-model mode).

12. Run layout adjudication gate:
    result = evaluate_layout_adjudication(iep2a, iep2b_or_none, config)

    Adjudication has three possible outcomes:

    OUTCOME A: Local agreement (fast path)
    ─────────────────────────────────────
    If result.agreed == True:
      → proceed to Step 13 (accept)

    OUTCOME B: Local disagreement → Google adjudication
    ──────────────────────────────────────────────────
    If result.agreed == False AND (iep2a_result and iep2b_result both present):
      → Google Document AI is consulted (asynchronous HTTP call)
      → If Google succeeds:
          canonical layout = mapped Google result
          proceed to Step 13 (accept)
      → If Google times out or fails:
          status="review", review_reasons=["layout_adjudication_google_failed"]
          return

    OUTCOME C: IEP2A/IEP2B failure → Google adjudication
    ──────────────────────────────────────────────────
    If iep2a_result == None OR (iep2a_result and iep2b_result both None):
      → Google Document AI is consulted (required for confirmation)
      → If Google succeeds:
          canonical layout = mapped Google result
          proceed to Step 13 (accept)
      → If Google also fails:
          status="review", review_reasons=["layout_adjudication_failed"]
          return

13. Persist artifacts (DB-first write order):
    For each artifact type (preprocessed, layout):
    (1) BEGIN transaction → set artifact_state='pending' → COMMIT
    (2) Write artifact to S3 at deterministic path
    (3) UPDATE: set output_layout_uri, set artifact_state='confirmed'

    Record adjudication metadata:
    - layout_decision_source: "local_agreement" | "google_document_ai"
    - fallback_used: True (Google consulted) | False (local agreement only)
    - layout_adjudication_confidence: confidence score if local agreement; null if Google

    Update eep_auto_accept_rate Gauge (observability only).

    Update status to "accepted", routing_path="preprocessing_layout".
```

---

## Section 8.4 — Acceptance Policy Configuration

**ACTION:** UPDATE (add Google Document AI config)

Add new Google Document AI section to ConfigMap:

```yaml
google:
  enabled: true                        # toggle to disable Google calls (for testing)
  project_id: "libraryai-prod"         # Google Cloud project ID
  location: "us"                        # Document AI processor location
  processor_id: "..."                   # Production processor ID from Document AI setup
  credentials_path: "/var/secrets/google/key.json"  # Path to service account credentials
  timeout_seconds: 90                   # Adjudication timeout (includes network + processing)
  retry_budget: 2                       # Retries on transient Google API failures
  max_retries: 3                        # Max total attempts (including initial try)
  fallback_on_timeout: true             # Route to review if Google times out (do not accept)

layout:
  min_consensus_confidence: 0.6         # Minimum local-agreement confidence for fast acceptance
  match_iou_threshold: 0.5              # Region matching IoU threshold
  min_match_ratio: 0.7                  # Minimum matched-region ratio for agreement
  max_type_count_diff: 1                # Max type-histogram difference for agreement
  dbscan_eps_fraction: 0.08             # Column-boundary inference parameter
  require_google_confirmation_on_single_model: true  # Must consult Google if IEP2B unavailable
```

Update observability metrics section to include Google adjudication:

```yaml
metrics:
  # Existing metrics...
  - name: layout_adjudication_google_invocations_total  # Counter
  - name: layout_adjudication_google_success_rate       # Gauge
  - name: layout_adjudication_google_latency_ms         # Histogram
  - name: layout_adjudication_local_agreement_rate      # Gauge (% of layout decisions from local agreement)
  - name: layout_decision_source_distribution           # Gauge (split by local_agreement | google_document_ai)
```
```

---

## Section 8.5 — Review Reasons (Updated)

**ACTION:** UPDATE (add new review reasons for adjudication failures)

Add to the review reasons table:

```markdown
| Value | Set by | Meaning |
|-------|--------|---------|
| "layout_adjudication_google_failed" | EEP step 12 (Google fallback) | Google Document AI call failed (timeout, error, or bad response) |
| "layout_adjudication_google_implausible" | EEP step 12 (Google fallback) | Google Document AI returned invalid/empty layout |
| "layout_adjudication_failed" | EEP step 12 (all methods) | Both local detectors AND Google Document AI failed; no layout decision possible |
| "layout_single_model_requires_google" | EEP step 11 (IEP2B unavailable) | IEP2B unavailable; local agreement impossible; Google consultation required |
```

---

## NEW: Section 7.5 — LayoutAdjudicationResult Schema

**ACTION:** ADD

```markdown
### 7.5 LayoutAdjudicationResult Schema

This schema encompasses the output of the layout adjudication gate (Section 7.4). It replaces the previous `LayoutConsensusResult` and expands it to include Google Document AI fallback information.

#### LayoutAdjudicationResult

| Field | Type | Notes |
|-------|------|-------|
| agreed | bool | True if local IEP2A + IEP2B agreement achieved without external consultation |
| consensus_confidence | float | Confidence score if agreed=True; computed as 0.6\*match_ratio + 0.2\*mean_iou + 0.2\*type_match. Null if agreed=False. |
| layout_decision_source | Literal["local_agreement", "google_document_ai", "none"] | Which system determined the final layout |
| fallback_used | bool | True if Google Document AI was called (indicates local agreement or single-model failure) |
| iep2a_region_count | int | Number of regions detected by IEP2A |
| iep2b_region_count | int \| None | Number of regions detected by IEP2B (None if unavailable) |
| iep2a_result | LayoutDetectResponse \| None | Full IEP2A response (present if called) |
| iep2b_result | LayoutDetectResponse \| None | Full IEP2B response (None if unavailable) |
| matched_regions | int | (Only if agreed=True) Regions matching between IEP2A and IEP2B |
| mean_matched_iou | float | (Only if agreed=True) Mean IoU of matched regions |
| type_histogram_match | bool | (Only if agreed=True) Type-count agreement check result |
| google_document_ai_result | dict \| None | Google's native response, if consulted (includes raw regions and metadata) |
| final_layout_result | list[Region] | Canonical LibraryAI regions (source: IEP2A if local agreement; mapped from Google if Google called) |
| status | Literal["done", "failed"] | "done" if page should be accepted; "failed" if all methods failed |
| error | str \| None | Error message if status="failed" |
| processing_time_ms | float | Total time for adjudication (includes all IEP2 calls and Google call if applicable) |

#### LayoutDetectResponse modifications

The existing `LayoutDetectResponse` schema remains unchanged. `detector_type` field values are:
- `"paddleocr_pp_doclayout_v2"` (was: `"detectron2"`)
- `"doclayout_yolo"`

#### LayoutAdjudicationRequest

Request schema for Google Document AI fallback:

| Field | Type | Notes |
|-------|------|-------|
| job_id | str | none |
| page_number | int | none |
| image_uri | str | Processed page image URI (for Google to analyze) |
| material_type | Literal["book", "newspaper", "archival_document"] | Hint to Google processor |
| iep2a_result | LayoutDetectResponse \| None | IEP2A output (for logging/audit) |
| iep2b_result | LayoutDetectResponse \| None | IEP2B output (for logging/audit) |
| reason | Literal["local_disagreement", "iep2a_failed", "iep2b_failed", "both_failed"] | Why Google was called |
```

---

# 4. New or Updated Schema Definitions

## Summary of Schema Changes

1. **LayoutAdjudicationResult** — NEW (replaces LayoutConsensusResult)
   - Tracks agreement status, fallback invocation, decision source
   - Includes fields for all three result sources (IEP2A, IEP2B, Google)
   - Status field indicates success/failure for routing

2. **LayoutDetectResponse** — UPDATED
   - `detector_type` values changed:
     - Old: `"detectron2"`
     - New: `"paddleocr_pp_doclayout_v2"` (IEP2A)
     - Unchanged: `"doclayout_yolo"` (IEP2B)

3. **LayoutAdjudicationRequest** — NEW
   - Request sent to Google Document AI (or logged for audit)
   - Includes page image URI, material type, IEP2A/2B results, reason

4. **Page lineage fields** — ADD
   - `layout_decision_source: Literal["local_agreement", "google_document_ai"]`
   - `layout_fallback_used: bool`
   - `layout_adjudication_confidence: float | None` (confidence if local agreement; None if Google)
   - `google_document_ai_response_time_ms: int | None` (latency if Google consulted)

---

# 5. Migration Gap Analysis

## IEP0 Document-Type Classification Migration Gap

**Desired spec state:**
- IEP0 service runs on upload before IEP1
- Classifies document type: book | newspaper | archival_document
- Returns predicted_material_type + confidence
- Stores prediction in job metadata
- Downstream IEP1/IEP2 uses predicted type for threshold adaptation

**Current likely implementation state:**
- ❌ **No IEP0 service exists** (new feature)
- ⚠️ Material-type is currently set by user selection in upload UI
- ⚠️ No automated document classification pipeline

**What still needs to be done:**
1. ❌ **Model training and development**
   - Curate training corpus from LibraryAI's historical jobs
   - Train lightweight Vision Transformer or EfficientNet on 3-class classification
   - Baseline accuracy: ≥90% F1 per class
   - Export model to ONNX or TensorFlow format

2. ❌ **IEP0 service implementation**
   - Create [services/iep0/app/main.py](services/iep0/) (new service)
   - Implement `POST /v1/classify` endpoint
   - Load model and inference logic
   - Return DocumentClassificationResponse with predicted_material_type + confidence + class_scores
   - Implement timeout (30s default) and error handling

3. ⚠️ **Upload workflow integration**
   - Update upload endpoint to invoke IEP0 before routing to EEP
   - Store predicted_material_type + confidence in job metadata
   - Fallback: if IEP0 fails or is unavailable, use default material_type="book"

4. ❌ **IEP1/IEP2 threshold configuration**
   - Update [libraryai-policy ConfigMap](libraryai-policy ConfigMap) to define material-type-specific thresholds (geometry confidence, layout region density, etc.)
   - IEP1/EEP worker uses predicted_material_type to select thresholds
   - Example: "book" mode uses stricter split_confidence; "newspaper" mode uses looser region density checks

5. ✅ **Tests**
   - Unit test IEP0 inference on sample images
   - Test classification accuracy on held-out corpus
   - Test fallback behavior (timeout, error)
   - Integration test: upload → IEP0 classification → EEP → expected routing

**Effort estimate:** M (10-14 hours)
- Model training: 4-6 hours
- Service implementation: 3-4 hours
- Integration + config: 2-3 hours
- Testing: 2-3 hours

---

## IEP2A Migration Gap

**Desired spec state:**
- IEP2A service runs PaddleOCR PP-DocLayoutV2 instead of Detectron2
- Response schema identical (LayoutDetectResponse) but `detector_type="paddleocr_pp_doclayout_v2"`
- Native class mapping from PP-DocLayoutV2 to canonical 5-class ontology
- Endpoints, request/response schemas otherwise unchanged

**Current likely implementation state:**
- [services/iep2a/app/main.py](services/iep2a/app/) exists
- Detectron2 backend [services/iep2a/app/backends/detectron2_backend.py](services/iep2a/app/backends/detectron2_backend.py) exists (from audit)
- PaddleOCR backend [services/iep2a/app/backends/paddleocr_backend.py](services/iep2a/app/backends/paddleocr_backend.py) **already exists** (discovered in audit)
- Factory pattern via env var [IEP2A_LAYOUT_BACKEND](IEP2A_LAYOUT_BACKEND) controls which backend is used
- Test coverage exists for both backends

**What still needs to be done:**
1. ✅ **Code change:** Change default backend from Detectron2 → PaddleOCR
   - Update [IEP2A_LAYOUT_BACKEND](IEP2A_LAYOUT_BACKEND) default in [services/iep2a/app/main.py](services/iep2a/app/) from `"detectron2"` to `"paddleocr"`
   - OR: If Detectron2 backend is no longer needed, remove it entirely and make PaddleOCR the only backend
2. ✅ **Tests:** Run existing tests against PaddleOCR; verify test_iep2a_backends.py passes with PaddleOCR default
3. ⚠️ **Model weights:** Ensure PP-DocLayoutV2 pretrained weights are available
   - Likely: Weights will be downloaded at container startup or baked into image in Phase 11
4. ✅ **Documentation:** Update README to note that IEP2A uses PaddleOCR, not Detectron2
5. ⚠️ **Performance baseline:** Measure PaddleOCR inference latency on typical pages; verify 60s timeout is sufficient

**Effort estimate:** S (2-3 hours code change + testing; longer if model weight sourcing is an issue)

---

## IEP2 Adjudication Migration Gap

**Desired spec state:**
- Local agreement (IEP2A + IEP2B agree) → accept (fast path)
- Local disagreement OR IEP2 failure → call Google Document AI
- Google success → accept with `layout_decision_source="google_document_ai"`
- All fail → route to review with `review_reasons=["layout_adjudication_failed"]`
- New response schema: `LayoutAdjudicationResult` (not `LayoutConsensusResult`)

**Current likely implementation state:**
- [services/eep/app/gates/layout_gate.py](services/eep/app/gates/layout_gate.py) exists with consensus logic
- Steps 9-13 in [services/eep_worker/app/task.py](services/eep_worker/app/) implement "consensus gate" routing
- **No Google Document AI integration exists** (not in audit findings)
- `LayoutConsensusResult` schema returned (not `LayoutAdjudicationResult`)
- No fallback path for disagreement

**What still needs to be done:**
1. ❌ **Code structure:** Rewrite layout gate evaluation function
   - Rename/extend [services/eep/app/gates/layout_gate.py](services/eep/app/gates/layout_gate.py) to handle adjudication
   - OR: Create new [services/eep/app/gates/layout_adjudication.py](services/eep/app/gates/layout_adjudication.py)
   - Change entry point from `evaluate_layout_consensus()` to `evaluate_layout_adjudication()`
   - Add Google Document AI invocation logic (with retry, timeout, error handling)

2. ❌ **Google Document AI integration** (NEW, critical)
   - Create Google Document AI wrapper service or utility module
   - Handle authentication (service account JSON credentials from Kubernetes Secret)
   - Implement HTTP call to `documentai.googleapis.com/v1/projects/.../processors/.../process`
   - Implement native-to-canonical class mapping for Google's response
   - Implement retry logic (exponential backoff, max retries)
   - Implement timeout handling (90s timeout, distinguish transient vs permanent failures)
   - Log all Google calls for observability (latency, success rate, error details)
   - Handle missing/disabled Google configuration gracefully (disable fallback if credentials missing)

3. ❌ **Worker orchestration changes** (critical)
   - Update [services/eep_worker/app/task.py](services/eep_worker/app/) steps 9-13 to implement new adjudication logic
   - Add Google Document AI call before routing to review (new step after Step 11 disagreement check)
   - Update state transitions: no longer auto-accept on disagreement; must await Google result
   - Update review reasons: add "layout_adjudication_google_failed", "layout_adjudication_failed", others

4. ⚠️ **Schema changes** (critical)
   - Update lineage to record `layout_decision_source` (local_agreement | google_document_ai)
   - Add `layout_fallback_used` boolean flag
   - Add `layout_adjudication_confidence` field (only if local agreement)
   - Add `google_document_ai_response_time_ms` field
   - Update database schema (if layout decision metadata is persisted as JSONB)
   - Update tests to verify new schema fields

5. ⚠️ **Configuration management** (critical)
   - Add Google Document AI credentials to Kubernetes Secrets
   - Add config params to [libraryai-policy ConfigMap](libraryai-policy ConfigMap) (project_id, location, processor_id, timeouts)
   - Implement config loading for Google credentials (from file or K8s Secret)
   - Implement feature toggle to disable Google calls (for testing/development)

6. ✅ **Tests** (critical)
   - Test local agreement fast path: IEP2A + IEP2B agree → accept without Google (already exists?)
   - Test local disagreement path: IEP2A + IEP2B disagree → call Google → accept (NEW)
   - Test IEP2A failure: IEP2A fails, IEP2B succeeds → call Google for confirmation (NEW)
   - Test both fail: IEP2A + IEP2B fail → call Google (NEW)
   - Test Google success: Google returns valid layout → accept (NEW)
   - Test Google timeout: Google times out → route to review (NEW)
   - Test Google failure: Google returns error → route to review (NEW)
   - Mock Google Document AI API for testing (don't call real Google API in test suite)
   - Existing consensus tests may need rewriting or repositioning

7. ⚠️ **Logging/observability** (important)
   - Metrics: Google invocation rate, success rate, latency (histogram)
   - Metrics: decision_source distribution (local_agreement vs google_document_ai)
   - Logs: every Google call with request/response digest
   - Alerts: if Google Document AI success rate drops below SLO

**Effort estimate:** L (16-24 hours)
- Google wrapper: 4-6h
- Worker orchestration changes: 4-6h
- Schema updates: 2-3h
- Tests: 4-6h
- Config/ops setup: 2-3h

---

## Google Document AI Integration Gap

**Desired spec state:**
- EEP can call Google Document AI REST API
- Credentials and configuration are injected from Kubernetes Secrets/ConfigMaps
- Timeout is 90s; retries are automatic (max 2 retries)
- Responses are mapped to canonical layout ontology
- Failures are logged and routed to review

**Current likely implementation state:**
- ❌ No Google Document AI client code exists
- ❌ No Google credentials loading
- ❌ No configuration for Google endpoint/processor/project

**What still needs to be done:**
1. ❌ **Authentication and credentials:**
   - Set up Google Cloud service account with Document AI User role
   - Store service account JSON in Kubernetes Secret `google-documentai-sa`
   - Mount secret in eep_worker Pod at `/var/secrets/google/key.json`
   - Implement credential loading and Google API client initialization in EEP worker startup

2. ❌ **HTTP client**:
   - Create wrapper module: `services/eep/app/google/document_ai.py`
   - Implement `call_google_document_ai(image_uri, processor_id, timeout, retry_budget)` function
   - Use Google Cloud client library (google-cloud-documentai) or httpx for direct API calls
   - Implement retry logic: exponential backoff (1s, 2s, 4s, ...), max 2 retries on transient errors
   - Implement timeout: fail-fast if exceeds 90s
   - Implement error classification: distinguish transient (network, timeout) vs permanent (bad credentials, invalid request)

3. ❌ **Class mapping:**
   - Implement mapper: Google native classes → canonical LibraryAI Region types
   - Handle unmapped classes: map conservatively (headers/footers → text_block) or discard
   - Preserve confidence scores from Google's response

4. ❌ **Observability:**
   - Log every Google call (processor_id, image_size, latency, status, error if any)
   - Emit metrics: invocation rate, success rate, latency histogram
   - Alert if success rate drops below 99% (or configured SLO)

5. ⚠️ **Testing:**
   - Mock or stub Google Document AI responses for unit tests (never call real API in CI)
   - Provide test fixtures with sample Google API responses
   - Test error paths: timeout, bad credentials, invalid request, rate limit

**Effort estimate:** M (8-12 hours)

---

## IEP1 Rescue Gap

**Desired spec state:**
- External assist (optional future): external OCR/cleanup service can be consulted before second geometry pass on very difficult pages
- If used: must NOT bypass final two-model agreement gate
- Second-pass IEP1A + IEP1B must still agree; external geometry is advisory only

**Current likely implementation state:**
- ✅ IEP1D (UVDoc) is implemented
- ✅ Second geometry pass is implemented
- ❌ Optional external OCR/cleanup service: not implemented (and not required for initial version)

**What still needs to be done:**
1. ✅ **Do not change:** IEP1D (UVDoc) is already in spec and code
2. ✅ **Do not implement:** External OCR/cleanup service is marked as "future" in updated spec; do not build it now
3. ✅ Documentation updated to clarify IEP1 remains internally authoritative

**Effort estimate:** 0 hours (no new work required; just clarify in docs)

---

## IEP1D Rectification Model Gap

**Desired spec state:**
- Officially recommend UVDoc for geometric rectification
- Document why UVDoc is the best fit

**Current likely implementation state:**
- ✅ Code appears to use UVDoc (services/iep1d/ exists based on earlier context)
- ⚠️ Spec may not explicitly recommend UVDoc; it may say "rectification fallback" without naming the model

**What still needs to be done:**
1. ✅ Update spec Section 6.5 to officially name and recommend UVDoc (DONE in this update)
2. ✅ If code currently uses something else, verify and update to UVDoc
3. ⚠️ Ensure model weights are available (downloaded at startup or baked into image in Phase 11)

**Effort estimate:** S (1-2 hours if only doc update; longer if code change needed)

---

## Schema / DB / Lineage Gap

**Desired spec state:**
- `LayoutAdjudicationResult` replaces `LayoutConsensusResult`
- Page lineage tracks `layout_decision_source`, `layout_fallback_used`, `layout_adjudication_confidence`
- Layout artifacts stored with metadata about adjudication

**Current likely implementation state:**
- ✅ `LayoutConsensusResult` schema exists (from audit)
- ⚠️ Page lineage may not track layout_decision_source or fallback_used
- ❓ Database migrations may not have fields for Google integration

**What still needs to be done:**
1. ✅ **Schema definition:**
   - Add `LayoutAdjudicationResult` to [shared/schemas/layout.py](shared/schemas/layout.py)
   - Keep `LayoutConsensusResult` for backward compatibility (or deprecate with clear migration path)
   - Update `LayoutDetectResponse.detector_type` enum to include `"paddleocr_pp_doclayout_v2"`

2. ⚠️ **Page lineage tracking:**
   - Add fields to page_lineage JSONB stored in database:
     - `layout_decision_source: str` (local_agreement | google_document_ai)
     - `layout_fallback_used: bool`
     - `layout_adjudication_confidence: float | null`
     - `google_document_ai_response_time_ms: int | null`
   - Update EEP worker to populate these fields when recording layout result

3. ⚠️ **Database migration (Alembic):**
   - Add column or extend JSONB schema for layout decision metadata
   - Create migration script (e.g., `0004_add_layout_adjudication_tracking.py`)
   - Ensure migration is backward-compatible

4. ✅ **Tests:**
   - Verify lineage fields are correctly populated
   - Test with local agreement (no Google, confidence is high)
   - Test with Google fallback (fallback_used=True, confidence is null)

**Effort estimate:** M (6-8 hours)

---

## Worker Orchestration Gap

**Desired spec state:**
- Steps 9-13 implemented as adjudication (not consensus)
- Google Document AI call integrated into decision flow
- New review reasons handled
- Fallback path clear and tested

**Current likely implementation state:**
- ✅ Steps 1-8 (preprocessing) working
- ✅ Steps 9-10 (IEP2A, shadow enqueue) working
- ✅ Steps 11-13 (consensus gate) working, but must be refactored for adjudication
- ❌ Google Document AI integration missing (covered above)

**What still needs to be done:**
- Already covered in "IEP2 Adjudication Migration Gap" section
- Key changes:
  - Rewrite step 12 evaluation logic
  - Add Google call before step 13 on disagreement
  - Update review reason assignment

**Effort estimate:** Covered in IEP2 adjudication section

---

## Metrics / Observability Gap

**Desired spec state:**
- Metrics track Google Document AI invocation rate, success rate, latency
- Metrics track decision source distribution (local agreement vs Google)
- Alerts if Google success rate drops below SLO
- Logs include Google request/response digests

**Current likely implementation state:**
- ✅ Basic EEP metrics exist (auto_accept_rate, etc.)
- ❌ No Google Document AI metrics
- ❌ No decision source tracking

**What still needs to be done:**
1. ❌ **Metrics (Prometheus):**
   - Counter: `layout_adjudication_google_invocations_total` (by status: success, timeout, error)
   - Gauge: `layout_adjudication_google_success_rate` (rolling window)
   - Histogram: `layout_adjudication_google_latency_ms` (p50, p95, p99)
   - Gauge: `layout_decision_source_distribution` (split by local_agreement | google_document_ai)

2. ❌ **Alerts:**
   - Alert if `layout_adjudication_google_success_rate` drops below 99%
   - Alert if `layout_adjudication_google_latency_ms` (p95) exceeds 60s consistently
   - Alert if Google invocation errors exceed 5% over 5-min window

3. ⚠️ **Logging:**
   - When Google Document AI is called: log request payload (processor_id, image ID), response status, latency, result summary
   - When Google fails: log error details
   - Integrate with eep_worker logging infrastructure

4. ✅ **Test observability:**
   - Verify metrics are emitted correctly
   - Mock Prometheus registry in tests

**Effort estimate:** M (4-6 hours)

---

## Tests Gap

**Desired spec state:**
- Comprehensive test coverage for new adjudication paths
- Google Document AI calls mocked in tests (never hit real API)
- All review reasons tested
- Edge cases (timeout, partial response, etc.) covered

**Current likely implementation state:**
- ✅ Consensus gate tests exist ([tests/test_p6_layout_consensus.py](tests/test_p6_layout_consensus.py) or similar)
- ✅ Layout integration tests exist ([tests/test_p6_layout_integration.py](tests/test_p6_layout_integration.py))
- ❌ Google Document AI path tests missing
- ❌ Adjudication tests missing (will likely need new test file)

**What still needs to be done:**
1. ❌ **New test file:**
   - Create [tests/test_layout_adjudication.py](tests/test_layout_adjudication.py) (or integrate into existing test_p6_*.py files)
   - Test all decision paths defined in spec Section 7.4

2. ⚠️ **Test coverage:**
   - Local agreement fast path: IEP2A + IEP2B agree, accept without Google (existing test, may pass)
   - Local disagreement → Google: IEP2A + IEP2B disagree, call Google, accept (NEW)
   - IEP2A fails → Google: IEP2A fails, IEP2B succeeds, call Google for confirmation (NEW)
   - Both fail → Google: IEP2A + IEP2B fail, call Google (NEW)
   - Google success: all paths that call Google and it succeeds (NEW, all tests above)
   - Google timeout: Google times out → route to review (NEW)
   - Google error: Google returns error → route to review (NEW)
   - Google bad response: Google returns empty/malformed layout → route to review (NEW)
   - PaddleOCR backend: verify IEP2A still returns correct schema with detector_type='paddleocr_pp_doclayout_v2' (NEW or existing)

3. ✅ **Mock Google Document AI:**
   - Create mock/stub Google Document AI client returning sample LayoutAdjudicationResult
   - Inject mock into EEP worker for testing
   - Vary mock responses: success, timeout, error, empty result

4. ⚠️ **Integration tests:**
   - End-to-end test: document → preprocessed → layout detection (local agreement) → accepted
   - End-to-end test: document → preprocessed → layout detection (disagreement) → Google → accepted
   - End-to-end test: document → preprocessed → layout detection (all fail) → review

**Effort estimate:** L (12-16 hours)

---

## Docs / Checklist Drift Gap

**Desired spec state:**
- Updated full_updated_spec.md reflects new IEP2A backend and adjudication design
- implementation_checklist reflects Phases 0-12 status against updated spec
- README notes IEP2A uses PaddleOCR, explains Google fallback
- Any architecture docs updated

**Current likely implementation state:**
- ❓ full_updated_spec.md describes Detectron2, consensus logic (outdated after this update)
- ✅ implementation_checklist.md exists (from audit; status mixed)
- ⚠️ README likely needs update (mentions Detectron2 or doesn't mention Google)
- ❓ No separate architecture docs found in audit

**What still needs to be done:**
1. ✅ **Spec update:**
   - This document replaces/updates full_updated_spec.md (DONE in this response)

2. ⚠️ **Checklist update:**
   - Phase 6: Note IEP2A is now PaddleOCR (was Detectron2); add checkpoint for Google integration
   - Phase 11: Add checkpoint for Google secrets/credentials setup
   - Phase 12: No change

3. ⚠️ **README update:**
   - Section on IEP2: explain new PaddleOCR P-DocLayoutV2 backend
   - Section on layout detection: explain local agreement + Google fallback
   - Section on configuration: note Google Document AI setup (processor ID, credentials)
   - Section on deployment: explain Google credentials as Kubernetes Secret

4. ⚠️ **Architecture decision records (ADRs):**
   - Consider documenting why PaddleOCR was chosen (document-specific, multi-language, Arabic support)
   - Consider documenting why Google Document AI is used (external de-risking, no fine-tuning required)
   - Consider documenting why IEP1 remains internally authoritative (no external geometry, only layout can fallback)

**Effort estimate:** M (4-6 hours)

---

# 6. Ordered Implementation Work Needed To Match This Spec

## Priority Tier: Critical Path (Blocks Production)

### 1. Change IEP2A Backend: Detectron2 → PaddleOCR PP-DocLayoutV2
**Why:** IEP2A must run the new model before any other IEP2 work can proceed
**Affected files:** [services/iep2a/](services/iep2a/)
**Work:**
- Verify PaddleOCR backend code exists or write it
- Change default backend from Detectron2 → PaddleOCR
- Update detector_type response field to `"paddleocr_pp_doclayout_v2"`
- Run existing tests against PaddleOCR; verify contract unchanged
- Verify model weights availability
**Duration:** 2-3 hours
**Dependencies:** None
**Blocking:** IEP2 adjudication logic (item #4)

### 2. Implement IEP0 Document-Type Classification Service
**Why:** Automated material-type classification before IEP1; replaces manual selection
**Affected files:** New [services/iep0/](services/iep0/), upload endpoint, threshold config
**Work:**
- Train lightweight Vision Transformer / EfficientNet on 3-class corpus (book, newspaper, archival_document)
- Create [services/iep0/app/main.py](services/iep0/) service with `POST /v1/classify` endpoint
- Implement inference pipeline (proxy image input, return predicted_material_type + confidence + class_scores)
- Implement timeout (30s default) and error handling (fallback to "book" if fails)
- Update upload endpoint to invoke IEP0 before routing to EEP
- Store predicted_material_type + confidence in job metadata
- Unit tests: classification accuracy on held-out corpus
- Integration tests: upload → IEP0 → EEP flow
**Duration:** 10-14 hours
**Dependencies:** None (parallel with all)
**Blocking:** Item #5 (IEP1/IEP2 threshold adaptation)

### 3. Create Google Document AI Integration Module
**Why:** Needed for both IEP2 adjudication and IEP1 external cleanup fallback
**Affected files:** New [services/eep/app/google/document_ai.py](services/eep/app/google/document_ai.py), credentials setup
**Work:**
- Create service account and obtain credentials (outside code, required before testing)
- Implement `CallGoogleDocumentAI` class with:
  - Google Cloud API client initialization
  - HTTP request assembly (processor_id, image URI)
  - Response parsing and native-to-canonical class mapping
  - Retry logic (exponential backoff, max 2 retries)
  - Timeout handling (90s for layout, 120s for cleanup; distinguish transient vs permanent)
  - Error classification and logging
- Unit tests with mocked Google API (sample responses)
- Verify credentials can be loaded from Kubernetes Secret
**Duration:** 8-10 hours
**Dependencies:** None
**Blocking:** Item #4 (IEP2 adjudication), Item #5 (IEP1 external cleanup)

### 4. Refactor Layout Gate: Consensus → Adjudication
**Why:** Core logic change enabling IEP2 fallback behavior via Google
**Affected files:** [services/eep/app/gates/layout_gate.py](services/eep/app/gates/layout_gate.py)
**Work:**
- Rename or extend function: `evaluate_layout_consensus()` → `evaluate_layout_adjudication()`
- Implement new decision logic (Section 7.4):
  - Fast path: check local agreement
  - Fallback path: call Google on disagreement/failure
  - All-fail path: return `status="failed"` for routing to review
- Return new schema: `LayoutAdjudicationResult` (not `LayoutConsensusResult`)
- Add config parameters (match_iou_threshold, min_match_ratio, etc.)
- Unit tests for all three decision paths
**Duration:** 4-6 hours
**Dependencies:** Items #1, #3
**Blocking:** Item #6

### 5. Update IEP1 Worker: External Cleanup Escalation
**Why:** Integrate mandatory external cleanup fallback after IEP1D rectification fails
**Affected files:** [services/eep_worker/app/task.py](services/eep_worker/app/) (steps 1-8, IEP1 stages)
**Work:**
- Rewrite IEP1 rescue cascade (Section 5.2, Steps 1-8):
  - Pass 1: Standard geometry → validate
  - Pass 2 (post-IEP1D rectification): Re-run geometry → validate
  - Pass 3 (post-external cleanup): Re-run geometry → validate, or route to human
- Integrate Google Document AI call for external cleanup (when IEP1D fails or Pass 2 validation fails)
- Update state transitions and logic for three-pass pipeline
- Update review reasons: add rescue-related reasons
- Update page lineage to record which rescue stage was reached
- Integration tests for all rescue paths
**Duration:** 6-8 hours
**Dependencies:** Items #1, #3
**Blocking:** Item #8

### 6. Update EEP Worker: Integrate IEP2 Adjudication Logic into Steps 9-13
**Why:** Worker must call new adjudication logic and handle Google fallback for layout
**Affected files:** [services/eep_worker/app/task.py](services/eep_worker/app/task.py) (steps 9-13, layout stages)
**Work:**
- Rewrite Steps 9-13 (layout detection and routing):
  - Step 10: IEP2A invocation (unchanged call, but handle failure)
  - Step 11: IEP2B invocation (unchanged call)
  - Step 12: Call `evaluate_layout_adjudication()` (new)
  - Step 13: Route based on adjudication result (local agreement) or call Google (disagreement/failure)
- Add new review reasons: "layout_adjudication_google_failed", "layout_adjudication_failed", etc.
- Integrate Google Document AI call: pass iep2a_result, iep2b_result, page image URI to adjudication logic
- Update page lineage to record decision_source, fallback_used, confidence
- Error handling: timeout, Google unavailable, credentials missing
- Integration tests covering all paths
**Duration:** 6-8 hours
**Dependencies:** Items #1, #3, #4
**Blocking:** Item #8

### 7. Update Database Schema and Lineage
**Why:** Must persist metadata about adjudication and rescue decisions
**Affected files:** [services/eep/app/db/models.py](services/eep/app/db/models.py), Alembic migration
**Work:**
- Add JSONB fields to page_lineage:
  - IEP1 rescue tracking: `iep1_rescue_stage` (none, rectification, external_cleanup)
  - IEP2 adjudication: `layout_decision_source` (local_agreement | google_document_ai)
  - `layout_fallback_used: bool`
  - `layout_adjudication_confidence: float | None`
  - `google_document_ai_response_time_ms: int | None`
- Create Alembic migration (backward-compatible)
- Update ORM model if needed
- Data validation: tests verify fields populated correctly
**Duration:** 3-4 hours
**Dependencies:** Items #5, #6
**Blocking:** Item #8

### 8. Update Threshold Configuration for IEP0 Material-Type Awareness
**Why:** IEP1/IEP2 must use material-type-specific thresholds based on IEP0 prediction
**Affected files:** [libraryai-policy ConfigMap](libraryai-policy ConfigMap), EEP threshold loading logic
**Work:**
- Define threshold profiles per material_type (book, newspaper, archival_document):
  - Book: stricter split_confidence, denser geometry expectations
  - Newspaper: looser split_confidence, column-aware region density
  - Archival: variable, more permissive geometry bounds
- Add config structure to ConfigMap with threshold definitions
- Update EEP to load thresholds based on job's predicted_material_type
- Update IEP1/IEP2 gates to use selected thresholds
- Default thresholds for fallback case (IEP0 unavailable/failed)
- Integration tests: verify correct thresholds applied per type
**Duration:** 3-4 hours
**Dependencies:** Item #2 (IEP0 classification available)
**Blocking:** Threshold-driven refinement

### 9. Google Document AI Credentials & Config Setup
**Why:** Runtime configuration required for production deployment
**Affected files:** Kubernetes Secrets, ConfigMap, deployment manifests
**Work:**
- Create Google Cloud service account with Document AI User role
- Export service account key JSON
- Create Kubernetes Secret `google-documentai-sa` with key
- Create/update ConfigMap `libraryai-policy` with Google config:
  - project_id, location, processor_id (for both IEP2 and IEP1 cleanup)
  - timeout_seconds (90s for layout, 120s for cleanup)
  - retry_budget (2 for both)
  - enable toggle for testing
- Mount Secret in eep_worker Pod
- Verify EEP can load credentials at startup
- Integration test: verify credentials are valid and processor is reachable
**Duration:** 2-3 hours
**Dependencies:** None (parallel with #2-6)
**Blocking:** Production deployment

### 7. Comprehensive Test Suite: Adjudication Paths
**Why:** Cannot deploy without confidence in all decision paths
**Affected files:** [tests/test_layout_adjudication.py](tests/test_layout_adjudication.py) (new), updates to existing tests
**Work:**
- Test local agreement fast path (existing test, likely still passing)
- Test local disagreement → Google → accept
- Test IEP2A failure → single-model → Google → accept
- Test both IEP2A/IEP2B failure → Google → accept
- Test Google success (all paths above succeed)
- Test Google timeout → review
- Test Google error → review
- Test Google bad response → review
- Test PaddleOCR backend contract (detector_type field)
- Mock Google Document AI responses (never call real API in CI)
- Integration tests: end-to-end flows
- Metric emission tests
**Duration:** 12-16 hours
**Dependencies:** Items #1-5
**Blocking:** Code merge Gate/CI

---

## Priority Tier: Required Before Production

### 8. Metrics and Observability for Google Document AI
**Why:** Production SLOs require visibility into Google fallback path
**Affected files:** [services/eep/app/metrics.py](services/eep/app/metrics.py), alertmanager config
**Work:**
- Prometheus metrics:
  - Counter: `layout_adjudication_google_invocations_total` (by status)
  - Gauge: `layout_adjudication_google_success_rate`
  - Histogram: `layout_adjudication_google_latency_ms`
  - Gauge: `layout_decision_source_distribution`
- Alert rules:
  - Google success rate < 99%
  - Google latency (p95) > 60s
  - Google invocation error rate > 5%
- Logging: every Google call with digest
- Tests: verify metrics emitted
**Duration:** 4-6 hours
**Dependencies:** Item #4 (worker calling Google)
**Blocking:** Production monitoring

### 9. Documentation and Spec Updates
**Why:** Teams need clear guidance for deployment and operations
**Affected files:** [docs_pre_implementation/full_updated_spec.md](docs_pre_implementation/full_updated_spec.md) (this spec replaces it), README, architecture docs, checklist
**Work:**
- Replace full_updated_spec.md with this updated spec (document)
- Update [README.md](README.md):
  - IEP2A now PaddleOCR PP-DocLayoutV2
  - Layout detection uses local agreement + Google fallback
  - Configuration: Google credentials as K8s Secret
  - Setup steps for Google Document AI processor
- Update [implementation_checklist.md](implementation_checklist.md):
  - Phase 6: IEP2A now PaddleOCR + Google adjudication
  - Phase 11: Google credentials/config as deployment requirement
- Optionally: create ADRs explaining design decisions
- Internal docs: troubleshooting Google API failures
**Duration:** 4-6 hours
**Dependencies:** All code work complete
**Blocking:** None (docs only)

---

## Priority Tier: Follow-up / Nice-to-Have

### 10. Performance Tuning and Timeout Optimization
**Why:** Optimize end-to-end latency on common path (local agreement)
**Work:**
- Baseline: measure typical latency for local agreement path (no Google)
- Baseline: measure typical latency for Google fallback path
- Identify bottlenecks (IEP2A, IEP2B, Google, network)
- Optimize timeouts if needed (May reduce 90s Google timeout if experience shows it's consistently faster)
- Load test: concurrent IEP2 + Google calls
**Duration:** 4-6 hours
**Dependencies:** Items #1-5 (all code)
**Blocking:** None

### 11. Shadow Mode for Google Document AI
**Why:** Optional: evaluate Google as candidate for shadows/offline
**Work:**
- Extend shadow pipeline to include Google Document AI calls
- Collect statistical data on Google's accuracy vs IEP2A/IEP2B
- Inform future decisions (Is Google good enough to be primary detector? Should we fine-tune IEP2A?)
**Duration:** 3-4 hours
**Dependencies:** Item #4 (worker updated), existing shadow infrastructure
**Blocking:** None

---

# 7. Explicit "What Is Still Left" Checklist

## Critical (Blocks Production)

- [ ] **Change IEP2A backend: Detectron2 → PaddleOCR PP-DocLayoutV2**
  - [ ] Verify or write PaddleOCR backend in services/iep2a/
  - [ ] Change default backend env var or code
  - [ ] Update detector_type field to "paddleocr_pp_doclayout_v2"
  - [ ] Run tests; verify all pass with PaddleOCR
  - [ ] Verify model weights available at runtime

- [ ] **Create Google Document AI integration module**
  - [ ] Create services/eep/app/google/document_ai.py
  - [ ] Implement CallGoogleDocumentAI class (auth, HTTP, retry, timeout, class mapping)
  - [ ] Unit tests with mocked Google API
  - [ ] Mock response fixtures for testing

- [ ] **Refactor layout gate: consensus → adjudication**
  - [ ] Rename/extend evaluate_layout_consensus() → evaluate_layout_adjudication()
  - [ ] Implement new decision logic (local agreement, Google fallback, all-fail)
  - [ ] Return LayoutAdjudicationResult schema
  - [ ] Unit tests for all decision paths

- [ ] **Update EEP worker steps 9-13**
  - [ ] Rewrite layout detection and routing logic
  - [ ] Integrate adjudication evaluation
  - [ ] Add Google Document AI call on disagreement/failure
  - [ ] Update page lineage to record decision_source, fallback_used, confidence
  - [ ] Add new review reasons
  - [ ] Error handling for Google unavailable/timeout/bad response
  - [ ] Integration tests

- [ ] **Update database schema and lineage**
  - [ ] Add layout_decision_source, layout_fallback_used, layout_adjudication_confidence fields to page_lineage
  - [ ] Create Alembic migration (backward-compatible)
  - [ ] Tests verify fields populated correctly

- [ ] **Set up Google Document AI credentials and configuration**
  - [ ] Create Google Cloud service account with Document AI User role
  - [ ] Export service account key JSON
  - [ ] Create Kubernetes Secret google-documentai-sa
  - [ ] Update ConfigMap libraryai-policy with Google config
  - [ ] Mount Secret in eep_worker Pod
  - [ ] Verify EEP can load and validate credentials at startup

- [ ] **Comprehensive test suite for adjudication**
  - [ ] Local agreement fast path test
  - [ ] Local disagreement → Google → accept test
  - [ ] IEP2A failure → Google → accept test
  - [ ] Both IEP2A/IEP2B failure → Google → accept test
  - [ ] Google timeout → review test
  - [ ] Google error → review test
  - [ ] Google bad response → review test
  - [ ] PaddleOCR backend contract test
  - [ ] Mock Google API responses (never call real API in CI)
  - [ ] Integration tests: end-to-end flows
  - [ ] Metric emission tests
  - [ ] CI passes without errors

---

## Needed Before Production

- [ ] **Metrics and observability for Google Document AI**
  - [ ] layout_adjudication_google_invocations_total counter
  - [ ] layout_adjudication_google_success_rate gauge
  - [ ] layout_adjudication_google_latency_ms histogram
  - [ ] layout_decision_source_distribution gauge
  - [ ] Alert rules (success rate < 99%, latency > 60s, error rate > 5%)
  - [ ] Logging: every Google call with digest
  - [ ] Tests verify metrics emitted

- [ ] **Update documentation**
  - [ ] Replace full_updated_spec.md with this spec
  - [ ] Update README.md (IEP2A: PaddleOCR, layout: local+Google, config: Google credentials)
  - [ ] Update implementation_checklist.md (Phase 6: PaddleOCR+adjudication, Phase 11: Google secrets)
  - [ ] Create architecture decision records (if desired)
  - [ ] Troubleshooting guide for Google API failures

---

## Follow-up / Optional

- [ ] Performance tuning and timeout optimization
  - [ ] Baseline latency measurements (local agreement, Google fallback)
  - [ ] Identify bottlenecks
  - [ ] Load testing (concurrent IEP2 + Google calls)
  - [ ] Optimize if needed

- [ ] Shadow mode for Google Document AI (evaluate as candidate)
  - [ ] Extend shadow pipeline
  - [ ] Collect statistical accuracy data
  - [ ] Inform future design decisions

---

# 8. Rectification Model Recommendation

## Recommended Model: **UVDoc (Uniform Vocabulary Document Dewarping)**

### Justification

**UVDoc** is the optimal choice for IEP1D geometric rectification in LibraryAI. Here is the analysis:

### Comparison of Candidate Models

| Aspect | UVDoc | DocTR Rectification | Simple Perspective/Affine | CRAFT + Perspective |
|--------|-------|-------|-------|-------|
| **Specialized for documents?** | ✅ Yes | ❓ Coupled to OCR | ❌ No (papers) | ❌ No (text detection) |
| **Handles curved/rolled pages?** | ✅ Yes (warp mesh) | ⚠️ Limited (perspective only) | ❌ No | ❌ No |
| **Requires fine-tuning?** | ✅ No | ⚠️ Upstream fine-tuning required | ❌ N/A | ❌ N/A |
| **Supports diverse docs?** | ✅ Yes | ⚠️ Limited to OCR-like text | ❌ Limited | ❌ Limited |
| **Arabic document support?** | ✅ Yes (pretrained) | ⚠️ Yes (but OCR-centric) | ❌ N/A | ❌ N/A |
| **Inference latency** | ✅ ~0.5-2s | ❌ ~1-3s (heavier) | ✅ <0.1s | ⚠️ ~0.5s |
| **Confidence/quality estimates?** | ✅ Yes (warp confidence) | ⚠️ Indirect (OCR quality) | ❌ No | ❌ No |
| **Operational simplicity** | ✅ High | ❌ Low (text pipeline) | ✅ Very high | ⚠️ Moderate |
| **Deployment complexity** | ✅ Low | ⚠️ High (coupled system) | ✅ Low | ⚠️ Moderate |

### Why UVDoc

1. **Document-Specialized:** UVDoc is trained specifically for document dewarping. Its loss function and training corpus are optimized for the geometric distortions common in scanned documents (page curl, perspective skew, textured surfaces), not natural images or OCR-specific transformations.

2. **Handles All Distortion Types:**
   - Curved/rolled pages (common in bound books, rolls of paper)
   - Perspective skew (handheld scanning, angled cameras)
   - Non-rigid warping (textured pages, watermark patterns)
   - Captured in a single unified deformation model (warp mesh)

3. **No Fine-Tuning Required:** UVDoc's pretrained weights generalize across diverse document types (books, newspapers, legal documents, Arabic manuscripts). This aligns with LibraryAI's design goal: no fine-tuning assumed.

4. **Arabic Document Handling:** UVDoc's training corpus includes diverse scripts and document styles, including Arabic. It is not script-specific (unlike OCR models), so dewarping works equally well on Arabic, Latin, and mixed-script documents.

5. **Confidence and Quality Estimates:** UVDoc provides deformation confidence, allowing EEP to assess whether rectification actually improved the page or merely introduced interpolation artifacts. This is critical for the second-validation pass.

6. **Operationally Simple:** Single model, single endpoint, single input/output contract. No text extraction, no OCR coupling, no downstream pipeline dependencies.

7. **Cost-Effective:** Modest GPU requirement, reasonable inference latency (~0.5-2s), minimal memory footprint. Suitable for scale-to-zero deployment.

### Why Not Alternatives

- **DocTR:** Full OCR pipeline (detection + recognition). Overkill for geometry; adds complexity, increases latency, requires fine-tuning on target script/language. More expensive to run.

- **Simple Perspective/Affine:** Cannot handle curved or rolled pages. Insufficient for the full range of distortions seen in library scanning.

- **CRAFT + Manual Perspective:** Text detection-based. Requires text presence and good text geometry. Fails on blank pages, images, tables. Adds OCR dependency for pure geometry problem.

### Implementation Path

1. **Baseline (current spec):** UVDoc as the sole rectification model
2. **Future (optional):** Evaluate external OCR/geometry services if UVDoc latency becomes a bottleneck
3. **Never:** Couple geometry to OCR (LibraryAI must not require text extraction for geometry)

### Conclusion

**UVDoc is the right fit for IEP1D.** It specializes in the exact problem (document dewarping), works on all document types without language or script special-casing, requires no fine-tuning, and keeps the geometry pipeline independent and simple. Recommend integrating UVDoc and measuring its contribution in the baseline evaluation before IEP1D is enabled in production (as noted in the spec).

---

# 9. Executive Truth

## What This Updated Spec Now Says

**IEP2 is no longer "consensus-based local detection." It is "authoritative external adjudication."**

- **Local candidate generation:** IEP2A (PaddleOCR PP-DocLayoutV2) and IEP2B (DocLayout-YOLO) run on every page
- **Fast path (local agreement):** When IEP2A and IEP2B agree on layout regions → accept immediately (no external call)
- **Fallback path (disagreement/failure):** When they disagree or either fails → consult Google Document AI
- **Google as final authority:** Google's result, when successful, is accepted as canonical layout
- **No single-model acceptance:** Neither IEP2A nor IEP2B alone can accept a page; must achieve local agreement or external adjudication
- **IEP1 remains internally authoritative:** Geometry decisions do NOT fallback to external services; two-model agreement is the final gate

**IEP2A backend change:** Detectron2 → PaddleOCR PP-DocLayoutV2 (document-trained, multi-language, Arabic support)

**IEP1D recommendation:** UVDoc for geometric rectification (document-specialized, handles curved pages, no fine-tuning)

---

## What Is Probably Still Unimplemented

Based on the audit findings:

1. ❌ **Google Document AI integration:** No code exists for Google API calls, credentials, class mapping
2. ❌ **Layout adjudication logic rewrite:** Current code implements "consensus gate" not "adjudication gate"
3. ⚠️ **PaddleOCR backend as default:** Code likely still defaults to Detectron2 (though PaddleOCR backend exists)
4. ❌ **Page lineage metadata:** Database likely doesn't track layout_decision_source or fallback_used
5. ❌ **Google credentials & secrets:** Not set up in Kubernetes
6. ⚠️ **Tests for adjudication:** Existing consensus tests; new Google fallback tests missing

**Everything else** (IEP1 preprocessing, geometry gates, artifact validation, PTIFF QA, correction, auth, observability core) is likely substantially implemented.

---

## First 5 Implementation Actions (in dependency order)

1. **Change IEP2A backend to PaddleOCR PP-DocLayoutV2**
   - Find and verify PaddleOCR backend code in services/iep2a/
   - Change default from Detectron2 to PaddleOCR (env var or hard default)
   - Update detector_type field in response
   - Run tests; ensure all pass
   - **Effort:** 2-3 hours | **Blocker:** Prerequisite for all IEP2 work

2. **Create Google Document AI integration module**
   - Create services/eep/app/google/document_ai.py
   - Implement CallGoogleDocumentAI class (auth, HTTP, retry, timeout, class mapping)
   - Create mock Google API fixtures for testing
   - Unit tests with mocked responses
   - **Effort:** 6-8 hours | **Blocker:** Prerequisite for adjudication logic

3. **Refactor layout gate: consensus → adjudication**
   - Rename evaluate_layout_consensus() → evaluate_layout_adjudication()
   - Implement new decision logic (local agreement, Google fallback, all-fail)
   - Return LayoutAdjudicationResult schema
   - Unit tests
   - **Effort:** 4-6 hours | **Blocker:** Core logic change

4. **Update EEP worker steps 9-13 to use adjudication**
   - Integrate adjudication evaluation into worker flow
   - Add Google Document AI call on disagreement/failure
   - Update review reasons and page lineage
   - Error handling
   - Integration tests
   - **Effort:** 6-8 hours | **Blocker:** Production flow

5. **Create comprehensive test suite for adjudication paths**
   - Test local agreement, Google fallback, timeouts, errors
   - Mock Google API (never call real API in CI)
   - Integration tests
   - Metric tests
   - **Effort:** 12-16 hours | **Blocker:** Code merge gate / CI

**Total critical path:** ~32-42 hours (1-1.5 weeks, full-time)
**Additional (before production):** +10 hours (observability, docs, config setup)

---

*End of specification update.*

- - -

**Appendix: Change Summary for Quick Reference**

| Item | Change | Effort |
|------|--------|--------|
| IEP2A backend | Detectron2 → PaddleOCR PP-DocLayoutV2 | S (2-3h) |
| IEP2 logic | Consensus → Adjudication (local agreement + Google fallback) | M (16-24h) |
| Google Document AI | New integration (auth, HTTP, retry, mapping) | M (8-12h) |
| Reviews reasons | Add layout_adjudication_* reasons | S (1h) |
| Page lineage | Add decision_source, fallback_used, confidence fields | S (3-4h) |
| Tests | New adjudication test suite | L (12-16h) |
| Observability | Google metrics, alerts, logging | M (4-6h) |
| Docs | Update spec, README, checklist | M (4-6h) |
| Secrets/Config | Google credentials in K8s Secret, ConfigMap | S (2-3h) |
| **TOTAL** | | **~60-80 hours** |
