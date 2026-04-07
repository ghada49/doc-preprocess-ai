"""
Visualize all 4 rotations of selected base images with their labels.
Shows 0°, 90°, 180°, 270° side by side for each base image.
"""
import random
from pathlib import Path
from PIL import Image
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Polygon as MplPolygon
import numpy as np

BASE_DIR = Path(__file__).parent
IMAGES_DIR = BASE_DIR / "images" / "train"
LABELS_DIR = BASE_DIR / "labels" / "train"

CLASS_NAMES = {0: "page_left", 1: "page_right"}
CLASS_COLORS = {0: (0.2, 0.4, 1.0, 0.35), 1: (1.0, 0.2, 0.2, 0.35)}
EDGE_COLORS = {0: "blue", 1: "red"}

NUM_BASE = 1  # how many base images to show
ROTATIONS = ["", "_rot90", "_rot180", "_rot270"]
ROT_LABELS = ["0°", "90°", "180°", "270°"]


def parse_label_file(label_path):
    annotations = []
    if not label_path.exists():
        return annotations
    with open(label_path) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 5:
                continue
            cls_id = int(parts[0])
            coords = list(map(float, parts[1:]))
            points = np.array(coords).reshape(-1, 2)
            annotations.append((cls_id, points))
    return annotations


def main():
    # Find base (non-rotated) images
    all_images = sorted(IMAGES_DIR.glob("*.tif"))
    base_images = [p for p in all_images if not any(p.stem.endswith(f"_rot{a}") for a in [90, 180, 270])]
    samples = random.sample(base_images, min(NUM_BASE, len(base_images)))

    fig, axes = plt.subplots(NUM_BASE, 4, figsize=(24, 6 * NUM_BASE))
    if NUM_BASE == 1:
        axes = axes.reshape(1, -1)

    for row, base_path in enumerate(samples):
        stem = base_path.stem
        ext = base_path.suffix

        for col, (rot_suffix, rot_label) in enumerate(zip(ROTATIONS, ROT_LABELS)):
            ax = axes[row, col]
            img_name = f"{stem}{rot_suffix}{ext}"
            lbl_name = f"{stem}{rot_suffix}.txt"
            img_path = IMAGES_DIR / img_name
            lbl_path = LABELS_DIR / lbl_name

            if not img_path.exists():
                ax.set_title(f"{rot_label} — missing", fontsize=10)
                ax.axis("off")
                continue

            img = Image.open(img_path)
            w, h = img.size
            ax.imshow(img)

            annotations = parse_label_file(lbl_path)
            for cls_id, points_norm in annotations:
                poly = points_norm.copy()
                poly[:, 0] *= w
                poly[:, 1] *= h
                patch = MplPolygon(poly, closed=True,
                                   facecolor=CLASS_COLORS[cls_id],
                                   edgecolor=EDGE_COLORS[cls_id],
                                   linewidth=2)
                ax.add_patch(patch)
                cx, cy = poly.mean(axis=0)
                ax.text(cx, cy, CLASS_NAMES[cls_id],
                        color="white", fontsize=8, fontweight="bold",
                        ha="center", va="center",
                        bbox=dict(boxstyle="round,pad=0.2",
                                  facecolor=EDGE_COLORS[cls_id], alpha=0.8))

            # Show which label group
            if rot_suffix in ["", "_rot180"]:
                group = "label: 0°/180° pair"
            else:
                group = "label: 90°/270° pair"
            ax.set_title(f"{stem[-6:]}{rot_suffix}  ({rot_label})\n{group}", fontsize=9)
            ax.axis("off")

    legend_patches = [
        mpatches.Patch(color="blue", label="page_left (0)"),
        mpatches.Patch(color="red", label="page_right (1)"),
    ]
    fig.legend(handles=legend_patches, loc="lower center", ncol=2, fontsize=12)
    plt.suptitle("All 4 Rotations — Labels paired: 0°=180°, 90°=270°", fontsize=14, y=1.01)
    plt.tight_layout()
    out = BASE_DIR / "label_verification_paired.png"
    plt.savefig(str(out), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
