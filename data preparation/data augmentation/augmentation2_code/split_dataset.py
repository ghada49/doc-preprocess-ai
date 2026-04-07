"""
==============================================================================
Split Dataset — For Augmentation 2 (2 rotations only: 0° and 90°)
==============================================================================

PURPOSE:
    Split the augmented dataset (126 images) into train/val/test sets.
    Ensures both rotations of the same base image stay in the same split
    to prevent data leakage.

DATA LEAKAGE PREVENTION:
    If image_001 and image_001_rot90 end up in different splits,
    the model effectively "sees" the test image during training.
    This script groups both rotations by base stem and splits at
    the base-image level.

SPLIT RATIOS:
    - Train: 70% (~44 base images × 2 = ~88 images)
    - Val:   15% (~9 base images × 2 = ~18 images)
    - Test:  15% (~10 base images × 2 = ~20 images)

SEED:
    42 (deterministic, reproducible splits)

ROTATION SUFFIXES (Augmentation 2):
    "", "_rot90"   (only 2, not 4)

INPUT (after running augment.py):
    - images/train/  — all 126 images
    - labels/train/  — all 126 labels

OUTPUT:
    - images/{train,val,test}/  — split image folders
    - labels/{train,val,test}/  — split label folders
    - train.txt, val.txt, test.txt — file lists for YOLO data.yaml

USAGE:
    python split_dataset.py

    Run this AFTER augment.py.
==============================================================================
"""
import random
import shutil
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
IMAGES_DIR = BASE_DIR / "images" / "train"
LABELS_DIR = BASE_DIR / "labels" / "train"

SEED = 42
TRAIN_RATIO, VAL_RATIO, TEST_RATIO = 0.70, 0.15, 0.15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_base_stems(labels_dir):
    """
    Get unique base stems (without _rot90 suffix).
    e.g., "image_001_rot90" → "image_001"
    """
    stems = set()
    for f in labels_dir.glob("*.txt"):
        s = f.stem
        if s.endswith("_rot90"):
            s = s[:-len("_rot90")]
        stems.add(s)
    return sorted(stems)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    base_stems = get_base_stems(LABELS_DIR)
    n = len(base_stems)
    print(f"Found {n} base images")

    # Deterministic shuffle
    random.seed(SEED)
    random.shuffle(base_stems)

    # Compute split sizes
    n_train = round(n * TRAIN_RATIO)
    n_val = round(n * VAL_RATIO)

    train_stems = base_stems[:n_train]
    val_stems = base_stems[n_train:n_train + n_val]
    test_stems = base_stems[n_train + n_val:]

    print(f"Split: {len(train_stems)} train / {len(val_stems)} val / {len(test_stems)} test")

    # Only 2 rotation suffixes for Augmentation 2
    suffixes = ["", "_rot90"]

    for split_name, stems in [("train", train_stems), ("val", val_stems), ("test", test_stems)]:
        img_dir = BASE_DIR / "images" / split_name
        lbl_dir = BASE_DIR / "labels" / split_name

        if split_name != "train":
            img_dir.mkdir(parents=True, exist_ok=True)
            lbl_dir.mkdir(parents=True, exist_ok=True)

        moved = 0
        for stem in stems:
            for suffix in suffixes:
                full_stem = f"{stem}{suffix}"

                # Move image
                for ext in [".tif", ".tiff", ".png", ".jpg"]:
                    src_img = IMAGES_DIR / f"{full_stem}{ext}"
                    if src_img.exists():
                        if split_name != "train":
                            shutil.move(str(src_img), str(img_dir / src_img.name))
                        moved += 1
                        break

                # Move label
                src_lbl = LABELS_DIR / f"{full_stem}.txt"
                if src_lbl.exists() and split_name != "train":
                    shutil.move(str(src_lbl), str(lbl_dir / src_lbl.name))

        print(f"  {split_name}: {moved} files")

    # Generate .txt files listing image paths for each split
    for split_name in ["train", "val", "test"]:
        img_dir = BASE_DIR / "images" / split_name
        entries = []
        for f in sorted(img_dir.iterdir()):
            if f.suffix.lower() in [".tif", ".tiff", ".png", ".jpg", ".jpeg"]:
                entries.append(f"images/{split_name}/{f.name}")
        txt_path = BASE_DIR / f"{split_name}.txt"
        txt_path.write_text("\n".join(entries) + "\n")
        print(f"  {split_name}.txt: {len(entries)} entries")


if __name__ == "__main__":
    main()
