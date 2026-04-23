"""
Compute empirical baseline metrics from the current golden dataset + weights.

Writes a JSON file you can version-control (or generate in CI) and later compare
against new training runs / promotion candidates.

Usage (from repository root; requires AWS env + ultralytics + weights files):

  python training/scripts/compute_baselines.py
  python training/scripts/compute_baselines.py --output golden_dataset/baselines.json
  python training/scripts/compute_baselines.py --skip iep1a --skip iep1b

Environment: same as ``evaluate_golden_dataset.py`` (repo ``.env`` with S3_* or
standard AWS_* variables).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import merge helper (requires cwd on PYTHONPATH = repo root when running this file)
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.retraining_worker.app.golden_gate_merge import (  # noqa: E402
    _aggregate_gate_results,
    measure_cross_model_gates,
    merge_iep1a_iep1b_gate_results,
)

_IEP1_MATERIALS = ("book", "newspaper", "microfilm")


def _run_evaluator(
    repo_root: Path,
    model: str,
    manifest: Path,
    weights: str | None,
    *,
    material: str | None = None,
) -> dict[str, Any]:
    script = repo_root / "training" / "scripts" / "evaluate_golden_dataset.py"
    cmd = [sys.executable, str(script), "--model", model, "--manifest", str(manifest)]
    if weights:
        cmd.extend(["--weights", weights])
    if material:
        cmd.extend(["--material", material])
    env = os.environ.copy()
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=7200,
        env=env,
        check=False,
    )
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(
            f"Evaluator empty stdout for {model} material={material!r} rc={proc.returncode}\n"
            f"stderr:\n{proc.stderr}"
        )
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Evaluator invalid JSON for {model} material={material!r} rc={proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        ) from exc
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("golden_dataset/baselines.json"),
        help="Where to write baseline JSON (default: golden_dataset/baselines.json)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("golden_dataset/manifest.json"),
        help="Golden manifest path",
    )
    parser.add_argument("--weights-iep0", default=None)
    parser.add_argument("--weights-iep1a", default=None)
    parser.add_argument("--weights-iep1b", default=None)
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        choices=["iep0", "iep1a", "iep1b"],
        help="Skip a model (repeatable), e.g. while IEP1 case SHAs are still placeholders",
    )
    args = parser.parse_args()

    _spec = importlib.util.spec_from_file_location(
        "_bootstrap_golden_eval",
        _REPO_ROOT / "training" / "scripts" / "evaluate_golden_dataset.py",
    )
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        getattr(_mod, "_ensure_aws_env_from_repo_dotenv", lambda: None)()

    repo_root = _REPO_ROOT
    manifest_path = args.manifest if args.manifest.is_absolute() else repo_root / args.manifest
    if not manifest_path.is_file():
        raise SystemExit(f"Manifest not found: {manifest_path}")
    try:
        manifest_rel = str(manifest_path.resolve().relative_to(repo_root.resolve())).replace("\\", "/")
    except ValueError:
        manifest_rel = str(manifest_path)

    skip = set(args.skip)
    models_raw: dict[str, Any] = {}
    errors: dict[str, str] = {}

    weights_map = {
        "iep0": args.weights_iep0,
        "iep1a": args.weights_iep1a,
        "iep1b": args.weights_iep1b,
    }

    for model in ("iep0", "iep1a", "iep1b"):
        if model in skip:
            models_raw[model] = None
            continue
        try:
            if model in ("iep1a", "iep1b"):
                per_material: list[dict[str, Any]] = []
                for mat in _IEP1_MATERIALS:
                    try:
                        per_material.append(
                            _run_evaluator(
                                repo_root,
                                model,
                                manifest_path,
                                weights_map[model],
                                material=mat,
                            )
                        )
                    except RuntimeError:
                        pass
                if not per_material:
                    raise RuntimeError(
                        f"No evaluable golden cases for {model} across materials {_IEP1_MATERIALS}"
                    )
                models_raw[model] = _aggregate_gate_results(per_material)
            else:
                models_raw[model] = _run_evaluator(repo_root, model, manifest_path, weights_map[model])
        except Exception as exc:  # noqa: BLE001 — surface per-model failure in output file
            errors[model] = str(exc)
            models_raw[model] = None

    merged: dict[str, Any] | None = None
    cross_detail: dict[str, Any] | None = None
    if models_raw.get("iep1a") and models_raw.get("iep1b"):
        merged = merge_iep1a_iep1b_gate_results(models_raw["iep1a"], models_raw["iep1b"])
        try:
            cross_detail = measure_cross_model_gates(repo_root, manifest_rel=manifest_rel)
            merged = merge_iep1a_iep1b_gate_results(
                models_raw["iep1a"],
                models_raw["iep1b"],
                structural_agreement_rate=cross_detail["structural_agreement_rate"],
                latency_p95=cross_detail["latency_p95"],
            )
        except Exception as exc:  # noqa: BLE001
            errors["cross_model_measurement"] = str(exc)

    payload = {
        "version": "1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest": manifest_rel,
        "weights": {k: v for k, v in weights_map.items()},
        "models": models_raw,
        "preprocessing_gate_results_merged": merged,
        "cross_model_gates": cross_detail,
        "errors": errors or None,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"wrote": str(args.output), "errors": errors}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
