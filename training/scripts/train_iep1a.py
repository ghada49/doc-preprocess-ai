#!/usr/bin/env python3
"""
Headless IEP1A (YOLOv8-seg) training — one material type at a time.

Usage:
  python training/scripts/train_iep1a.py --material book --data path/to/book/data.yaml
  python training/scripts/train_iep1a.py --material newspaper --data path/to/newspaper/data.yaml
  python training/scripts/train_iep1a.py --material microfilm --data path/to/microfilm/data.yaml

Output weights land in ``--project`` / ``--name`` / ``weights/best.pt``.

Per spec section 10.1: ``dataset_version`` and ``dataset_checksum`` are logged to MLflow.
A run that does not log these is invalid and must not promote a model.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

MATERIAL_CONFIGS = {
    "book": dict(
        model="yolov8s-seg.pt",
        imgsz=2048,
        epochs=200,
        batch=8,
        patience=40,
        mosaic=1.0,
        flipud=0.5,
        fliplr=0.5,
        degrees=10.0,
        scale=0.4,
        translate=0.2,
        dropout=0.15,
        weight_decay=0.001,
        workers=4,
    ),
    "microfilm": dict(
        model="yolov8n-seg.pt",
        imgsz=640,
        epochs=100,
        batch=16,
        patience=20,
        flipud=0.0,
        fliplr=0.5,
        mosaic=1.0,
        mixup=0.1,
        degrees=5.0,
        translate=0.1,
        scale=0.3,
        shear=2.0,
        workers=2,
    ),
    "newspaper": dict(
        model="yolov8s-seg.pt",
        imgsz=640,
        epochs=50,
        batch=8,
        patience=20,
        workers=2,
    ),
}

DATASET_VERSION = "aub_v1"


def _compute_dataset_checksum(data_yaml: Path) -> str:
    dataset_dir = data_yaml.parent
    h = hashlib.sha256()
    for f in sorted(dataset_dir.rglob("*")):
        if f.is_file():
            h.update(f.read_bytes())
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--material",
        required=True,
        choices=["book", "newspaper", "microfilm"],
        help="Material type to train",
    )
    parser.add_argument("--data", type=Path, required=True, help="Path to data.yaml")
    parser.add_argument("--device", default="0")
    parser.add_argument("--project", type=Path, default=Path("runs/train_iep1a"))
    parser.add_argument("--name", default=None, help="Run name (defaults to material type)")
    parser.add_argument("--dataset-version", default=DATASET_VERSION)
    parser.add_argument(
        "--epochs",
        type=int,
        default=None,
        help="Override epochs from material preset (e.g. smoke runs)",
    )
    parser.add_argument(
        "--pretrained",
        default=None,
        help=(
            "Path to existing weights (.pt) to fine-tune from instead of the COCO "
            "base model.  If the file is absent the script falls back to the base model "
            "and emits a warning."
        ),
    )
    args = parser.parse_args()

    if not args.data.is_file():
        raise SystemExit(f"data.yaml not found: {args.data}")

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

    cfg = MATERIAL_CONFIGS[args.material].copy()
    if args.epochs is not None:
        cfg["epochs"] = args.epochs
    run_name = args.name or args.material
    base_model = cfg.pop("model")

    # Resolve starting weights: prefer --pretrained if the file exists
    start_model = base_model
    pretrained_source = "base_model"
    if args.pretrained:
        pretrained_path = Path(args.pretrained)
        if pretrained_path.is_file():
            start_model = str(pretrained_path)
            pretrained_source = str(pretrained_path)
        else:
            print(
                f"WARNING: --pretrained {args.pretrained!r} not found; "
                f"falling back to base model {base_model!r}",
                flush=True,
            )

    print(
        f"Training IEP1A — material={args.material} "
        f"start_model={start_model} (source={pretrained_source})"
    )

    dataset_checksum = _compute_dataset_checksum(args.data)

    best_pt = args.project / run_name / "weights" / "best.pt"
    mlflow.set_experiment("libraryai_iep1a")
    with mlflow.start_run(run_name=run_name) as active_run:
        mlflow.log_param("dataset_version", args.dataset_version)
        mlflow.log_param("dataset_checksum", dataset_checksum)
        mlflow.log_param("material_type", args.material)
        mlflow.log_param("base_model", base_model)
        mlflow.log_param("pretrained_source", pretrained_source)
        mlflow.log_params(cfg)

        model = YOLO(start_model)
        model.train(
            data=str(args.data),
            project=str(args.project),
            name=run_name,
            device=args.device,
            exist_ok=True,
            **cfg,
        )

        best_pt = args.project / run_name / "weights" / "best.pt"
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
