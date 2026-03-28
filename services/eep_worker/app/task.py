"""
services/eep_worker/app/task.py
---------------------------------
Packet 9.2 — Policy-driven gate configuration entry point for worker tasks.

This module is the **single canonical location** where gate configuration is
loaded from the active policy before any preprocessing step runs.  Every
task execution entry point must call ``build_gate_config(session)`` once and
pass the result to all downstream steps via their ``gate_config=`` parameter.

Policy loading is done once per task, not once per step.  The DB round-trip
cost is negligible relative to GPU inference latency (~30–60 s per page).

Gate config flow
----------------

All four preprocessing step functions accept ``gate_config: PreprocessingGateConfig``.
A single call to ``build_gate_config(session)`` at the top of the task produces
the config object that flows through every step:

    gate_config = build_gate_config(session)

    # Step 2–3 — parallel geometry inference + selection gate
    geom_result = await invoke_geometry_services(
        ...
        gate_config=gate_config,
        session=session,
    )

    # Step 4–5 — normalization + first artifact validation gate
    norm_outcome = run_normalization_and_first_validation(
        ...
        gate_config=gate_config,
    )

    # Step 6–7 — rectification rescue + second geometry pass + final validation
    rescue_outcome = await run_rescue_flow(
        ...
        gate_config=gate_config,
        session=session,
    )

    # Split path — normalise and validate both children
    split_outcome = await run_split_normalization(
        ...
        gate_config=gate_config,
        session=session,
    )

The same ``gate_config`` instance is passed to every step — thresholds are
consistent across all gate decisions for one page processing run.

Thresholds driven by the active policy
---------------------------------------

All 27 fields of ``PreprocessingGateConfig`` are loaded from policy (see
``policy_loader.parse_gate_config`` for the full field mapping).  The main
operational thresholds are:

  split_confidence_threshold      — minimum split confidence to trust a split decision
  tta_variance_ceiling            — maximum TTA variance before a geometry candidate
                                    is discarded
  page_area_preference_threshold  — below this, IEP1B is preferred over IEP1A
  geometry_sanity_area_min/max    — page area fraction bounds for sanity check
  aspect_ratio_bounds             — per-material-type aspect ratio bounds
  artifact_validation_threshold   — minimum soft score for artifact acceptance
  blur/border/skew/foreground     — soft signal normalization bounds
  weight_*                        — per-signal weights for artifact scoring

Falls back to ``PreprocessingGateConfig()`` spec defaults when:
  - no policy has been applied yet (fresh deploy)
  - the stored YAML is malformed
  - any individual field is absent or non-numeric

Exported:
  build_gate_config  — load PreprocessingGateConfig from the active policy
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from services.eep.app.gates.geometry_selection import PreprocessingGateConfig
from services.eep.app.policy_loader import load_gate_config

__all__ = ["build_gate_config"]


def build_gate_config(session: Session) -> PreprocessingGateConfig:
    """
    Load and return a PreprocessingGateConfig from the active policy in the DB.

    This is the canonical policy-loading entry point for all preprocessing
    task execution.  Call once per task at the top of the processing function
    and pass the returned config to every step via the ``gate_config=``
    keyword argument.

    Args:
        session — SQLAlchemy Session (read-only; no commit performed here).

    Returns:
        PreprocessingGateConfig populated with the active policy thresholds,
        or spec defaults if no policy has been applied yet.
    """
    return load_gate_config(session)
