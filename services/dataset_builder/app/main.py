"""
Dataset Builder Job
-------------------

Modes:
  - scheduled/triggered: register an existing manifest into the dataset registry.
  - corrected-export: build a fresh retraining dataset from human-corrected rows
    in page_lineage, emit YOLO segmentation labels + data.yaml files, write a
    retraining manifest, and register it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
import yaml
from sqlalchemy import create_engine, text


@dataclass
class CorrectedRow:
    lineage_id: str
    job_id: str
    page_number: int
    material_type: str
    iep1a_used: bool
    iep1b_used: bool
    human_correction_timestamp: datetime | None
    correction: dict[str, Any]
    gate_results: dict[str, Any]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_path(p: str, root: Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (root / path).resolve()


def _iter_manifest_paths(manifest: dict[str, Any], root: Path) -> list[Path]:
    paths: list[Path] = []
    iep0 = (manifest.get("iep0") or {}).get("data_root")
    if isinstance(iep0, str) and iep0.strip():
        paths.append(_resolve_path(iep0, root))
    for family in ("iep1a", "iep1b"):
        block = manifest.get(family) or {}
        if not isinstance(block, dict):
            continue
        for material in ("book", "newspaper", "microfilm"):
            raw = block.get(material)
            if isinstance(raw, str) and raw.strip():
                paths.append(_resolve_path(raw, root))
    return paths


def _compute_manifest_checksum(manifest: dict[str, Any], root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(_iter_manifest_paths(manifest, root)):
        if path.is_file():
            h.update(path.read_bytes())
            continue
        if path.is_dir():
            for f in sorted(path.rglob("*")):
                if f.is_file():
                    h.update(f.read_bytes())
            continue
        raise FileNotFoundError(f"Referenced training path does not exist: {path}")
    return h.hexdigest()


def _resolve_dimensions(correction: dict[str, Any], gate_results: dict[str, Any]) -> tuple[float, float]:
    w = correction.get("image_width")
    h = correction.get("image_height")
    if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
        return float(w), float(h)
    downsample = gate_results.get("downsample") if isinstance(gate_results, dict) else None
    if isinstance(downsample, dict):
        dw = downsample.get("downsampled_width")
        dh = downsample.get("downsampled_height")
        if isinstance(dw, (int, float)) and isinstance(dh, (int, float)) and dw > 0 and dh > 0:
            return float(dw), float(dh)
    raise ValueError("missing image dimensions in correction fields and gate_results.downsample")


def _rotate_point(x: float, y: float, cx: float, cy: float, angle_deg: float) -> tuple[float, float]:
    import math

    theta = math.radians(angle_deg)
    c, s = math.cos(theta), math.sin(theta)
    tx, ty = x - cx, y - cy
    rx = tx * c - ty * s
    ry = tx * s + ty * c
    return rx + cx, ry + cy


def _extract_corners_abs(correction: dict[str, Any]) -> list[tuple[float, float]]:
    raw_quad = correction.get("quad_points")
    if isinstance(raw_quad, list) and len(raw_quad) == 4:
        corners: list[tuple[float, float]] = []
        for p in raw_quad:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise ValueError("quad_points must be [[x,y] x4]")
            x, y = p
            corners.append((float(x), float(y)))
        return corners

    crop = correction.get("crop_box")
    if isinstance(crop, list) and len(crop) == 4:
        x_min, y_min, x_max, y_max = [float(v) for v in crop]
        corners = [
            (x_min, y_min),
            (x_max, y_min),
            (x_max, y_max),
            (x_min, y_max),
        ]
        angle = correction.get("deskew_angle")
        if isinstance(angle, (int, float)) and abs(float(angle)) > 1e-9:
            cx = (x_min + x_max) / 2.0
            cy = (y_min + y_max) / 2.0
            corners = [_rotate_point(x, y, cx, cy, float(angle)) for x, y in corners]
        return corners

    raise ValueError("neither quad_points nor crop_box found in human_correction_fields")


def _canonicalize_corners(corners_abs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(corners_abs) != 4:
        raise ValueError("exactly 4 corners required")
    sums = [x + y for x, y in corners_abs]
    diffs = [x - y for x, y in corners_abs]
    tl = corners_abs[sums.index(min(sums))]
    br = corners_abs[sums.index(max(sums))]
    tr = corners_abs[diffs.index(max(diffs))]
    bl = corners_abs[diffs.index(min(diffs))]
    return [tl, tr, br, bl]


def _to_norm(corners_abs: list[tuple[float, float]], width: float, height: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for x, y in corners_abs:
        out.append((max(0.0, min(1.0, x / width)), max(0.0, min(1.0, y / height))))
    return out


def _seg_line(corners_norm: list[tuple[float, float]]) -> str:
    vals = " ".join(f"{x:.6f} {y:.6f}" for x, y in corners_norm)
    return f"0 {vals}"


def _material_bucket(material_type: str) -> str:
    mt = material_type.strip().lower()
    if mt in {"book", "newspaper", "microfilm"}:
        return mt
    if mt == "archival_document":
        return "microfilm"
    return "book"


def _fetch_corrected_rows(database_url: str) -> list[CorrectedRow]:
    engine = create_engine(database_url)
    stmt = text(
        """
        SELECT
            lineage_id,
            job_id,
            page_number,
            material_type,
            iep1a_used,
            iep1b_used,
            human_correction_timestamp,
            human_correction_fields,
            gate_results
        FROM page_lineage
        WHERE human_corrected = TRUE
          AND human_correction_fields IS NOT NULL
          AND acceptance_decision = 'accepted'
        """
    )
    rows: list[CorrectedRow] = []
    with engine.connect() as conn:
        for r in conn.execute(stmt).mappings():
            corr = r["human_correction_fields"] or {}
            if not isinstance(corr, dict):
                continue
            gate = r["gate_results"] or {}
            if not isinstance(gate, dict):
                gate = {}
            rows.append(
                CorrectedRow(
                    lineage_id=str(r["lineage_id"]),
                    job_id=str(r["job_id"]),
                    page_number=int(r["page_number"]),
                    material_type=str(r["material_type"]),
                    iep1a_used=bool(r["iep1a_used"]),
                    iep1b_used=bool(r["iep1b_used"]),
                    human_correction_timestamp=r["human_correction_timestamp"],
                    correction=corr,
                    gate_results=gate,
                )
            )
    return rows


def _matches_source_window(ts: datetime | None, source_window: str) -> bool:
    sw = source_window.strip().lower()
    if not sw or sw in {"all", "manual"}:
        return True
    if ts is None:
        return False
    if len(sw) == 7 and sw[4] == "w" and sw[:4].isdigit() and sw[5:].isdigit():
        target_year = int(sw[:4])
        target_week = int(sw[5:])
        y, w, _ = ts.isocalendar()
        return y == target_year and w == target_week
    return True


def _download_source_image(uri: str, out_path: Path, s3_client: Any | None) -> None:
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if scheme in {"", "file"}:
        src_path = Path(parsed.path if scheme == "file" else uri)
        if not src_path.is_file():
            raise FileNotFoundError(f"source image not found: {uri}")
        shutil.copy2(src_path, out_path)
        return
    if scheme == "s3":
        if s3_client is None:
            raise RuntimeError("S3 uri encountered but S3 client is not configured")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        s3_client.download_file(bucket, key, str(out_path))
        return
    raise RuntimeError(f"Unsupported source_artifact_uri scheme: {scheme}")


def _split_for_job(job_id: str, seed: str) -> str:
    digest = hashlib.sha1(f"{seed}:{job_id}".encode("utf-8")).hexdigest()
    return "val" if int(digest[:8], 16) % 5 == 0 else "train"


def _write_data_yaml(material_root: Path, name: str) -> Path:
    data = {
        "path": str(material_root),
        "train": "images/train",
        "val": "images/val",
        "names": {0: name},
        "nc": 1,
    }
    out = material_root / "data.yaml"
    out.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return out


def _run_corrected_export(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        raise SystemExit("DATABASE_URL is required for --mode corrected-export")

    min_iep1a_book = int(os.getenv("RETRAINING_MIN_CORRECTED_IEP1A_BOOK", "200"))
    min_iep1b_total = int(os.getenv("RETRAINING_MIN_CORRECTED_IEP1B_TOTAL", "200"))
    split_seed = os.getenv("RETRAINING_DATASET_SPLIT_SEED", "libraryai-corrected-v1")

    all_rows = _fetch_corrected_rows(db_url)
    rows = [r for r in all_rows if _matches_source_window(r.human_correction_timestamp, args.source_window)]

    count_iep1a_book = sum(1 for r in rows if r.iep1a_used and _material_bucket(r.material_type) == "book")
    count_iep1b_total = sum(1 for r in rows if r.iep1b_used and _material_bucket(r.material_type) in {"newspaper", "microfilm"})
    counts = {
        "iep1a_book": count_iep1a_book,
        "iep1b_total": count_iep1b_total,
        "rows_total": len(rows),
    }
    if count_iep1a_book < min_iep1a_book or count_iep1b_total < min_iep1b_total:
        return {
            "status": "min_samples_not_met",
            "counts": counts,
            "minimums": {
                "iep1a_book": min_iep1a_book,
                "iep1b_total": min_iep1b_total,
            },
        }

    if args.dataset_version:
        dataset_version = args.dataset_version
    else:
        now = datetime.now(timezone.utc)
        dataset_version = f"ds-hc-{now.strftime('%Y%m%d-%H%M%S')}"

    export_root = root / "training" / "preprocessing" / "corrected_export" / dataset_version
    manifest_path = export_root / "retraining_train_manifest.json"
    iep0_stub_root = export_root / "iep0_placeholder"
    iep0_stub_root.mkdir(parents=True, exist_ok=True)

    s3_client = None
    if os.getenv("S3_BUCKET_NAME", "").strip() or os.getenv("AWS_ACCESS_KEY_ID", "").strip() or os.getenv("S3_ACCESS_KEY_ID", "").strip():
        kwargs: dict[str, Any] = {}
        if os.getenv("S3_ENDPOINT_URL", "").strip():
            kwargs["endpoint_url"] = os.getenv("S3_ENDPOINT_URL", "").strip()
        if os.getenv("S3_REGION", "").strip():
            kwargs["region_name"] = os.getenv("S3_REGION", "").strip()
        access_key = os.getenv("S3_ACCESS_KEY_ID", "").strip() or os.getenv("AWS_ACCESS_KEY_ID", "").strip()
        secret_key = os.getenv("S3_SECRET_ACCESS_KEY", "").strip() or os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        s3_client = boto3.client("s3", **kwargs)

    family_material_roots: dict[tuple[str, str], Path] = {}
    for fam in ("iep1a", "iep1b"):
        materials = ("book", "newspaper", "microfilm")
        if fam == "iep1a":
            materials = ("book",)
        for mat in materials:
            base = export_root / fam / mat
            (base / "images" / "train").mkdir(parents=True, exist_ok=True)
            (base / "images" / "val").mkdir(parents=True, exist_ok=True)
            (base / "labels" / "train").mkdir(parents=True, exist_ok=True)
            (base / "labels" / "val").mkdir(parents=True, exist_ok=True)
            family_material_roots[(fam, mat)] = base

    exported_counts: dict[str, int] = {
        "iep1a_book": 0,
        "iep1b_newspaper": 0,
        "iep1b_microfilm": 0,
    }
    skipped_counts: dict[str, int] = {}
    for row in rows:
        src_uri = str(row.correction.get("source_artifact_uri", "")).strip()
        if not src_uri:
            skipped_counts["missing_source_artifact_uri"] = skipped_counts.get("missing_source_artifact_uri", 0) + 1
            continue
        try:
            width, height = _resolve_dimensions(row.correction, row.gate_results)
            corners_abs = _extract_corners_abs(row.correction)
            corners_abs = _canonicalize_corners(corners_abs)
            corners_norm = _to_norm(corners_abs, width, height)
            seg_line = _seg_line(corners_norm)
        except Exception:
            skipped_counts["invalid_geometry_or_dimensions"] = skipped_counts.get("invalid_geometry_or_dimensions", 0) + 1
            continue

        split = _split_for_job(row.job_id, split_seed)
        image_name = f"{row.job_id}_{row.page_number}_{row.lineage_id[:8]}"
        ext = Path(urlparse(src_uri).path).suffix or ".png"

        targets: list[tuple[str, str]] = []
        mat = _material_bucket(row.material_type)
        if row.iep1a_used and mat == "book":
            targets.append(("iep1a", "book"))
        if row.iep1b_used and mat in {"newspaper", "microfilm"}:
            targets.append(("iep1b", mat))
        if not targets:
            skipped_counts["unused_service_material"] = skipped_counts.get("unused_service_material", 0) + 1
            continue

        for fam, material in targets:
            base = family_material_roots[(fam, material)]
            image_out = base / "images" / split / f"{image_name}{ext}"
            label_out = base / "labels" / split / f"{image_name}.txt"
            try:
                _download_source_image(src_uri, image_out, s3_client)
            except Exception:
                skipped_counts["image_download_failed"] = skipped_counts.get("image_download_failed", 0) + 1
                continue
            label_out.write_text(seg_line + "\n", encoding="utf-8")
            exported_counts[f"{fam}_{material}"] = exported_counts.get(f"{fam}_{material}", 0) + 1

    iep1a_manifest = {"book": str(_write_data_yaml(family_material_roots[("iep1a", "book")], "page"))}
    iep1b_manifest = {
        "newspaper": str(_write_data_yaml(family_material_roots[("iep1b", "newspaper")], "page")),
        "microfilm": str(_write_data_yaml(family_material_roots[("iep1b", "microfilm")], "page")),
    }
    train_manifest = {
        "iep0": {"data_root": str(iep0_stub_root)},
        "iep1a": iep1a_manifest,
        "iep1b": iep1b_manifest,
    }
    manifest_path.write_text(json.dumps(train_manifest, indent=2), encoding="utf-8")

    dataset_checksum = _compute_manifest_checksum(train_manifest, root)
    registry_path = _resolve_path(args.registry_path, root)
    approved = args.approved or args.mode == "scheduled"
    _append_registry_entry(
        registry_path,
        dataset_version=dataset_version,
        dataset_checksum=dataset_checksum,
        manifest_path=manifest_path,
        mode="corrected_export",
        source_window=args.source_window,
        approved=approved,
    )

    return {
        "status": "ok",
        "dataset_version": dataset_version,
        "dataset_checksum": dataset_checksum,
        "manifest_path": str(manifest_path),
        "registry_path": str(registry_path),
        "build_mode": "rebuilt",
        "approved": approved,
        "counts": exported_counts,
        "skipped_counts": skipped_counts,
    }


def _load_registry(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "generated_at": "", "datasets": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("datasets"), list):
        payload["datasets"] = []
    return payload


def _append_registry_entry(
    registry_path: Path,
    *,
    dataset_version: str,
    dataset_checksum: str,
    manifest_path: Path,
    mode: str,
    source_window: str,
    approved: bool,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    payload = _load_registry(registry_path)
    payload["generated_at"] = now
    payload.setdefault("version", 1)
    payload["datasets"].append(
        {
            "dataset_version": dataset_version,
            "dataset_checksum": dataset_checksum,
            "manifest_path": str(manifest_path),
            "approved": approved,
            "build_mode": mode,
            "source_window": source_window,
            "created_at": now,
        }
    )
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("scheduled", "triggered", "corrected-export"),
        default="triggered",
    )
    parser.add_argument(
        "--manifest",
        default=os.getenv("RETRAINING_SOURCE_MANIFEST", "").strip()
        or os.getenv("RETRAINING_TRAIN_MANIFEST", "").strip(),
        help="Path to curated retraining_train_manifest.json to register",
    )
    parser.add_argument(
        "--registry-path",
        default=os.getenv("RETRAINING_DATASET_REGISTRY_PATH", "").strip()
        or "training/preprocessing/dataset_registry.json",
    )
    parser.add_argument(
        "--dataset-version",
        default=os.getenv("RETRAINING_DATASET_VERSION", "").strip(),
    )
    parser.add_argument(
        "--source-window",
        default=os.getenv("RETRAINING_SOURCE_WINDOW", "manual"),
        help="Human-readable window label for provenance, e.g. 2026W16",
    )
    parser.add_argument(
        "--approved",
        action="store_true",
        help="Mark entry approved (default true for scheduled mode, false for triggered)",
    )
    args = parser.parse_args()

    root = _repo_root()
    if args.mode == "corrected-export":
        print(json.dumps(_run_corrected_export(args, root)))
        return 0

    if not args.manifest:
        raise SystemExit(
            "Missing --manifest (or RETRAINING_SOURCE_MANIFEST / RETRAINING_TRAIN_MANIFEST)"
        )
    manifest_path = _resolve_path(args.manifest, root)
    if not manifest_path.is_file():
        raise SystemExit(f"Manifest file not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dataset_checksum = _compute_manifest_checksum(manifest, root)

    if args.dataset_version:
        dataset_version = args.dataset_version
    else:
        now = datetime.now(timezone.utc)
        dataset_version = f"ds-{now.strftime('%Y%m%d-%H%M%S')}"
    approved = args.approved or args.mode == "scheduled"

    registry_path = _resolve_path(args.registry_path, root)
    _append_registry_entry(
        registry_path,
        dataset_version=dataset_version,
        dataset_checksum=dataset_checksum,
        manifest_path=manifest_path,
        mode=args.mode,
        source_window=args.source_window,
        approved=approved,
    )

    print(
        json.dumps(
            {
                "dataset_version": dataset_version,
                "dataset_checksum": dataset_checksum,
                "manifest_path": str(manifest_path),
                "registry_path": str(registry_path),
                "build_mode": "rebuilt" if args.mode == "triggered" else "prebuilt",
                "approved": approved,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
