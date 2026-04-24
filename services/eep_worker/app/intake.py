"""
services/eep_worker/app/intake.py
-----------------------------------
Packet 4.3a — OTIFF intake, SHA-256 hash computation, and proxy image derivation.

Implements Step 1 of the EEP worker pipeline (spec Section 6.1, Step 1):

  1. Resolve and download the OTIFF from the provided URI.
  2. Compute SHA-256 hash; store in page_lineage.input_image_hash.
  3. If prior lineage exists with a different hash, raise OtiffHashMismatchError
     (data integrity violation — page routes to `failed`, spec Section 18.10).
  4. Decode raw bytes to a numpy uint8 BGR array (H×W×C).
  5. Derive a proxy (downscaled) image for geometry inference.

Proxy resolution (spec Section 6.2):
  "Proxy resolution is an implementation-critical parameter.  It must be
   calibrated empirically on a held-out AUB validation set.  The system must
   not assume one fixed downscale ratio is equally safe for books, newspapers,
   and microfilm frames."
  ProxyConfig.max_long_edge_px maps each material_type to the maximum pixel
  length of the longer image edge.  Defaults are illustrative starting points.

Error classification (spec Section 18.14):
  OtiffDecodeError      — OTIFF cannot be decoded or displayed  → failed
  OtiffHashMismatchError — hash mismatch (data integrity violation) → failed
  OtiffLoadError        — URI cannot be fetched (caller decides retry vs fail)

Exported:
    ProxyConfig             — per-material-type proxy resolution configuration
    OtiffHashMismatchError  — raised on input_image_hash mismatch vs prior lineage
    OtiffDecodeError        — raised when bytes cannot be decoded as an image
    OtiffLoadError          — raised when the URI cannot be fetched from storage
    compute_hash            — SHA-256 hex digest of raw bytes
    load_otiff              — fetch raw bytes via StorageBackend
    decode_otiff            — bytes → numpy uint8 BGR array
    check_hash_consistency  — raise OtiffHashMismatchError if hashes differ
    derive_proxy            — downscale image array to proxy resolution
"""

from __future__ import annotations

import dataclasses
import hashlib

import cv2
import numpy as np

from shared.io.storage import StorageBackend

__all__ = [
    "ProxyConfig",
    "OtiffHashMismatchError",
    "OtiffDecodeError",
    "OtiffLoadError",
    "compute_hash",
    "load_otiff",
    "decode_otiff",
    "check_hash_consistency",
    "derive_proxy",
]


# ── Errors ─────────────────────────────────────────────────────────────────────


class OtiffHashMismatchError(ValueError):
    """
    Raised when the SHA-256 hash of the downloaded OTIFF differs from the
    hash stored in a prior page_lineage record.

    Per spec Section 18.10 / 18.14: this is a data integrity violation.
    The page MUST be routed to ``failed`` and must not be retried.
    """

    def __init__(self, uri: str, expected: str, actual: str) -> None:
        super().__init__(
            f"OTIFF hash mismatch for {uri!r}: "
            f"expected {expected!r}, got {actual!r}. "
            "Data integrity violation — page routed to `failed`."
        )
        self.uri = uri
        self.expected = expected
        self.actual = actual


class OtiffDecodeError(ValueError):
    """
    Raised when the raw bytes cannot be decoded into an image array.

    Per spec Section 18.14: if the OTIFF cannot be decoded or displayed,
    the page MUST be routed to ``failed`` and must not be retried.
    """

    def __init__(self, uri: str, detail: str = "") -> None:
        msg = f"Cannot decode OTIFF at {uri!r}"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)
        self.uri = uri


class OtiffLoadError(OSError):
    """
    Raised when the OTIFF URI cannot be fetched from storage.

    Callers decide whether to retry (transient network errors) or route the
    page to ``failed`` (after retry budget is exhausted).
    """

    def __init__(self, uri: str, cause: Exception) -> None:
        super().__init__(f"Failed to load OTIFF from {uri!r}: {cause}")
        self.uri = uri
        self.cause = cause


# ── Proxy configuration ────────────────────────────────────────────────────────


@dataclasses.dataclass
class ProxyConfig:
    """
    Per-material-type proxy image resolution configuration.

    ``max_long_edge_px`` maps each material_type to the maximum pixel length
    of the longer image dimension.  If an image is already smaller than the
    configured limit it is returned unchanged.

    Defaults are illustrative starting points only.  Per spec Section 6.2,
    operators MUST calibrate these empirically on a held-out AUB validation
    set before deploying to production.
    """

    max_long_edge_px: dict[str, int] = dataclasses.field(
        default_factory=lambda: {
            "book": 1024,
            "newspaper": 1024,
            "archival_document": 1024,
            "microfilm": 1024,
        }
    )


# ── Core functions ─────────────────────────────────────────────────────────────


def compute_hash(data: bytes) -> str:
    """
    Return the SHA-256 hex digest of *data*.

    This is the canonical hash stored in ``page_lineage.input_image_hash``
    (spec Section 6.1, Step 1).

    Args:
        data: Raw file bytes.

    Returns:
        Lower-case 64-character hex string of the SHA-256 digest.
    """
    return hashlib.sha256(data).hexdigest()


def load_otiff(uri: str, storage: StorageBackend) -> bytes:
    """
    Download and return the raw bytes of the OTIFF at *uri*.

    Wraps storage backend exceptions in ``OtiffLoadError`` so callers can
    make a uniform retry-vs-fail decision without inspecting backend-specific
    exception types.

    Args:
        uri:     Storage URI (``s3://`` or ``file://``).
        storage: StorageBackend instance.  In production, obtain via
                 ``shared.io.storage.get_backend(uri)``.

    Returns:
        Raw file bytes.

    Raises:
        OtiffLoadError: If the storage backend raises any exception.
    """
    try:
        return storage.get_bytes(uri)
    except Exception as exc:
        raise OtiffLoadError(uri, exc) from exc


def decode_otiff(data: bytes, uri: str = "<unknown>") -> np.ndarray:
    """
    Decode raw OTIFF bytes into a numpy uint8 BGR image array (H×W×C).

    Uses OpenCV's imdecode, which supports TIFF, JPEG, PNG, and other common
    formats.  Grayscale images are promoted to 3-channel BGR via
    ``cv2.IMREAD_COLOR``.

    Args:
        data: Raw file bytes from ``load_otiff()``.
        uri:  Source URI for error message attribution (optional;
              defaults to ``"<unknown>"``).

    Returns:
        numpy array of shape (H, W, 3), dtype uint8, channels in BGR order.

    Raises:
        OtiffDecodeError: If the bytes cannot be decoded (corrupt, truncated,
                          or unsupported format).  The page must be routed to
                          ``failed`` (spec Section 18.14).
    """
    try:
        buf = np.frombuffer(data, dtype=np.uint8)
        image: np.ndarray | None = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise OtiffDecodeError(uri, str(exc)) from exc
    if image is None:
        raise OtiffDecodeError(uri, "cv2.imdecode returned None")
    return image


def check_hash_consistency(
    uri: str,
    current_hash: str,
    prior_hash: str | None,
) -> None:
    """
    Assert that *current_hash* matches *prior_hash* when a prior record exists.

    If *prior_hash* is ``None`` (first-time processing — no prior lineage row)
    the check is a no-op.

    Args:
        uri:          Source URI for error message attribution.
        current_hash: SHA-256 hex digest of the just-downloaded bytes.
        prior_hash:   ``input_image_hash`` from a prior page_lineage row,
                      or ``None`` if no prior record exists.

    Raises:
        OtiffHashMismatchError: If *prior_hash* is not None and differs from
                                *current_hash*.  Caller must route the page to
                                ``failed`` (spec Section 18.10 / 18.14).
    """
    if prior_hash is not None and current_hash != prior_hash:
        raise OtiffHashMismatchError(uri, expected=prior_hash, actual=current_hash)


def derive_proxy(
    image: np.ndarray,
    material_type: str,
    config: ProxyConfig | None = None,
) -> np.ndarray:
    """
    Derive a proxy (downscaled) image for geometry inference.

    Scales the image so that its longer dimension is at most
    ``config.max_long_edge_px[material_type]`` pixels, preserving aspect ratio.
    If the image is already within the configured limit, it is returned
    unchanged (same object — no copy).

    Downscaling uses ``cv2.INTER_AREA``, which is the recommended
    interpolation mode for image shrinking in OpenCV.

    Args:
        image:         Full-resolution uint8 BGR numpy array (H×W×C).
        material_type: One of ``'book'``, ``'newspaper'``,
                       ``'archival_document'``.
        config:        ProxyConfig instance.  Uses a default-constructed
                       ``ProxyConfig()`` when ``None``.

    Returns:
        Proxy numpy array (same dtype as input, possibly the same object if no
        scaling was required).

    Raises:
        ValueError: If *material_type* is not in ``config.max_long_edge_px``.
    """
    if config is None:
        config = ProxyConfig()

    if material_type not in config.max_long_edge_px:
        raise ValueError(
            f"Unknown material_type: {material_type!r}. "
            f"Known types: {sorted(config.max_long_edge_px)}"
        )

    h, w = image.shape[:2]
    max_edge = config.max_long_edge_px[material_type]
    long_edge = max(h, w)

    if long_edge <= max_edge:
        return image  # already within the configured limit

    scale = max_edge / long_edge
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
