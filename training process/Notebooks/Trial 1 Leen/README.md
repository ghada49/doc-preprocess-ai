# Trial 1 Leen - Newspaper Segmentation (Augmentation 2)

This trial trains a YOLOv8 segmentation model to detect:
- `page_left` (class 0)
- `page_right` (class 1)

Dataset used: `augmentation2_code` in Google Drive.

## Files in this folder

- `Newspaper.ipynb` - Colab notebook version (interactive).
- `newspaper_yolov8s.py` - script version of the same flow.

## Colab setup

Install dependencies and mount Drive:

```python
!pip -q install ultralytics
from google.colab import drive
drive.mount('/content/drive')
```

## Dataset path

Default path expected by the script:

`/content/drive/MyDrive/augmentation2_code`

The dataset should include:
- `images/train`, `images/val`, `images/test`
- `labels/train`, `labels/val`, `labels/test`

## What the script does

1. Rebuilds `train.txt`, `val.txt`, `test.txt` from existing split images.
2. Writes `/content/aug2_data.yaml`.
3. Converts non-RGB images to RGB.
4. Trains `yolov8s-seg.pt` for 50 epochs.
5. Evaluates on `split=test` using `best.pt`.

## Run (script mode)

```bash
python "training process/Notebooks/Trial 1 Leen/newspaper_yolov8s.py"
```

In Colab, you can also run the same with:

```python
!python "/content/drive/MyDrive/doc-preprocess-ai/training process/Notebooks/Trial 1 Leen/newspaper_yolov8s.py"
```

## Core commands (manual notebook mode)

Train:

```bash
!yolo task=segment mode=train \
  model=yolov8s-seg.pt \
  data=/content/aug2_data.yaml \
  imgsz=640 batch=8 epochs=50 device=0 \
  name=aug2_yolov8s2
```

Test:

```bash
!yolo task=segment mode=val \
  model=/content/runs/segment/aug2_yolov8s2/weights/best.pt \
  data=/content/aug2_data.yaml \
  split=test device=0
```

Single image predict:

```bash
!yolo task=segment mode=predict \
  model=/content/runs/segment/aug2_yolov8s2/weights/best.pt \
  source="/content/drive/MyDrive/image1.png" \
  conf=0.25 save=True device=0
```

## Notes

- Keep model weights in Drive, not in Git.
- No forced `max_det` is used in this trial.
- No checkpoint-copy logic is embedded in the script by request.
