#!/usr/bin/env python3
"""
Rebuild ``golden_dataset/cases/iep1b_newspaper*.json`` annotations from YOLO
pose label files (no geometric corner inference).

Label line format (one detection per line, normalized 0–1)::

    class cx cy w h kx1 ky1 v kx2 ky2 v kx3 ky3 v kx4 ky4 v

The four keypoint pairs are taken **in file order** as::

    (kx1, ky1) = TL, (kx2, ky2) = TR, (kx3, ky3) = BR, (kx4, ky4) = BL

Bounding box is derived from the box head only::

    x1 = cx - w/2, y1 = cy - h/2, x2 = cx + w/2, y2 = cy + h/2

Filename vs case JSON
--------------------
For case_id ``iep1b_newspaper_<suffix>`` (e.g. ``na121_..._00008`` or
``..._rot90``), the label file must be named ``<suffix>.txt`` under
``--labels-dir``.

Examples::

    iep1b_newspaper_na121_00008_rot0.json
        → na121_00008_rot0.txt

    iep1b_newspaper_na246_0261_rot90.json
        → na246_0261_rot90.txt

Run from repo root::

    python training/scripts/regenerate_iep1b_newspaper_from_yolo_labels.py \\
        --labels-dir golden_dataset/yolo_labels/iep1b_newspaper

Use ``--dry-run`` to print matches without writing.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _round6(x: float) -> float:
    return round(float(x), 6)


def _xyxy_from_cxcywh(cx: float, cy: float, w: float, h: float) -> list[float]:
    x1 = cx - w / 2.0
    y1 = cy - h / 2.0
    x2 = cx + w / 2.0
    y2 = cy + h / 2.0
    return [_round6(x1), _round6(y1), _round6(x2), _round6(y2)]


def parse_yolo_pose_lines(text: str) -> list[dict[str, object]]:
    """
    Parse all non-empty lines into detections.
    Each line: class cx cy w h + 4 × (kx ky v).
    """
    detections: list[dict[str, object]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 17:
            raise ValueError(f"Expected ≥17 tokens, got {len(parts)}: {line!r}")
        cls = int(float(parts[0]))
        cx, cy, w, h = map(float, parts[1:5])
        kps: list[list[float]] = []
        idx = 5
        for _ in range(4):
            kx, ky = float(parts[idx]), float(parts[idx + 1])
            kps.append([_round6(kx), _round6(ky)])
            idx += 3
        detections.append(
            {
                "class_id": cls,
                "bbox_xyxy": _xyxy_from_cxcywh(cx, cy, w, h),
                "keypoints": kps,
            }
        )
    return detections


def case_suffix_from_case_id(case_id: str) -> str | None:
    prefix = "iep1b_newspaper_"
    if not case_id.startswith(prefix):
        return None
    return case_id[len(prefix) :]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labels-dir",
        type=Path,
        default=Path("golden_dataset/yolo_labels/iep1b_newspaper"),
        help="Directory of <suffix>.txt label files",
    )
    parser.add_argument(
        "--cases-dir",
        type=Path,
        default=Path("golden_dataset/cases"),
        help="Case JSON directory",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[2]
    labels_dir = args.labels_dir if args.labels_dir.is_absolute() else repo / args.labels_dir
    cases_dir = args.cases_dir if args.cases_dir.is_absolute() else repo / args.cases_dir

    if not labels_dir.is_dir():
        raise SystemExit(f"Labels directory not found: {labels_dir}\n" + __doc__)

    paths = sorted(cases_dir.glob("iep1b_newspaper*.json"))
    if not paths:
        raise SystemExit(f"No iep1b_newspaper*.json under {cases_dir}")

    updated = 0
    missing = 0
    for jpath in paths:
        data = json.loads(jpath.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("model") != "iep1b":
            continue
        if data.get("material_type") != "newspaper":
            continue
        cid = data.get("case_id")
        if not isinstance(cid, str):
            continue
        suffix = case_suffix_from_case_id(cid)
        if suffix is None:
            continue
        label_path = labels_dir / f"{suffix}.txt"
        if not label_path.is_file():
            print(f"missing label: {label_path.relative_to(repo) if label_path.is_relative_to(repo) else label_path}")
            missing += 1
            continue
        text = label_path.read_text(encoding="utf-8")
        detections = parse_yolo_pose_lines(text)
        if not detections:
            print(f"empty label (no detections): {label_path}")
            missing += 1
            continue

        n = len(detections)
        data["split_expected"] = n >= 2
        data["sub_page_count"] = n
        data["annotations"] = {"detections": detections}

        rel = jpath.relative_to(repo) if jpath.is_relative_to(repo) else jpath
        print(f"{'would update' if args.dry_run else 'update'}: {rel}  ←  {label_path.name}  ({n} det)")

        if not args.dry_run:
            jpath.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            updated += 1

    print(f"done. {'would write' if args.dry_run else 'wrote'} {updated} case(s); missing/empty labels: {missing}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
