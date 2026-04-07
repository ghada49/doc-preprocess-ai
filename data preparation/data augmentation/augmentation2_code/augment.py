"""
==============================================================================
Augmentation 2 — Option B: Only 2 Rotations (0° and 90°)
==============================================================================

PURPOSE:
    Generate rotation-augmented images and labels for YOLOv8 segmentation
    training. Takes 63 original labeled images and produces 126 total
    (63 × 2 rotations: original + 90°).

STRATEGY:
    - Only 0° and 90° rotations (no 180° or 270°)
    - Fewer images but every label is perfectly correct
    - No class swapping: page_left stays page_left, page_right stays page_right
    - The model must learn to identify pages from content cues

WHY ONLY 2 ROTATIONS:
    - 180° rotation produces an image where left/right are mirrored — the
      coordinate transform is correct but the visual semantic is ambiguous
    - 90° is the safest rotation: the pages become top/bottom, clearly
      distinguishable from left/right
    - This is a conservative approach prioritizing label correctness over
      dataset size

COORDINATE TRANSFORM (clockwise 90°):
    (x, y) → (1-y, x)

    Coordinates are normalized [0, 1]. The transform maps each polygon
    vertex to its new position after rotating the image 90° clockwise.

PIL ROTATION NOTE:
    PIL's transpose uses counter-clockwise convention:
    - To rotate image 90° CW, use ROTATE_270 (270° CCW = 90° CW)

INPUT:
    - images/train/*.{tif,tiff,png,jpg,jpeg}  — 63 original images
    - labels/train/*.txt                       — 63 YOLO segmentation labels

OUTPUT:
    - images/train/*_rot90.{ext}  — 63 rotated images
    - labels/train/*_rot90.txt    — 63 rotated labels

LABEL FORMAT (YOLO segmentation):
    class_id x1 y1 x2 y2 ... xn yn
    - class_id: 0 = page_left, 1 = page_right
    - Coordinates are normalized polygon vertices

USAGE:
    python augment.py

    Run this BEFORE split_dataset.py. All files must be in images/train/
    and labels/train/ initially.
==============================================================================
"""
from pathlib import Path
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
IMAGES_DIR = BASE_DIR / "images" / "train"
LABELS_DIR = BASE_DIR / "labels" / "train"

IMAGE_EXTENSIONS = [".tif", ".tiff", ".png", ".jpg", ".jpeg"]

# 90° CW = 270° CCW in PIL's convention
PIL_ROTATION_90 = Image.Transpose.ROTATE_270


# ---------------------------------------------------------------------------
# Coordinate transformation
# ---------------------------------------------------------------------------
def transform_coords_90(coords):
    """
    Transform normalized polygon coordinates for 90° CW rotation.

    Args:
        coords: List of (x, y) tuples, normalized [0, 1]

    Returns:
        List of transformed (x, y) tuples: (x, y) → (1-y, x)
    """
    return [(1.0 - y, x) for x, y in coords]


# ---------------------------------------------------------------------------
# Label parsing
# ---------------------------------------------------------------------------
def parse_label_line(line):
    """
    Parse a single YOLO segmentation label line.

    Format: class_id x1 y1 x2 y2 ... xn yn

    Returns:
        Tuple of (class_id, [(x1,y1), (x2,y2), ...]) or None if invalid
    """
    parts = line.strip().split()
    if len(parts) < 5:
        return None
    class_id = int(parts[0])
    coords = [(float(parts[i]), float(parts[i + 1]))
              for i in range(1, len(parts) - 1, 2)]
    return class_id, coords


def format_label_line(class_id, coords):
    """Format a label line back to YOLO segmentation format."""
    coord_str = " ".join(f"{x:.6f} {y:.6f}" for x, y in coords)
    return f"{class_id} {coord_str}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Find original labels (exclude any previously generated _rot90)
    originals = sorted([
        f for f in LABELS_DIR.glob("*.txt")
        if not f.stem.endswith("_rot90")
    ])
    print(f"Found {len(originals)} original label files")

    created = 0
    for label_path in originals:
        stem = label_path.stem

        # Find corresponding image
        image_path = None
        for ext in IMAGE_EXTENSIONS:
            candidate = IMAGES_DIR / f"{stem}{ext}"
            if candidate.exists():
                image_path = candidate
                break

        new_stem = f"{stem}_rot90"

        # --- Augment label: rotate coordinates for 90°, no class swap ---
        lines = label_path.read_text().strip().splitlines()
        new_lines = []
        for line in lines:
            parsed = parse_label_line(line)
            if parsed is None:
                continue
            class_id, coords = parsed
            new_coords = transform_coords_90(coords)
            new_lines.append(format_label_line(class_id, new_coords))

        new_label = LABELS_DIR / f"{new_stem}.txt"
        new_label.write_text("\n".join(new_lines) + "\n")

        # --- Rotate image ---
        if image_path:
            new_img = IMAGES_DIR / f"{new_stem}{image_path.suffix}"
            if not new_img.exists():
                img = Image.open(image_path)
                rotated = img.transpose(PIL_ROTATION_90)
                rotated.save(new_img)
                rotated.close()
                img.close()

        created += 1
        print(f"  ✓ {stem}")

    total = len(originals) + created
    print(f"\nDone! Created {created} augmented samples.")
    print(f"Total: {total} images")


if __name__ == "__main__":
    main()
