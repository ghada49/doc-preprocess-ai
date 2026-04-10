from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import torch

from services.iep1d.app.uvdoc import UVDocRectifier

_lock = threading.Lock()
_rectifier: UVDocRectifier | None = None
_load_error: Exception | None = None
_loaded = False
_loaded_device: str | None = None
_loaded_model_version: str | None = None
_loaded_weights_path: str | None = None

logger = logging.getLogger(__name__)

_DEFAULT_WEIGHTS_PATH = "/opt/models/iep1d/best_model.pkl"
_DEFAULT_MODEL_VERSION = "uvdoc-official"


def is_model_ready() -> bool:
    return _loaded and _load_error is None


def get_model_status() -> dict[str, str | bool | None]:
    extra_error: str | None = None
    try:
        device = _loaded_device or _resolve_device_name(raise_on_unavailable=False)
    except RuntimeError as exc:
        device = None
        extra_error = str(exc)

    return {
        "ready": is_model_ready(),
        "device": device,
        "weights_path": _loaded_weights_path or _requested_weights_path(),
        "model_version": _loaded_model_version or os.environ.get("IEP1D_MODEL_VERSION") or None,
        "error": str(_load_error) if _load_error is not None else extra_error,
    }


def initialize_model_if_configured() -> None:
    try:
        get_rectifier()
    except RuntimeError as exc:
        logger.warning(
            "IEP1D UVDoc warmup failed; readiness will remain not_ready",
            extra={"load_error": str(exc)},
        )


def _is_remote_reference(path: str) -> bool:
    return path.startswith(("lp://", "http://", "https://", "hf://", "s3://"))


def _requested_weights_path() -> str:
    requested = os.environ.get("IEP1D_WEIGHTS_PATH", _DEFAULT_WEIGHTS_PATH).strip()
    return requested or _DEFAULT_WEIGHTS_PATH


def _resolve_weights_path() -> tuple[str, str]:
    local_override = os.environ.get("IEP1D_LOCAL_WEIGHTS_PATH", "").strip()
    if local_override:
        if _is_remote_reference(local_override):
            raise RuntimeError(
                "IEP1D_LOCAL_WEIGHTS_PATH must point to a mounted local checkpoint, "
                f"not a remote reference: {local_override}"
            )
        if not os.path.isfile(local_override):
            raise RuntimeError(
                "IEP1D_LOCAL_WEIGHTS_PATH is set but invalid: "
                f"{local_override}. Download the official UVDoc checkpoint into "
                "./models/iep1d/best_model.pkl or point this variable at the mounted file."
            )
        return local_override, "local_override"

    weights_path = _requested_weights_path()
    if _is_remote_reference(weights_path):
        raise RuntimeError(
            "IEP1D_WEIGHTS_PATH must point to a local in-image checkpoint; "
            f"remote references are not supported in serving mode: {weights_path}"
        )
    if not os.path.isfile(weights_path):
        raise RuntimeError(
            f"IEP1D weights file not found at {weights_path}. "
            "Bake the approved UVDoc checkpoint into the image or mount it at "
            "./models/iep1d/best_model.pkl for local development."
        )
    return weights_path, "weights_path"


def _resolve_model_version(weights_path: str) -> str:
    env_model_version = os.environ.get("IEP1D_MODEL_VERSION", "").strip()
    version_path = f"{weights_path}.version"
    if os.path.isfile(version_path):
        with open(version_path, encoding="utf-8") as handle:
            model_version = handle.read().strip()
        if not model_version:
            raise RuntimeError(f"IEP1D model version sidecar is empty: {version_path}")
        if env_model_version and env_model_version != model_version:
            raise RuntimeError(
                "IEP1D_MODEL_VERSION does not match the loaded weights sidecar: "
                f"env={env_model_version!r}, sidecar={model_version!r}, path={version_path}"
            )
        return model_version
    if env_model_version:
        return env_model_version
    return f"{_DEFAULT_MODEL_VERSION}:{Path(weights_path).name}"


def _resolve_device_name(*, raise_on_unavailable: bool = True) -> str:
    requested = (os.environ.get("IEP1D_DEVICE", "auto").strip() or "auto").lower()
    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested == "cpu":
        return "cpu"
    if requested.startswith("cuda"):
        if torch.cuda.is_available():
            return requested
        if raise_on_unavailable:
            raise RuntimeError(
                f"IEP1D_DEVICE={requested!r} was requested but CUDA is not available"
            )
        return requested
    raise RuntimeError("IEP1D_DEVICE must be 'auto', 'cpu', or a CUDA device like 'cuda:0'")


def get_rectifier() -> UVDocRectifier:
    global _rectifier, _load_error, _loaded
    global _loaded_device, _loaded_model_version, _loaded_weights_path

    if _loaded:
        assert _rectifier is not None
        return _rectifier
    if _load_error is not None:
        raise RuntimeError(f"UVDoc model failed to load: {_load_error}") from _load_error

    with _lock:
        if _loaded:
            assert _rectifier is not None
            return _rectifier
        if _load_error is not None:
            raise RuntimeError(f"UVDoc model failed to load: {_load_error}") from _load_error

        requested_weights_path = _requested_weights_path()
        local_weights_override = os.environ.get("IEP1D_LOCAL_WEIGHTS_PATH", "").strip() or None
        active_weights_path = local_weights_override or requested_weights_path
        device = _resolve_device_name()
        model_version = os.environ.get("IEP1D_MODEL_VERSION", _DEFAULT_MODEL_VERSION)

        logger.info(
            "Loading IEP1D UVDoc model",
            extra={
                "requested_weights_path": requested_weights_path,
                "local_weights_override": local_weights_override,
                "active_weights_path": active_weights_path,
                "device": device,
                "model_version": model_version,
            },
        )

        try:
            weights_path, _weights_source = _resolve_weights_path()
            model_version = _resolve_model_version(weights_path)
            active_weights_path = weights_path

            _rectifier = UVDocRectifier.from_checkpoint(weights_path, device=device)
            _loaded = True
            _loaded_device = device
            _loaded_model_version = model_version
            _loaded_weights_path = active_weights_path

            logger.info(
                "IEP1D UVDoc model loaded successfully",
                extra={
                    "active_weights_path": active_weights_path,
                    "device": device,
                    "model_version": model_version,
                },
            )
        except Exception as exc:
            _rectifier = None
            _loaded = False
            _loaded_device = None
            _loaded_model_version = None
            _loaded_weights_path = active_weights_path
            _load_error = exc
            logger.exception(
                "IEP1D UVDoc model initialization failed",
                extra={
                    "requested_weights_path": requested_weights_path,
                    "local_weights_override": local_weights_override,
                    "active_weights_path": active_weights_path,
                    "device": device,
                    "model_version": model_version,
                    "load_error": str(exc),
                },
            )
            raise RuntimeError(f"UVDoc model failed to load: {exc}") from exc

    assert _rectifier is not None
    return _rectifier


def reset_for_testing() -> None:
    global _rectifier, _load_error, _loaded
    global _loaded_device, _loaded_model_version, _loaded_weights_path
    with _lock:
        _rectifier = None
        _load_error = None
        _loaded = False
        _loaded_device = None
        _loaded_model_version = None
        _loaded_weights_path = None
