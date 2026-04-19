"""
shared.semantic_norm.ocr_scorer
--------------------------------
Pure-logic module for IEP1E semantic normalization.

Uses PaddleOCR as a decision signal (not a text reader) to:
  1. Score four candidate rotations per page crop.
  2. Extract script-direction evidence (Arabic / Latin / garbage ratios).
  3. Select the best rotation with a confidence gate.
  4. Determine reading direction for a spread (LTR / RTL / unresolved).
  5. Assign reading order from physical geometry + reading direction.

No FastAPI dependency — fully unit-testable in isolation.

Design constraints:
  - Blank pages (zero OCR results) must never raise.
  - Early-exit: if 0° and 90° already meet the confidence gate, skip 180° / 270°.
  - PaddleOCR angle classification (angle_cls) is DISABLED; orientation is
    decided solely by comparing final_score across the four rotations.

Scoring formula
---------------
For each rotation:
    n_boxes  = number of text boxes
    n_chars  = total alphanumeric character count
    mean_conf = average OCR confidence across all boxes  (0 when n_boxes == 0)
    arabic_ratio, latin_ratio, garbage_ratio — character-script fractions

    base_score  = 0.45 * n_boxes + 0.35 * n_chars + 0.20 * (100 * mean_conf)
    final_score = base_score + 15 * max(arabic_ratio, latin_ratio)
                             - 10 * garbage_ratio

Confidence gate
---------------
    ratio = best_score / second_best_score   (0.0 when second_best == 0)
    diff  = best_score - second_best_score

    orientation_confident = (ratio >= CONF_RATIO_THRESHOLD)
                            AND (diff >= CONF_DIFF_THRESHOLD)

Exported:
    CONF_RATIO_THRESHOLD
    CONF_DIFF_THRESHOLD
    build_ocr_engine        — initialise PaddleOCR once at service startup
    score_rotation          — run OCR on four rotations, return score dict
    extract_script_evidence — parse raw OCR output into ScriptEvidence
    select_orientation      — apply confidence gate, return PageOrientationResult
    determine_reading_direction — combine evidence across pages
    assign_reading_order    — order URIs by direction + physical x_center
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

import cv2
import numpy as np

from shared.schemas.semantic_norm import (
    PageOrientationResult,
    ScriptEvidence,
)

logger = logging.getLogger(__name__)

# ── Confidence gate thresholds ────────────────────────────────────────────────

CONF_RATIO_THRESHOLD: float = 1.2
CONF_DIFF_THRESHOLD: float = 15.0

# ── Rotation constants ────────────────────────────────────────────────────────

_ROTATIONS = (0, 90, 180, 270)

# OpenCV rotation codes for 90/180/270 CW
_CV2_ROTATION = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


# ── OCR engine factory ────────────────────────────────────────────────────────


def build_ocr_engine(*, use_gpu: bool = True) -> Any:
    """
    Initialise and return a PaddleOCR engine for orientation scoring.

    Initialise once at service startup; pass the returned object to
    score_rotation().

    Notes:
        - lang="arabic" covers Arabic + Latin text in a single model.
        - angle_cls is disabled; we decide orientation ourselves.
        - use_gpu should be False on CPU-only hosts.
    """
    from paddleocr import PaddleOCR  # type: ignore[import]

    ocr = PaddleOCR(
        use_angle_cls=False,
        lang="arabic",
        det=True,
        rec=True,
        use_gpu=use_gpu,
        show_log=False,
    )
    logger.info("iep1e: PaddleOCR engine initialised (lang=arabic, use_gpu=%s)", use_gpu)
    return ocr


# ── Image rotation ────────────────────────────────────────────────────────────


def _rotate_image(image: np.ndarray, deg: int) -> np.ndarray:
    """Return a rotated copy of *image* (0 → identity, others via cv2.rotate)."""
    if deg == 0:
        return image
    return cv2.rotate(image, _CV2_ROTATION[deg])


# ── Script character classification ──────────────────────────────────────────


def _classify_char(ch: str) -> str:
    """
    Classify a single character as 'arabic', 'latin', or 'other'.

    Uses Unicode name prefix for robustness across Arabic variants.
    """
    if not ch.isalnum():
        return "other"
    try:
        name = unicodedata.name(ch, "")
    except (ValueError, TypeError):
        return "other"
    name_upper = name.upper()
    if "ARABIC" in name_upper:
        return "arabic"
    if (
        "LATIN" in name_upper
        or "DIGIT" in name_upper
        or ch.isascii()
    ):
        return "latin"
    return "other"


# ── Raw OCR result parsing ────────────────────────────────────────────────────


def extract_script_evidence(results: list[Any]) -> ScriptEvidence:
    """
    Parse raw PaddleOCR output into a ScriptEvidence instance.

    Always returns a valid ScriptEvidence; never raises, even on empty input.

    Args:
        results: Raw output from ``ocr.ocr(image, cls=False)`` — a list of
                 page results, where each page result is a list of lines.
                 Each line is ``[bbox, (text, confidence)]``.
                 PaddleOCR sometimes returns ``None`` or nested None; handled.
    """
    n_boxes = 0
    total_conf = 0.0
    arabic_count = 0
    latin_count = 0
    other_count = 0

    # Flatten: PaddleOCR wraps results in an outer list (one entry per image).
    lines: list[Any] = []
    for page_result in (results or []):
        if page_result is None:
            continue
        if isinstance(page_result, list):
            lines.extend(page_result)

    for line in lines:
        if not isinstance(line, (list, tuple)) or len(line) < 2:
            continue
        text_conf = line[1]
        if not isinstance(text_conf, (list, tuple)) or len(text_conf) < 2:
            continue
        text = text_conf[0]
        conf = text_conf[1]
        if not isinstance(text, str):
            continue
        try:
            conf_float = float(conf)
        except (TypeError, ValueError):
            conf_float = 0.0

        n_boxes += 1
        total_conf += max(0.0, min(1.0, conf_float))

        for ch in text:
            cls = _classify_char(ch)
            if cls == "arabic":
                arabic_count += 1
            elif cls == "latin":
                latin_count += 1
            else:
                other_count += 1

    n_chars = arabic_count + latin_count
    total_chars = arabic_count + latin_count + other_count

    arabic_ratio = arabic_count / total_chars if total_chars > 0 else 0.0
    latin_ratio = latin_count / total_chars if total_chars > 0 else 0.0
    garbage_ratio = other_count / total_chars if total_chars > 0 else 0.0
    mean_conf = total_conf / n_boxes if n_boxes > 0 else 0.0

    return ScriptEvidence(
        arabic_ratio=arabic_ratio,
        latin_ratio=latin_ratio,
        garbage_ratio=garbage_ratio,
        n_boxes=n_boxes,
        n_chars=n_chars,
        mean_conf=mean_conf,
    )


# ── Scoring ───────────────────────────────────────────────────────────────────


def _compute_final_score(evidence: ScriptEvidence) -> float:
    """
    Compute the final orientation score from a ScriptEvidence.

    Weights revised to suppress PaddleOCR false-positive detections at wrong
    orientations (Arabic characters are prone to spurious hits when sideways):

        conf_weighted_boxes = n_boxes * mean_conf   ← only high-confidence boxes count
        base  = 0.30 * conf_weighted_boxes
              + 0.25 * n_chars * mean_conf           ← chars weighted by confidence
              + 0.45 * (100 * mean_conf)             ← mean confidence dominates
        final = base + 15 * max(arabic_ratio, latin_ratio) - 10 * garbage_ratio
    """
    conf_weighted_boxes = evidence.n_boxes * evidence.mean_conf
    conf_weighted_chars = evidence.n_chars * evidence.mean_conf
    base = (
        0.30 * conf_weighted_boxes
        + 0.25 * conf_weighted_chars
        + 0.45 * (100.0 * evidence.mean_conf)
    )
    script_bonus = 15.0 * max(evidence.arabic_ratio, evidence.latin_ratio)
    garbage_penalty = 10.0 * evidence.garbage_ratio
    return base + script_bonus - garbage_penalty


def score_rotation(
    image: np.ndarray,
    ocr: Any,
) -> dict[int, tuple[float, ScriptEvidence]]:
    """
    Score all four rotations and return a mapping of rotation → (score, evidence).

    All four rotations are always tested.  The early-exit optimisation was
    removed because it caused 270° to be silently skipped when 0° or 90°
    produced spuriously high scores on sideways Arabic text, leading to the
    wrong orientation being selected for landscape-input pages.

    Design note: document processing prioritises correctness over latency.
    Four OCR passes on a CPU take ~2–4 s per page, which is acceptable.

    Args:
        image: Page crop as a BGR ndarray (as loaded by OpenCV).
        ocr:   PaddleOCR engine returned by build_ocr_engine().

    Returns:
        ``{0: (score, evidence), 90: (score, evidence),
           180: (score, evidence), 270: (score, evidence)}``

    Never raises — OCR failures for a single rotation produce a zero score.
    """
    scores: dict[int, tuple[float, ScriptEvidence]] = {}

    for deg in _ROTATIONS:
        rotated = _rotate_image(image, deg)
        try:
            results = ocr.ocr(rotated, cls=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("iep1e: OCR failed at rotation=%d: %s", deg, exc)
            results = []

        evidence = extract_script_evidence(results)
        final_score = _compute_final_score(evidence)
        scores[deg] = (final_score, evidence)
        logger.debug(
            "iep1e: rotation=%d score=%.3f boxes=%d chars=%d conf=%.3f "
            "arabic=%.2f latin=%.2f garbage=%.2f",
            deg,
            final_score,
            evidence.n_boxes,
            evidence.n_chars,
            evidence.mean_conf,
            evidence.arabic_ratio,
            evidence.latin_ratio,
            evidence.garbage_ratio,
        )

    return scores


# ── Orientation selection ─────────────────────────────────────────────────────


def select_orientation(
    scores: dict[int, tuple[float, ScriptEvidence]],
) -> PageOrientationResult:
    """
    Apply the confidence gate and return the orientation decision.

    When all scores are zero (blank page), returns rotation=0,
    orientation_confident=False without raising.
    """
    sorted_items = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
    best_deg, (best_score, best_evidence) = sorted_items[0]
    second_score = sorted_items[1][1][0] if len(sorted_items) > 1 else 0.0

    ratio = best_score / second_score if second_score > 0.0 else 0.0
    diff = best_score - second_score
    confident = ratio >= CONF_RATIO_THRESHOLD and diff >= CONF_DIFF_THRESHOLD

    logger.info(
        "iep1e: orientation selected best_deg=%d score=%.3f ratio=%.3f diff=%.3f confident=%s "
        "scores={%s}",
        best_deg,
        best_score,
        ratio,
        diff,
        confident,
        ", ".join(f"{d}°:{s[0]:.2f}" for d, s in sorted(sorted_items, key=lambda x: x[0])),
    )

    return PageOrientationResult(
        best_rotation_deg=best_deg,  # type: ignore[arg-type]
        orientation_confident=confident,
        score_ratio=ratio,
        score_diff=diff,
        script_evidence=best_evidence,
    )


# ── Reading direction ─────────────────────────────────────────────────────────


def determine_reading_direction(
    evidences: list[ScriptEvidence],
) -> str:
    """
    Combine script evidence from all pages and decide reading direction.

    Per-page arabic_ratio and latin_ratio are weighted by n_chars so that
    pages with more text have more influence on the final decision.
    Split-script spreads follow the dominant direction.

    Returns:
        "ltr"        — Latin dominant
        "rtl"        — Arabic dominant
        "unresolved" — no usable text on any page, or tie
    """
    total_arabic = 0.0
    total_latin = 0.0
    total_weight = 0.0

    for ev in evidences:
        weight = float(ev.n_chars)
        total_arabic += ev.arabic_ratio * weight
        total_latin += ev.latin_ratio * weight
        total_weight += weight

    if total_weight == 0.0:
        return "unresolved"

    w_arabic = total_arabic / total_weight
    w_latin = total_latin / total_weight

    if w_arabic == w_latin:
        return "unresolved"
    if w_arabic > w_latin:
        return "rtl"
    return "ltr"


# ── Reading order ─────────────────────────────────────────────────────────────


def assign_reading_order(
    page_uris: list[str],
    x_centers: list[float],
    direction: str,
) -> list[str]:
    """
    Order *page_uris* by reading direction using physical x_centers.

    Physical left (smaller x_center) is first for LTR; right (larger x_center)
    is first for RTL.  For "unresolved", use LTR (left-first) as default.

    Args:
        page_uris:  URIs in physical left-first order.
        x_centers:  Physical x-center for each URI (same order as page_uris).
        direction:  "ltr", "rtl", or "unresolved".

    Returns:
        URIs in reading order (first page first).
    """
    if len(page_uris) == 1:
        return list(page_uris)

    # Sort by x_center ascending (left → right)
    paired = sorted(zip(x_centers, page_uris), key=lambda t: t[0])
    left_uri = paired[0][1]
    right_uri = paired[-1][1]

    if direction == "rtl":
        return [right_uri, left_uri]
    # "ltr" or "unresolved" → left first
    return [left_uri, right_uri]
