"""services.eep.app.gates.artifact_validation
--------------------------------------------
Artifact validation gate for the IEP1 preprocessing pipeline.

Implements artifact validation as defined in spec Section 6.9.

Packet 3.4: hard requirements — five checks that must all pass before scoring.
Packet 3.5: soft signal scoring — weighted score + threshold.  [pending]

Exported (Packet 3.4):
    ArtifactImageDimensions    — (width, height) carrier returned by image_loader
    ArtifactHardCheckResult    — result of the five hard requirement checks
    ARTIFACT_HARD_CHECK_NAMES  — canonical names for the five hard checks
    check_artifact_hard_requirements — run all five hard checks against one artifact
    make_cv2_image_loader      — production image loader factory (cv2 / OpenCV)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from shared.schemas.preprocessing import PreprocessBranchResponse

# ---------------------------------------------------------------------------
# Image dimensions carrier
# ---------------------------------------------------------------------------


@dataclass
class ArtifactImageDimensions:
    """
    Width and height of a decoded artifact image.

    Returned by the image_loader callable passed to
    check_artifact_hard_requirements.

    The image_loader contract:
      - Return ArtifactImageDimensions on success.
      - Raise FileNotFoundError if the URI does not resolve to a readable file.
      - Raise any other exception (ValueError, OSError, etc.) if the file
        exists but cannot be decoded as a valid image.
    """

    width: int
    height: int


# ---------------------------------------------------------------------------
# Hard check names and result
# ---------------------------------------------------------------------------

#: Canonical names for the five artifact hard checks (spec Section 6.9).
ARTIFACT_HARD_CHECK_NAMES: tuple[str, ...] = (
    "file_exists",
    "valid_image",
    "non_degenerate",
    "bounds_consistent",
    "dimensions_consistent",
)


@dataclass
class ArtifactHardCheckResult:
    """
    Result of running all five hard requirement checks against one artifact.

    Attributes:
        passed        — True only if ALL five checks pass.
        failed_checks — list of canonical check names that failed (empty when
                        passed=True).  Suitable for storage in quality_gate_log
                        JSONB column sanity_check_results.
    """

    passed: bool
    failed_checks: list[str]

    def as_dict(self) -> dict[str, object]:
        """Serialize to a plain dict suitable for JSONB storage."""
        return {"passed": self.passed, "failed_checks": list(self.failed_checks)}


# ---------------------------------------------------------------------------
# Hard check implementation  (spec Section 6.9 "Hard Requirements")
# ---------------------------------------------------------------------------


def check_artifact_hard_requirements(
    response: PreprocessBranchResponse,
    image_loader: Callable[[str], ArtifactImageDimensions],
    dimension_tolerance: int = 2,
) -> ArtifactHardCheckResult:
    """
    Apply all five hard requirement checks to a single normalized artifact.

    Any failure → artifact is invalid; soft scoring must not be performed.

    The five checks (spec Section 6.9 "Hard Requirements"):
      1. file_exists          — URI resolves to a readable file.
      2. valid_image          — file decodes as a valid image without error.
      3. non_degenerate       — decoded image width > 0 and height > 0.
      4. bounds_consistent    — crop box coordinates within original image bounds.
      5. dimensions_consistent — actual artifact dimensions match the expected
                                 post_preprocessing_dimensions (within tolerance).

    Checks 1–3 require the image to be loaded.  If the file is missing (check 1
    fails), the function returns early — no further checks are possible.  If the
    file exists but cannot be decoded (check 2 fails), checks 3 and 5 are skipped
    because they require valid dimensions; check 4 is always evaluated since it
    is data-only.

    Args:
        response            — PreprocessBranchResponse from IEP1C normalization
        image_loader        — callable ``(uri) → ArtifactImageDimensions``; must
                              raise ``FileNotFoundError`` for missing files and
                              any other exception for decode failures
        dimension_tolerance — allowed pixel difference (per axis) between actual
                              and expected post_preprocessing_dimensions; accounts
                              for TIFF rounding.  Default: 2 pixels.
    """
    failed: list[str] = []
    dims: ArtifactImageDimensions | None = None

    # -------------------------------------------------------------------
    # Checks 1 & 2: File exists + valid image
    # -------------------------------------------------------------------
    try:
        dims = image_loader(response.processed_image_uri)
    except FileNotFoundError:
        failed.append("file_exists")
        # Cannot continue — no file to inspect for remaining checks.
        return ArtifactHardCheckResult(passed=False, failed_checks=failed)
    except Exception:
        # File is accessible but cannot be decoded as a valid image.
        failed.append("valid_image")

    # -------------------------------------------------------------------
    # Check 3: Non-degenerate dimensions
    # -------------------------------------------------------------------
    if dims is not None:
        if not (dims.width > 0 and dims.height > 0):
            failed.append("non_degenerate")

    # -------------------------------------------------------------------
    # Check 4: Bounds consistency  (data-only — no I/O needed)
    # Crop box must lie within the original image dimensions recorded in the
    # TransformRecord.  This is already enforced by the Pydantic schema
    # validator; the explicit gate check is a defense-in-depth redundancy.
    # -------------------------------------------------------------------
    crop = response.transform.crop_box
    orig = response.transform.original_dimensions
    if not (
        crop.x_min >= 0
        and crop.y_min >= 0
        and crop.x_max <= orig.width
        and crop.y_max <= orig.height
    ):
        failed.append("bounds_consistent")

    # -------------------------------------------------------------------
    # Check 5: Dimension consistency
    # The actual decoded image dimensions must match post_preprocessing_dimensions
    # within the rounding tolerance.  Only performed when we have valid dims
    # (check 2 passed) and those dims are non-degenerate (check 3 passed).
    # -------------------------------------------------------------------
    if dims is not None and "non_degenerate" not in failed:
        expected = response.transform.post_preprocessing_dimensions
        if not (
            abs(dims.width - expected.width) <= dimension_tolerance
            and abs(dims.height - expected.height) <= dimension_tolerance
        ):
            failed.append("dimensions_consistent")

    return ArtifactHardCheckResult(passed=len(failed) == 0, failed_checks=failed)


# ---------------------------------------------------------------------------
# Production image loader factory
# ---------------------------------------------------------------------------


def make_cv2_image_loader(
    storage: object,
) -> Callable[[str], ArtifactImageDimensions]:
    """
    Build an image_loader callable backed by a StorageBackend and OpenCV.

    The returned callable reads raw bytes from the storage backend and decodes
    them with cv2.imdecode.  TIFF and common raster formats are supported.

    Args:
        storage — a StorageBackend instance (shared.io.storage); must expose
                  ``get_bytes(uri: str) -> bytes``.

    Returns:
        Callable[[str], ArtifactImageDimensions] suitable for passing to
        check_artifact_hard_requirements.

    Raises:
        ImportError if cv2 (opencv-python-headless) or numpy are not installed.
    """
    import cv2  # noqa: PLC0415 — deferred; not available in all environments
    import numpy as np  # noqa: PLC0415

    def _load(uri: str) -> ArtifactImageDimensions:
        try:
            data = storage.get_bytes(uri)  # type: ignore[attr-defined]
        except (FileNotFoundError, KeyError, Exception) as exc:
            if isinstance(exc, FileNotFoundError | KeyError):
                raise FileNotFoundError(uri) from exc
            raise
        buf = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"cv2.imdecode returned None for URI: {uri}")
        h, w = img.shape[:2]
        return ArtifactImageDimensions(width=w, height=h)

    return _load
