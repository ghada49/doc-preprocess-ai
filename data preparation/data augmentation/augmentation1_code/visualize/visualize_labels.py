"""
Visualize images with their YOLO segmentation labels overlaid.
Shows a grid of random samples from the training set.
"""
import random
import sys
from pathlib import Path
from PIL import Image
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

BASE_DIR = Path(__file__).parent
IMAGES_DIR = BASE_DIR / "images" / "train"
LABELS_DIR = BASE_DIR / "labels" / "train"

CLASS_NAMES = {0: "page_left", 1: "page_right"}
CLASS_COLORS = {0: (0.2, 0.4, 1.0, 0.35), 1: (1.0, 0.2, 0.2, 0.35)}  # RGBA fill
EDGE_COLORS = {0: "blue", 1: "red"}

NUM_SAMPLES = 8  # how many images to show
COLS = 4
ROWS = (NUM_SAMPLES + COLS - 1) // COLS


def parse_label_file(label_path):
    """Parse a YOLO segmentation label file.
    Each line: class_id x1 y1 x2 y2 ... xn yn  (normalized)
    Returns list of (class_id, polygon_coords_normalized).
    """
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
            # group into (x, y) pairs
            points = np.array(coords).reshape(-1, 2)
            annotations.append((cls_id, points))
    return annotations


def draw_annotations(ax, annotations, img_w, img_h):
    """Draw polygon overlays on the axes."""
    for cls_id, points_norm in annotations:
        # Scale to pixel coordinates
        poly = points_norm.copy()
        poly[:, 0] *= img_w
        poly[:, 1] *= img_h

        # Fill polygon
        from matplotlib.patches import Polygon as MplPolygon
        patch = MplPolygon(poly, closed=True,
                           facecolor=CLASS_COLORS[cls_id],
                           edgecolor=EDGE_COLORS[cls_id],
                           linewidth=2)
        ax.add_patch(patch)

        # Label at centroid
        cx, cy = poly.mean(axis=0)
        ax.text(cx, cy, CLASS_NAMES[cls_id],
                color="white", fontsize=9, fontweight="bold",
                ha="center", va="center",
                bbox=dict(boxstyle="round,pad=0.3",
                          facecolor=EDGE_COLORS[cls_id], alpha=0.8))


def main():
    # Collect image-label pairs
    image_files = sorted(IMAGES_DIR.glob("*.tif"))
    if not image_files:
        print("No .tif images found in", IMAGES_DIR)
        sys.exit(1)

    # Sample randomly
    samples = random.sample(image_files, min(NUM_SAMPLES, len(image_files)))

    fig, axes = plt.subplots(ROWS, COLS, figsize=(5 * COLS, 5 * ROWS))
    if ROWS == 1:
        axes = [axes] if COLS == 1 else list(axes)
    else:
        axes = axes.flatten()

    for i, ax in enumerate(axes):
        if i >= len(samples):
            ax.axis("off")
            continue

        img_path = samples[i]
        label_path = LABELS_DIR / (img_path.stem + ".txt")

        img = Image.open(img_path)
        w, h = img.size
        ax.imshow(img)

        annotations = parse_label_file(label_path)
        draw_annotations(ax, annotations, w, h)

        # Title: short name + rotation info
        name = img_path.stem
        n_ann = len(annotations)
        classes_str = ", ".join(CLASS_NAMES[a[0]] for a in annotations)
        ax.set_title(f"{name}\n{classes_str}", fontsize=8)
        ax.axis("off")

    # Legend
    legend_patches = [
        mpatches.Patch(color=EDGE_COLORS[0], label=CLASS_NAMES[0]),
        mpatches.Patch(color=EDGE_COLORS[1], label=CLASS_NAMES[1]),
    ]
    fig.legend(handles=legend_patches, loc="lower center",
               ncol=2, fontsize=12, frameon=True)

    plt.suptitle("Dataset Samples with Segmentation Labels", fontsize=14, y=1.01)
    plt.tight_layout()
    plt.savefig(str(BASE_DIR / "label_visualization.png"), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Showed {len(samples)} samples. Saved to label_visualization.png")


if __name__ == "__main__":
    main()
