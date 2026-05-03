"""
services/retraining_worker/app/task.py
----------------------------------------
Packet 8.5 — Retraining task execution.

Implements the core task lifecycle for a single retraining trigger:

  1. Determine pipeline_type from trigger_type (spec Section 16.3).
  2. Create a RetrainingJob record (status='running').
  3. Link trigger.retraining_job_id → job.job_id.
  4. Training — default stub (placeholder ``mlflow_run_id`` / ``dataset_version``).
     Set ``LIBRARYAI_RETRAINING_TRAIN=live`` plus ``RETRAINING_TRAIN_MANIFEST`` or
     per-dataset env vars (see ``.env.example``) to run ``training/scripts/train_iep*.py``,
     upload ``best.pt`` to S3 when ``S3_BUCKET_NAME`` is set, and record real MLflow run ids.
  5. Offline evaluation — writes gate_results to model_versions in the format
     read by promotion_api._check_gates (spec Section 16.2).
     Default: stub. Set ``LIBRARYAI_RETRAINING_GOLDEN_EVAL=live`` to run
     ``training/scripts/evaluate_golden_dataset.py`` for **IEP1A** and
     **IEP1B**. Cross-model structural + latency measurement uses manifest
     pairs sharing the same ``image_s3_key`` (requires AWS, SHAs,
     torch/ultralytics). Set ``GOLDEN_SKIP_CROSS_MODEL=1`` to skip the extra
     cross-model pass (faster; structural/latency fall back to placeholders).
  6. Create ModelVersion rows (stage='staging') for each target service with
     the computed gate_results.
  7. Mark job completed, trigger completed.

layout_confidence_degradation is a monitoring-only trigger (spec Section 16.3):
no automated retraining job is created; trigger is marked completed immediately.

Gate results format (each top-level value must include ``pass``; see
promotion_api._check_gates):

  **iep1a / iep1b** (merged preprocessing evaluation):
  {
    "geometry_iou":              {"pass": bool, "value": float},
    "split_precision":           {"pass": bool, "value": float},
    "structural_agreement_rate": {"pass": bool, "value": float},
    "golden_dataset":            {"pass": bool, "regressions": int},
    "latency_p95":               {"pass": bool, "value": float},
  }

Exported:
  execute_retraining_task(trigger, db) — callable from the poll loop
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from services.eep.app.db.models import ModelVersion, RetrainingJob, RetrainingTrigger
from services.retraining_worker.app.dataset_registry import (
    DatasetSelectionDeferred,
    DatasetSelectionError,
    select_retraining_dataset,
)
from services.retraining_worker.app.golden_gate_merge import build_iep1ab_live_gates
from services.retraining_worker.app.live_train import (
    RetrainingTrainConfigError,
    run_live_preprocessing_training,
)

logger = logging.getLogger(__name__)

_MATERIALS: tuple[str, ...] = ("book", "newspaper", "microfilm")

# trigger_type → pipeline_type (spec Section 16.3).
# None means monitoring-only; no automated job is created.
_TRIGGER_PIPELINE: dict[str, str | None] = {
    "manual_retraining": "preprocessing",
    "escalation_rate_anomaly": "preprocessing",
    "auto_accept_rate_collapse": "preprocessing",
    "structural_agreement_degradation": "preprocessing",
    "drift_alert_persistence": "preprocessing",
    "layout_confidence_degradation": None,
}

# Services retrained for a preprocessing pipeline job.
_PREPROCESSING_SERVICES: tuple[str, ...] = ("iep1a", "iep1b")


# ── Stub helpers ──────────────────────────────────────────────────────────────


def _stub_gate_results() -> dict:
    """
    Placeholder offline evaluation results.

    Format is identical to the gate_results written by the real evaluation
    worker and read by promotion_api._check_gates.  All gates pass with
    conservative but realistic placeholder values.

    Stub — real per-model evaluation against held-out datasets is wired in
    Phase 12 when IEP1A/B model weights are available.
    """
    return {
        "geometry_iou": {"pass": True, "value": 0.84},
        "split_precision": {"pass": True, "value": 0.77},
        "structural_agreement_rate": {"pass": True, "value": 0.71},
        "golden_dataset": {"pass": True, "regressions": 0},
        "latency_p95": {"pass": True, "value": 2.3},
    }


def _stub_mlflow_train(pipeline_type: str, trigger_id: str) -> tuple[str, str]:
    """
    Placeholder MLflow training run.

    Returns (mlflow_run_id, dataset_version).  Real MLflow client integration
    and actual IEP1 model training are wired in Phase 12.
    """
    mlflow_run_id = f"stub-run-{uuid.uuid4().hex[:12]}"
    dataset_version = f"ds-stub-{pipeline_type}-001"
    logger.info(
        "_stub_mlflow_train: pipeline_type=%s trigger_id=%s → run_id=%s (STUB — mlflow not wired)",
        pipeline_type,
        trigger_id,
        mlflow_run_id,
    )
    return mlflow_run_id, dataset_version


# ── Task entry point ──────────────────────────────────────────────────────────


def _resolve_production_weights(service_name: str, db: Session) -> dict[str, str]:
    """
    Return a material → S3 URI mapping for the current production ModelVersion.

    Parses the ``notes`` field written by execute_retraining_task when live
    training uploads weights to S3:
        "s3_weights:s3://bucket/path/iep1a_book.pt,s3://bucket/path/iep1a_newspaper.pt,..."

    Returns an empty dict (graceful no-op) when:
      - no production ModelVersion exists for the service
      - notes field is absent or has no s3_weights section
      - S3 URIs can't be mapped to known materials

    Failures here must never block retraining — the caller falls back to the
    COCO base model when this returns empty.
    """
    mv: ModelVersion | None = (
        db.query(ModelVersion)
        .filter(
            ModelVersion.service_name == service_name,
            ModelVersion.stage == "production",
        )
        .order_by(ModelVersion.promoted_at.desc())
        .first()
    )
    if mv is None or not mv.notes:
        return {}

    # Extract s3_weights:... segment (notes may have additional space-delimited fields)
    s3_weights_str = ""
    for token in mv.notes.split(" "):
        if token.startswith("s3_weights:"):
            s3_weights_str = token[len("s3_weights:"):]
            break
    if not s3_weights_str:
        return {}

    result: dict[str, str] = {}
    for uri in s3_weights_str.split(","):
        uri = uri.strip()
        if not uri.startswith("s3://"):
            continue
        # Filename format: {service}_{material}.pt  e.g. "iep1a_book.pt"
        filename = uri.rsplit("/", 1)[-1]  # last path component
        stem = filename.removesuffix(".pt")  # "iep1a_book"
        # Strip any service prefix to isolate the material
        for prefix in (f"{service_name}_", "iep1a_", "iep1b_"):
            if stem.startswith(prefix):
                material = stem[len(prefix):]
                if material in _MATERIALS:
                    result[material] = uri
                break

    logger.debug(
        "_resolve_production_weights: service=%s model_id=%s found_materials=%s",
        service_name,
        mv.model_id,
        sorted(result.keys()),
    )
    return result


def _resolve_env_pretrained_weights(service_name: str) -> dict[str, str]:
    """
    Return material weight URIs from IEP1A_WEIGHTS_URI / IEP1B_WEIGHTS_URI.

    Accepted formats:
      - s3://.../iep1a-models.tar.gz  -> archive extracted by live_train.py
      - book=s3://...pt,newspaper=s3://...pt,microfilm=s3://...pt
    """
    env_name = "IEP1A_WEIGHTS_URI" if service_name == "iep1a" else "IEP1B_WEIGHTS_URI"
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return {}

    if "=" not in raw:
        return {"__archive__": raw}

    result: dict[str, str] = {}
    for item in raw.split(","):
        material, sep, uri = item.partition("=")
        material = material.strip().lower()
        uri = uri.strip()
        if sep and material in _MATERIALS and uri:
            result[material] = uri

    if not result:
        logger.warning(
            "_resolve_env_pretrained_weights: %s is set but no material URI mapping could be parsed",
            env_name,
        )
    return result


def _run_manual_evaluation(trigger: RetrainingTrigger, db: Session, now: datetime) -> None:
    """
    Run offline evaluation for an existing ModelVersion without retraining.

    Called when trigger_type='manual_evaluation'.  The model_id to evaluate is
    stored in trigger.notes (written by POST /v1/models/evaluate).  Gate results
    are written directly to model_versions.gate_results in the format that
    promotion_api._check_gates expects.
    """
    model_id = (trigger.notes or "").strip()
    if not model_id:
        raise ValueError("manual_evaluation trigger missing model_id in notes")

    candidate = db.get(ModelVersion, model_id)
    if candidate is None:
        raise ValueError(f"model_version not found for manual_evaluation: {model_id!r}")

    eval_mode = os.getenv("LIBRARYAI_RETRAINING_GOLDEN_EVAL", "stub").strip().lower()
    if eval_mode == "live":
        repo_root = Path(__file__).resolve().parents[3]
        gate_results = build_iep1ab_live_gates(repo_root)
    else:
        gate_results = _stub_gate_results()

    candidate.gate_results = gate_results
    trigger.status = "completed"
    trigger.resolved_at = now
    db.commit()

    logger.info(
        "_run_manual_evaluation: wrote gate_results to model_id=%s service=%s tag=%s (%s mode)",
        model_id,
        candidate.service_name,
        candidate.version_tag,
        eval_mode,
    )


def execute_retraining_task(trigger: RetrainingTrigger, db: Session) -> None:
    """
    Execute a single retraining task for *trigger*.

    The poll loop is responsible for claiming the trigger (status='processing')
    before calling this function.  On success this function sets
    trigger.status='completed'.  On failure the caller is responsible for
    catching the exception, rolling back, and marking the trigger failed.

    Args:
        trigger: ORM row; must already have status='processing'.
        db:      Open SQLAlchemy session owned by the caller.
    """
    now = datetime.now(timezone.utc)
    trigger_type = trigger.trigger_type

    # Manual evaluation-only: update gate_results on the existing model, no training
    if trigger_type == "manual_evaluation":
        _run_manual_evaluation(trigger, db, now)
        return

    pipeline_type = _TRIGGER_PIPELINE.get(trigger_type)

    # Monitoring-only trigger: mark completed immediately, no job
    if pipeline_type is None:
        logger.info(
            "execute_retraining_task: trigger_type=%s → monitoring-only, no job created",
            trigger_type,
        )
        trigger.status = "completed"
        trigger.resolved_at = now
        trigger.notes = "monitoring-only trigger; no automated retraining job created"
        db.commit()
        return

    # Create retraining job
    job = RetrainingJob(
        job_id=str(uuid.uuid4()),
        trigger_id=trigger.trigger_id,
        pipeline_type=pipeline_type,
        status="running",
        started_at=now,
    )
    db.add(job)

    # Link trigger → job before first commit
    trigger.retraining_job_id = job.job_id
    db.commit()
    db.refresh(job)

    logger.info(
        "execute_retraining_task: created job_id=%s pipeline_type=%s trigger_id=%s",
        job.job_id,
        pipeline_type,
        trigger.trigger_id,
    )

    dataset_version = os.getenv("RETRAINING_DATASET_VERSION", "").strip()
    dataset_checksum = ""
    selected_manifest: Path | None = None
    train_mode = os.getenv("LIBRARYAI_RETRAINING_TRAIN", "stub").strip().lower()
    trained_weights = None
    if train_mode == "live":
        repo_root = Path(__file__).resolve().parents[3]
        try:
            selection = select_retraining_dataset(repo_root)
        except DatasetSelectionDeferred as exc:
            logger.info("execute_retraining_task: dataset selection deferred: %s", exc)
            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            job.promotion_decision = "skipped_insufficient_data"
            trigger.status = "completed"
            trigger.resolved_at = datetime.now(timezone.utc)
            trigger.notes = str(exc)
            db.commit()
            return
        except DatasetSelectionError:
            logger.exception("execute_retraining_task: dataset selection failed")
            raise
        selected_manifest = selection.manifest_path
        if not selected_manifest.is_file():
            raise RuntimeError(
                f"Selected training manifest does not exist: {selected_manifest}"
            )
        if selection.dataset_version:
            dataset_version = selection.dataset_version
        if not dataset_version:
            dataset_version = f"rt-{job.job_id.replace('-', '')[:12]}"
        dataset_checksum = selection.dataset_checksum
        # Resolve production weights for fine-tuning (best-effort; falls back to
        # COCO base model on any failure so retraining is never blocked).
        pretrained_iep1a: dict[str, str] = {}
        pretrained_iep1b: dict[str, str] = {}
        try:
            pretrained_iep1a = _resolve_production_weights("iep1a", db)
            pretrained_iep1b = _resolve_production_weights("iep1b", db)
            if not pretrained_iep1a:
                pretrained_iep1a = _resolve_env_pretrained_weights("iep1a")
            if not pretrained_iep1b:
                pretrained_iep1b = _resolve_env_pretrained_weights("iep1b")
            if pretrained_iep1a or pretrained_iep1b:
                logger.info(
                    "execute_retraining_task: fine-tuning from production weights "
                    "iep1a_materials=%s iep1b_materials=%s",
                    sorted(pretrained_iep1a.keys()),
                    sorted(pretrained_iep1b.keys()),
                )
            else:
                logger.info(
                    "execute_retraining_task: no production weights found; "
                    "training from COCO base model"
                )
        except Exception:
            logger.warning(
                "execute_retraining_task: failed to resolve production weights; "
                "falling back to COCO base model",
                exc_info=True,
            )

        try:
            trained_weights = run_live_preprocessing_training(
                repo_root,
                job.job_id,
                dataset_version,
                manifest_path=selected_manifest,
                include_iep0=False,
                pretrained_iep1a=pretrained_iep1a or None,
                pretrained_iep1b=pretrained_iep1b or None,
            )
        except RetrainingTrainConfigError:
            raise
        mlflow_run_id = (
            ",".join(trained_weights.mlflow_run_ids)
            if trained_weights.mlflow_run_ids
            else f"live-no-runid-{uuid.uuid4().hex[:12]}"
        )
        job.mlflow_run_id = mlflow_run_id
        job.dataset_version = dataset_version
        provenance_bits = [
            f"build_mode={selection.build_mode}",
            f"source={selection.source}",
            f"manifest={selected_manifest}",
        ]
        if dataset_checksum:
            provenance_bits.append(f"dataset_checksum={dataset_checksum}")
        trigger.notes = " | ".join(provenance_bits)
        job.mlflow_experiment = "libraryai_preprocessing"
    else:
        mlflow_run_id, ds_stub = _stub_mlflow_train(pipeline_type, trigger.trigger_id)
        job.mlflow_run_id = mlflow_run_id
        job.dataset_version = ds_stub
        dataset_version = ds_stub

    # Offline evaluation — stub by default; set LIBRARYAI_RETRAINING_GOLDEN_EVAL=live
    # for real golden-dataset runs (requires AWS creds, S3, valid case SHAs, torch).
    eval_mode = os.getenv("LIBRARYAI_RETRAINING_GOLDEN_EVAL", "stub").strip().lower()
    iep1ab_live_gates = None
    if eval_mode == "live":
        repo_root = Path(__file__).resolve().parents[3]
        kw: dict[str, Any] = {}
        if trained_weights is not None:
            kw["weights_iep1a_by_material"] = trained_weights.iep1a_weights
            kw["weights_iep1b_by_material"] = trained_weights.iep1b_weights
        iep1ab_live_gates = build_iep1ab_live_gates(
            repo_root,
            weights_iep1a_by_material=kw.get("weights_iep1a_by_material"),
            weights_iep1b_by_material=kw.get("weights_iep1b_by_material"),
        )

    services = _PREPROCESSING_SERVICES if pipeline_type == "preprocessing" else ()
    if trained_weights is not None:
        trained_services: list[str] = []
        if trained_weights.iep1a_weights:
            trained_services.append("iep1a")
        if trained_weights.iep1b_weights:
            trained_services.append("iep1b")
        services = tuple(trained_services)
    created_version_tags: list[str] = []

    for service_name in services:
        if iep1ab_live_gates is not None:
            gate_results = iep1ab_live_gates
            eval_label = "live golden evaluation"
        else:
            gate_results = _stub_gate_results()
            eval_label = "stub evaluation"
        if train_mode == "live":
            version_tag = f"rt-{job.job_id.replace('-', '')[:12]}-{service_name}"
        else:
            version_tag = f"stub-{service_name}-{uuid.uuid4().hex[:8]}"
        mv_notes = None
        if trained_weights is not None and trained_weights.s3_uris:
            mv_notes = "s3_weights:" + ",".join(trained_weights.s3_uris)
        if selected_manifest is not None:
            suffix = f" dataset_manifest={selected_manifest}"
            if dataset_checksum:
                suffix += f" dataset_checksum={dataset_checksum}"
            mv_notes = (mv_notes + suffix) if mv_notes else suffix.strip()
        mv = ModelVersion(
            model_id=str(uuid.uuid4()),
            service_name=service_name,
            version_tag=version_tag,
            mlflow_run_id=mlflow_run_id,
            dataset_version=dataset_version,
            stage="staging",
            gate_results=gate_results,
            notes=mv_notes,
        )
        db.add(mv)
        created_version_tags.append(version_tag)
        logger.info(
            "execute_retraining_task: created ModelVersion service=%s version=%s stage=staging "
            "gate_results written (%s)",
            service_name,
            version_tag,
            eval_label,
        )

    # Mark job completed
    job.status = "completed"
    job.completed_at = datetime.now(timezone.utc)
    job.result_model_version = ",".join(created_version_tags) if created_version_tags else None
    job.promotion_decision = "pending_gate_review"

    # Mark trigger completed
    trigger.status = "completed"
    trigger.resolved_at = datetime.now(timezone.utc)

    db.commit()

    logger.info(
        "execute_retraining_task: completed job_id=%s versions_created=%s",
        job.job_id,
        created_version_tags,
    )
