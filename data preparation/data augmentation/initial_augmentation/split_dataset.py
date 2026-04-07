"""
Split augmented dataset into train / val / test sets.

Key: all 4 rotations (original + rot90/180/270) of the same base image
stay in the SAME split to prevent data leakage.

Split ratio: ~70% train, ~15% val, ~15% test (by original image count).
63 originals → 44 train, 10 val, 9 test (× 4 rotations each = 176 / 40 / 36).
"""

import os
import random
import shutil
from pathlib import Path
from collections import defaultdict

# ── Configuration ──────────────────────────────────────────────────
BASE_DIR = Path(r"c:\Users\ayaae\OneDrive - American University of Beirut\Desktop\Segmentation data augmentation")
IMAGES_SRC = BASE_DIR / "images" / "train"
LABELS_SRC = BASE_DIR / "labels" / "train"

SPLITS = ["train", "val", "test"]
RATIOS = {"train": 0.70, "val": 0.15, "test": 0.15}
SEED = 42

ROTATIONS = ["", "_rot90", "_rot180", "_rot270"]


def get_base_stem(stem):
    """Strip rotation suffix to get the base image name."""
    for suffix in ["_rot90", "_rot180", "_rot270"]:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def main():
    random.seed(SEED)

    # Discover all label files and group by base stem
    all_labels = sorted(LABELS_SRC.glob("*.txt"))
    groups = defaultdict(list)
    for lp in all_labels:
        base = get_base_stem(lp.stem)
        groups[base].append(lp.stem)

    base_stems = sorted(groups.keys())
    random.shuffle(base_stems)

    n = len(base_stems)
    n_train = round(n * RATIOS["train"])
    n_val = round(n * RATIOS["val"])
    # rest goes to test

    split_assignment = {}
    for i, stem in enumerate(base_stems):
        if i < n_train:
            split_assignment[stem] = "train"
        elif i < n_train + n_val:
            split_assignment[stem] = "val"
        else:
            split_assignment[stem] = "test"

    # Create output directories
    for split in SPLITS:
        (BASE_DIR / "images" / split).mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "labels" / split).mkdir(parents=True, exist_ok=True)

    # Move/copy files and build split .txt files
    split_entries = {s: [] for s in SPLITS}
    counts = {s: 0 for s in SPLITS}

    for base, variants in sorted(groups.items()):
        split = split_assignment[base]
        for stem in variants:
            # Find image
            img_path = None
            for ext in [".tif", ".tiff", ".png", ".jpg"]:
                candidate = IMAGES_SRC / f"{stem}{ext}"
                if candidate.exists():
                    img_path = candidate
                    break

            label_path = LABELS_SRC / f"{stem}.txt"
            dest_split = split if split != "train" else "train"

            if split != "train":
                # Move to val/test directories
                if img_path and img_path.exists():
                    dest_img = BASE_DIR / "images" / split / img_path.name
                    if not dest_img.exists():
                        shutil.move(str(img_path), str(dest_img))
                if label_path.exists():
                    dest_lbl = BASE_DIR / "labels" / split / label_path.name
                    if not dest_lbl.exists():
                        shutil.move(str(label_path), str(dest_lbl))

            # Build path entry
            ext_str = img_path.suffix if img_path else ".tif"
            split_entries[split].append(f"data/images/{split}/{stem}{ext_str}")
            counts[split] += 1

    # For train, update paths (they stay in images/train)
    # Write split text files
    for split in SPLITS:
        txt_path = BASE_DIR / f"{split}.txt"
        split_entries[split].sort()
        txt_path.write_text("\n".join(split_entries[split]) + "\n")

    # Print summary
    print("Dataset split complete!")
    print(f"  Originals: {n} base images")
    for s in SPLITS:
        base_count = sum(1 for b in base_stems if split_assignment[b] == s)
        print(f"  {s:5s}: {base_count} originals × 4 = {counts[s]} total samples")
    print(f"\nFiles written: train.txt, val.txt, test.txt")


if __name__ == "__main__":
    main()
