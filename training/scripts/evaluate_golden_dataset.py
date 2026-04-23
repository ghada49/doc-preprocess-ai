"""
Evaluate golden dataset cases for IEP0, IEP1A, and IEP1B.

Usage examples:
  python training/scripts/evaluate_golden_dataset.py --model iep1a
  python training/scripts/evaluate_golden_dataset.py --model iep1b --weights models/iep1b/Newspaper_Keypoints.pt
  python training/scripts/evaluate_golden_dataset.py --model iep0 --manifest golden_dataset/manifest.json
  python training/scripts/evaluate_golden_dataset.py --model iep1a --material newspaper

S3 downloads retry on transient resets (``GOLDEN_S3_READ_MAX_ATTEMPTS``,
``GOLDEN_S3_READ_RETRY_BASE_SECONDS``) and use adaptive botocore retries plus
``S3_READ_TIMEOUT`` / ``S3_CONNECT_TIMEOUT`` (see ``_build_s3_client``).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import boto3
import numpy as np
from PIL import Image
from botocore.config import Config
from botocore.exceptions import ClientError, ResponseStreamingError
from ultralytics import YOLO
from urllib3.exceptions import ProtocolError as Urllib3ProtocolError


DEFAULT_WEIGHTS: dict[str, Any] = {
    "iep0": "models/iep0/classifier.pt",
    "iep1a": {
        "book": "models/iep1a/Book_segmentation.pt",
        "newspaper": "models/iep1a/Newspaper_Segmentation.pt",
        "microfilm": "models/iep1a/Segmentation_microfilm.pt",
    },
    "iep1b": {
        "book": "models/iep1b/Book_keypoint.pt",
        "newspaper": "models/iep1b/Newspaper_Keypoints.pt",
        "microfilm": "models/iep1b/Microfilm_Keypoints.pt",
    },
}


def _resolve_weights_path(
    model: str,
    cases: list[dict[str, Any]],
    weights_override: str | None,
    material: str | None,
) -> str:
    """Pick ``.pt`` path: explicit ``--weights``, then per-material defaults for IEP1A/B."""
    if weights_override:
        return weights_override
    spec = DEFAULT_WEIGHTS[model]
    if isinstance(spec, str):
        return spec
    if not isinstance(spec, dict):
        return str(spec)
    if material:
        if material not in spec:
            raise ValueError(
                f"No DEFAULT_WEIGHTS entry for model={model!r} material_type={material!r}"
            )
        return str(spec[material])
    materials = {c.get("material_type") for c in cases if c.get("material_type")}
    if len(materials) != 1:
        raise ValueError(
            f"Need a single material_type in golden cases for model={model!r} (got {materials!r}). "
            "Pass --material book|newspaper|microfilm or --weights PATH."
        )
    m = materials.pop()
    if m not in spec:
        raise ValueError(f"No DEFAULT_WEIGHTS entry for model={model!r} material_type={m!r}")
    return str(spec[m])


def _load_dotenv_file(path: Path) -> None:
    """Populate os.environ from a simple KEY=VAL .env file (no shell expansion)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _ensure_aws_env_from_repo_dotenv() -> None:
    """
    Load repo-root .env if present so plain `python ...evaluate...` works.

    Does not override variables already set in the process environment.
    Maps S3_* names used in this project to standard AWS_* names for boto3.
    """
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for path in candidates:
        _load_dotenv_file(path)

    if not os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("S3_ACCESS_KEY_ID"):
        os.environ["AWS_ACCESS_KEY_ID"] = os.environ["S3_ACCESS_KEY_ID"]
    if not os.getenv("AWS_SECRET_ACCESS_KEY") and os.getenv("S3_SECRET_ACCESS_KEY"):
        os.environ["AWS_SECRET_ACCESS_KEY"] = os.environ["S3_SECRET_ACCESS_KEY"]
    if not os.getenv("AWS_DEFAULT_REGION") and os.getenv("S3_REGION"):
        os.environ["AWS_DEFAULT_REGION"] = os.environ["S3_REGION"]


def _load_case(case_meta: dict[str, Any], manifest_dir: Path) -> dict[str, Any]:
    path = manifest_dir / case_meta["annotation_path"]
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_transient_s3_read_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            ResponseStreamingError,
            Urllib3ProtocolError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
        ),
    ):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) in (10054, 10053):
            return True
        if exc.errno in (104,):  # ECONNRESET on some POSIX stacks
            return True
    return False


def _fetch_image_bytes(
    s3_client: Any, bucket: str, key: str, *, case_id: str | None = None
) -> bytes:
    """
    Download full object bytes with retries.

    Long runs (many large TIFFs) often hit transient TLS resets
    (``ConnectionResetError`` / ``ResponseStreamingError``) on Windows; retrying
    the whole ``get_object`` + ``read()`` usually succeeds.
    """
    max_attempts = int(os.getenv("GOLDEN_S3_READ_MAX_ATTEMPTS", "6"))
    base_sleep = float(os.getenv("GOLDEN_S3_READ_RETRY_BASE_SECONDS", "1.0"))
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            obj = s3_client.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()
        except Exception as exc:
            # botocore often raises ClientError; some versions raise errorfactory.NoSuchKey
            # (not always isinstance(exc, ClientError)).
            resp = getattr(exc, "response", None)
            err = (resp or {}).get("Error", {}) if isinstance(resp, dict) else {}
            code = err.get("Code", "") if isinstance(err, dict) else ""
            missing = code in ("NoSuchKey", "404") or type(exc).__name__ == "NoSuchKey"
            if missing:
                who = f"case_id={case_id!r} " if case_id else ""
                raise FileNotFoundError(
                    f"{who}S3 object not found: s3://{bucket}/{key}. "
                    "Upload to this exact key or fix image_s3_key in the case JSON "
                    "(keys are case-sensitive; iep1a/ vs iep1b/ are different prefixes)."
                ) from exc
            if isinstance(exc, ClientError):
                raise
            if not _is_transient_s3_read_error(exc):
                raise
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            delay = min(base_sleep * (2**attempt), 30.0)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _write_temp_image(image_bytes: bytes, key: str) -> str:
    """Decode ``image_bytes`` and write a temp file as 3-channel RGB.

    YOLO / Ultralytics expect 3-channel input. Microfilm scans are often
    single-channel (``L``) or palette (``P``) TIFFs, which otherwise yield
    ``expected input ... to have 3 channels, but got 1``.

    ``key`` is unused but kept for call-site clarity. Output is always PNG.
    """
    _ = key
    dest = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    dest.close()
    tmp_path = dest.name
    with Image.open(io.BytesIO(image_bytes)) as img:
        img.convert("RGB").save(tmp_path, format="PNG")
    return tmp_path


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


def _match_best_iou(pred_boxes: list[list[float]], gt_box: list[float]) -> float:
    if not pred_boxes:
        return 0.0
    return max(_compute_iou_xyxy(p, gt_box) for p in pred_boxes)


def _match_pred_detection(
    pred_dets: list[dict[str, Any]], gt_class: int, gt_box: list[float]
) -> dict[str, Any] | None:
    best = None
    best_iou = -1.0
    for det in pred_dets:
        if det["class_id"] != gt_class:
            continue
        iou = _compute_iou_xyxy(det["bbox_xyxy"], gt_box)
        if iou > best_iou:
            best_iou = iou
            best = det
    return best


def evaluate_iep0(cases: list[dict[str, Any]], model: YOLO, s3_client: Any, bucket: str) -> dict[str, Any]:
    regressions = 0
    confidences: list[float] = []

    for case in cases:
        image_bytes = _fetch_image_bytes(
            s3_client, bucket, case["image_s3_key"], case_id=case.get("case_id")
        )
        actual_sha = _sha256_bytes(image_bytes)
        if actual_sha != case["image_sha256"]:
            raise ValueError(
                f"SHA mismatch for {case['case_id']}: expected {case['image_sha256']}, got {actual_sha}"
            )

        tmp_path = _write_temp_image(image_bytes, case["image_s3_key"])
        try:
            result = model.predict(source=tmp_path, verbose=False)[0]
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        top1_id = int(result.probs.top1)
        top1_name = result.names[top1_id]
        top1_conf = float(result.probs.top1conf)
        confidences.append(top1_conf)

        expected = case["annotations"]["expected_class"]
        min_conf = float(case["thresholds"]["confidence_min"])
        if top1_name != expected or top1_conf < min_conf:
            regressions += 1

    mean_conf = float(np.mean(confidences)) if confidences else 0.0
    return {
        "classification_confidence": {
            "pass": mean_conf >= 0.8,
            "value": round(mean_conf, 4),
        },
        "golden_dataset": {
            "pass": regressions == 0,
            "regressions": regressions,
        },
    }


def evaluate_iep1a(cases: list[dict[str, Any]], model: YOLO, s3_client: Any, bucket: str) -> dict[str, Any]:
    regressions = 0
    iou_scores: list[float] = []
    split_correct = 0
    split_total = 0

    for case in cases:
        image_bytes = _fetch_image_bytes(
            s3_client, bucket, case["image_s3_key"], case_id=case.get("case_id")
        )
        actual_sha = _sha256_bytes(image_bytes)
        if actual_sha != case["image_sha256"]:
            raise ValueError(
                f"SHA mismatch for {case['case_id']}: expected {case['image_sha256']}, got {actual_sha}"
            )

        tmp_path = _write_temp_image(image_bytes, case["image_s3_key"])
        try:
            result = model.predict(source=tmp_path, verbose=False)[0]
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        pred_boxes = []
        pred_confs = []
        if result.boxes is not None and len(result.boxes) > 0:
            for xywhn, conf in zip(result.boxes.xywhn.tolist(), result.boxes.conf.tolist()):
                pred_boxes.append(_xywhn_to_xyxy(xywhn))
                pred_confs.append(float(conf))

        if case.get("split_expected") is not None:
            split_total += 1
            predicted_split = len(pred_boxes) >= 2
            if predicted_split == bool(case["split_expected"]):
                split_correct += 1
            else:
                regressions += 1

        gt_boxes = case["annotations"].get("bboxes", [])
        conf_min = float(case["thresholds"].get("confidence_min", 0.0))
        iou_min = float(case["thresholds"].get("iou_min", 0.0))

        for gt_box in gt_boxes:
            best_iou = _match_best_iou(pred_boxes, gt_box)
            iou_scores.append(best_iou)
            if best_iou < iou_min:
                regressions += 1

        if pred_confs and min(pred_confs) < conf_min:
            regressions += 1

    mean_iou = float(np.mean(iou_scores)) if iou_scores else 0.0
    split_rate = (split_correct / split_total) if split_total else 1.0
    return {
        "geometry_iou": {
            "pass": mean_iou >= 0.75,
            "value": round(mean_iou, 4),
        },
        "split_detection_rate": {
            "pass": split_rate >= 1.0,
            "value": round(split_rate, 4),
        },
        "golden_dataset": {
            "pass": regressions == 0,
            "regressions": regressions,
        },
    }


def _pred_detections_from_yolo_result(result: Any) -> list[dict[str, Any]]:
    """Normalize one Ultralytics ``result`` into IEP1B-style detection dicts."""
    pred_detections: list[dict[str, Any]] = []
    if result.boxes is None or len(result.boxes) == 0:
        return pred_detections
    keypoints = result.keypoints.xyn.tolist() if result.keypoints is not None else []
    for idx, (xywhn, cls_id, conf) in enumerate(
        zip(
            result.boxes.xywhn.tolist(),
            result.boxes.cls.tolist(),
            result.boxes.conf.tolist(),
        )
    ):
        pred_detections.append(
            {
                "class_id": int(cls_id),
                "bbox_xyxy": _xywhn_to_xyxy(xywhn),
                "confidence": float(conf),
                "keypoints": keypoints[idx] if idx < len(keypoints) else [],
            }
        )
    return pred_detections


def dump_iep1b_case_predictions(
    case: dict[str, Any], model: YOLO, s3_client: Any, bucket: str
) -> dict[str, Any]:
    """
    Run inference once for ``case`` and return GT + predictions + per-GT match stats.

    Intended for debugging (e.g. compare failing vs passing ``rot90`` cases).
    """
    image_bytes = _fetch_image_bytes(
        s3_client, bucket, case["image_s3_key"], case_id=case.get("case_id")
    )
    actual_sha = _sha256_bytes(image_bytes)
    if actual_sha != case["image_sha256"]:
        raise ValueError(
            f"SHA mismatch for {case['case_id']}: expected {case['image_sha256']}, got {actual_sha}"
        )
    tmp_path = _write_temp_image(image_bytes, case["image_s3_key"])
    try:
        result = model.predict(source=tmp_path, verbose=False)[0]
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    pred_detections = _pred_detections_from_yolo_result(result)
    iou_min = float(case["thresholds"].get("iou_min", 0.0))
    kp_dist_max = float(case["thresholds"].get("mean_keypoint_distance_max", 1.0))
    conf_min = float(case["thresholds"].get("confidence_min", 0.0))

    per_gt: list[dict[str, Any]] = []
    for gt_det in case["annotations"].get("detections", []):
        gt_class = int(gt_det["class_id"])
        gt_box = gt_det["bbox_xyxy"]
        pred = _match_pred_detection(pred_detections, gt_class, gt_box)
        entry: dict[str, Any] = {
            "class_id": gt_class,
            "gt_bbox_xyxy": gt_box,
            "gt_keypoints": gt_det.get("keypoints", []),
        }
        if pred is None:
            entry["matched_prediction"] = None
            per_gt.append(entry)
            continue
        entry["matched_prediction"] = {
            "bbox_xyxy": pred["bbox_xyxy"],
            "confidence": pred["confidence"],
            "keypoints": pred.get("keypoints", []),
        }
        entry["iou_vs_gt"] = round(_compute_iou_xyxy(pred["bbox_xyxy"], gt_box), 6)
        entry["confidence_passes"] = bool(pred["confidence"] >= conf_min)
        gt_kps = gt_det.get("keypoints", [])
        pred_kps = pred.get("keypoints", [])
        if gt_kps and pred_kps and len(gt_kps) == len(pred_kps):
            corner_dists = [
                round(float(np.hypot(px - gx, py - gy)), 6)
                for (gx, gy), (px, py) in zip(gt_kps, pred_kps)
            ]
            entry["keypoint_corner_distances"] = corner_dists
            entry["mean_keypoint_distance"] = round(float(np.mean(corner_dists)), 6)
            entry["mean_keypoint_distance_passes"] = bool(
                float(np.mean(corner_dists)) <= kp_dist_max
            )
        else:
            entry["keypoint_corner_distances"] = None
            entry["mean_keypoint_distance"] = None
            entry["mean_keypoint_distance_passes"] = False
            entry["keypoint_note"] = (
                f"gt_kps={len(gt_kps)} pred_kps={len(pred_kps)} (need equal length for distance)"
            )
        per_gt.append(entry)

    return {
        "case_id": case["case_id"],
        "image_s3_key": case["image_s3_key"],
        "thresholds": case.get("thresholds", {}),
        "ground_truth_detections": case["annotations"].get("detections", []),
        "predicted_detections": pred_detections,
        "per_ground_truth_match": per_gt,
    }


def evaluate_iep1b(
    cases: list[dict[str, Any]],
    model: YOLO,
    s3_client: Any,
    bucket: str,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    regressions = 0
    iou_scores: list[float] = []
    mean_kp_distances: list[float] = []
    split_correct = 0
    split_total = 0

    for case in cases:
        cid = case["case_id"]

        def _log(msg: str) -> None:
            if verbose:
                print(f"[evaluate iep1b] {cid}: {msg}", file=sys.stderr)

        def _regression_debug(
            reason: str,
            *,
            gt_class: str | int = "-",
            pred_conf: float | None = None,
            iou: float | None = None,
            conf_min: float,
            iou_min: float,
        ) -> None:
            pc = f"{pred_conf:.6f}" if pred_conf is not None else "n/a"
            piou = f"{iou:.6f}" if iou is not None else "n/a"
            print(
                "[evaluate iep1b regression] "
                f"case_id={cid} reason={reason} gt_class={gt_class} "
                f"pred_conf={pc} confidence_min={conf_min} pred_iou={piou} iou_min={iou_min}",
                file=sys.stderr,
            )

        image_bytes = _fetch_image_bytes(
            s3_client, bucket, case["image_s3_key"], case_id=case.get("case_id")
        )
        actual_sha = _sha256_bytes(image_bytes)
        if actual_sha != case["image_sha256"]:
            raise ValueError(
                f"SHA mismatch for {case['case_id']}: expected {case['image_sha256']}, got {actual_sha}"
            )

        tmp_path = _write_temp_image(image_bytes, case["image_s3_key"])
        try:
            result = model.predict(source=tmp_path, verbose=False)[0]
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        pred_detections = _pred_detections_from_yolo_result(result)

        iou_min = float(case["thresholds"].get("iou_min", 0.0))
        kp_dist_max = float(case["thresholds"].get("mean_keypoint_distance_max", 1.0))
        conf_min = float(case["thresholds"].get("confidence_min", 0.0))

        if case.get("split_expected") is not None:
            split_total += 1
            predicted_split = len(pred_detections) >= 2
            if predicted_split == bool(case["split_expected"]):
                split_correct += 1
            else:
                regressions += 1
                _regression_debug(
                    "split_expected_mismatch",
                    gt_class="-",
                    pred_conf=None,
                    iou=None,
                    conf_min=conf_min,
                    iou_min=iou_min,
                )
                _log(
                    f"split_expected={case['split_expected']!r} but predicted_split={predicted_split!r} "
                    f"(pred_detections={len(pred_detections)})"
                )

        for gt_det in case["annotations"].get("detections", []):
            gt_class = int(gt_det["class_id"])
            gt_box = gt_det["bbox_xyxy"]
            pred = _match_pred_detection(pred_detections, gt_class, gt_box)
            if pred is None:
                regressions += 1
                iou_scores.append(0.0)
                _regression_debug(
                    "no_pred_box_match",
                    gt_class=gt_class,
                    pred_conf=None,
                    iou=None,
                    conf_min=conf_min,
                    iou_min=iou_min,
                )
                _log(f"class_id={gt_class}: no predicted box matched GT (IoU gate)")
                continue

            iou = _compute_iou_xyxy(pred["bbox_xyxy"], gt_box)
            pred_conf_f = float(pred["confidence"])
            iou_scores.append(iou)
            if iou < iou_min:
                regressions += 1
                _regression_debug(
                    "iou_below_min",
                    gt_class=gt_class,
                    pred_conf=pred_conf_f,
                    iou=iou,
                    conf_min=conf_min,
                    iou_min=iou_min,
                )
                _log(f"class_id={gt_class}: IoU={iou:.4f} < iou_min={iou_min}")

            if pred["confidence"] < conf_min:
                regressions += 1
                _regression_debug(
                    "confidence_below_min",
                    gt_class=gt_class,
                    pred_conf=pred_conf_f,
                    iou=iou,
                    conf_min=conf_min,
                    iou_min=iou_min,
                )
                _log(
                    f"class_id={gt_class}: confidence={float(pred['confidence']):.4f} < confidence_min={conf_min}"
                )

            gt_kps = gt_det.get("keypoints", [])
            pred_kps = pred.get("keypoints", [])
            if gt_kps and pred_kps and len(gt_kps) == len(pred_kps):
                dists = []
                for (gx, gy), (px, py) in zip(gt_kps, pred_kps):
                    dists.append(float(np.hypot(px - gx, py - gy)))
                mean_dist = float(np.mean(dists))
                mean_kp_distances.append(mean_dist)
                if mean_dist > kp_dist_max:
                    regressions += 1
                    _regression_debug(
                        "mean_keypoint_distance_above_max",
                        gt_class=gt_class,
                        pred_conf=pred_conf_f,
                        iou=iou,
                        conf_min=conf_min,
                        iou_min=iou_min,
                    )
                    _log(
                        f"class_id={gt_class}: mean_keypoint_distance={mean_dist:.4f} > max={kp_dist_max}"
                    )
            else:
                regressions += 1
                _regression_debug(
                    "keypoint_layout_mismatch",
                    gt_class=gt_class,
                    pred_conf=pred_conf_f,
                    iou=iou,
                    conf_min=conf_min,
                    iou_min=iou_min,
                )
                _log(
                    f"class_id={gt_class}: keypoint layout mismatch "
                    f"(gt_kps={len(gt_kps)}, pred_kps={len(pred_kps)})"
                )

    mean_iou = float(np.mean(iou_scores)) if iou_scores else 0.0
    mean_kp = float(np.mean(mean_kp_distances)) if mean_kp_distances else 1.0
    split_rate = (split_correct / split_total) if split_total else 1.0
    return {
        "geometry_iou": {
            "pass": mean_iou >= 0.7,
            "value": round(mean_iou, 4),
        },
        "keypoint_distance": {
            "pass": mean_kp <= 0.03,
            "value": round(mean_kp, 4),
        },
        "split_detection_rate": {
            "pass": split_rate >= 1.0,
            "value": round(split_rate, 4),
        },
        "golden_dataset": {
            "pass": regressions == 0,
            "regressions": regressions,
        },
    }


def _build_s3_client(endpoint_url: str | None) -> Any:
    kwargs: dict[str, Any] = {}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION")
    if region:
        kwargs["region_name"] = region
    kwargs["config"] = Config(
        retries={"max_attempts": int(os.getenv("AWS_MAX_ATTEMPTS", "10")), "mode": "adaptive"},
        connect_timeout=int(os.getenv("S3_CONNECT_TIMEOUT", "60")),
        read_timeout=int(os.getenv("S3_READ_TIMEOUT", "300")),
    )
    return boto3.client("s3", **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, choices=["iep0", "iep1a", "iep1b"])
    parser.add_argument("--manifest", default="golden_dataset/manifest.json")
    parser.add_argument("--weights", default=None, help="Path to model weights (.pt)")
    parser.add_argument(
        "--material",
        default=None,
        choices=["book", "newspaper", "microfilm"],
        help="For IEP1A/IEP1B: keep only cases with this material_type and load matching DEFAULT_WEIGHTS",
    )
    parser.add_argument("--s3-bucket", default=None, help="Override bucket from manifest")
    parser.add_argument("--s3-endpoint-url", default=os.getenv("S3_ENDPOINT_URL", ""))
    parser.add_argument(
        "--device",
        default=os.getenv("GOLDEN_YOLO_DEVICE", ""),
        help="YOLO device (e.g. cpu, 0). Default: env GOLDEN_YOLO_DEVICE or Ultralytics default. "
        "Use cpu if loading weights hits CUDA out-of-memory.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="For IEP1B: print each golden_dataset regression to stderr (case_id + reason).",
    )
    parser.add_argument(
        "--dump-predictions-for",
        metavar="CASE_ID",
        default=None,
        help="IEP1B only: load one case by case_id, run predict, print JSON "
        "(GT + predicted boxes/keypoints + per-GT corner distances) to stdout and exit.",
    )
    args = parser.parse_args()

    _ensure_aws_env_from_repo_dotenv()

    if (args.device or "").strip().lower() == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""

    manifest_path = Path(args.manifest)
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest_dir = manifest_path.parent

    s3_bucket = args.s3_bucket or manifest["s3_bucket"]
    s3_client = _build_s3_client(args.s3_endpoint_url or None)

    case_metas = [c for c in manifest["cases"] if c["model"] == args.model]
    cases = [_load_case(meta, manifest_dir) for meta in case_metas]
    if not cases:
        raise ValueError(f"No cases found for model '{args.model}' in {manifest_path}")

    if args.material and args.model in ("iep1a", "iep1b"):
        cases = [c for c in cases if c.get("material_type") == args.material]
        if not cases:
            raise ValueError(
                f"No cases for model={args.model!r} material_type={args.material!r} in {manifest_path}"
            )

    weights = _resolve_weights_path(args.model, cases, args.weights, args.material)
    try:
        model = YOLO(weights)
    except (RuntimeError, MemoryError) as exc:
        msg = str(exc).lower()
        if isinstance(exc, MemoryError) or "out of memory" in msg or "cuda" in msg:
            raise SystemExit(
                f"Failed to load YOLO weights (likely GPU/CPU memory): {weights}\n"
                f"Original error: {exc}\n"
                "Try: python ... --device cpu   (or set GOLDEN_YOLO_DEVICE=cpu), "
                "or free VRAM / use a machine with enough RAM for this checkpoint."
            ) from exc
        raise

    if args.dump_predictions_for:
        if args.model != "iep1b":
            raise SystemExit("--dump-predictions-for requires --model iep1b")
        want = args.dump_predictions_for.strip()
        hits = [c for c in cases if c.get("case_id") == want]
        if not hits:
            raise SystemExit(
                f"No case with case_id={want!r} after manifest + material filters."
            )
        payload = dump_iep1b_case_predictions(hits[0], model, s3_client, s3_bucket)
        print(json.dumps(payload, indent=2))
        return 0

    if args.model == "iep0":
        gate_results = evaluate_iep0(cases, model, s3_client, s3_bucket)
    elif args.model == "iep1a":
        gate_results = evaluate_iep1a(cases, model, s3_client, s3_bucket)
    else:
        gate_results = evaluate_iep1b(
            cases, model, s3_client, s3_bucket, verbose=args.verbose
        )

    print(json.dumps(gate_results, indent=2))
    failed = [name for name, result in gate_results.items() if not result.get("pass", False)]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
