"""
shared.health
-------------
Factory for /health and /ready FastAPI endpoints.

Usage::

    from shared.health import make_health_router

    # No checks — always ready (Phase 0 / stateless skeleton policy)
    app.include_router(make_health_router())

    # With readiness checks (added in later phases)
    async def db_check() -> bool:
        ...

    app.include_router(make_health_router(checks=[db_check]))
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def make_health_router(
    checks: list[Callable[[], Any]] | None = None,
) -> APIRouter:
    """
    Return an APIRouter that mounts:

    - ``GET /health`` — always 200 while the process is alive.
    - ``GET /ready``  — 200 if every registered check passes; 503 otherwise.
                        With no checks registered, always returns 200.

    Each check is a zero-argument callable that returns a truthy value on
    success. Both sync and async callables are accepted.
    """
    _checks: list[Callable[[], Any]] = list(checks or [])
    router = APIRouter(tags=["ops"])

    @router.get("/health", summary="Liveness probe")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/ready", summary="Readiness probe")
    async def ready() -> JSONResponse | dict[str, str]:
        for check in _checks:
            try:
                if asyncio.iscoroutinefunction(check):
                    result = await check()
                else:
                    result = check()
                if not result:
                    return JSONResponse(
                        status_code=503,
                        content={"status": "not_ready"},
                    )
            except Exception:
                logger.exception("Readiness check %r raised an exception", check)
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready"},
                )
        return {"status": "ready"}

    return router
