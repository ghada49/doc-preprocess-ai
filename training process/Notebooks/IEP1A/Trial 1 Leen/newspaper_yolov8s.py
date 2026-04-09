"""
YOLOv8s-seg training pipeline for Augmentation 2 newspaper data.

This script is Colab-friendly and mirrors the notebook flow:
1) rebuild train/val/test lists from existing images
2) write a Colab-local data yaml
3) convert non-RGB images to RGB
4) train YOLOv8s-seg
5) evaluate on test split

No checkpoint copy logic is included by design.
"""

from pathlib import Path
import subprocess

from PIL import Image


DATA_DIR = Path("/content/drive/MyDrive/augmentation2_code")
YAML_PATH = Path("/content/aug2_data.yaml")

RUN_NAME = "aug2_yolov8s2"
MODEL = "yolov8s-seg.pt"
EPOCHS = 50
BATCH = 8
IMGSZ = 640
DEVICE = "0"
CONF = 0.25

IMAGE_EXTS = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}


def run(command: str) -> None:
    """Run a shell command and stop on failure."""
    print(f"\n[RUN] {command}")
    subprocess.run(command, shell=True, check=True)


def rebuild_split_txt() -> None:
    """Rebuild train/val/test .txt files with absolute paths."""
    for split in ["train", "val", "test"]:
        img_dir = DATA_DIR / "images" / split
        files = sorted(
            p for p in img_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )
        (DATA_DIR / f"{split}.txt").write_text(
            "\n".join(str(p) for p in files) + ("\n" if files else "")
        )
    print("Rebuilt train/val/test txt files.")


def write_yaml() -> None:
    """Write a temporary Colab yaml for YOLO."""
    yaml_text = f"""path: {DATA_DIR}
train: train.txt
val: val.txt
test: test.txt
names:
  0: page_left
  1: page_right
"""
    YAML_PATH.write_text(yaml_text)
    print(f"Wrote {YAML_PATH}")


def convert_images_to_rgb() -> None:
    """Ensure all images are RGB to avoid mixed-channel mosaic errors."""
    converted = 0
    for split in ["train", "val", "test"]:
        for image_path in (DATA_DIR / "images" / split).iterdir():
            if image_path.suffix.lower() not in IMAGE_EXTS:
                continue
            with Image.open(image_path) as image:
                if image.mode != "RGB":
                    image.convert("RGB").save(image_path)
                    converted += 1
    print(f"Converted to RGB: {converted}")


def train() -> None:
    run(
        "yolo task=segment mode=train "
        f"model={MODEL} data={YAML_PATH} imgsz={IMGSZ} batch={BATCH} "
        f"epochs={EPOCHS} device={DEVICE} name={RUN_NAME}"
    )


def test() -> None:
    run(
        "yolo task=segment mode=val "
        f"model=/content/runs/segment/{RUN_NAME}/weights/best.pt "
        f"data={YAML_PATH} split=test device={DEVICE}"
    )


def predict_one(image_path: str) -> None:
    """Optional: run prediction on a single image."""
    run(
        "yolo task=segment mode=predict "
        f"model=/content/runs/segment/{RUN_NAME}/weights/best.pt "
        f"source=\"{image_path}\" conf={CONF} save=True device={DEVICE}"
    )


if __name__ == "__main__":
    rebuild_split_txt()
    write_yaml()
    convert_images_to_rgb()
    train()
    test()
