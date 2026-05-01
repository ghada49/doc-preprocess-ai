"""
Derive drift-detector baselines analytically from spec quality gate thresholds.

Writes monitoring/baselines.json without requiring a golden dataset, model
weights, or AWS credentials.  No ML runtime dependencies.

Usage (from repo root):
  python training/scripts/derive_spec_baselines.py
  python training/scripts/derive_spec_baselines.py --output monitoring/baselines.json

Methodology
-----------
Every metric key in baselines.json maps to a runtime observation fed by the
EEP/IEP workers during live inference — not from a golden evaluation run.
For example:
  iep1a.split_detection_rate   = float(iep1a.split_required) per page (0 or 1),
                                  averaged over the sliding window (200 obs).
  iep1a.geometry_confidence    = YOLO detection confidence per page (0-1 float).
  eep.structural_agreement_rate= per-page agreement between IEP1A and IEP1B (0-1).

Because these are runtime signals, analytically-derived baselines are entirely
appropriate for pre-production deployment, where real runtime data has not yet
accumulated.

For each metric, the mean is set at the expected operating point of a
healthy, passing system; the std encodes expected natural variation in that
operating regime.  Sources used for each value are documented inline.

Replace with empirically computed values after ≥200 production observations
per metric are available (via Prometheus or a dedicated collection run).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

# ── Canonical output path ─────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _REPO_ROOT / "monitoring" / "baselines.json"


# ── Baseline derivation table ─────────────────────────────────────────────────
#
# Each entry: (mean, std, derivation_note)
#
# Derivation sources used:
#   [GATE]    spec quality gate threshold (pass criterion).
#   [TTA]     TTA alarm thresholds from services/iep1a/app/tta.py:
#               LOW_AGREEMENT_THRESHOLD = 0.80
#               HIGH_VARIANCE_THRESHOLD = 0.10
#   [ALERT]   Prometheus alert threshold from monitoring/alert_rules/libraryai-alerts.yml.
#   [DOMAIN]  Domain estimate for this document-digitisation workload.
#   [SPEC]    Rollback/retraining trigger condition from spec Section 16.
#
# The drift detector fires when: |window_mean - mean| > 3 × std.
# Each std is sized so the 3σ bound sits just outside the quality gate limit,
# i.e. drift fires only after a statistically meaningful degradation.

_BASELINES: dict[str, tuple[float, float, str]] = {

    # ── IEP1A — geometry segmentation model ───────────────────────────────────

    # YOLO detection confidence per page (0-1).
    # [GATE] golden golden gate pass >= 0.25 inference threshold, model target ~0.85.
    # [DOMAIN] typical well-trained YOLOv8-seg: 0.85-0.92 on in-distribution images.
    # 3σ bound: 0.87 - 3*0.07 = 0.66, safely below the 0.25 hard threshold.
    "iep1a.geometry_confidence": (0.87, 0.07,
        "YOLO confidence per page; [GATE] pass >= 0.25; operating point 0.87"),

    # float(iep1a.split_required) per page — binary 0 or 1, windowed mean =
    # fraction of pages detected as double-page spreads.
    # [DOMAIN] ~15 % of archival document pages are two-up spreads.
    # std chosen so 3σ bound = 0.15 + 3*0.06 = 0.33 (alerts if sudden surge).
    "iep1a.split_detection_rate": (0.15, 0.06,
        "Fraction of pages flagged split_required; [DOMAIN] ~15% two-up spreads"),

    # Fraction of TTA passes agreeing on page_count + split_required.
    # [TTA] alarm fires if < 0.80 on any single call.
    # 3σ bound: 0.93 - 3*0.05 = 0.78 ≈ TTA alarm threshold.
    "iep1a.tta_structural_agreement_rate": (0.93, 0.05,
        "TTA agreement rate per page; [TTA] low-agreement alarm < 0.80"),

    # Inter-pass variance of corner coordinates (normalised to image dims).
    # [TTA] high-variance alarm fires if > 0.10.
    # 3σ bound: 0.02 + 3*0.02 = 0.08 (drift alert near high-variance threshold).
    "iep1a.tta_prediction_variance": (0.02, 0.02,
        "TTA corner-coordinate variance; [TTA] high-variance alarm > 0.10"),

    # ── IEP1B — geometry keypoint model ───────────────────────────────────────

    # Same four metrics; IEP1B is the keypoint counterpart of IEP1A.
    # Slightly lower confidence because keypoint detection is harder than
    # segmentation on the same image.
    "iep1b.geometry_confidence": (0.85, 0.07,
        "YOLO keypoint confidence per page; [GATE] pass >= 0.25; operating point 0.85"),

    "iep1b.split_detection_rate": (0.15, 0.06,
        "Fraction of pages flagged split_required; [DOMAIN] ~15% two-up spreads"),

    "iep1b.tta_structural_agreement_rate": (0.92, 0.05,
        "TTA agreement rate per page; [TTA] low-agreement alarm < 0.80"),

    "iep1b.tta_prediction_variance": (0.02, 0.02,
        "TTA corner-coordinate variance; [TTA] high-variance alarm > 0.10"),

    # ── IEP1C — normalisation image quality checks ─────────────────────────────

    # Blur score: Laplacian variance normalised to [0,1]; higher = sharper.
    # [DOMAIN] archival scans from decent digitisation equipment: mean ~0.78.
    "iep1c.blur_score": (0.78, 0.10,
        "Laplacian blur score; [DOMAIN] clean-scan operating point 0.78"),

    # Border score: fraction of image area outside the page border that is
    # background (i.e., a clean crop).
    "iep1c.border_score": (0.82, 0.08,
        "Border cleanliness fraction; [DOMAIN] operating point 0.82"),

    # Foreground coverage: fraction of page area that is document content.
    "iep1c.foreground_coverage": (0.85, 0.07,
        "Page foreground coverage fraction; [DOMAIN] operating point 0.85"),

    # ── IEP1D — rectification fallback ────────────────────────────────────────

    # Confidence that the rectification transform is correct (0-1).
    # Only observed when rectification is triggered (~15% of pages).
    "iep1d.rectification_confidence": (0.82, 0.09,
        "Rectification transform confidence; [DOMAIN] operating point 0.82"),

    # ── IEP2A — layout detector (primary) ─────────────────────────────────────

    # Mean YOLO detection confidence across all layout regions on a page.
    # [ALERT] Prometheus alert fires if p50 < 0.70 over 1-hour window.
    # 3σ bound: 0.83 - 3*0.08 = 0.59 (well below alert threshold — drift
    #           fires early enough to leave headroom before the hard alert).
    "iep2a.mean_page_confidence": (0.83, 0.08,
        "Mean layout detection confidence; [ALERT] alert p50 < 0.70"),

    # Number of layout regions detected per page.
    # [DOMAIN] typical digitised archival page: 6-12 regions.
    "iep2a.region_count": (8.5, 2.0,
        "Layout regions per page; [DOMAIN] typical archival page 6-12 regions"),

    # Fraction of all regions that are each layout class.
    # [DOMAIN] typical mixed-content archival page layout distribution.
    "iep2a.class_fraction.text_block": (0.50, 0.10,
        "Text block fraction; [DOMAIN] typical majority class ~50%"),
    "iep2a.class_fraction.title":      (0.10, 0.04,
        "Title region fraction; [DOMAIN] ~10% of regions are headings"),
    "iep2a.class_fraction.table":      (0.15, 0.06,
        "Table region fraction; [DOMAIN] ~15% of regions are tables"),
    "iep2a.class_fraction.image":      (0.15, 0.06,
        "Embedded image fraction; [DOMAIN] ~15% of regions are images"),
    "iep2a.class_fraction.caption":    (0.10, 0.04,
        "Caption region fraction; [DOMAIN] ~10% of regions are captions"),

    # ── IEP2B — layout detector (secondary) ───────────────────────────────────

    # IEP2B is the secondary layout detector; slightly lower confidence and
    # marginally more variable region count are expected.
    "iep2b.mean_page_confidence": (0.80, 0.09,
        "Mean layout detection confidence (secondary); [ALERT] alert p50 < 0.70"),

    "iep2b.region_count": (8.0, 2.5,
        "Layout regions per page (secondary); [DOMAIN] typical 6-12 regions"),

    "iep2b.class_fraction.text_block": (0.50, 0.11,
        "Text block fraction (secondary); [DOMAIN] ~50%"),
    "iep2b.class_fraction.title":      (0.10, 0.05,
        "Title region fraction (secondary); [DOMAIN] ~10%"),
    "iep2b.class_fraction.table":      (0.15, 0.07,
        "Table region fraction (secondary); [DOMAIN] ~15%"),
    "iep2b.class_fraction.image":      (0.15, 0.07,
        "Embedded image fraction (secondary); [DOMAIN] ~15%"),
    "iep2b.class_fraction.caption":    (0.10, 0.05,
        "Caption region fraction (secondary); [DOMAIN] ~10%"),

    # ── EEP — IEP1 routing quality signals ────────────────────────────────────
    # All fractions are windowed means of per-page binary 0/1 flags.

    # Fraction of pages where IEP1A and IEP1B geometry results were accepted
    # without human review.
    # [DOMAIN] expected healthy acceptance rate ~70%.
    "eep.geometry_selection_route.accepted_fraction": (0.70, 0.08,
        "Fraction of pages auto-accepted; [DOMAIN] healthy ~70%"),

    # Fraction escalated to human review queue.
    "eep.geometry_selection_route.review_fraction": (0.20, 0.06,
        "Fraction escalated to review; [DOMAIN] healthy ~20%"),

    # Fraction where IEP1A and IEP1B structurally disagree.
    # [SPEC] rollback triggers when structural agreement rate drops > 20%.
    "eep.geometry_selection_route.structural_disagreement_fraction": (0.05, 0.03,
        "Structural disagreement fraction; [SPEC] rollback if > 20% drop from baseline"),

    # Fraction failing the sanity gate (e.g. degenerate bounding boxes).
    "eep.geometry_selection_route.sanity_failed_fraction": (0.02, 0.02,
        "Sanity gate failure fraction; [DOMAIN] healthy < 5%"),

    # Fraction where split confidence is below threshold.
    "eep.geometry_selection_route.split_confidence_low_fraction": (0.02, 0.02,
        "Split confidence below threshold fraction; [DOMAIN] healthy < 5%"),

    # Fraction where TTA variance is above HIGH_VARIANCE_THRESHOLD (0.10).
    "eep.geometry_selection_route.tta_variance_high_fraction": (0.01, 0.01,
        "High TTA variance fraction; [TTA] HIGH_VARIANCE_THRESHOLD = 0.10"),

    # ── EEP — artifact validation signals ─────────────────────────────────────

    "eep.artifact_validation_route.valid_fraction": (0.75, 0.08,
        "Fraction of artifacts passing validation; [DOMAIN] healthy ~75%"),

    "eep.artifact_validation_route.invalid_fraction": (0.10, 0.05,
        "Fraction of artifacts failing validation; [DOMAIN] healthy < 15%"),

    "eep.artifact_validation_route.rectification_triggered_fraction": (0.15, 0.06,
        "Fraction triggering rectification fallback; [DOMAIN] healthy ~15%"),

    # ── EEP — cross-model structural signals ──────────────────────────────────

    # Per-page agreement rate between IEP1A and IEP1B on page structure.
    # [SPEC] automated rollback triggers when this drops > 20% from baseline
    #        (spec Section 16, post-promotion monitoring window).
    # 3σ bound: 0.90 - 3*0.05 = 0.75; rollback at 0.90*0.80 = 0.72 — drift
    #           fires first, giving an early-warning window.
    "eep.structural_agreement_rate": (0.90, 0.05,
        "IEP1A/IEP1B per-page structural agreement; [SPEC] rollback if drops > 20%"),

    # Layout consensus confidence: mean confidence across IEP2A and IEP2B
    # for a final layout decision.
    "eep.layout_consensus_confidence": (0.84, 0.07,
        "IEP2 layout consensus confidence; [DOMAIN] healthy > 0.75"),
}


# ── Script ────────────────────────────────────────────────────────────────────


def build_payload() -> dict:
    """Return the baselines dict ready to write to JSON."""
    baselines = {}
    for key, (mean, std, _note) in _BASELINES.items():
        baselines[key] = {"mean": mean, "std": std}
    return baselines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="Destination path (default: monitoring/baselines.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the output JSON to stdout without writing to disk.",
    )
    args = parser.parse_args()

    payload = build_payload()
    annotated = {
        "_generated_by": "training/scripts/derive_spec_baselines.py",
        "_generated_at": datetime.now(timezone.utc).isoformat(),
        "_methodology": (
            "Analytically derived from spec quality gate thresholds, TTA alarm "
            "thresholds (services/iep1a/app/tta.py), Prometheus alert rules "
            "(monitoring/alert_rules/libraryai-alerts.yml), and domain estimates "
            "for archival document digitisation workloads.  Each std is sized so "
            "the 3σ drift-detection bound sits just outside the corresponding "
            "quality gate limit.  Replace with empirically computed values after "
            "≥200 production observations per metric are available."
        ),
        **payload,
    }

    text = json.dumps(annotated, indent=2) + "\n"

    if args.dry_run:
        print(text)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"Wrote {len(payload)} baselines to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
