"""
IEP1B newspaper keypoint training (YOLOv8-pose).

Clean script version of Colab notebook flow:
1) mount Drive
2) prepare data yaml
3) convert non-RGB images to RGB
4) train YOLOv8-pose
5) evaluate best model on val and test
"""

from pathlib import Path

import yaml
from PIL import Image
from ultralytics import YOLO

try:
    from google.colab import drive
except ImportError as exc:
    raise RuntimeError("This script is intended for Colab.") from exc


# ---------- Config ----------
DATA_ROOT = Path("/content/drive/MyDrive/augm_keypoints")
YAML_PATH = Path("/content/iep1b_data.yaml")

MODEL_SIZE = "m"
IMGSZ = 1024
EPOCHS = 100
BATCH = 4
PATIENCE = 20

RUN_PROJECT = "/content/drive/MyDrive/yolo_runs/iep1b_keypoints"
RUN_NAME = f"pose_{MODEL_SIZE}_imgsz{IMGSZ}_e{EPOCHS}"

IMAGE_EXTS = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}


def convert_images_to_rgb() -> None:
    """Ensure all dataset images are RGB (avoids mixed-mode training issues)."""
    converted = 0
    for split in ["train", "val", "test"]:
        img_dir = DATA_ROOT / "images" / split
        if not img_dir.exists():
            continue
        for image_path in img_dir.iterdir():
            if image_path.suffix.lower() not in IMAGE_EXTS:
                continue
            with Image.open(image_path) as image:
                if image.mode != "RGB":
                    image.convert("RGB").save(image_path)
                    converted += 1
    print(f"Converted to RGB: {converted}")


def write_data_yaml() -> None:
    """Write a Colab-local YAML that points to the Drive dataset."""
    data_yaml = {
        "path": str(DATA_ROOT),
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": {0: "page_left", 1: "page_right"},
        "kpt_shape": [4, 3],
    }
    YAML_PATH.write_text(yaml.dump(data_yaml, default_flow_style=False), encoding="utf-8")
    print(f"Wrote {YAML_PATH}")
    print(YAML_PATH.read_text(encoding="utf-8"))


def main() -> None:
    drive.mount("/content/drive")
    write_data_yaml()
    convert_images_to_rgb()

    model = YOLO(f"yolov8{MODEL_SIZE}-pose.pt")
    model.train(
        data=str(YAML_PATH),
        epochs=EPOCHS,
        imgsz=IMGSZ,
        batch=BATCH,
        patience=PATIENCE,
        project=RUN_PROJECT,
        name=RUN_NAME,
        exist_ok=True,
        hsv_h=0.01,
        hsv_s=0.3,
        hsv_v=0.3,
        degrees=5.0,
        translate=0.05,
        scale=0.15,
        flipud=0.0,
        fliplr=0.0,
        mosaic=0.0,
        mixup=0.0,
        copy_paste=0.0,
        optimizer="AdamW",
        lr0=0.001,
        lrf=0.01,
        weight_decay=0.0005,
        warmup_epochs=5,
        workers=2,
        device=0,
        verbose=True,
    )

    best_model = YOLO(f"{RUN_PROJECT}/{RUN_NAME}/weights/best.pt")
    metrics_val = best_model.val(data=str(YAML_PATH), imgsz=IMGSZ, batch=BATCH, device=0)
    metrics_test = best_model.val(data=str(YAML_PATH), split="test", imgsz=IMGSZ, batch=BATCH, device=0)

    print("val  pose mAP50:", metrics_val.pose.map50)
    print("test pose mAP50:", metrics_test.pose.map50)


if __name__ == "__main__":
    main()