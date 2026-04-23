#!/usr/bin/env python3
"""
Headless IEP0 (YOLOv8s-cls) document type classifier training.

Colab reference: classifier.ipynb (doc_type_cls_rot_l4)

Dataset root must have train/val/test subdirs each containing:
  book/, newspaper/, microfilm/

Example:
  python training/scripts/train_iep0.py --data /path/to/classifier

Per spec section 10.1: dataset_version + dataset_checksum logged to MLflow.
A run that does not log these is invalid and must not promote a model.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

DATASET_VERSION = "aub_v1"


def _compute_dataset_checksum(data_root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(data_root.rglob("*")):
        if f.is_file():
            h.update(f.read_bytes())
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        type=Path,
        required=True,
        help="ImageFolder root with train/val/test subdirs",
    )
    parser.add_argument("--model", default="yolov8s-cls.pt")
    parser.add_argument("--imgsz", type=int, default=384)
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--patience", type=int, default=20)
    parser.add_argument("--device", default="0")
    parser.add_argument("--project", type=Path, default=Path("runs/train_iep0"))
    parser.add_argument("--name", default="doc_type_cls")
    parser.add_argument("--dataset-version", default=DATASET_VERSION)
    args = parser.parse_args()

    if not args.data.is_dir():
        raise SystemExit(f"Dataset root not found: {args.data}")

    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit(
            "Ultralytics import failed. Usually this means ultralytics is not installed, "
            "or OpenCV native deps are missing in the container image.\n"
            f"ImportError: {exc}"
        ) from exc

    try:
        import mlflow
    except ImportError as exc:
        raise SystemExit("Install mlflow: pip install mlflow") from exc

    dataset_checksum = _compute_dataset_checksum(args.data)

    mlflow.set_experiment("libraryai_iep0")
    best_pt = args.project / args.name / "weights" / "best.pt"
    with mlflow.start_run(run_name=args.name) as active_run:
        mlflow.log_param("dataset_version", args.dataset_version)
        mlflow.log_param("dataset_checksum", dataset_checksum)
        mlflow.log_param("base_model", args.model)
        mlflow.log_param("imgsz", args.imgsz)
        mlflow.log_param("epochs", args.epochs)
        mlflow.log_param("batch", args.batch)
        mlflow.log_param("patience", args.patience)

        model = YOLO(args.model)
        model.train(
            data=str(args.data),
            imgsz=args.imgsz,
            batch=args.batch,
            epochs=args.epochs,
            patience=args.patience,
            project=str(args.project),
            name=args.name,
            device=args.device,
            exist_ok=True,
        )

        best_pt = args.project / args.name / "weights" / "best.pt"
        if best_pt.exists():
            mlflow.log_artifact(str(best_pt), artifact_path="weights")
            mlflow.log_param("best_weights", str(best_pt))
        print(f"LIBRARYAI_MLFLOW_RUN_ID={active_run.info.run_id}", flush=True)

    if best_pt.exists():
        print(f"LIBRARYAI_BEST_WEIGHTS={best_pt.resolve()}", flush=True)
    print(f"Done. Best weights: {best_pt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
