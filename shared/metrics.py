"""
shared.metrics
--------------
Prometheus metric objects shared across services, and a factory for the
/metrics FastAPI endpoint.

Usage::

    from shared.metrics import make_metrics_router, HTTP_REQUESTS_TOTAL

    app.include_router(make_metrics_router())

Metric objects defined here are used by RequestTracingMiddleware.
Service-specific metrics (e.g. eep_auto_accept_rate) are defined in the
owning service module and registered in later phases.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

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
