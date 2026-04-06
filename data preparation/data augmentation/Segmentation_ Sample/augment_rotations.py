"""
Rotation augmentation for YOLOv8-seg page segmentation dataset.

Applies 90°, 180°, 270° CW rotations to images.

Label strategy (paired labels):
  - 0° and 180° share the SAME original label (no coord transform)
  - 90° and 270° share the SAME 90°-rotated label
  - No class swapping for any rotation

Only coordinates for 90° are computed:
  - 90°:  (x, y) -> (1-y, x)
  - 180°: copy of 0° label as-is
  - 270°: copy of 90° label as-is
"""

import os
import sys
from pathlib import Path
from PIL import Image

# ── Configuration ──────────────────────────────────────────────────
BASE_DIR = Path(r"c:\Users\ayaae\OneDrive - American University of Beirut\Desktop\Segmentation data augmentation")
IMAGES_DIR = BASE_DIR / "images" / "train"
LABELS_DIR = BASE_DIR / "labels" / "train"
TRAIN_TXT  = BASE_DIR / "train.txt"

ROTATIONS = [90, 180, 270]
IMAGE_EXTENSIONS = [".tif", ".tiff", ".png", ".jpg", ".jpeg"]


# ── Coordinate transforms ─────────────────────────────────────────
def transform_coords(coords, angle):
    """Transform normalized polygon coordinates for a CW rotation."""
    out = []
    for x, y in coords:
        if angle == 90:
            nx, ny = 1.0 - y, x
        elif angle == 180:
            nx, ny = 1.0 - x, 1.0 - y
        elif angle == 270:
            nx, ny = y, 1.0 - x
        else:
            nx, ny = x, y
        out.append((nx, ny))
    return out


# ── Label I/O ──────────────────────────────────────────────────────
def parse_label_line(line):
    """Parse: class_id x1 y1 x2 y2 ... xn yn"""
    parts = line.strip().split()
    if len(parts) < 5:
        return None
    class_id = int(parts[0])
    coords = [(float(parts[i]), float(parts[i + 1]))
              for i in range(1, len(parts) - 1, 2)]
    return class_id, coords


def format_label_line(class_id, coords):
    coord_str = " ".join(f"{x:.6f} {y:.6f}" for x, y in coords)
    return f"{class_id} {coord_str}"


def augment_label_file(label_path, angle):
    """Return augmented label text for the given rotation angle.
    
    Paired strategy:
      - 90°: transform coordinates (1-y, x)
      - 180°: copy original label as-is
      - 270°: copy 90° label (computed from original with 90° transform)
    No class swapping for any rotation.
    """
    lines = label_path.read_text().strip().splitlines()
    new_lines = []
    for line in lines:
        parsed = parse_label_line(line)
        if parsed is None:
            continue
        class_id, coords = parsed

        if angle == 180:
            # 180° gets the original label — no coord transform
            new_coords = coords
        elif angle in (90, 270):
            # Both 90° and 270° get the 90°-rotated coords
            new_coords = transform_coords(coords, 90)
        else:
            new_coords = coords

        # No class swapping
        new_lines.append(format_label_line(class_id, new_coords))

    return "\n".join(new_lines) + "\n" if new_lines else ""


# ── Image rotation ─────────────────────────────────────────────────
PIL_ROTATION = {
    90:  Image.Transpose.ROTATE_270,   # 270° CCW = 90° CW
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,    # 90° CCW = 270° CW
}


def rotate_image(image_path, angle):
    img = Image.open(image_path)
    rotated = img.transpose(PIL_ROTATION[angle])
    img.close()
    return rotated


# ── Main ───────────────────────────────────────────────────────────
def main():
    label_files = sorted(LABELS_DIR.glob("*.txt"))
    print(f"Found {len(label_files)} label files")

    new_train_entries = []
    augmented = 0
    skipped = 0

    for label_path in label_files:
        stem = label_path.stem

        # Skip files that are already augmented (re-run safety)
        if any(stem.endswith(f"_rot{a}") for a in ROTATIONS):
            continue

        # Find corresponding image
        image_path = None
        for ext in IMAGE_EXTENSIONS:
            candidate = IMAGES_DIR / f"{stem}{ext}"
            if candidate.exists():
                image_path = candidate
                break

        for angle in ROTATIONS:
            new_stem = f"{stem}_rot{angle}"

            # Skip if already exists (idempotent)
            new_label_path = LABELS_DIR / f"{new_stem}.txt"
            if new_label_path.exists():
                skipped += 1
                continue

            # Augment labels
            new_label_content = augment_label_file(label_path, angle)
            new_label_path.write_text(new_label_content)

            # Rotate image
            if image_path:
                new_image_path = IMAGES_DIR / f"{new_stem}{image_path.suffix}"
                if not new_image_path.exists():
                    try:
                        rotated = rotate_image(image_path, angle)
                        rotated.save(new_image_path)
                        rotated.close()
                    except Exception as e:
                        print(f"  WARNING: Could not rotate image {stem}: {e}")
                new_train_entries.append(
                    f"data/images/train/{new_stem}{image_path.suffix}"
                )
            else:
                new_train_entries.append(f"data/images/train/{new_stem}.tif")

            augmented += 1

        if image_path:
            print(f"  ✓ {stem} (image + labels)")
        else:
            print(f"  ✓ {stem} (labels only)")

    # Append to train.txt
    existing = TRAIN_TXT.read_text().strip().splitlines() if TRAIN_TXT.exists() else []
    # Avoid duplicates
    existing_set = set(existing)
    to_add = [e for e in new_train_entries if e not in existing_set]
    all_entries = existing + to_add
    TRAIN_TXT.write_text("\n".join(all_entries) + "\n")

    print(f"\nDone!")
    print(f"  New augmented samples: {augmented}")
    print(f"  Skipped (already exist): {skipped}")
    print(f"  Total train.txt entries: {len(all_entries)}")


if __name__ == "__main__":
    main()
