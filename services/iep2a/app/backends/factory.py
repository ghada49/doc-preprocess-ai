"""
services/iep2a/app/backends/factory.py
----------------------------------------
IEP2A backend singleton factory.

The active backend is created and initialized once at service startup
(via initialize_backend()).  All subsequent requests retrieve the same
instance via get_active_backend().

Backend selection env var:
    IEP2A_LAYOUT_BACKEND = "paddleocr" (default) | "detectron2"

Initialization is a no-op when IEP2A_USE_REAL_MODEL != "true" (stub mode)
so that the stub test path is unaffected.
"""

from __future__ import annotations

import logging
import os

from .base import LayoutBackend

logger = logging.getLogger(__name__)

_SUPPORTED_BACKENDS = ("detectron2", "paddleocr")

# Module-level singleton — set once at startup, read-only thereafter.
_backend: LayoutBackend | None = None


def _use_real_model() -> bool:
    return os.environ.get("IEP2A_USE_REAL_MODEL", "false").lower() == "true"


def initialize_backend() -> None:
    """
    Create and initialize the selected IEP2A layout backend.

    Must be called once during service startup (FastAPI lifespan).
    In stub mode (IEP2A_USE_REAL_MODEL != "true") this is a no-op so that
    the existing stub test harness is unaffected.

    Raises:
        ValueError:    when IEP2A_LAYOUT_BACKEND names an unknown backend.
        RuntimeError:  when the selected backend fails to load (propagated
                       so that /ready reflects the failure).
    """
    global _backend

    if not _use_real_model():
        logger.debug("IEP2A stub mode active — backend initialization skipped")
        return

    backend_name = os.environ.get("IEP2A_LAYOUT_BACKEND", "paddleocr").strip().lower()

    logger.info(
        "IEP2A initializing layout backend",
        extra={"backend": backend_name},
    )

    if backend_name == "detectron2":
        from .detectron2_backend import Detectron2Backend

        b: LayoutBackend = Detectron2Backend()
    elif backend_name == "paddleocr":
        from .paddleocr_backend import PaddleOCRBackend

        b = PaddleOCRBackend()
    else:
        raise ValueError(
            f"Unknown IEP2A_LAYOUT_BACKEND={backend_name!r}. "
            f"Supported values: {_SUPPORTED_BACKENDS}"
        )

    _backend = b
    _backend.initialize()


def get_active_backend() -> LayoutBackend:
    """
    Return the initialized backend instance.

    Raises:
        RuntimeError: when called before initialize_backend() (i.e., startup
                      failed or real mode is not active).
    """
    if _backend is None:
        raise RuntimeError(
            "No IEP2A backend is initialized. "
            "Ensure IEP2A_USE_REAL_MODEL=true and that startup completed successfully."
        )
    return _backend


def get_active_backend_optional() -> LayoutBackend | None:
    """Return the backend instance or None if not yet initialized (for readiness checks)."""
    return _backend


def reset_for_testing() -> None:
    """Reset the factory singleton.  Call from test teardowns only."""
    global _backend
    _backend = None
