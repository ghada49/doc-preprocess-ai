"""
services/iep2a/app/backends/paddleocr_backend.py
--------------------------------------------------
IEP2A PaddleOCR layout analysis backend.

This backend uses the official PaddleOCR 3.x LayoutDetection API with
model_name="PP-DocLayoutV2". It does not fall back to PPStructure and it
never fabricates confidence scores.

Production serving is local-artifact-first:
    - default baked model dir: /opt/models/iep2a/paddle/PP-DocLayoutV2
    - default baked version sidecar:
      /opt/models/iep2a/paddle/PP-DocLayoutV2.version
    - no automatic online download unless explicitly enabled for development

Relevant env vars:
    IEP2A_PADDLE_MODEL_DIR             local in-image PP-DocLayoutV2 model dir
    IEP2A_PADDLE_LOCAL_MODEL_DIR       optional mounted local dev override dir
    IEP2A_PADDLE_MODEL_VERSION         optional validation/override input
    IEP2A_PADDLE_ALLOW_ONLINE_DOWNLOAD dev-only escape hatch; default false
    IEP2A_PADDLE_MODEL_SOURCE          optional upstream source selector
                                       (mirrors to PADDLE_PDX_MODEL_SOURCE)
    IEP2A_PADDLE_DISABLE_MODEL_SOURCE_CHECK
                                       disables Paddle hoster connectivity
                                       checks; defaults to true for local model
                                       directories
    IEP2A_PADDLE_DEVICE                "cpu" (default) or "gpu:0" style string

Result parsing expects official PP-DocLayoutV2 boxes containing:
    label
    score
    coordinate  -> [xmin, ymin, xmax, ymax]

Safer production behavior for missing scores:
    - individual detections with missing/invalid scores are dropped with warnings
    - if detections exist but all scored detections are unusable, inference fails
"""

from __future__ import annotations

import logging
import math
import os
import threading
from pathlib import Path
from typing import Any

from shared.schemas.layout import RegionType

from .base import BackendResult, ImageLoadError, LayoutBackend

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_NAME = "PP-DocLayoutV2"
_DEFAULT_MODEL_DIR = Path("/opt/models/iep2a/paddle/PP-DocLayoutV2")
_DEFAULT_MODEL_VERSION = "paddleocr-pp-doclayoutv2"
_OFFICIAL_MODEL_SOURCE_ENV = "PADDLE_PDX_MODEL_SOURCE"
_OFFICIAL_DISABLE_SOURCE_CHECK_ENV = "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def _is_remote_reference(path: str) -> bool:
    return path.startswith(("s3://", "hf://", "http://", "https://"))


def _normalize_label(label: str) -> str:
    normalized = label.strip().lower().replace("-", " ").replace("/", " ")
    return "_".join(normalized.split())


# Conservative mapping only. Labels outside this map are ignored with warnings.
PADDLE_CLASS_MAP: dict[str, RegionType] = {
    "text": RegionType.text_block,
    "title": RegionType.title,
    "document_title": RegionType.title,
    "doc_title": RegionType.title,
    "paragraph_title": RegionType.title,
    "section_header": RegionType.title,
    "section_title": RegionType.title,
    "table": RegionType.table,
    "image": RegionType.image,
    "figure": RegionType.image,
    "chart": RegionType.image,
    "caption": RegionType.caption,
    "figure_title": RegionType.caption,
    "figure_caption": RegionType.caption,
    "table_caption": RegionType.caption,
    "image_caption": RegionType.caption,
}


def _resolve_model_dir() -> tuple[str | None, str]:
    local_override = os.environ.get("IEP2A_PADDLE_LOCAL_MODEL_DIR", "").strip()
    if local_override:
        if _is_remote_reference(local_override):
            raise RuntimeError(
                "IEP2A_PADDLE_LOCAL_MODEL_DIR must be a mounted local directory, "
                f"not a remote reference: {local_override}"
            )
        if not os.path.isdir(local_override):
            raise RuntimeError(f"IEP2A_PADDLE_LOCAL_MODEL_DIR is set but invalid: {local_override}")
        return local_override, "local_override"

    explicit_model_dir = os.environ.get("IEP2A_PADDLE_MODEL_DIR", "").strip()
    if explicit_model_dir:
        if _is_remote_reference(explicit_model_dir):
            raise RuntimeError(
                "IEP2A_PADDLE_MODEL_DIR must point to a local in-image model directory; "
                f"remote references are not supported in serving mode: {explicit_model_dir}"
            )
        if not os.path.isdir(explicit_model_dir):
            raise RuntimeError(f"IEP2A_PADDLE_MODEL_DIR is set but invalid: {explicit_model_dir}")
        return explicit_model_dir, "model_dir"

    default_model_dir = str(_DEFAULT_MODEL_DIR)
    if os.path.isdir(default_model_dir):
        return default_model_dir, "model_dir"

    if _is_truthy(os.environ.get("IEP2A_PADDLE_ALLOW_ONLINE_DOWNLOAD")):
        return None, "online_download"

    raise RuntimeError(
        "IEP2A Paddle model directory not found. Bake PP-DocLayoutV2 into "
        f"{_DEFAULT_MODEL_DIR}, set IEP2A_PADDLE_MODEL_DIR, or set "
        "IEP2A_PADDLE_LOCAL_MODEL_DIR for local development. Online download "
        "is disabled by default."
    )


def _resolve_model_version(
    model_dir: str | None,
    model_source: str,
) -> tuple[str, str, str | None]:
    env_model_version = os.environ.get("IEP2A_PADDLE_MODEL_VERSION", "").strip()

    if model_dir is None:
        if env_model_version:
            return env_model_version, "env_override", None
        return _DEFAULT_MODEL_VERSION, "default_online", None

    version_path = f"{model_dir}.version"
    if os.path.isfile(version_path):
        with open(version_path, encoding="utf-8") as f:
            model_version = f.read().strip()
        if not model_version:
            raise RuntimeError(f"IEP2A Paddle model version sidecar is empty: {version_path}")
        if env_model_version and env_model_version != model_version:
            raise RuntimeError(
                "IEP2A_PADDLE_MODEL_VERSION does not match the loaded model sidecar: "
                f"env={env_model_version!r}, sidecar={model_version!r}, path={version_path}"
            )
        return model_version, "sidecar", version_path

    if model_source != "local_override":
        raise RuntimeError(
            "Missing IEP2A Paddle model version sidecar for baked-in model directory: "
            f"{version_path}. Bake the approved PP-DocLayoutV2 directory and matching "
            "'.version' file into the inference image."
        )

    if env_model_version:
        return env_model_version, "env_override", None

    return f"dev-local:{Path(model_dir).name}", "derived_from_dirname", None


def _extract_boxes(prediction: Any) -> list[dict[str, Any]]:
    if isinstance(prediction, dict):
        if isinstance(prediction.get("boxes"), list):
            return [box for box in prediction["boxes"] if isinstance(box, dict)]
        res = prediction.get("res")
        if isinstance(res, dict) and isinstance(res.get("boxes"), list):
            return [box for box in res["boxes"] if isinstance(box, dict)]

    result_payload = getattr(prediction, "res", None)
    if isinstance(result_payload, dict) and isinstance(result_payload.get("boxes"), list):
        return [box for box in result_payload["boxes"] if isinstance(box, dict)]

    boxes = getattr(prediction, "boxes", None)
    if isinstance(boxes, list):
        return [box for box in boxes if isinstance(box, dict)]

    to_dict = getattr(prediction, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return _extract_boxes(payload)

    return []


def _parse_score(raw_score: Any) -> float | None:
    if raw_score is None:
        return None
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    return score


def _parse_coordinate(raw_coordinate: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw_coordinate, list | tuple) or len(raw_coordinate) < 4:
        return None
    try:
        x1 = float(raw_coordinate[0])
        y1 = float(raw_coordinate[1])
        x2 = float(raw_coordinate[2])
        y2 = float(raw_coordinate[3])
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        return None
    return (x1, y1, x2, y2)


def _collect_detections(
    predictions: list[Any],
) -> tuple[list[tuple[str, tuple[float, float, float, float], float]], list[str]]:
    detections: list[tuple[str, tuple[float, float, float, float], float]] = []
    warnings: list[str] = []

    unknown_labels: set[str] = set()
    invalid_score_labels: set[str] = set()
    invalid_coordinate_labels: set[str] = set()
    malformed_box_count = 0
    total_boxes = 0

    for prediction in predictions:
        boxes = _extract_boxes(prediction)
        for box in boxes:
            total_boxes += 1
            raw_label = str(box.get("label", "")).strip()
            if not raw_label:
                malformed_box_count += 1
                continue

            normalized_label = _normalize_label(raw_label)
            if normalized_label not in PADDLE_CLASS_MAP:
                unknown_labels.add(raw_label)
                continue

            score = _parse_score(box.get("score"))
            if score is None:
                invalid_score_labels.add(raw_label)
                continue

            coordinate = _parse_coordinate(box.get("coordinate", box.get("bbox", box.get("box"))))
            if coordinate is None:
                invalid_coordinate_labels.add(raw_label)
                continue

            detections.append((normalized_label, coordinate, score))

    if unknown_labels:
        warnings.append(
            "Ignored unmapped PaddleOCR PP-DocLayoutV2 labels: " f"{sorted(unknown_labels)}"
        )
    if invalid_score_labels:
        warnings.append(
            "Dropped PaddleOCR detections with missing/invalid scores for labels: "
            f"{sorted(invalid_score_labels)}"
        )
    if invalid_coordinate_labels:
        warnings.append(
            "Dropped PaddleOCR detections with invalid coordinates for labels: "
            f"{sorted(invalid_coordinate_labels)}"
        )
    if malformed_box_count:
        warnings.append(
            "Dropped malformed PaddleOCR detection entries without usable label/box data: "
            f"{malformed_box_count}"
        )

    if (
        total_boxes > 0
        and not detections
        and (invalid_score_labels or invalid_coordinate_labels or malformed_box_count)
    ):
        raise RuntimeError(
            "PaddleOCR PP-DocLayoutV2 returned detections, but none had valid "
            "canonical labels, coordinates, and real scores."
        )

    return detections, warnings


class PaddleOCRBackend(LayoutBackend):
    """IEP2A PaddleOCR PP-DocLayoutV2 layout backend."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engine: Any = None
        self._ready = False
        self._init_error: Exception | None = None
        self._model_version = _DEFAULT_MODEL_VERSION

    def initialize(self) -> None:
        """Load the official PP-DocLayoutV2 backend. Raises RuntimeError on failure."""
        with self._lock:
            if self._ready:
                return
            if self._init_error is not None:
                raise RuntimeError(
                    f"PaddleOCR backend previously failed to initialize: {self._init_error}"
                ) from self._init_error

            requested_model_dir = os.environ.get(
                "IEP2A_PADDLE_MODEL_DIR", str(_DEFAULT_MODEL_DIR)
            ).strip() or str(_DEFAULT_MODEL_DIR)
            local_model_override = os.environ.get("IEP2A_PADDLE_LOCAL_MODEL_DIR", "").strip()
            model_source = os.environ.get("IEP2A_PADDLE_MODEL_SOURCE", "").strip()
            disable_source_check = os.environ.get(
                "IEP2A_PADDLE_DISABLE_MODEL_SOURCE_CHECK", ""
            ).strip()
            device = os.environ.get("IEP2A_PADDLE_DEVICE", "cpu").strip() or "cpu"

            active_model_dir = local_model_override or requested_model_dir
            resolved_model_source = "unresolved"
            resolved_disable_source_check = disable_source_check or None
            resolved_model_version = (
                os.environ.get("IEP2A_PADDLE_MODEL_VERSION", _DEFAULT_MODEL_VERSION).strip()
                or _DEFAULT_MODEL_VERSION
            )
            model_version_source = "env_default"
            model_version_path: str | None = None

            logger.info(
                "Loading IEP2A PaddleOCR PP-DocLayoutV2 backend",
                extra={
                    "requested_model_dir": requested_model_dir,
                    "local_model_override": local_model_override or None,
                    "active_model_dir": active_model_dir,
                    "model_source": resolved_model_source,
                    "disable_model_source_check": resolved_disable_source_check,
                    "device": device,
                    "model_name": _DEFAULT_MODEL_NAME,
                    "model_version": resolved_model_version,
                    "model_version_source": model_version_source,
                    "model_version_path": model_version_path,
                },
            )

            try:
                from paddleocr import LayoutDetection
            except (ImportError, AttributeError) as exc:
                self._init_error = exc
                raise RuntimeError(
                    "PaddleOCR LayoutDetection API is unavailable. "
                    "Install PaddleOCR 3.x with the doc-parser extra and "
                    "PP-DocLayoutV2 support."
                ) from exc

            try:
                model_dir, resolved_model_source = _resolve_model_dir()
                resolved_model_version, model_version_source, model_version_path = (
                    _resolve_model_version(model_dir, resolved_model_source)
                )
                active_model_dir = model_dir or "<official online source>"

                if model_source:
                    os.environ[_OFFICIAL_MODEL_SOURCE_ENV] = model_source
                if disable_source_check:
                    resolved_disable_source_check = disable_source_check
                elif model_dir is not None:
                    resolved_disable_source_check = "True"
                if resolved_disable_source_check:
                    os.environ[_OFFICIAL_DISABLE_SOURCE_CHECK_ENV] = resolved_disable_source_check

                kwargs: dict[str, Any] = {
                    "model_name": _DEFAULT_MODEL_NAME,
                    "device": device,
                }
                if model_dir is not None:
                    kwargs["model_dir"] = model_dir

                self._engine = LayoutDetection(**kwargs)
                self._model_version = resolved_model_version
                self._ready = True
                self._init_error = None

                logger.info(
                    "IEP2A PaddleOCR PP-DocLayoutV2 backend loaded successfully",
                    extra={
                        "requested_model_dir": requested_model_dir,
                        "local_model_override": local_model_override or None,
                        "active_model_dir": active_model_dir,
                        "model_source": resolved_model_source,
                        "disable_model_source_check": resolved_disable_source_check,
                        "device": device,
                        "model_name": _DEFAULT_MODEL_NAME,
                        "model_version": resolved_model_version,
                        "model_version_source": model_version_source,
                        "model_version_path": model_version_path,
                        "paddle_model_source_env": model_source or None,
                    },
                )
            except Exception as exc:
                self._engine = None
                self._ready = False
                self._init_error = exc
                logger.exception(
                    "IEP2A PaddleOCR PP-DocLayoutV2 backend initialization failed",
                    extra={
                        "requested_model_dir": requested_model_dir,
                        "local_model_override": local_model_override or None,
                        "active_model_dir": active_model_dir,
                        "model_source": resolved_model_source,
                        "disable_model_source_check": resolved_disable_source_check,
                        "device": device,
                        "model_name": _DEFAULT_MODEL_NAME,
                        "model_version": resolved_model_version,
                        "model_version_source": model_version_source,
                        "model_version_path": model_version_path,
                        "load_error": str(exc),
                    },
                )
                raise RuntimeError(
                    f"PaddleOCR PP-DocLayoutV2 backend failed to initialize: {exc}"
                ) from exc

    def is_ready(self) -> bool:
        return self._ready and self._init_error is None

    def detect(self, image_uri: str) -> BackendResult:
        if not self.is_ready():
            raise RuntimeError("PaddleOCR PP-DocLayoutV2 backend is not ready; check startup logs")

        import cv2

        from services.iep2a.app.inference import load_image_from_uri, raw_detections_to_regions
        from services.iep2a.app.postprocess import postprocess_regions

        try:
            image_rgb = load_image_from_uri(image_uri)
        except Exception as exc:
            raise ImageLoadError(f"Cannot load image from {image_uri!r}: {exc}") from exc

        image_bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
        h, w = image_bgr.shape[:2]

        raw_predictions = list(
            self._engine.predict(
                image_bgr,
                batch_size=1,
                layout_nms=True,
            )
        )

        raw_detections, warnings = _collect_detections(raw_predictions)
        if warnings:
            for warning in warnings:
                logger.warning(
                    "IEP2A PaddleOCR PP-DocLayoutV2 inference warning",
                    extra={"warning": warning, "image_uri": image_uri},
                )

        raw_regions = raw_detections_to_regions(raw_detections, PADDLE_CLASS_MAP)
        regions, col_struct = postprocess_regions(
            raw_regions,
            page_width=float(w),
            page_height=float(h),
        )

        return BackendResult(
            regions=regions,
            column_structure=col_struct,
            model_version=self._model_version,
            detector_type="paddleocr",
            warnings=warnings,
        )
