"""
services/iep0/app/inference.py
-------------------------------
IEP0 material-type classification inference module.

Loads a YOLOv8-cls model (classifier.pt) via ultralytics at startup.
If the model file is missing or empty, falls back to mock behaviour
controlled by environment variables.

Supports:
  - Single-image classification via classify_single()
  - Batch classification with majority voting via classify_batch()

The model is expected to output 3 classes: book, newspaper, microfilm.

Mock configuration (env vars, read at call time):
  IEP0_MOCK_FAIL             "true"  → raise InferenceError
  IEP0_MOCK_MATERIAL_TYPE    one of book|newspaper|microfilm (default: "book")
  IEP0_MOCK_CONFIDENCE       float in [0, 1]  (default: "0.92")
  IEP0_MOCK_NOT_READY        "true"  → is_model_ready() returns False
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from pathlib import Path

from shared.schemas.iep0 import (
    BatchClassifyRequest,
    BatchClassifyResponse,
    ClassifyRequest,
    ClassifyResponse,
)
from shared.schemas.preprocessing import PreprocessError

logger = logging.getLogger(__name__)

_CLASS_NAMES = ("book", "newspaper", "microfilm")
_MODEL_PATH = Path(os.environ.get("IEP0_MODEL_PATH", "/app/models/iep0/classifier.pt"))
_IMGSZ = 384  # training image size for the classifier

# ── Model state ──────────────────────────────────────────────────────────────

_model = None
_model_loaded = False
_using_mock = False


def _try_load_model() -> None:
    """Attempt to load the YOLO-cls model. Fall back to mock if unavailable."""
    global _model, _model_loaded, _using_mock

    if _model_loaded:
        return

    if not _MODEL_PATH.exists() or _MODEL_PATH.stat().st_size == 0:
        logger.warning(
            "iep0: model file %s not found or empty; using mock inference",
            _MODEL_PATH,
        )
        _using_mock = True
        _model_loaded = True
        return

    try:
        from ultralytics import YOLO

        _model = YOLO(str(_MODEL_PATH))
        _using_mock = False
        _model_loaded = True
        logger.info("iep0: model loaded from %s", _MODEL_PATH)
    except Exception as exc:
        logger.warning("iep0: failed to load model from %s: %s; using mock", _MODEL_PATH, exc)
        _using_mock = True
        _model_loaded = True


class InferenceError(Exception):
    """Raised when classification fails."""

    def __init__(self, error: PreprocessError) -> None:
        super().__init__(error.error_message)
        self.preprocess_error = error


def is_model_ready() -> bool:
    """Return True when the model (or mock) is loaded and ready."""
    if os.environ.get("IEP0_MOCK_NOT_READY", "false").lower() == "true":
        return False
    _try_load_model()
    return _model_loaded


# ── Mock inference ───────────────────────────────────────────────────────────

def _run_mock_single(image_uri: str) -> tuple[str, float, dict[str, float]]:
    """Return mock classification result for a single image."""
    if os.environ.get("IEP0_MOCK_FAIL", "false").lower() == "true":
        raise InferenceError(
            PreprocessError(
                error_code="CLASSIFICATION_FAILED",
                error_message="Mock IEP0 failure: CLASSIFICATION_FAILED",
                fallback_action="RETRY",
            )
        )

    predicted_type = os.environ.get("IEP0_MOCK_MATERIAL_TYPE", "book").lower()
    if predicted_type not in _CLASS_NAMES:
        predicted_type = "book"

    confidence = float(os.environ.get("IEP0_MOCK_CONFIDENCE", "0.92"))
    other_types = [t for t in _CLASS_NAMES if t != predicted_type]
    remaining = max(0.0, 1.0 - confidence)
    other_prob = round(remaining / len(other_types), 4) if other_types else 0.0

    probabilities = {t: other_prob for t in _CLASS_NAMES}
    probabilities[predicted_type] = confidence

    return predicted_type, confidence, probabilities


# ── Real model inference ─────────────────────────────────────────────────────

def _load_image(image_uri: str) -> "np.ndarray | None":
    """Load an image from a storage URI into a numpy array."""
    import cv2
    import numpy as np
    from shared.io.storage import get_backend

    try:
        backend = get_backend(image_uri)
        image_bytes = backend.get_bytes(image_uri)
        image = cv2.imdecode(
            np.frombuffer(image_bytes, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        return image
    except Exception:
        logger.exception("iep0: failed to load image from %s", image_uri)
        return None


def _run_model_single(image_uri: str) -> tuple[str, float, dict[str, float]]:
    """Run the real YOLO-cls model on a single image and return (class, confidence, probs)."""
    image = _load_image(image_uri)
    if image is None:
        raise InferenceError(
            PreprocessError(
                error_code="CLASSIFICATION_FAILED",
                error_message=f"Could not load image from {image_uri}",
                fallback_action="RETRY",
            )
        )

    results = _model(image, imgsz=_IMGSZ, verbose=False)  # type: ignore[misc]
    if not results or len(results) == 0:
        raise InferenceError(
            PreprocessError(
                error_code="CLASSIFICATION_FAILED",
                error_message="Model returned no results",
                fallback_action="RETRY",
            )
        )

    result = results[0]
    probs = result.probs  # ultralytics Probs object

    # Build probabilities dict using model's own class names
    model_names = result.names  # {0: 'book', 1: 'microfilm', 2: 'newspaper'} etc.
    probabilities: dict[str, float] = {}
    for idx, class_name in model_names.items():
        name = class_name.lower()
        if name in _CLASS_NAMES:
            probabilities[name] = round(float(probs.data[idx]), 4)

    # Ensure all expected classes are present
    for name in _CLASS_NAMES:
        if name not in probabilities:
            probabilities[name] = 0.0

    # Predicted class
    top_idx = int(probs.top1)
    predicted_type = model_names[top_idx].lower()
    if predicted_type not in _CLASS_NAMES:
        predicted_type = "book"  # safety fallback
    confidence = round(float(probs.top1conf), 4)

    return predicted_type, confidence, probabilities


# ── Public API ───────────────────────────────────────────────────────────────

def classify_single(req: ClassifyRequest) -> ClassifyResponse:
    """Classify a single image and return the response."""
    _try_load_model()
    t0 = time.monotonic()

    if _using_mock:
        predicted_type, confidence, probabilities = _run_mock_single(req.image_uri)
    else:
        predicted_type, confidence, probabilities = _run_model_single(req.image_uri)

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    return ClassifyResponse(
        material_type=predicted_type,  # type: ignore[arg-type]
        confidence=confidence,
        probabilities=probabilities,
        processing_time_ms=elapsed_ms,
        warnings=["mock_inference"] if _using_mock else [],
    )


def classify_batch(req: BatchClassifyRequest) -> BatchClassifyResponse:
    """
    Classify multiple images and return the majority-voted material type.

    Samples up to all provided images, classifies each independently,
    then uses majority voting to determine the final type.
    """
    _try_load_model()
    t0 = time.monotonic()

    per_image_results: list[ClassifyResponse] = []
    warnings: list[str] = []

    for idx, image_uri in enumerate(req.image_uris):
        single_req = ClassifyRequest(
            job_id=req.job_id,
            page_number=idx + 1,
            image_uri=image_uri,
        )
        try:
            result = classify_single(single_req)
            per_image_results.append(result)
        except InferenceError:
            warnings.append(f"classification failed for image {idx + 1}: {image_uri}")

    if not per_image_results:
        raise InferenceError(
            PreprocessError(
                error_code="CLASSIFICATION_FAILED",
                error_message="All images failed classification",
                fallback_action="RETRY",
            )
        )

    # ── Majority voting ─────────────────────────────────────────────────────
    votes = Counter(r.material_type for r in per_image_results)
    vote_counts = {t: votes.get(t, 0) for t in _CLASS_NAMES}
    winner = votes.most_common(1)[0][0]

    # Average confidence of images that voted for the winner
    winner_confidences = [r.confidence for r in per_image_results if r.material_type == winner]
    avg_confidence = sum(winner_confidences) / len(winner_confidences)

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    if _using_mock:
        warnings.append("mock_inference")

    return BatchClassifyResponse(
        material_type=winner,  # type: ignore[arg-type]
        confidence=round(avg_confidence, 4),
        vote_counts=vote_counts,
        per_image_results=per_image_results,
        sample_size=len(per_image_results),
        processing_time_ms=elapsed_ms,
        warnings=warnings,
    )
