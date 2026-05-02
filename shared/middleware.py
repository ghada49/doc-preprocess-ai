"""
shared.middleware
-----------------
FastAPI/Starlette middleware and app-configuration helper shared by all
LibraryAI services.

Usage::

    from shared.middleware import configure_observability

    app = FastAPI(title="eep")
    configure_observability(app, service_name="eep")

``configure_observability`` is a convenience wrapper that wires up:
- ``GET /health`` and ``GET /ready``  (from shared.health)
- ``GET /metrics``                    (from shared.metrics)
- ``RequestTracingMiddleware``        (request ID, Prometheus, structured log)
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from shared.metrics import HTTP_REQUEST_DURATION_SECONDS, HTTP_REQUESTS_TOTAL

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


class RequestTracingMiddleware(BaseHTTPMiddleware):
    """
    Per-request middleware that:

    1. Attaches a ``request_id`` (from ``X-Request-ID`` header or a new UUID4)
       to ``request.state`` and echoes it in the response header.
    2. Records ``http_requests_total`` and ``http_request_duration_seconds``
       Prometheus metrics.
    3. Emits a structured log line for every request/response pair.

    Args:
        app:          The ASGI application to wrap.
        service_name: Label embedded in Prometheus metrics and log lines.
    """

    def __init__(self, app: ASGIApp, service_name: str = "unknown") -> None:
        super().__init__(app)
        self._service = service_name

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        start = time.perf_counter()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration = time.perf_counter() - start
            path = request.url.path
            method = request.method

            HTTP_REQUESTS_TOTAL.labels(
                service=self._service,
                method=method,
                path=path,
                status_code=str(status_code),
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                service=self._service,
                method=method,
                path=path,
            ).observe(duration)

            logger.info(
                "%s %s %s %.3fs",
                method,
                path,
                status_code,
                duration,
                extra={"request_id": request_id},
            )


def configure_observability(
    app: FastAPI,
    *,
    service_name: str,
    health_checks: list[Callable[[], Any]] | None = None,
    metrics_before_collect: Callable[[], Any] | None = None,
) -> None:
    """
    Wire shared observability onto a FastAPI app in one call.

    Mounts ``/health``, ``/ready``, and ``/metrics``, and installs
    ``RequestTracingMiddleware``.

    Args:
        app:           The FastAPI application instance.
        service_name:  Embedded in metrics labels and log output.
        health_checks: Optional list of readiness-check callables (sync or
                       async) passed to ``make_health_router``.  Services add
                       their own checks in later phases (e.g. DB ping, model
                       load check).
        metrics_before_collect: Optional hook invoked immediately before
                       rendering ``/metrics``. Services can use this to refresh
                       DB-backed gauges.
    """
    from shared.health import make_health_router
    from shared.metrics import make_metrics_router

    app.include_router(make_health_router(checks=health_checks))
    app.include_router(make_metrics_router(before_collect=metrics_before_collect))
    app.add_middleware(RequestTracingMiddleware, service_name=service_name)
