from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

_lock = threading.Lock()
_predictor: object | None = None
_load_error: Exception | None = None
_loaded: bool = False
_loaded_model_version: str | None = None

logger = logging.getLogger(__name__)

_DEFAULT_WEIGHTS_PATH = "/opt/models/iep2a/model_final.pth"
_DEFAULT_SCORE_THRESH = 0.5
_DEFAULT_NUM_CLASSES = 5
_DEFAULT_MODEL_VERSION = "detectron2-publaynet-r50-fpn-3x"
_DEFAULT_CONFIG_SENTINEL = "<detectron2 packaged default>"
_DEFAULT_CONFIG_RELATIVE_PATH = (
    Path("model_zoo") / "configs" / "COCO-Detection" / "faster_rcnn_R_50_FPN_3x.yaml"
)
_DEFAULT_LABEL_MAP = {
    0: "Text",
    1: "Title",
    2: "List",
    3: "Table",
    4: "Figure",
}


def use_real_model() -> bool:
    return os.environ.get("IEP2A_USE_REAL_MODEL", "false").lower() == "true"


def is_real_model_loaded() -> bool:
    return _loaded and _load_error is None


def get_loaded_model_version() -> str:
    if _loaded_model_version:
        return _loaded_model_version
    return os.environ.get("IEP2A_MODEL_VERSION", _DEFAULT_MODEL_VERSION)


def initialize_model_if_configured() -> None:
    if not use_real_model():
        return

    try:
        get_predictor()
    except RuntimeError as exc:
        logger.warning(
            "IEP2A Detectron2 warmup failed; readiness will remain not_ready",
            extra={"load_error": str(exc)},
        )


def _is_remote_reference(path: str) -> bool:
    return path.startswith(("lp://", "http://", "https://", "hf://", "s3://"))


def _resolve_default_config_path() -> str:
    import detectron2

    config_path = Path(detectron2.__file__).resolve().parent / _DEFAULT_CONFIG_RELATIVE_PATH
    if not config_path.is_file():
        raise RuntimeError(f"Detectron2 packaged config not found: {config_path}")
    return str(config_path)


def _resolve_config_path() -> tuple[str, str]:
    legacy_config_path = os.environ.get("IEP2A_CONFIG_FILE", "").strip()
    if legacy_config_path:
        raise RuntimeError("IEP2A_CONFIG_FILE is not supported. Use IEP2A_CONFIG_PATH instead.")

    explicit_config_path = os.environ.get("IEP2A_CONFIG_PATH", "").strip()
    if explicit_config_path:
        if _is_remote_reference(explicit_config_path):
            raise RuntimeError(
                "IEP2A_CONFIG_PATH must be a local in-image file path; "
                f"remote references are not supported: {explicit_config_path}"
            )
        if not os.path.isfile(explicit_config_path):
            raise RuntimeError(f"IEP2A config file not found at {explicit_config_path}")
        return explicit_config_path, "env"

    return _resolve_default_config_path(), "detectron2_package"


def _resolve_weights_path() -> tuple[str, str]:
    local_override = os.environ.get("IEP2A_LOCAL_WEIGHTS_PATH", "").strip()
    if local_override:
        if _is_remote_reference(local_override):
            raise RuntimeError(
                "IEP2A_LOCAL_WEIGHTS_PATH must be a mounted local file path, "
                f"not a remote reference: {local_override}"
            )
        if not os.path.isfile(local_override):
            raise RuntimeError(f"IEP2A_LOCAL_WEIGHTS_PATH is set but invalid: {local_override}")
        return local_override, "local_override"

    weights_path = os.environ.get("IEP2A_WEIGHTS_PATH", _DEFAULT_WEIGHTS_PATH).strip()
    if not weights_path:
        weights_path = _DEFAULT_WEIGHTS_PATH

    if _is_remote_reference(weights_path):
        raise RuntimeError(
            "IEP2A_WEIGHTS_PATH must point to a local in-image checkpoint; "
            f"remote references are not supported in serving mode: {weights_path}"
        )
    if not os.path.isfile(weights_path):
        raise RuntimeError(
            f"IEP2A weights file not found at {weights_path}. "
            "Build the approved inference image with the PubLayNet checkpoint "
            "or set IEP2A_LOCAL_WEIGHTS_PATH for local development."
        )

    return weights_path, "weights_path"


def _resolve_model_version(weights_path: str, weights_source: str) -> tuple[str, str, str | None]:
    env_model_version = os.environ.get("IEP2A_MODEL_VERSION", "").strip()
    version_path = f"{weights_path}.version"

    if os.path.isfile(version_path):
        with open(version_path, encoding="utf-8") as f:
            model_version = f.read().strip()
        if not model_version:
            raise RuntimeError(f"IEP2A model version sidecar is empty: {version_path}")
        if env_model_version and env_model_version != model_version:
            raise RuntimeError(
                "IEP2A_MODEL_VERSION does not match the loaded weights sidecar: "
                f"env={env_model_version!r}, sidecar={model_version!r}, path={version_path}"
            )
        return model_version, "sidecar", version_path

    if weights_source != "local_override":
        raise RuntimeError(
            "Missing IEP2A model version sidecar for baked-in weights: "
            f"{version_path}. Bake the approved checkpoint and matching "
            "'.version' file into the inference image."
        )

    if env_model_version:
        return env_model_version, "env_override", None

    return f"dev-local:{Path(weights_path).name}", "derived_from_filename", None


def get_predictor() -> object:
    global _predictor, _load_error, _loaded, _loaded_model_version

    if _loaded:
        return _predictor
    if _load_error is not None:
        raise RuntimeError(f"Detectron2 model failed to load: {_load_error}") from _load_error

    with _lock:
        if _loaded:
            return _predictor
        if _load_error is not None:
            raise RuntimeError(f"Detectron2 model failed to load: {_load_error}") from _load_error

        requested_config_path = (
            os.environ.get("IEP2A_CONFIG_PATH", "").strip() or _DEFAULT_CONFIG_SENTINEL
        )
        legacy_config_path = os.environ.get("IEP2A_CONFIG_FILE", "").strip()
        requested_weights_path = (
            os.environ.get("IEP2A_WEIGHTS_PATH", _DEFAULT_WEIGHTS_PATH).strip()
            or _DEFAULT_WEIGHTS_PATH
        )
        local_weights_override = os.environ.get("IEP2A_LOCAL_WEIGHTS_PATH", "").strip()
        score_thresh = float(os.environ.get("IEP2A_SCORE_THRESH", str(_DEFAULT_SCORE_THRESH)))
        num_classes = int(os.environ.get("IEP2A_NUM_CLASSES", str(_DEFAULT_NUM_CLASSES)))
        device = os.environ.get("IEP2A_DEVICE", "cpu").lower()

        active_config_path = requested_config_path
        active_weights_path = local_weights_override or requested_weights_path
        config_source = "unresolved"
        weights_source = "unresolved"
        model_version = os.environ.get("IEP2A_MODEL_VERSION", _DEFAULT_MODEL_VERSION)
        model_version_source = "env_default"
        model_version_path: str | None = None

        logger.info(
            "Loading IEP2A Detectron2 model",
            extra={
                "requested_config_path": requested_config_path,
                "legacy_config_path": legacy_config_path or None,
                "requested_weights_path": requested_weights_path,
                "local_weights_override": local_weights_override or None,
                "active_config_path": active_config_path,
                "active_weights_path": active_weights_path,
                "config_source": config_source,
                "weights_source": weights_source,
                "device": device,
                "score_thresh": score_thresh,
                "num_classes": num_classes,
                "model_version": model_version,
                "model_version_source": model_version_source,
                "model_version_path": model_version_path,
            },
        )

        try:
            from layoutparser.models import Detectron2LayoutModel

            config_path, config_source = _resolve_config_path()
            weights_path, weights_source = _resolve_weights_path()
            model_version, model_version_source, model_version_path = _resolve_model_version(
                weights_path, weights_source
            )
            active_config_path = config_path
            active_weights_path = weights_path

            _predictor = Detectron2LayoutModel(
                config_path=config_path,
                model_path=weights_path,
                label_map=_DEFAULT_LABEL_MAP,
                extra_config=[
                    "MODEL.ROI_HEADS.SCORE_THRESH_TEST",
                    score_thresh,
                    "MODEL.DEVICE",
                    device,
                    "MODEL.ROI_HEADS.NUM_CLASSES",
                    num_classes,
                ],
            )

            _loaded = True
            _loaded_model_version = model_version
            logger.info(
                "IEP2A Detectron2 model loaded successfully",
                extra={
                    "requested_config_path": requested_config_path,
                    "legacy_config_path": legacy_config_path or None,
                    "requested_weights_path": requested_weights_path,
                    "local_weights_override": local_weights_override or None,
                    "active_config_path": active_config_path,
                    "active_weights_path": active_weights_path,
                    "config_source": config_source,
                    "weights_source": weights_source,
                    "model_version": model_version,
                    "model_version_source": model_version_source,
                    "model_version_path": model_version_path,
                },
            )
        except Exception as exc:
            _predictor = None
            _loaded = False
            _loaded_model_version = None
            _load_error = exc
            logger.exception(
                "IEP2A Detectron2 model initialization failed",
                extra={
                    "requested_config_path": requested_config_path,
                    "legacy_config_path": legacy_config_path or None,
                    "requested_weights_path": requested_weights_path,
                    "local_weights_override": local_weights_override or None,
                    "active_config_path": active_config_path,
                    "active_weights_path": active_weights_path,
                    "config_source": config_source,
                    "weights_source": weights_source,
                    "model_version": model_version,
                    "model_version_source": model_version_source,
                    "model_version_path": model_version_path,
                    "load_error": str(exc),
                },
            )
            raise RuntimeError(f"Detectron2 model failed to load: {exc}") from exc

    return _predictor


def reset_for_testing() -> None:
    global _predictor, _load_error, _loaded, _loaded_model_version
    with _lock:
        _predictor = None
        _load_error = None
        _loaded = False
        _loaded_model_version = None
