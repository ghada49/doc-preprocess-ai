"""
shared.metrics
--------------
Prometheus metric objects shared across services, and a factory for the
/metrics FastAPI endpoint.

Usage::

    from shared.metrics import make_metrics_router, HTTP_REQUESTS_TOTAL

    app.include_router(make_metrics_router())

Metric objects defined here are used by RequestTracingMiddleware and by
service-specific code paths throughout the pipeline.

All metric names and types match spec Section 12.3 exactly.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# ── Bucket sets ───────────────────────────────────────────────────────────────

_CONFIDENCE_BUCKETS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
_GPU_SECONDS_BUCKETS = [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
_QUALITY_SCORE_BUCKETS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
_SKEW_RESIDUAL_BUCKETS = [0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0]
_PAGE_COUNT_BUCKETS = [0, 1, 2, 3]
_TTA_VARIANCE_BUCKETS = [0.0, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5]
_REGIONS_PER_PAGE_BUCKETS = [0, 1, 2, 3, 5, 8, 12, 20, 30]
_CONF_DELTA_BUCKETS = [-1.0, -0.5, -0.25, -0.1, -0.05, 0.0, 0.05, 0.1, 0.25, 0.5, 1.0]

# ── Common HTTP metrics (populated by RequestTracingMiddleware) ────────────────

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total number of HTTP requests handled",
    ["service", "method", "path", "status_code"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["service", "method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ── EEP metrics ───────────────────────────────────────────────────────────────

EEP_GEOMETRY_SELECTION_ROUTE = Counter(
    "eep_geometry_selection_route",
    "Geometry selection gate routing decisions",
    ["route"],
    # valid route label values:
    #   accepted, review, structural_disagreement,
    #   sanity_failed, split_confidence_low, tta_variance_high
)

EEP_ARTIFACT_VALIDATION_ROUTE = Counter(
    "eep_artifact_validation_route",
    "Artifact validation gate routing decisions",
    ["route"],
    # valid route label values: valid, invalid, rectification_triggered
)

EEP_LAYOUT_CONSENSUS_CONFIDENCE = Histogram(
    "eep_layout_consensus_confidence",
    "Layout consensus gate confidence score",
    buckets=_CONFIDENCE_BUCKETS,
)

EEP_CONSENSUS_ROUTE = Counter(
    "eep_consensus_route",
    "Layout consensus gate routing decisions",
    ["route"],
    # valid route label values: accepted, review
)

# NOTE: eep_auto_accept_rate and eep_structural_agreement_rate are OBSERVABILITY-ONLY
# gauges. They MUST NOT influence any routing decision (spec Section 12.3).
EEP_AUTO_ACCEPT_RATE = Gauge(
    "eep_auto_accept_rate",
    "Rolling auto-acceptance rate — observability only, never used for routing",
)

EEP_STRUCTURAL_AGREEMENT_RATE = Gauge(
    "eep_structural_agreement_rate",
    "Rolling IEP1A/IEP1B structural agreement rate — observability only, never used for routing",
)

EEP_REQUESTS_TOTAL = Counter(
    "eep_requests_total",
    "Total EEP processing requests",
)

# ── IEP0 metrics ──────────────────────────────────────────────────────────────

IEP0_CLASSIFICATION_CONFIDENCE = Histogram(
    "iep0_classification_confidence",
    "IEP0 material-type classification confidence score",
    buckets=_CONFIDENCE_BUCKETS,
)

IEP0_CLASSIFICATION_TOTAL = Counter(
    "iep0_classification_total",
    "IEP0 classification requests by predicted material type",
    ["material_type"],
)

IEP0_GPU_INFERENCE_SECONDS = Histogram(
    "iep0_gpu_inference_seconds",
    "IEP0 classification inference latency in seconds",
    buckets=_GPU_SECONDS_BUCKETS,
)

# ── IEP1A metrics ─────────────────────────────────────────────────────────────

IEP1A_GEOMETRY_CONFIDENCE = Histogram(
    "iep1a_geometry_confidence",
    "IEP1A geometry confidence score",
    buckets=_CONFIDENCE_BUCKETS,
)

IEP1A_PAGE_COUNT = Histogram(
    "iep1a_page_count",
    "IEP1A predicted page count per inference",
    buckets=_PAGE_COUNT_BUCKETS,
)

IEP1A_SPLIT_DETECTION_RATE = Counter(
    "iep1a_split_detection_rate",
    "IEP1A spread/split detections",
)

IEP1A_TTA_STRUCTURAL_AGREEMENT_RATE = Histogram(
    "iep1a_tta_structural_agreement_rate",
    "IEP1A TTA structural agreement rate across passes",
    buckets=_CONFIDENCE_BUCKETS,
)

IEP1A_TTA_PREDICTION_VARIANCE = Histogram(
    "iep1a_tta_prediction_variance",
    "IEP1A TTA inter-pass geometry prediction variance",
    buckets=_TTA_VARIANCE_BUCKETS,
)

IEP1A_GPU_INFERENCE_SECONDS = Histogram(
    "iep1a_gpu_inference_seconds",
    "IEP1A GPU inference wall-clock time in seconds",
    buckets=_GPU_SECONDS_BUCKETS,
)

# ── IEP1B metrics ─────────────────────────────────────────────────────────────

IEP1B_GEOMETRY_CONFIDENCE = Histogram(
    "iep1b_geometry_confidence",
    "IEP1B geometry confidence score",
    buckets=_CONFIDENCE_BUCKETS,
)

IEP1B_PAGE_COUNT = Histogram(
    "iep1b_page_count",
    "IEP1B predicted page count per inference",
    buckets=_PAGE_COUNT_BUCKETS,
)

IEP1B_SPLIT_DETECTION_RATE = Counter(
    "iep1b_split_detection_rate",
    "IEP1B spread/split detections",
)

IEP1B_TTA_STRUCTURAL_AGREEMENT_RATE = Histogram(
    "iep1b_tta_structural_agreement_rate",
    "IEP1B TTA structural agreement rate across passes",
    buckets=_CONFIDENCE_BUCKETS,
)

IEP1B_TTA_PREDICTION_VARIANCE = Histogram(
    "iep1b_tta_prediction_variance",
    "IEP1B TTA inter-pass geometry prediction variance",
    buckets=_TTA_VARIANCE_BUCKETS,
)

IEP1B_GPU_INFERENCE_SECONDS = Histogram(
    "iep1b_gpu_inference_seconds",
    "IEP1B GPU inference wall-clock time in seconds",
    buckets=_GPU_SECONDS_BUCKETS,
)

# ── IEP1C metrics ─────────────────────────────────────────────────────────────

IEP1C_BLUR_SCORE = Histogram(
    "iep1c_blur_score",
    "IEP1C normalized artifact blur quality score",
    buckets=_QUALITY_SCORE_BUCKETS,
)

IEP1C_BORDER_SCORE = Histogram(
    "iep1c_border_score",
    "IEP1C normalized artifact border quality score",
    buckets=_QUALITY_SCORE_BUCKETS,
)

IEP1C_SKEW_RESIDUAL = Histogram(
    "iep1c_skew_residual",
    "IEP1C normalized artifact skew residual in degrees",
    buckets=_SKEW_RESIDUAL_BUCKETS,
)

IEP1C_FOREGROUND_COVERAGE = Histogram(
    "iep1c_foreground_coverage",
    "IEP1C normalized artifact foreground coverage fraction",
    buckets=_QUALITY_SCORE_BUCKETS,
)

# ── IEP1D metrics ─────────────────────────────────────────────────────────────

IEP1D_RECTIFICATION_CONFIDENCE = Histogram(
    "iep1d_rectification_confidence",
    "IEP1D rectification output confidence score",
    buckets=_CONFIDENCE_BUCKETS,
)

IEP1D_RECTIFICATION_TRIGGERED = Counter(
    "iep1d_rectification_triggered",
    "Number of times IEP1D rectification was triggered",
)

IEP1D_GPU_INFERENCE_SECONDS = Histogram(
    "iep1d_gpu_inference_seconds",
    "IEP1D GPU inference wall-clock time in seconds",
    buckets=_GPU_SECONDS_BUCKETS,
)

IEP1D_QUALITY_GATE_DECISIONS = Counter(
    "iep1d_quality_gate_decisions_total",
    "IEP1D quality gate decision count",
    ["decision"],
    # decision label values: rectified_accepted, rectification_rejected
)

IEP1D_REJECTION_REASONS = Counter(
    "iep1d_rejection_reasons_total",
    "IEP1D quality gate rejection reason count (multiple reasons may fire per event)",
    ["reason"],
    # reason label values: low_confidence, skew_not_improved, border_regressed, warning_veto
)

# ── IEP1E metrics ─────────────────────────────────────────────────────────────

IEP1E_ORIENTATION_DECISIONS = Counter(
    "iep1e_orientation_decisions_total",
    "IEP1E orientation decision count",
    ["confident"],
    # confident label values: "true", "false"
)

IEP1E_READING_DIRECTION = Counter(
    "iep1e_reading_direction_total",
    "IEP1E reading-direction decision count",
    ["direction"],
    # direction label values: ltr, rtl, unresolved
)

IEP1E_PROCESSING_SECONDS = Histogram(
    "iep1e_processing_seconds",
    "IEP1E semantic-norm wall-clock time in seconds",
    buckets=_GPU_SECONDS_BUCKETS,
)

IEP1E_FALLBACK_TOTAL = Counter(
    "iep1e_fallback_total",
    "IEP1E calls that used geometry-only fallback (blank pages or OCR unavailable)",
)

# ── Google Document AI cleanup metrics ────────────────────────────────────────

GOOGLE_CLEANUP_DECISIONS = Counter(
    "google_cleanup_decisions_total",
    "Google Document AI cleanup decision count in rescue flow",
    ["decision"],
    # decision label values: cleanup_accepted, cleanup_failed
)

# ── Google Document AI layout adjudication metrics ────────────────────────────

GOOGLE_LAYOUT_ADJUDICATION_DECISIONS = Counter(
    "google_layout_adjudication_decisions_total",
    "Google Document AI layout adjudication decision count in IEP2 gate",
    ["source"],
    # source label values:
    #   local_agreement          — IEP2A+IEP2B agreed; Google not called
    #   google_document_ai       — Google called and succeeded (including empty result)
    #   local_fallback_unverified — Google hard-failed; best local result used
    #   google_skipped           — Google client not available; local fallback used
)

# ── IEP2A metrics ─────────────────────────────────────────────────────────────

IEP2A_REGION_CONFIDENCE = Histogram(
    "iep2a_region_confidence",
    "IEP2A per-region detection confidence score",
    buckets=_CONFIDENCE_BUCKETS,
)

IEP2A_MEAN_PAGE_CONFIDENCE = Histogram(
    "iep2a_mean_page_confidence",
    "IEP2A mean per-page region confidence score",
    buckets=_CONFIDENCE_BUCKETS,
)

IEP2A_REGIONS_PER_PAGE = Histogram(
    "iep2a_regions_per_page",
    "IEP2A number of layout regions detected per page",
    buckets=_REGIONS_PER_PAGE_BUCKETS,
)

IEP2A_GPU_INFERENCE_SECONDS = Histogram(
    "iep2a_gpu_inference_seconds",
    "IEP2A GPU inference wall-clock time in seconds",
    buckets=_GPU_SECONDS_BUCKETS,
)

# ── IEP2B metrics ─────────────────────────────────────────────────────────────

IEP2B_REGION_CONFIDENCE = Histogram(
    "iep2b_region_confidence",
    "IEP2B per-region detection confidence score",
    buckets=_CONFIDENCE_BUCKETS,
)

IEP2B_MEAN_PAGE_CONFIDENCE = Histogram(
    "iep2b_mean_page_confidence",
    "IEP2B mean per-page region confidence score",
    buckets=_CONFIDENCE_BUCKETS,
)

IEP2B_REGIONS_PER_PAGE = Histogram(
    "iep2b_regions_per_page",
    "IEP2B number of layout regions detected per page",
    buckets=_REGIONS_PER_PAGE_BUCKETS,
)

IEP2B_GPU_INFERENCE_SECONDS = Histogram(
    "iep2b_gpu_inference_seconds",
    "IEP2B GPU inference wall-clock time in seconds",
    buckets=_GPU_SECONDS_BUCKETS,
)

# ── Shadow worker metrics ─────────────────────────────────────────────────────

SHADOW_TASKS_ENQUEUED = Counter(
    "shadow_tasks_enqueued",
    "Shadow evaluation tasks enqueued",
)

SHADOW_TASKS_PROCESSED = Counter(
    "shadow_tasks_processed",
    "Shadow evaluation tasks completed processing",
)

SHADOW_TASKS_FAILED = Counter(
    "shadow_tasks_failed",
    "Shadow evaluation tasks that failed",
)

SHADOW_CONF_DELTA = Histogram(
    "shadow_conf_delta",
    "Confidence delta between shadow candidate and live model (candidate minus live)",
    buckets=_CONF_DELTA_BUCKETS,
)


# ── /metrics endpoint factory ─────────────────────────────────────────────────


def make_metrics_router() -> APIRouter:
    """
    Return an APIRouter that mounts ``GET /metrics`` (Prometheus text format).
    The endpoint is excluded from the OpenAPI schema.
    """
    router = APIRouter(tags=["ops"])

    @router.get("/metrics", include_in_schema=False, summary="Prometheus metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return router
