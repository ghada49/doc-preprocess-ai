"""
services/iep2b/app/model.py
----------------------------
DocLayout-YOLO model singleton for IEP2B layout detection.

All doclayout_yolo / torch imports are lazy so this file can be imported in
environments that do not have ML dependencies installed. The test suite runs
in stub mode by default and never triggers these imports unless explicitly
testing real-model behavior.

Real-model env vars:
    IEP2B_USE_REAL_MODEL     Enable DocLayout-YOLO inference.
    IEP2B_WEIGHTS_PATH       Primary local in-image weights path.
                             Default: /opt/models/iep2b/
                             doclayout_yolo_docstructbench_imgsz1024.pt
    IEP2B_LOCAL_WEIGHTS_PATH Optional local development override for a mounted
                             weights file. This is not the production default.
    IEP2B_MODEL_VERSION      Validation/override input. Production images should
                             carry `<weights>.version`, which is authoritative.

Exported:
    use_real_model()         True when IEP2B_USE_REAL_MODEL=true
    is_real_model_loaded()   True after a successful load_model() call
    initialize_model_if_configured()
    get_model()              Return loaded model; loads on first call
    reset_for_testing()      Reset singleton state (test isolation only)
"""

from __future__ import annotations

import logging
import os
import threading

_lock = threading.Lock()
_model: object | None = None
_load_error: Exception | None = None
_loaded: bool = False
_loaded_model_version: str | None = None

logger = logging.getLogger(__name__)

_DEFAULT_WEIGHTS_PATH = "/opt/models/iep2b/doclayout_yolo_docstructbench_imgsz1024.pt"
_DEFAULT_MODEL_VERSION = "doclayout-yolo-docstructbench"


def use_real_model() -> bool:
    return os.environ.get("IEP2B_USE_REAL_MODEL", "false").lower() == "true"


def is_real_model_loaded() -> bool:
    return _loaded and _load_error is None


def get_loaded_model_version() -> str:
    if _loaded_model_version:
        return _loaded_model_version
    return os.environ.get("IEP2B_MODEL_VERSION", _DEFAULT_MODEL_VERSION)


def initialize_model_if_configured() -> None:
    if not use_real_model():
        return

    try:
        get_model()
    except RuntimeError as exc:
        logger.warning(
            "IEP2B DocLayout-YOLO warmup failed; readiness will remain not_ready",
            extra={"load_error": str(exc)},
        )


def _is_remote_reference(path: str) -> bool:
    return path.startswith(("hf://", "http://", "https://", "s3://"))


def _resolve_weights_path() -> tuple[str, str]:
    local_override = os.environ.get("IEP2B_LOCAL_WEIGHTS_PATH", "").strip()
    if local_override:
        if _is_remote_reference(local_override):
            raise RuntimeError(
                "IEP2B_LOCAL_WEIGHTS_PATH must be a mounted local file path, "
                f"not a remote reference: {local_override}"
            )
        if not os.path.isfile(local_override):
            raise RuntimeError(f"IEP2B_LOCAL_WEIGHTS_PATH is set but invalid: {local_override}")
        return local_override, "local_override"

    weights_path = os.environ.get("IEP2B_WEIGHTS_PATH", _DEFAULT_WEIGHTS_PATH).strip()
    if not weights_path:
        weights_path = _DEFAULT_WEIGHTS_PATH

    if _is_remote_reference(weights_path):
        raise RuntimeError(
            "IEP2B_WEIGHTS_PATH must point to a local in-image checkpoint; "
            f"remote references are not supported in serving mode: {weights_path}"
        )
    if not os.path.isfile(weights_path):
        raise RuntimeError(
            f"IEP2B weights file not found at {weights_path}. "
            "Build the approved inference image with the DocLayout-YOLO "
            "checkpoint or set IEP2B_LOCAL_WEIGHTS_PATH for local development."
        )

    return weights_path, "weights_path"


def _resolve_model_version(weights_path: str, weights_source: str) -> tuple[str, str, str | None]:
    env_model_version = os.environ.get("IEP2B_MODEL_VERSION", "").strip()
    version_path = f"{weights_path}.version"

    if os.path.isfile(version_path):
        with open(version_path, encoding="utf-8") as f:
            model_version = f.read().strip()
        if not model_version:
            raise RuntimeError(f"IEP2B model version sidecar is empty: {version_path}")
        if env_model_version and env_model_version != model_version:
            raise RuntimeError(
                "IEP2B_MODEL_VERSION does not match the loaded weights sidecar: "
                f"env={env_model_version!r}, sidecar={model_version!r}, path={version_path}"
            )
        return model_version, "sidecar", version_path

    if weights_source != "local_override":
        raise RuntimeError(
            "Missing IEP2B model version sidecar for baked-in weights: "
            f"{version_path}. Bake the approved checkpoint and matching "
            "'.version' file into the inference image."
        )

    if env_model_version:
        return env_model_version, "env_override", None

    return f"dev-local:{os.path.basename(weights_path)}", "derived_from_filename", None


def get_model() -> object:
    global _model, _load_error, _loaded, _loaded_model_version

    if _loaded:
        return _model
    if _load_error is not None:
        raise RuntimeError(f"DocLayout-YOLO model failed to load: {_load_error}") from _load_error

    with _lock:
        if _loaded:
            return _model
        if _load_error is not None:
            raise RuntimeError(
                f"DocLayout-YOLO model failed to load: {_load_error}"
            ) from _load_error

        requested_weights_path = (
            os.environ.get("IEP2B_WEIGHTS_PATH", _DEFAULT_WEIGHTS_PATH).strip()
            or _DEFAULT_WEIGHTS_PATH
        )
        local_weights_override = os.environ.get("IEP2B_LOCAL_WEIGHTS_PATH", "").strip()
        model_version = os.environ.get("IEP2B_MODEL_VERSION", _DEFAULT_MODEL_VERSION)
        active_weights_path = local_weights_override or requested_weights_path
        weights_source = "unresolved"
        model_version_source = "env_default"
        model_version_path: str | None = None

        logger.info(
            "Loading IEP2B DocLayout-YOLO model",
            extra={
                "requested_weights_path": requested_weights_path,
                "local_weights_override": local_weights_override or None,
                "active_weights_path": active_weights_path,
                "weights_source": weights_source,
                "model_version": model_version,
                "model_version_source": model_version_source,
                "model_version_path": model_version_path,
            },
        )

        try:
            from doclayout_yolo import YOLOv10

            weights_path, weights_source = _resolve_weights_path()
            model_version, model_version_source, model_version_path = _resolve_model_version(
                weights_path, weights_source
            )
            active_weights_path = weights_path
            _model = YOLOv10(weights_path)
            _loaded = True
            _loaded_model_version = model_version

            logger.info(
                "IEP2B DocLayout-YOLO model loaded successfully",
                extra={
                    "requested_weights_path": requested_weights_path,
                    "local_weights_override": local_weights_override or None,
                    "active_weights_path": active_weights_path,
                    "weights_source": weights_source,
                    "model_version": model_version,
                    "model_version_source": model_version_source,
                    "model_version_path": model_version_path,
                },
            )
        except Exception as exc:
            _model = None
            _loaded = False
            _loaded_model_version = None
            _load_error = exc
            logger.exception(
                "IEP2B DocLayout-YOLO model initialization failed",
                extra={
                    "requested_weights_path": requested_weights_path,
                    "local_weights_override": local_weights_override or None,
                    "active_weights_path": active_weights_path,
                    "weights_source": weights_source,
                    "model_version": model_version,
                    "model_version_source": model_version_source,
                    "model_version_path": model_version_path,
                    "load_error": str(exc),
                },
            )
            raise RuntimeError(f"DocLayout-YOLO model failed to load: {exc}") from exc

    return _model


def reset_for_testing() -> None:
    global _model, _load_error, _loaded, _loaded_model_version
    with _lock:
        _model = None
        _load_error = None
        _loaded = False
        _loaded_model_version = None
