"""
monitoring/drift_detector.py
-----------------------------
Packet 9.3 — Drift detector skeleton.

Implements the ``DriftDetector`` class described in spec Section 16.4.

``DriftDetector`` maintains a per-metric sliding window of recent
observations and compares the current window mean against pre-computed
baseline statistics.  A metric is flagged as drifting when its window
mean deviates more than ``threshold_std`` standard deviations from the
baseline mean.

Baseline format (monitoring/baselines.json)
-------------------------------------------

The JSON file is a flat mapping from metric key to an object with
``mean`` and ``std`` fields::

    {
      "iep1a.geometry_confidence": {"mean": 0.85, "std": 0.05},
      "iep1b.geometry_confidence": {"mean": 0.83, "std": 0.06},
      ...
    }

Monitored metric keys (spec Section 16.4)
------------------------------------------

IEP1A:
  iep1a.geometry_confidence, iep1a.split_detection_rate,
  iep1a.tta_structural_agreement_rate, iep1a.tta_prediction_variance

IEP1B:
  iep1b.geometry_confidence, iep1b.split_detection_rate,
  iep1b.tta_structural_agreement_rate, iep1b.tta_prediction_variance

IEP1C:
  iep1c.blur_score, iep1c.border_score, iep1c.foreground_coverage

IEP1D:
  iep1d.rectification_confidence

IEP2A:
  iep2a.mean_page_confidence, iep2a.region_count

IEP2B:
  iep2b.mean_page_confidence, iep2b.region_count

EEP:
  eep.structural_agreement_rate, eep.layout_consensus_confidence

Drift check algorithm
---------------------

For a metric with baseline (mean=μ, std=σ) and current window W::

    drifting = |mean(W) - μ| > threshold_std × σ

If σ == 0 the check is skipped and False is returned (cannot compute a
z-score with zero standard deviation).  If the window is empty or the
metric has no baseline the check is also skipped.

Exported:
  Baseline         — dataclass (mean, std)
  DriftDetector    — sliding-window drift detector
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["Baseline", "DriftDetector"]

_DEFAULT_BASELINES_PATH = Path(__file__).parent / "baselines.json"


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Baseline:
    """Baseline statistics for a single monitored metric."""

    mean: float
    std: float


# ── Detector ──────────────────────────────────────────────────────────────────


class DriftDetector:
    """
    Sliding-window drift detector.

    Parameters
    ----------
    baselines:
        Mapping from metric key (e.g. ``"iep1a.geometry_confidence"``) to
        its ``Baseline`` (mean, std).
    window_size:
        Maximum number of observations retained per metric.  Default: 200
        (spec Section 16.4).
    threshold_std:
        Number of standard deviations from baseline that constitutes drift.
        Default: 3.0.
    """

    def __init__(
        self,
        baselines: dict[str, Baseline],
        window_size: int = 200,
        threshold_std: float = 3.0,
    ) -> None:
        self._baselines: dict[str, Baseline] = dict(baselines)
        self._window_size = window_size
        self._threshold_std = threshold_std
        self._windows: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._window_size)
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def observe(self, metric: str, value: float) -> None:
        """
        Record a new observation for ``metric``.

        The observation is appended to the metric's sliding window.  When
        the window is full the oldest value is evicted automatically.
        """
        self._windows[metric].append(float(value))

    def is_drifting(self, metric: str) -> bool:
        """
        Return True if ``metric``'s current window mean deviates more than
        ``threshold_std`` standard deviations from its baseline mean.

        Returns False (no drift signal) when:
        - the window for ``metric`` is empty
        - ``metric`` has no baseline entry
        - the baseline standard deviation is zero
        """
        baseline = self._baselines.get(metric)
        if baseline is None:
            logger.debug(
                "DriftDetector.is_drifting: no baseline for %r — skipping", metric
            )
            return False

        window = self._windows.get(metric)
        if not window:
            return False

        if baseline.std == 0.0:
            logger.debug(
                "DriftDetector.is_drifting: baseline std=0 for %r — skipping", metric
            )
            return False

        current_mean = sum(window) / len(window)
        return abs(current_mean - baseline.mean) > self._threshold_std * baseline.std

    def window_mean(self, metric: str) -> float | None:
        """
        Return the current window mean for ``metric``, or None if the
        window is empty.
        """
        window = self._windows.get(metric)
        if not window:
            return None
        return sum(window) / len(window)

    def window_size(self, metric: str) -> int:
        """Return the number of observations currently in the window for ``metric``."""
        return len(self._windows.get(metric) or [])

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        baselines_path: str | Path = _DEFAULT_BASELINES_PATH,
        window_size: int = 200,
        threshold_std: float = 3.0,
    ) -> "DriftDetector":
        """
        Construct a ``DriftDetector`` by loading baselines from a JSON file.

        The file must be a JSON object mapping metric key → ``{"mean": …,
        "std": …}``.  Raises ``FileNotFoundError`` if the file is absent,
        ``ValueError`` if the JSON is malformed or a baseline entry is
        missing a required field.

        Args:
            baselines_path: Path to ``baselines.json``.  Defaults to
                ``monitoring/baselines.json`` relative to this module.
            window_size: Passed through to ``DriftDetector.__init__``.
            threshold_std: Passed through to ``DriftDetector.__init__``.
        """
        path = Path(baselines_path)
        with path.open() as fh:
            raw = json.load(fh)

        if not isinstance(raw, dict):
            raise ValueError(
                f"baselines.json top-level must be a JSON object, got {type(raw).__name__}"
            )

        baselines: dict[str, Baseline] = {}
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"baselines.json: entry for {key!r} must be an object, got {type(entry).__name__}"
                )
            try:
                baselines[key] = Baseline(
                    mean=float(entry["mean"]),
                    std=float(entry["std"]),
                )
            except KeyError as exc:
                raise ValueError(
                    f"baselines.json: entry for {key!r} is missing field {exc}"
                ) from exc

        logger.debug("DriftDetector.load: loaded %d baselines from %s", len(baselines), path)
        return cls(baselines=baselines, window_size=window_size, threshold_std=threshold_std)
