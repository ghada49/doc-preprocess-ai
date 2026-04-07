# LibraryAI Page Segmentation — Data Preparation

YOLOv8s-seg dataset for detecting `page_left` (class 0) and `page_right` (class 1) in scanned document images.

## Dataset

- **63 base images** (OTIFF scans from collection `aub_aco003575`)
- **252 total** after augmentation (4 rotations: 0°, 90°, 180°, 270°)
- **Split:** 176 train / 36 val / 40 test (seed 42, no data leakage)
- **Label format:** YOLO segmentation — `class_id x1 y1 x2 y2 ... xn yn` (normalized polygons)

## Label Strategy

Labels are **paired** across rotations — no class swapping:

| Rotation | Label source |
|----------|-------------|
| 0° (original) | Original label |
| 180° | **Copy of 0° label** (no coord transform) |
| 90° | 90°-rotated coordinates |
| 270° | **Copy of 90° label** (no coord transform) |

The model learns to identify `page_left` and `page_right` from content cues, not spatial position.

## Setup from Scratch

You need the 63 original images in `images/train/` and 63 original labels in `labels/train/`.

```bash
# 1. Generate rotated images + paired labels (63 → 252)
python augment_rotations.py

# 2. Split into train/val/test folders
python split_dataset.py

# 3. Generate split .txt files for YOLO
python regen_splits.py
```

## Verify

```bash
# Visualize all 4 rotations of a random image with label overlays
python visualize_paired.py
# → saves label_verification_paired.png
```

## Train (Google Colab)

1. Zip this entire folder
2. Upload to Google Drive
3. Open `train_yolov8s_seg.ipynb` in Colab
4. Update `ZIP_PATH` to your zip location
5. Run all cells (A100 recommended: imgsz=2048, batch=8)

## Files

| File | Purpose |
|------|---------|
| `augment_rotations.py` | Rotation augmentation with paired labels |
| `split_dataset.py` | Train/val/test split (seed 42) |
| `regen_splits.py` | Regenerate .txt split files |
| `data.yaml` | YOLO dataset config |
| `postprocess_pages.py` | Post-processing validation for inference |
| `visualize_paired.py` | Verify labels visually |
| `train_yolov8s_seg.ipynb` | Colab training notebook |
