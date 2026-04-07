"""
==============================================================================
Augmentation 1 — Option A: All 4 Rotations (0°, 90°, 180°, 270°)
==============================================================================

PURPOSE:
    Generate rotation-augmented images and labels for YOLOv8 segmentation
    training. Takes 63 original labeled images and produces 252 total
    (63 × 4 rotations).

STRATEGY:
    - No class swapping: page_left stays page_left, page_right stays page_right
    - Labels track physical page identity, not spatial position
    - The model must learn to identify pages from content cues, not position

COORDINATE TRANSFORMS (clockwise rotation):
    - 0°   (original): (x, y) → (x, y)
    - 90°:              (x, y) → (1-y, x)
    - 180°:             (x, y) → (1-x, 1-y)
    - 270°:             (x, y) → (y, 1-x)

    Coordinates are normalized [0, 1]. The transform maps each polygon
    vertex to its new position after rotating the image.

PIL ROTATION NOTE:
    PIL's transpose uses counter-clockwise convention internally:
    - To rotate image 90° CW, use ROTATE_270 (270° CCW = 90° CW)
    - To rotate image 270° CW, use ROTATE_90 (90° CCW = 270° CW)

INPUT:
    - images/train/*.{tif,tiff,png,jpg,jpeg}  — 63 original images
    - labels/train/*.txt                       — 63 YOLO segmentation labels

OUTPUT:
    - images/train/*_rot{90,180,270}.{ext}  — 189 rotated images
    - labels/train/*_rot{90,180,270}.txt    — 189 rotated labels

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

ROTATIONS = [90, 180, 270]
IMAGE_EXTENSIONS = [".tif", ".tiff", ".png", ".jpg", ".jpeg"]

# PIL transpose mapping: desired CW angle → PIL constant
PIL_ROTATION = {
    90:  Image.Transpose.ROTATE_270,   # 90° CW  = 270° CCW
    180: Image.Transpose.ROTATE_180,   # 180° CW = 180° CCW
    270: Image.Transpose.ROTATE_90,    # 270° CW = 90° CCW
}


# ---------------------------------------------------------------------------
# Coordinate transformation
# ---------------------------------------------------------------------------
def transform_coords(coords, angle):
    """
    Transform normalized polygon coordinates for a given CW rotation angle.

    Args:
        coords: List of (x, y) tuples, normalized [0, 1]
        angle:  Rotation angle in degrees (90, 180, or 270)

    Returns:
        List of transformed (x, y) tuples
    """
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
    # Find original labels (exclude any previously generated rotations)
    originals = sorted([
        f for f in LABELS_DIR.glob("*.txt")
        if not any(f.stem.endswith(f"_rot{a}") for a in ROTATIONS)
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

        for angle in ROTATIONS:
            new_stem = f"{stem}_rot{angle}"

            # --- Augment label: rotate coordinates, NO class swap ---
            lines = label_path.read_text().strip().splitlines()
            new_lines = []
            for line in lines:
                parsed = parse_label_line(line)
                if parsed is None:
                    continue
                class_id, coords = parsed
                new_coords = transform_coords(coords, angle)
                new_lines.append(format_label_line(class_id, new_coords))

            new_label = LABELS_DIR / f"{new_stem}.txt"
            new_label.write_text("\n".join(new_lines) + "\n")

            # --- Rotate image ---
            if image_path:
                new_img = IMAGES_DIR / f"{new_stem}{image_path.suffix}"
                if not new_img.exists():
                    img = Image.open(image_path)
                    rotated = img.transpose(PIL_ROTATION[angle])
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
