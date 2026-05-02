"""
services/iep1e/app/model.py
----------------------------
Thread-safe singleton for the IEP1E PaddleOCR orientation engine.

Environment variables:
    IEP1E_MOCK_MODE   — "true" to skip real OCR; orientation always returns
                        rotation=0 with orientation_confident=False.
    IEP1E_USE_GPU     — "true" (default) to use GPU; "false" to use CPU.

Exported:
    is_model_ready            — readiness check for /ready endpoint
    readiness_failure_extras  — optional detail dict for 503 /ready responses
    get_ocr_engine            — returns the live PaddleOCR instance
    initialize_model          — called at startup; logs but does not raise
    reset_for_testing         — test-only teardown helper
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_ocr_engine: Any = None
_load_error: Exception | None = None
_loaded = False


def _is_mock_mode() -> bool:
    return os.environ.get("IEP1E_MOCK_MODE", "false").strip().lower() in {"1", "true", "yes"}


def _use_gpu() -> bool:
    return os.environ.get("IEP1E_USE_GPU", "true").strip().lower() not in {"0", "false", "no"}


def is_model_ready() -> bool:
    """Return True when the OCR engine is loaded and usable."""
    if _is_mock_mode():
        return True
    return _loaded and _load_error is None


def readiness_failure_extras() -> dict[str, Any]:
    """Extra JSON fields for GET /ready when returning 503 (loading or failed)."""
    if _is_mock_mode():
        return {}
    details: dict[str, Any] = {}
    if _load_error is not None:
        details["error"] = str(_load_error)
        details["error_type"] = type(_load_error).__name__
    elif not _loaded:
        details["phase"] = "loading"
    return details


def get_ocr_engine() -> Any:
    """
    Return the PaddleOCR engine.  Raises RuntimeError if not loaded.

    In mock mode, returns None (callers must handle None).
    """
    if _is_mock_mode():
        return None

    global _ocr_engine, _load_error, _loaded

    if _loaded:
        return _ocr_engine
    if _load_error is not None:
        raise RuntimeError(f"IEP1E OCR engine failed to load: {_load_error}") from _load_error

    with _lock:
        if _loaded:
            return _ocr_engine
        if _load_error is not None:
            raise RuntimeError(
                f"IEP1E OCR engine failed to load: {_load_error}"
            ) from _load_error

        try:
            from shared.semantic_norm.ocr_scorer import build_ocr_engine, warmup_ocr_engine

            engine = build_ocr_engine(use_gpu=_use_gpu())
            logger.info(
                "iep1e: running OCR warmup before marking ready (use_gpu=%s)",
                _use_gpu(),
            )
            warmup_ocr_engine(engine)
            _ocr_engine = engine
            _loaded = True
            _load_error = None
            logger.info("iep1e: OCR engine loaded successfully (use_gpu=%s)", _use_gpu())
        except Exception as exc:
            _ocr_engine = None
            _loaded = False
            _load_error = exc
            logger.exception("iep1e: OCR engine initialization failed: %s", exc)
            raise RuntimeError(f"IEP1E OCR engine failed to load: {exc}") from exc

    return _ocr_engine


def initialize_model() -> None:
    """Eagerly load the OCR engine at startup.  Logs but does not raise."""
    if _is_mock_mode():
        logger.info("iep1e: mock mode — skipping real OCR engine load")
        return
    try:
        get_ocr_engine()
    except RuntimeError as exc:
        logger.warning(
            "iep1e: background model initialisation failed; /ready stays not_ready: %s",
            exc,
        )
    else:
        logger.info("iep1e: background model initialisation completed successfully")


def reset_for_testing() -> None:
    """Reset all module-level state.  Test-only — never call in production."""
    global _ocr_engine, _load_error, _loaded
    with _lock:
        _ocr_engine = None
        _load_error = None
        _loaded = False
