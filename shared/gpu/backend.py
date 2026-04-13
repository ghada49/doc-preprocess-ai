"""
shared/gpu/backend.py
---------------------
GPU/inference backend abstraction.

Two backends:
  LocalHTTPBackend  — calls IEP services over HTTP using Docker container-name
                      endpoints; distinguishes cold-start timeout, warm-inference
                      timeout, and service error.
  RunpodBackend     — Runpod production scaffold; correct interface, no live
                      provider integration (Phase 11).

Usage::

    backend = LocalHTTPBackend(
        cold_start_timeout_seconds=30.0,
        execution_timeout_seconds=60.0,
    )
    response_bytes = await backend.call(
        endpoint="http://iep1a:8001/v1/geometry",
        payload={"image_b64": "..."},
    )
"""

from __future__ import annotations

import abc
import enum
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class BackendErrorKind(str, enum.Enum):
    """Classifies why a backend call failed."""

    COLD_START_TIMEOUT = "cold_start_timeout"
    """Service did not become reachable within cold_start_timeout_seconds."""

    WARM_INFERENCE_TIMEOUT = "warm_inference_timeout"
    """Service was reachable but did not return a response within execution_timeout_seconds."""

    SERVICE_ERROR = "service_error"
    """Service returned an HTTP error or a connection-level failure after the warm phase."""


class BackendError(Exception):
    """Raised by any backend when a call cannot be completed."""

    def __init__(self, kind: BackendErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

    def __repr__(self) -> str:
        return f"BackendError(kind={self.kind!r}, message={self.message!r})"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class GPUBackend(abc.ABC):
    """
    Common interface for all inference backends.

    Switching from LocalHTTPBackend to RunpodBackend must not require
    any schema or orchestrator changes.
    """

    @abc.abstractmethod
    async def call(
        self,
        endpoint: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        POST *payload* (JSON) to *endpoint* and return the parsed JSON response.

        Raises:
            BackendError: with an appropriate BackendErrorKind on any failure.
        """

    @abc.abstractmethod
    async def close(self) -> None:
        """Release any held resources (e.g. HTTP connection pool)."""


# ---------------------------------------------------------------------------
# Local HTTP backend
# ---------------------------------------------------------------------------

#: How long to wait for a single /health probe during cold-start polling.
_HEALTH_PROBE_TIMEOUT_SECONDS = 2.0

#: Interval between cold-start /health probes.
_COLD_START_POLL_INTERVAL_SECONDS = 1.0


@dataclass
class LocalHTTPBackend(GPUBackend):
    """
    Calls IEP services over HTTP using Docker container-name endpoints.

    Cold-start detection
    --------------------
    Before sending the inference request the backend probes ``GET /health``
    on the target host until it receives a 2xx response or
    ``cold_start_timeout_seconds`` elapses.  A timeout here raises
    ``BackendError(COLD_START_TIMEOUT, ...)``.

    Warm-inference timeout
    ----------------------
    Once ``/health`` succeeds the backend posts to *endpoint* with a timeout
    of ``execution_timeout_seconds``.  A timeout here raises
    ``BackendError(WARM_INFERENCE_TIMEOUT, ...)``.

    Service error
    -------------
    Any non-2xx HTTP status or connection failure after the warm phase raises
    ``BackendError(SERVICE_ERROR, ...)``.

    Note: in local Docker the services do not scale to zero, so the cold-start
    probe will typically succeed on the first attempt.  The logic is present so
    that the same code path is exercised in every environment.
    """

    cold_start_timeout_seconds: float = 30.0
    execution_timeout_seconds: float = 60.0

    # Internal HTTP client; created lazily so the dataclass can be constructed
    # synchronously and the client opened on first use.
    _client: httpx.AsyncClient | None = field(init=False, default=None, repr=False)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    def _health_url(self, endpoint: str) -> str:
        """Derive the /health URL from the inference endpoint URL."""
        # endpoint example: "http://iep1a:8001/v1/geometry"
        # health URL:        "http://iep1a:8001/health"
        client = httpx.URL(endpoint)
        return str(client.copy_with(path="/health", query=None))

    async def _wait_for_warm(self, endpoint: str) -> None:
        """
        Poll /health until the service is up or cold_start_timeout_seconds lapses.

        Raises:
            BackendError(COLD_START_TIMEOUT): if the deadline is reached.
        """
        import asyncio

        health_url = self._health_url(endpoint)
        client = self._get_client()
        deadline = asyncio.get_event_loop().time() + self.cold_start_timeout_seconds

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise BackendError(
                    BackendErrorKind.COLD_START_TIMEOUT,
                    f"Service did not become reachable within "
                    f"{self.cold_start_timeout_seconds}s (health: {health_url})",
                )

            probe_timeout = min(_HEALTH_PROBE_TIMEOUT_SECONDS, remaining)
            try:
                resp = await client.get(health_url, timeout=probe_timeout)
                if resp.is_success:
                    return  # service is warm
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
                pass  # not yet reachable

            await asyncio.sleep(_COLD_START_POLL_INTERVAL_SECONDS)

    async def call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        POST *payload* to *endpoint*, returning the parsed JSON response.

        Raises:
            BackendError(COLD_START_TIMEOUT):    service unreachable during warm-up.
            BackendError(WARM_INFERENCE_TIMEOUT): inference did not complete in time.
            BackendError(SERVICE_ERROR):          non-2xx status or connection failure.
        """
        await self._wait_for_warm(endpoint)

        client = self._get_client()
        logger.debug(
            "LocalHTTPBackend POST %s timeout=%.1fs", endpoint, self.execution_timeout_seconds
        )

        try:
            response = await client.post(
                endpoint,
                json=payload,
                timeout=self.execution_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise BackendError(
                BackendErrorKind.WARM_INFERENCE_TIMEOUT,
                f"Inference did not complete within {self.execution_timeout_seconds}s "
                f"(endpoint: {endpoint}): {exc}",
            ) from exc
        except httpx.RequestError as exc:
            raise BackendError(
                BackendErrorKind.SERVICE_ERROR,
                f"Connection error calling {endpoint}: {exc}",
            ) from exc

        if not response.is_success:
            raise BackendError(
                BackendErrorKind.SERVICE_ERROR,
                f"Service returned HTTP {response.status_code} from {endpoint}: "
                f"{response.text[:200]}",
            )

        result: dict[str, Any] = response.json()
        return result

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# Runpod backend scaffold (Phase 11)
# ---------------------------------------------------------------------------


@dataclass
class RunpodBackend(GPUBackend):
    """
    Production backend targeting Runpod on-demand GPU endpoints.

    Supports:
    - Runpod on-demand GPU endpoints for IEP1A, IEP1B, IEP1D, IEP2A, IEP2B
    - Runpod CPU endpoints for non-GPU control-plane tasks where applicable

    Same request/response contracts as LocalHTTPBackend.
    Accepts the same cold_start_timeout_seconds and execution_timeout_seconds.
    Distinguishes cold-start timeout, warm-inference timeout, provider/service error.

    Live provider integration is implemented in Phase 11 (Packet 11.2).
    All methods raise NotImplementedError until then.
    """

    cold_start_timeout_seconds: float = 120.0
    execution_timeout_seconds: float = 120.0

    # Runpod API key; populated from environment in production.
    api_key: str = ""

    # Base URL for the Runpod serverless API; overridable for testing.
    runpod_api_base: str = "https://api.runpod.io/v2"

    async def call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Submit an inference job to Runpod and poll for the result.

        Raises:
            NotImplementedError: until Phase 11 (Packet 11.2).
        """
        raise NotImplementedError(
            "RunpodBackend.call is not yet implemented. "
            "Live Runpod integration is scheduled for Phase 11 (Packet 11.2)."
        )

    async def close(self) -> None:
        """No-op until Phase 11."""
