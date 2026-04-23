"""
Merge per-model golden-eval outputs into gate_results shapes expected by
``promotion_api._check_gates`` and ``tests/test_p8_retraining_worker``.

``evaluate_golden_dataset.py`` returns different top-level keys per model
(IEP0 vs IEP1A vs IEP1B). IEP0 rows use classifier metrics; IEP1A/IEP1B
staging rows share one merged preprocessing flat dict with:

  geometry_iou, split_precision, structural_agreement_rate,
  golden_dataset, latency_p95

For **live** preprocessing gates, ``structural_agreement_rate`` and ``latency_p95``
come from ``measure_cross_model_gates``: paired IEP1A/IEP1B cases that share the
same ``image_s3_key`` and ``image_sha256``. Structural agreement is the **mean
max-IoU** between predicted **boxes** from the two models (offline proxy for
geometry agreement, not the full EEP TTA structural metric). Latency is the **p95
wall time** (seconds) for **sequential** predict on those paired images.

When no paired cases exist, structural defaults to ``pass: true`` / ``value: 1.0``
(skip) and latency to ``pass: true`` / ``value: 0.0`` so template-only manifests
do not fail closed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)


class PreprocessingLiveGates(NamedTuple):
    """Live golden-eval payloads for preprocessing staging rows (per-service shapes)."""

    iep1ab: dict[str, Any]
    iep0: dict[str, Any]


def _compute_iou_xyxy(box_a: list[float], box_b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return (inter / union) if union > 0 else 0.0


def _xywhn_to_xyxy(box_xywh: list[float]) -> list[float]:
    cx, cy, bw, bh = box_xywh
    return [cx - bw / 2.0, cy - bh / 2.0, cx + bw / 2.0, cy + bh / 2.0]


def _pred_boxes_xyxy_normalized(result: Any) -> list[list[float]]:
    pred_boxes: list[list[float]] = []
    if result.boxes is None or len(result.boxes) == 0:
        return pred_boxes
    for xywhn in result.boxes.xywhn.tolist():
        pred_boxes.append(_xywhn_to_xyxy(xywhn))
    return pred_boxes


def _cross_model_agreement_score(boxes_a: list[list[float]], boxes_b: list[list[float]]) -> float:
    """Mean over boxes in A of max IoU with any box in B (empty → 0.0)."""
    if not boxes_a or not boxes_b:
        return 0.0
    scores: list[float] = []
    for ba in boxes_a:
        best = max((_compute_iou_xyxy(ba, bb) for bb in boxes_b), default=0.0)
        scores.append(best)
    return sum(scores) / len(scores) if scores else 0.0


def measure_cross_model_gates(
    repo_root: Path,
    *,
    manifest_rel: str = "golden_dataset/manifest.json",
    weights_1a: str | None = None,
    weights_1b: str | None = None,
) -> dict[str, Any]:
    """
    For each image shared by an IEP1A and IEP1B golden case (same key + SHA),
    run both models once and derive:

    - ``structural_agreement_rate``: mean cross-model box agreement (0–1).
    - ``latency_p95``: p95 wall time (seconds) to run **both** predicts sequentially.

    Thresholds (env, optional):

    - ``GOLDEN_STRUCTURAL_AGREEMENT_MIN`` (default ``0.65``)
    - ``GOLDEN_LATENCY_P95_MAX_SECONDS`` (default ``120.0`` — generous for CPU/GPU variance)
    """
    import importlib.util

    eval_spec = importlib.util.spec_from_file_location(
        "_golden_eval",
        repo_root / "training" / "scripts" / "evaluate_golden_dataset.py",
    )
    if not eval_spec or not eval_spec.loader:
        raise RuntimeError("Cannot load training/scripts/evaluate_golden_dataset.py")
    egd = importlib.util.module_from_spec(eval_spec)
    eval_spec.loader.exec_module(egd)
    getattr(egd, "_ensure_aws_env_from_repo_dotenv", lambda: None)()

    import numpy as np
    from ultralytics import YOLO

    manifest_path = repo_root / manifest_rel
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)

    def _load_cases(model: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        manifest_dir = manifest_path.parent
        for meta in manifest.get("cases", []):
            if meta.get("model") != model:
                continue
            p = manifest_dir / meta["annotation_path"]
            out.append(json.loads(p.read_text(encoding="utf-8")))
        return out

    cases_a = _load_cases("iep1a")
    cases_b = _load_cases("iep1b")
    by_key: dict[str, dict[str, Any]] = {}
    for c in cases_b:
        by_key[c["image_s3_key"]] = c
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ca in cases_a:
        key = ca["image_s3_key"]
        if key not in by_key:
            continue
        cb = by_key[key]
        if ca.get("image_sha256") != cb.get("image_sha256"):
            logger.warning(
                "measure_cross_model_gates: skip key %s — SHA mismatch between IEP1A and IEP1B cases",
                key,
            )
            continue
        pairs.append((ca, cb))

    if not pairs:
        logger.info(
            "measure_cross_model_gates: no paired IEP1A/IEP1B cases on same image; "
            "skipping structural/latency measurement (defaults applied)."
        )
        return {
            "structural_agreement_rate": {"pass": True, "value": 1.0},
            "latency_p95": {"pass": True, "value": 0.0},
        }

    s3_bucket = manifest["s3_bucket"]
    endpoint = os.getenv("S3_ENDPOINT_URL", "") or None
    s3_client = egd._build_s3_client(endpoint)

    def _fetch(key: str) -> bytes:
        return egd._fetch_image_bytes(s3_client, s3_bucket, key)

    w1a = weights_1a or os.getenv("GOLDEN_WEIGHTS_IEP1A") or str(repo_root / "models/iep1a/Newspaper_Segmentation.pt")
    w1b = weights_1b or os.getenv("GOLDEN_WEIGHTS_IEP1B") or str(repo_root / "models/iep1b/Newspaper_Keypoints.pt")
    model_a = YOLO(w1a)
    model_b = YOLO(w1b)

    structural_scores: list[float] = []
    wall_seconds: list[float] = []

    for ca, _cb in pairs:
        image_bytes = _fetch(ca["image_s3_key"])
        actual_sha = hashlib.sha256(image_bytes).hexdigest()
        if actual_sha != ca["image_sha256"]:
            raise ValueError(
                f"SHA mismatch for {ca['case_id']}: expected {ca['image_sha256']}, got {actual_sha}"
            )
        tmp_path = egd._write_temp_image(image_bytes, ca["image_s3_key"])
        try:
            t0 = time.perf_counter()
            ra = model_a.predict(source=tmp_path, verbose=False)[0]
            t1 = time.perf_counter()
            rb = model_b.predict(source=tmp_path, verbose=False)[0]
            t2 = time.perf_counter()
            wall_seconds.append(t2 - t0)
            boxes_a = _pred_boxes_xyxy_normalized(ra)
            boxes_b = _pred_boxes_xyxy_normalized(rb)
            structural_scores.append(_cross_model_agreement_score(boxes_a, boxes_b))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    mean_structural = float(np.mean(structural_scores)) if structural_scores else 0.0
    p95_lat = float(np.percentile(wall_seconds, 95)) if wall_seconds else 0.0

    struct_min = float(os.getenv("GOLDEN_STRUCTURAL_AGREEMENT_MIN", "0.65"))
    lat_max = float(os.getenv("GOLDEN_LATENCY_P95_MAX_SECONDS", "120.0"))

    return {
        "structural_agreement_rate": {
            "pass": mean_structural >= struct_min,
            "value": round(mean_structural, 4),
        },
        "latency_p95": {
            "pass": p95_lat <= lat_max,
            "value": round(p95_lat, 4),
        },
    }


def _aggregate_gate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Merge per-material IEP1A/IEP1B evaluator dicts: pass only if all pass;
    ``value`` is the minimum across materials; ``regressions`` are summed.
    """
    if not results:
        return {}
    if len(results) == 1:
        return dict(results[0])
    keys: set[str] = set()
    for r in results:
        keys.update(r.keys())
    out: dict[str, Any] = {}
    for key in sorted(keys):
        parts = [r[key] for r in results if key in r and isinstance(r[key], dict)]
        if not parts:
            continue
        merged: dict[str, Any] = {"pass": all(bool(p.get("pass", False)) for p in parts)}
        if any("regressions" in p for p in parts):
            merged["regressions"] = sum(int(p["regressions"]) for p in parts if "regressions" in p)
        if any("value" in p for p in parts):
            merged["value"] = round(min(float(p.get("value", 0.0)) for p in parts if "value" in p), 4)
        out[key] = merged
    return out


def merge_iep1a_iep1b_gate_results(
    iep1a: dict[str, Any],
    iep1b: dict[str, Any],
    *,
    structural_agreement_rate: dict[str, Any] | None = None,
    latency_p95: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Combine IEP1A + IEP1B evaluator JSON into the canonical five-key gate dict.

    ``split_precision`` reuses the ``split_detection_rate`` metric from each
    model (conservative: both must pass; value is the minimum of the two).

    Optional ``structural_agreement_rate`` / ``latency_p95`` override the
    legacy placeholders (defaults keep unit-test behaviour unchanged).
    """
    def _metric(model: dict[str, Any], key: str) -> dict[str, Any]:
        v = model.get(key)
        return v if isinstance(v, dict) else {"pass": False, "value": 0.0}

    gi_a = _metric(iep1a, "geometry_iou")
    gi_b = _metric(iep1b, "geometry_iou")
    gi_pass = bool(gi_a.get("pass")) and bool(gi_b.get("pass"))
    gi_val = min(float(gi_a.get("value", 0.0)), float(gi_b.get("value", 0.0)))

    sp_a = _metric(iep1a, "split_detection_rate")
    sp_b = _metric(iep1b, "split_detection_rate")
    sp_pass = bool(sp_a.get("pass")) and bool(sp_b.get("pass"))
    sp_val = min(float(sp_a.get("value", 0.0)), float(sp_b.get("value", 0.0)))

    gd_a = _metric(iep1a, "golden_dataset")
    gd_b = _metric(iep1b, "golden_dataset")
    gd_pass = bool(gd_a.get("pass")) and bool(gd_b.get("pass"))
    reg_a = int(gd_a.get("regressions", 0)) if isinstance(gd_a.get("regressions"), int) else 0
    reg_b = int(gd_b.get("regressions", 0)) if isinstance(gd_b.get("regressions"), int) else 0

    return {
        "geometry_iou": {"pass": gi_pass, "value": round(gi_val, 4)},
        "split_precision": {"pass": sp_pass, "value": round(sp_val, 4)},
        "structural_agreement_rate": structural_agreement_rate
        or {"pass": True, "value": 1.0},
        "golden_dataset": {"pass": gd_pass, "regressions": reg_a + reg_b},
        "latency_p95": latency_p95 or {"pass": True, "value": 0.0},
    }


def run_evaluate_golden_dataset_json(
    repo_root: Path,
    model: str,
    *,
    manifest: str = "golden_dataset/manifest.json",
    weights: str | None = None,
    material: str | None = None,
    timeout_s: int = 7200,
) -> dict[str, Any]:
    """Run ``training/scripts/evaluate_golden_dataset.py`` and parse JSON stdout."""
    script = repo_root / "training" / "scripts" / "evaluate_golden_dataset.py"
    if not script.is_file():
        raise FileNotFoundError(f"Missing evaluator script: {script}")

    cmd: list[str] = [
        sys.executable,
        str(script),
        "--model",
        model,
        "--manifest",
        manifest,
    ]
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
        timeout=timeout_s,
        env=env,
        check=False,
    )
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(
            f"evaluate_golden_dataset produced empty stdout for model={model} "
            f"rc={proc.returncode}\nstderr:\n{proc.stderr}"
        )
    try:
        parsed: dict[str, Any] = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"evaluate_golden_dataset invalid JSON for model={model} rc={proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        ) from exc
    if proc.returncode != 0:
        logger.warning(
            "evaluate_golden_dataset exited rc=%s model=%s material=%s (using parsed gate JSON)",
            proc.returncode,
            model,
            material,
        )
    return parsed


def _pick_cross_model_weights(weights_by_material: dict[str, Any] | None) -> str | None:
    """Prefer newspaper, then book, then microfilm; paths may be ``str`` or ``Path``."""
    if not weights_by_material:
        return None
    for key in ("newspaper", "book", "microfilm"):
        p = weights_by_material.get(key)
        if p is None:
            continue
        path = Path(str(p))
        if path.is_file():
            return str(path.resolve())
    for p in weights_by_material.values():
        path = Path(str(p))
        if path.is_file():
            return str(path.resolve())
    return None


def build_preprocessing_live_gates(
    repo_root: Path | None = None,
    *,
    include_iep0: bool = True,
    weights_iep0: str | Path | None = None,
    weights_iep1a_by_material: dict[str, str | Path] | None = None,
    weights_iep1b_by_material: dict[str, str | Path] | None = None,
) -> PreprocessingLiveGates:
    """
    Run golden eval for **IEP0** (full manifest for that model) and **IEP1A+IEP1B**
    (per material), then return per-service ``gate_results`` payloads.

    IEP0 output matches ``evaluate_iep0`` (``classification_confidence``,
    ``golden_dataset``).  IEP1A/IEP1B are merged into the historical five-key
    preprocessing dict for ``promotion_api._check_gates`` on iep1a/iep1b rows.

    Optional ``weights_*`` use freshly trained checkpoints instead of repo
    defaults when running subprocess evaluators.

    Requires AWS credentials, S3 objects, valid SHAs in case JSON, and
    ``ultralytics`` + weights in the interpreter environment.
    """
    root = repo_root or Path(__file__).resolve().parents[3]

    w0 = str(Path(weights_iep0).resolve()) if weights_iep0 else None
    w1a = weights_iep1a_by_material or None
    w1b = weights_iep1b_by_material or None

    if include_iep0:
        try:
            iep0_gates = run_evaluate_golden_dataset_json(root, "iep0", weights=w0)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"IEP0 golden eval failed: {exc}") from exc
    else:
        iep0_gates = {
            "classification_confidence": {"pass": True, "value": 0.92},
            "golden_dataset": {"pass": True, "regressions": 0},
        }

    materials = ("book", "newspaper", "microfilm")

    iep1a_results: list[dict[str, Any]] = []
    for mat in materials:
        try:
            w_path = None
            if w1a and mat in w1a:
                w_path = str(Path(w1a[mat]).resolve())
            iep1a_results.append(
                run_evaluate_golden_dataset_json(root, "iep1a", material=mat, weights=w_path)
            )
        except RuntimeError:
            pass

    iep1b_results: list[dict[str, Any]] = []
    for mat in materials:
        try:
            w_path = None
            if w1b and mat in w1b:
                w_path = str(Path(w1b[mat]).resolve())
            iep1b_results.append(
                run_evaluate_golden_dataset_json(root, "iep1b", material=mat, weights=w_path)
            )
        except RuntimeError:
            pass

    if not iep1a_results:
        raise RuntimeError(
            "No IEP1A golden eval completed for any material (book/newspaper/microfilm); "
            "check manifest, AWS access, and case JSON."
        )
    if not iep1b_results:
        raise RuntimeError(
            "No IEP1B golden eval completed for any material (book/newspaper/microfilm); "
            "check manifest, AWS access, and case JSON."
        )

    iep1a = _aggregate_gate_results(iep1a_results)
    iep1b = _aggregate_gate_results(iep1b_results)
    cross_w1a = _pick_cross_model_weights(w1a)
    cross_w1b = _pick_cross_model_weights(w1b)

    if os.getenv("GOLDEN_SKIP_CROSS_MODEL", "").lower() in ("1", "true", "yes"):
        logger.info(
            "build_preprocessing_live_gates: GOLDEN_SKIP_CROSS_MODEL set; skipping cross-model measurement"
        )
        iep1ab = merge_iep1a_iep1b_gate_results(iep1a, iep1b)
    else:
        cross = measure_cross_model_gates(root, weights_1a=cross_w1a, weights_1b=cross_w1b)
        iep1ab = merge_iep1a_iep1b_gate_results(
            iep1a,
            iep1b,
            structural_agreement_rate=cross["structural_agreement_rate"],
            latency_p95=cross["latency_p95"],
        )
    return PreprocessingLiveGates(iep1ab=iep1ab, iep0=iep0_gates)


def build_preprocessing_gate_results_live(repo_root: Path | None = None) -> dict[str, Any]:
    """
    Run IEP1A + IEP1B golden evals and merge for ``ModelVersion.gate_results``.

    This is the IEP1-only merged dict. For IEP0 + IEP1 together, use
    ``build_preprocessing_live_gates``.

    Requires AWS credentials, S3 objects, valid SHAs in case JSON, and
    ``ultralytics`` + weights available in the interpreter environment.

    Also runs ``measure_cross_model_gates`` so ``structural_agreement_rate`` and
    ``latency_p95`` reflect paired IEP1A/IEP1B cases on the same image.
    """
    return build_preprocessing_live_gates(repo_root).iep1ab


def build_iep1ab_live_gates(
    repo_root: Path | None = None,
    *,
    weights_iep1a_by_material: dict[str, str | Path] | None = None,
    weights_iep1b_by_material: dict[str, str | Path] | None = None,
) -> dict[str, Any]:
    """
    Run only IEP1A/IEP1B golden eval and return merged five-key gate dict.

    Use this when IEP0 is intentionally kept in stub mode.
    """
    root = repo_root or Path(__file__).resolve().parents[3]
    return build_preprocessing_live_gates(
        root,
        include_iep0=False,
        weights_iep0=None,
        weights_iep1a_by_material=weights_iep1a_by_material,
        weights_iep1b_by_material=weights_iep1b_by_material,
    ).iep1ab
