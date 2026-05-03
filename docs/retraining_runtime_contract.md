# Retraining Runtime Contract

This document defines the retraining dataset contract shared by:

- `services/dataset_builder` (corrected-export producer)
- `services/retraining_worker` (training/evaluation consumer)

It is valid for local Docker Compose and Kubernetes deployments.

## Dataset Contract

Each retraining run must resolve:

- `dataset_version` (string)
- `dataset_checksum` (sha256 string; may be empty only for legacy bootstrap)
- `manifest_path` (path to `retraining_train_manifest.json`)
- `build_mode` (`corrected_prebuilt` or `corrected_export`)

`retraining_train_manifest.json` shape:

```json
{
  "iep0": { "data_root": "/abs/or/container/path/to/imagefolder" },
  "iep1a": {
    "book": "/path/to/iep1a/book/data.yaml",
    "newspaper": "/path/to/iep1a/newspaper/data.yaml",
    "microfilm": "/path/to/iep1a/microfilm/data.yaml"
  },
  "iep1b": {
    "book": "/path/to/iep1b/book/data.yaml",
    "newspaper": "/path/to/iep1b/newspaper/data.yaml",
    "microfilm": "/path/to/iep1b/microfilm/data.yaml"
  }
}
```

Corrected-export manifests may include only the service/material combinations
that met the corrected-sample threshold for that run. Consumers must not assume
all six IEP1A/IEP1B material entries are present.

## Dataset Registry

Default path: `training/preprocessing/dataset_registry.json` (override via
`RETRAINING_DATASET_REGISTRY_PATH`).

Registry record:

```json
{
  "dataset_version": "ds-20260420-120001",
  "dataset_checksum": "sha256...",
  "manifest_path": "/app/.../retraining_train_manifest.json",
  "approved": true,
  "build_mode": "corrected_prebuilt",
  "source_window": "2026W16",
  "created_at": "2026-04-20T12:00:01+00:00"
}
```

## Selection Modes (`RETRAINING_DATASET_MODE`)

- `corrected_only`: always run corrected-export builder (strict corrected-data path).
- `corrected_hybrid` (recommended default): use latest approved registry entry when available; otherwise run corrected-export builder.

No legacy dataset modes are supported in corrected-only retraining.

## Builder Command Contract

Dataset builder command (default for corrected modes):

`python services/dataset_builder/app/main.py --mode corrected-export`

Override:

- `RETRAINING_DATASET_BUILDER_CMD`
- `RETRAINING_DATASET_BUILDER_TIMEOUT` (seconds)

Builder stdout must be a JSON object containing:

- `status` (`ok` | `min_samples_not_met` | `error`)
- `dataset_version`
- `dataset_checksum`
- `manifest_path`

## Compose Runtime Matrix

- Retraining worker service: `retraining-worker`
- Optional dataset build job profile: `dataset-build`

Examples:

- Export corrected dataset and register:
  `docker compose --profile dataset-build run --rm dataset-builder --mode corrected-export --approved`
- Run Step 7 with corrected-hybrid selection:
  `docker compose -f docker-compose.yml -f docker-compose.step7.yml up -d`

## Kubernetes Mapping

- `retraining-worker` -> Deployment
- `dataset-builder --mode corrected-export` -> CronJob or Job
- Registry and manifests should live on shared volume or object-store-backed sync path accessible by both jobs and worker.
