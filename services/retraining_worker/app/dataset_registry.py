"""
Dataset registry selector for live retraining.

Corrected-data-first modes:
  - corrected_only: always run corrected-export dataset builder
  - corrected_hybrid: use latest approved registry dataset, else run builder

Legacy modes (prebuilt/rebuild/hybrid) are kept for backward compatibility.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DatasetSelectionError(ValueError):
    """Raised when dataset selection or registry parsing fails."""


class DatasetSelectionDeferred(DatasetSelectionError):
    """Raised when selection is intentionally deferred (e.g. min samples not met)."""


@dataclass(frozen=True)
class DatasetSelection:
    """Resolved training dataset contract for a retraining run."""

    dataset_version: str
    dataset_checksum: str
    manifest_path: Path
    build_mode: str  # corrected_prebuilt | corrected_export
    source: str


def _registry_path(repo_root: Path) -> Path:
    raw = os.getenv("RETRAINING_DATASET_REGISTRY_PATH", "").strip()
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            return (repo_root / path).resolve()
        return path
    return (repo_root / "training" / "preprocessing" / "dataset_registry.json").resolve()


def _read_registry_bytes(path: Path) -> bytes | None:
    """Read registry bytes from a local path or an s3:// URI. Returns None if absent."""
    path_str = str(path)
    if path_str.startswith("s3://"):
        try:
            import boto3  # type: ignore[import]
            without_scheme = path_str[5:]
            bucket, _, key = without_scheme.partition("/")
            if not bucket or not key:
                raise DatasetSelectionError(
                    f"Invalid S3 registry URI (expected s3://bucket/key): {path_str}"
                )
            obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()
        except ImportError as exc:
            raise DatasetSelectionError(
                "boto3 is required to read a registry from S3 "
                f"(RETRAINING_DATASET_REGISTRY_PATH={path_str})"
            ) from exc
        except Exception as exc:
            # Object not found or access denied → treat as missing registry
            if "NoSuchKey" in type(exc).__name__ or "404" in str(exc):
                return None
            raise DatasetSelectionError(
                f"Failed to read S3 registry {path_str}: {exc}"
            ) from exc
    if not path.is_file():
        return None
    return path.read_bytes()


def _latest_approved_dataset(repo_root: Path) -> DatasetSelection | None:
    path = _registry_path(repo_root)
    content = _read_registry_bytes(path)
    if content is None:
        return None
    payload = json.loads(content.decode("utf-8"))
    datasets = payload.get("datasets")
    if not isinstance(datasets, list):
        raise DatasetSelectionError(f"Invalid dataset registry format: {path}")
    approved = [d for d in datasets if isinstance(d, dict) and d.get("approved") is True]
    if not approved:
        return None
    # Prefer latest approved by created_at lexicographically (ISO-8601), fallback dataset_version
    approved.sort(
        key=lambda d: (
            str(d.get("created_at", "")),
            str(d.get("dataset_version", "")),
        ),
        reverse=True,
    )
    chosen = approved[0]
    manifest_raw = chosen.get("manifest_path")
    if not isinstance(manifest_raw, str) or not manifest_raw.strip():
        raise DatasetSelectionError("Approved dataset missing manifest_path")
    manifest = Path(manifest_raw)
    if not manifest.is_absolute():
        manifest = (repo_root / manifest).resolve()
    return DatasetSelection(
        dataset_version=str(chosen.get("dataset_version", "")).strip(),
        dataset_checksum=str(chosen.get("dataset_checksum", "")).strip(),
        manifest_path=manifest,
        build_mode="corrected_prebuilt",
        source=f"registry:{path}",
    )


def _run_builder(repo_root: Path) -> DatasetSelection:
    raw_cmd = os.getenv("RETRAINING_DATASET_BUILDER_CMD", "").strip()
    use_shell = False
    if raw_cmd:
        if raw_cmd.startswith("["):
            try:
                parsed = json.loads(raw_cmd)
                if not isinstance(parsed, list) or not parsed:
                    raise DatasetSelectionError("RETRAINING_DATASET_BUILDER_CMD JSON must be a non-empty list")
                cmd = [str(x) for x in parsed]
            except json.JSONDecodeError as exc:
                raise DatasetSelectionError(
                    "RETRAINING_DATASET_BUILDER_CMD starts with '[' but is not valid JSON"
                ) from exc
        else:
            cmd = raw_cmd
            use_shell = True
    else:
        cmd = [
            sys.executable,
            str(repo_root / "services" / "dataset_builder" / "app" / "main.py"),
            "--mode",
            "corrected-export",
        ]
    proc = subprocess.run(
        cmd,
        shell=use_shell,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=int(os.getenv("RETRAINING_DATASET_BUILDER_TIMEOUT", "1800")),
        check=False,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        raise DatasetSelectionError(
            "Dataset builder command failed: "
            f"rc={proc.returncode} cmd={' '.join(cmd)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    out = (proc.stdout or "").strip()
    if not out:
        raise DatasetSelectionError("Dataset builder produced empty stdout")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise DatasetSelectionError(
            f"Dataset builder did not emit JSON stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        ) from exc
    status = str(payload.get("status", "ok")).strip().lower()
    if status == "min_samples_not_met":
        counts = payload.get("counts")
        raise DatasetSelectionDeferred(
            "Dataset builder deferred retraining: min_samples_not_met"
            + (f" counts={counts}" if counts is not None else "")
        )

    manifest_raw = payload.get("manifest_path")
    if not isinstance(manifest_raw, str) or not manifest_raw.strip():
        raise DatasetSelectionError("Dataset builder JSON missing manifest_path")
    manifest = Path(manifest_raw)
    if not manifest.is_absolute():
        manifest = (repo_root / manifest).resolve()
    return DatasetSelection(
        dataset_version=str(payload.get("dataset_version", "")).strip(),
        dataset_checksum=str(payload.get("dataset_checksum", "")).strip(),
        manifest_path=manifest,
        build_mode="corrected_export",
        source="builder",
    )


def select_retraining_dataset(repo_root: Path, *, prefer_mode: str | None = None) -> DatasetSelection:
    """
    Select a retraining dataset according to RETRAINING_DATASET_MODE.

    Modes:
      - corrected_only: always run corrected-export builder
      - corrected_hybrid: prefer approved registry dataset, otherwise builder
    """
    mode = (prefer_mode or os.getenv("RETRAINING_DATASET_MODE", "corrected_hybrid")).strip().lower()
    if mode not in {"corrected_only", "corrected_hybrid"}:
        raise DatasetSelectionError(
            "RETRAINING_DATASET_MODE must be "
            "corrected_only|corrected_hybrid "
            f"(got {mode!r})"
        )

    if mode == "corrected_only":
        return _run_builder(repo_root)

    if mode == "corrected_hybrid":
        chosen = _latest_approved_dataset(repo_root)
        if chosen is not None:
            return chosen
        return _run_builder(repo_root)
    raise DatasetSelectionError(f"Unsupported RETRAINING_DATASET_MODE: {mode!r}")


def emit_default_registry(path: Path, manifest_path: Path) -> dict[str, Any]:
    """
    Bootstrap a minimal registry file for operators starting from a known manifest.
    """
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "version": 1,
        "generated_at": now,
        "datasets": [
            {
                "dataset_version": f"bootstrap-{now[:19].replace(':', '').replace('-', '')}",
                "dataset_checksum": "",
                "manifest_path": str(manifest_path),
                "approved": True,
                "build_mode": "corrected_prebuilt",
                "source_window": "bootstrap",
                "created_at": now,
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
