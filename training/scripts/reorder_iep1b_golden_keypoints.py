#!/usr/bin/env python3
"""
DEPRECATED for newspaper: use ``regenerate_iep1b_newspaper_from_yolo_labels.py``
with CVAT/YOLO label files (keypoint order = file order: TL, TR, BR, BL).

This script still uses geometry (min-x / max-x pairs) and can disagree with
exported label order. Prefer label-driven regeneration when .txt exports exist.

---

Reorder IEP1B golden case keypoints to [TL, TR, BR, BL] using geometry:

  TL — smallest (x + y)
  TR — among the two points with largest x, the one with smallest y
  BR — largest (x + y)
  BL — among the two points with smallest x, the one with largest y

Pairs (min-x edge, max-x edge) are taken as the two smallest / two largest x
coordinates so TR/BR (and TL/BL) stay consistent when x values differ slightly.

Run from repo root:
  python training/scripts/reorder_iep1b_golden_keypoints.py
"""

from __future__ import annotations

import json
from pathlib import Path


def _round_pt(p: tuple[float, float]) -> list[float]:
    return [round(float(p[0]), 6), round(float(p[1]), 6)]


def reorder_keypoints_tl_tr_br_bl(kps: object) -> list[list[float]] | object:
    if not isinstance(kps, list) or len(kps) != 4:
        return kps
    pts: list[tuple[float, float]] = []
    for p in kps:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            return kps
        pts.append((float(p[0]), float(p[1])))
    if len({(round(a, 5), round(b, 5)) for a, b in pts}) < 4:
        return kps

    min_pair = sorted(pts, key=lambda p: (p[0], p[1]))[:2]
    max_pair = sorted(pts, key=lambda p: (-p[0], p[1]))[:2]

    tl = min(min_pair, key=lambda p: p[0] + p[1])
    bl = max(min_pair, key=lambda p: p[1])
    tr = min(max_pair, key=lambda p: p[1])
    br = max(max_pair, key=lambda p: p[0] + p[1])

    ordered = (tl, tr, br, bl)
    if len(set(ordered)) < 4:
        tl2 = min(pts, key=lambda p: p[0] + p[1])
        br2 = max(pts, key=lambda p: p[0] + p[1])
        rem = [p for p in pts if p not in (tl2, br2)]
        if len(rem) != 2:
            return kps
        a, b = rem[0], rem[1]
        tr2, bl2 = (a, b) if a[0] >= b[0] else (b, a)
        ordered = (tl2, tr2, br2, bl2)

    return [_round_pt(p) for p in ordered]


def process_case(data: dict) -> bool:
    changed = False
    anns = data.get("annotations")
    if not isinstance(anns, dict):
        return False
    dets = anns.get("detections")
    if not isinstance(dets, list):
        return False
    for det in dets:
        if not isinstance(det, dict):
            continue
        kps = det.get("keypoints")
        new_kps = reorder_keypoints_tl_tr_br_bl(kps)
        if new_kps is kps or not isinstance(new_kps, list):
            continue
        old = json.dumps(kps, sort_keys=True)
        new = json.dumps(new_kps, sort_keys=True)
        if old != new:
            changed = True
        det["keypoints"] = new_kps
    return changed


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    cases_dir = repo / "golden_dataset" / "cases"
    paths = sorted(cases_dir.glob("iep1b*.json"))
    if not paths:
        print(f"No iep1b*.json under {cases_dir}")
        return 1
    updated = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("model") != "iep1b":
            continue
        if process_case(data):
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            updated += 1
            print(f"updated: {path.relative_to(repo)}")
    print(f"done. files touched: {updated} / {len(paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
