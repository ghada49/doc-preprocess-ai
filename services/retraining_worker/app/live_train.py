"""
Optional live training for preprocessing retraining jobs.

Set ``LIBRARYAI_RETRAINING_TRAIN=live`` and provide dataset paths via
``RETRAINING_TRAIN_MANIFEST`` (JSON file) or the per-path env vars documented
in ``.env.example``. Trained ``best.pt`` files are optionally uploaded to S3
when ``S3_BUCKET_NAME`` is set.

Stdout markers from ``training/scripts/train_iep*.py``:

- ``LIBRARYAI_MLFLOW_RUN_ID=…``
- ``LIBRARYAI_BEST_WEIGHTS=…`` (absolute path)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MATERIALS = ("book", "newspaper", "microfilm")


class RetrainingTrainConfigError(ValueError):
    """Missing or invalid training path configuration for live mode."""


@dataclass
class PreprocessingTrainArtifacts:
    """Local weight paths and MLflow run ids from a live preprocessing train."""

    iep0_weights: Path | None
    iep1a_weights: dict[str, Path]
    iep1b_weights: dict[str, Path]
    mlflow_run_ids: list[str]
    s3_uris: list[str]


def parse_train_script_stdout(stdout: str) -> tuple[str | None, Path | None]:
    """Extract MLflow run id and best weights path from training script stdout."""
    run_id: str | None = None
    weights: Path | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("LIBRARYAI_MLFLOW_RUN_ID="):
            run_id = line.split("=", 1)[1].strip() or None
        elif line.startswith("LIBRARYAI_BEST_WEIGHTS="):
            p = line.split("=", 1)[1].strip()
            weights = Path(p) if p else None
    return run_id, weights


def _load_train_manifest(manifest_path: str | Path | None = None) -> dict[str, Any]:
    raw = str(manifest_path).strip() if manifest_path is not None else os.getenv("RETRAINING_TRAIN_MANIFEST", "").strip()
    if not raw:
        return {}
    path = Path(raw)
    if not path.is_file():
        raise RetrainingTrainConfigError(f"RETRAINING_TRAIN_MANIFEST not a file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_train_paths(
    *, manifest_path: str | Path | None = None
) -> tuple[Path, dict[str, Path], dict[str, Path]]:
    """
    Return (iep0_data_root, iep1a_yaml_by_material, iep1b_yaml_by_material).

    Manifest keys (if using JSON file):
      iep0.data_root
      iep1a.book | newspaper | microfilm  (paths to data.yaml)
      iep1b.book | newspaper | microfilm
    """
    manifest = _load_train_manifest(manifest_path)
    iep0: Path | None = None
    iep1a: dict[str, Path] = {}
    iep1b: dict[str, Path] = {}

    if manifest:
        iep0_raw = (manifest.get("iep0") or {}).get("data_root")
        if not iep0_raw:
            raise RetrainingTrainConfigError("manifest.iep0.data_root is required")
        iep0 = Path(iep0_raw)
        for m in _MATERIALS:
            p = (manifest.get("iep1a") or {}).get(m)
            if not p:
                raise RetrainingTrainConfigError(f"manifest.iep1a.{m} (data.yaml path) is required")
            iep1a[m] = Path(p)
            p2 = (manifest.get("iep1b") or {}).get(m)
            if not p2:
                raise RetrainingTrainConfigError(f"manifest.iep1b.{m} (data.yaml path) is required")
            iep1b[m] = Path(p2)
    else:
        iep0_env = os.getenv("RETRAINING_IEP0_DATA_ROOT", "").strip()
        if not iep0_env:
            raise RetrainingTrainConfigError(
                "Live training requires RETRAINING_TRAIN_MANIFEST (JSON) or RETRAINING_IEP0_DATA_ROOT"
            )
        iep0 = Path(iep0_env)
        for m in _MATERIALS:
            ev_a = os.getenv(f"RETRAINING_IEP1A_{m.upper()}_YAML", "").strip()
            ev_b = os.getenv(f"RETRAINING_IEP1B_{m.upper()}_YAML", "").strip()
            if not ev_a or not ev_b:
                raise RetrainingTrainConfigError(
                    f"Live training requires RETRAINING_IEP1A_{m.upper()}_YAML and "
                    f"RETRAINING_IEP1B_{m.upper()}_YAML (or use RETRAINING_TRAIN_MANIFEST)"
                )
            iep1a[m] = Path(ev_a)
            iep1b[m] = Path(ev_b)

    if not iep0.is_dir():
        raise RetrainingTrainConfigError(f"IEP0 dataset root is not a directory: {iep0}")
    for m, p in iep1a.items():
        if not p.is_file():
            raise RetrainingTrainConfigError(f"IEP1A {m} data.yaml not found: {p}")
    for m, p in iep1b.items():
        if not p.is_file():
            raise RetrainingTrainConfigError(f"IEP1B {m} data.yaml not found: {p}")

    return iep0, iep1a, iep1b


def _run_script(
    repo_root: Path,
    argv: list[str],
    *,
    timeout_s: int,
) -> tuple[str, str | None, Path | None]:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(repo_root))
    proc = subprocess.run(
        argv,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
        check=False,
    )
    out = proc.stdout or ""
    err = proc.stderr or ""
    if proc.returncode != 0:
        raise RuntimeError(
            f"Training subprocess failed rc={proc.returncode}\n"
            f"cmd={' '.join(argv)}\nstdout:\n{out}\nstderr:\n{err}"
        )
    rid, wpath = parse_train_script_stdout(out)
    if not rid:
        logger.warning("Training script did not emit LIBRARYAI_MLFLOW_RUN_ID; stdout tail:\n%s", out[-2000:])
    if wpath is None or not wpath.is_file():
        raise RuntimeError(
            f"Training did not produce LIBRARYAI_BEST_WEIGHTS or file missing (run_id={rid})\n"
            f"stdout:\n{out}\nstderr:\n{err}"
        )
    return out, rid, wpath


def _download_pretrained_weights(
    s3_uris: dict[str, str],
    dest_dir: Path,
    service: str,
) -> dict[str, Path]:
    """
    Download per-material pretrained weights from S3 to *dest_dir*.

    Returns a material → local Path dict containing only the materials that
    downloaded successfully.  Failures are logged and skipped so the caller
    can still attempt training (falling back to the COCO base model for any
    material whose download failed).

    Args:
        s3_uris:  material → s3:// URI for the weights to download.
        dest_dir: local directory to write downloaded .pt files into.
        service:  "iep1a" or "iep1b" (used only for log messages).
    """
    if not s3_uris:
        return {}
    try:
        import boto3
    except ImportError:
        logger.warning(
            "_download_pretrained_weights: boto3 not available; skipping pretrained download for %s",
            service,
        )
        return {}

    endpoint = os.getenv("S3_ENDPOINT_URL", "").strip() or None
    region = os.getenv("S3_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    client = boto3.client("s3", endpoint_url=endpoint, region_name=region)

    dest_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for mat, uri in s3_uris.items():
        if not uri.startswith("s3://"):
            logger.warning(
                "_download_pretrained_weights: skipping non-S3 URI for %s/%s: %r",
                service,
                mat,
                uri,
            )
            continue
        without_scheme = uri[len("s3://"):]
        bucket, _, key = without_scheme.partition("/")
        local_path = dest_dir / f"{service}_{mat}.pt"
        try:
            client.download_file(bucket, key, str(local_path))
            result[mat] = local_path
            logger.info(
                "_download_pretrained_weights: %s/%s → %s",
                service,
                mat,
                local_path,
            )
        except Exception as exc:
            logger.warning(
                "_download_pretrained_weights: failed to download %s/%s from %s — %s; "
                "will use base model for this material",
                service,
                mat,
                uri,
                exc,
            )
    return result


def _maybe_upload_s3(local_path: Path, job_id: str, label: str) -> str | None:
    bucket = os.getenv("S3_BUCKET_NAME", "").strip()
    if not bucket:
        return None
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        logger.warning("boto3 not available; skipping S3 upload for %s", label)
        return None

    key = f"retraining/weights/{job_id}/{label}.pt"
    endpoint = os.getenv("S3_ENDPOINT_URL", "").strip() or None
    region = os.getenv("S3_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        config=Config(
            retries={"max_attempts": int(os.getenv("S3_MAX_RETRIES", "3")), "mode": "adaptive"},
            connect_timeout=int(os.getenv("S3_CONNECT_TIMEOUT_SECONDS", "5")),
            read_timeout=int(os.getenv("S3_READ_TIMEOUT_SECONDS", "300")),
        ),
    )
    client.upload_file(str(local_path), bucket, key)
    uri = f"s3://{bucket}/{key}"
    logger.info("Uploaded weights label=%s → %s", label, uri)
    return uri


def run_live_preprocessing_training(
    repo_root: Path,
    job_id: str,
    dataset_version: str,
    *,
    manifest_path: str | Path | None = None,
    include_iep0: bool = True,
    pretrained_iep1a: dict[str, str] | None = None,
    pretrained_iep1b: dict[str, str] | None = None,
) -> PreprocessingTrainArtifacts:
    """
    Run ``train_iep0`` / ``train_iep1a`` / ``train_iep1b`` for all materials.

    Uses ``RETRAINING_DEVICE`` (default ``cpu``), ``RETRAINING_QUICK=1`` for
    ``--epochs 1`` smoke runs, and ``RETRAINING_SUBPROCESS_TIMEOUT`` seconds
    per subprocess (default 86400).

    Args:
        pretrained_iep1a: Optional material → S3 URI mapping for IEP1A pretrained
            weights.  When provided, weights are downloaded to a local temp dir and
            passed as ``--pretrained`` to the training subprocess so training starts
            from the current production model rather than the generic COCO base.
            S3 download failures are non-fatal — affected materials fall back to the
            COCO base model and a warning is logged.
        pretrained_iep1b: Same as above for IEP1B.
    """
    iep0_root, iep1a_yamls, iep1b_yamls = _resolve_train_paths(manifest_path=manifest_path)

    timeout_s = int(os.getenv("RETRAINING_SUBPROCESS_TIMEOUT", "86400"))
    device = os.getenv("RETRAINING_DEVICE", "cpu").strip() or "cpu"
    quick = os.getenv("RETRAINING_QUICK", "").lower() in ("1", "true", "yes")
    quick_epochs = ["--epochs", "1"] if quick else []

    base_project = repo_root / "runs" / "retraining" / job_id
    base_project.mkdir(parents=True, exist_ok=True)

    # Download pretrained weights from S3 to a job-local directory.
    # Failures are non-fatal: _download_pretrained_weights logs warnings and
    # returns only successfully downloaded materials.
    pretrained_dir = base_project / "pretrained_weights"
    local_pretrained_iep1a: dict[str, Path] = {}
    local_pretrained_iep1b: dict[str, Path] = {}
    if pretrained_iep1a:
        local_pretrained_iep1a = _download_pretrained_weights(
            pretrained_iep1a, pretrained_dir, "iep1a"
        )
    if pretrained_iep1b:
        local_pretrained_iep1b = _download_pretrained_weights(
            pretrained_iep1b, pretrained_dir, "iep1b"
        )

    py = sys.executable
    run_ids: list[str] = []
    s3_uris: list[str] = []

    w0: Path | None = None
    if include_iep0:
        proj0 = base_project / "iep0"
        argv0 = [
            py,
            str(repo_root / "training" / "scripts" / "train_iep0.py"),
            "--data",
            str(iep0_root),
            "--project",
            str(proj0),
            "--name",
            "iep0",
            "--dataset-version",
            dataset_version,
            "--device",
            device,
        ]
        argv0.extend(quick_epochs)
        _, rid0, w0 = _run_script(repo_root, argv0, timeout_s=timeout_s)
        if rid0:
            run_ids.append(rid0)
        uri0 = _maybe_upload_s3(w0, job_id, "iep0")
        if uri0:
            s3_uris.append(uri0)

    iep1a_weights: dict[str, Path] = {}
    iep1b_weights: dict[str, Path] = {}

    for mat in _MATERIALS:
        proj_a = base_project / "iep1a" / mat
        argv_a = [
            py,
            str(repo_root / "training" / "scripts" / "train_iep1a.py"),
            "--material",
            mat,
            "--data",
            str(iep1a_yamls[mat]),
            "--project",
            str(proj_a.parent),
            "--name",
            mat,
            "--dataset-version",
            dataset_version,
            "--device",
            device,
        ]
        argv_a.extend(quick_epochs)
        if mat in local_pretrained_iep1a:
            argv_a.extend(["--pretrained", str(local_pretrained_iep1a[mat])])
        _, rid_a, wa = _run_script(repo_root, argv_a, timeout_s=timeout_s)
        if rid_a:
            run_ids.append(rid_a)
        iep1a_weights[mat] = wa
        u_a = _maybe_upload_s3(wa, job_id, f"iep1a_{mat}")
        if u_a:
            s3_uris.append(u_a)

        proj_b = base_project / "iep1b" / mat
        argv_b = [
            py,
            str(repo_root / "training" / "scripts" / "train_iep1b.py"),
            "--material",
            mat,
            "--data",
            str(iep1b_yamls[mat]),
            "--project",
            str(proj_b.parent),
            "--name",
            mat,
            "--dataset-version",
            dataset_version,
            "--device",
            device,
        ]
        argv_b.extend(quick_epochs)
        if mat in local_pretrained_iep1b:
            argv_b.extend(["--pretrained", str(local_pretrained_iep1b[mat])])
        _, rid_b, wb = _run_script(repo_root, argv_b, timeout_s=timeout_s)
        if rid_b:
            run_ids.append(rid_b)
        iep1b_weights[mat] = wb
        u_b = _maybe_upload_s3(wb, job_id, f"iep1b_{mat}")
        if u_b:
            s3_uris.append(u_b)

    return PreprocessingTrainArtifacts(
        iep0_weights=w0,
        iep1a_weights=iep1a_weights,
        iep1b_weights=iep1b_weights,
        mlflow_run_ids=run_ids,
        s3_uris=s3_uris,
    )
