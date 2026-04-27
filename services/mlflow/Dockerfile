# services/mlflow/Dockerfile
#
# Custom MLflow image based on the official ghcr.io/mlflow/mlflow image.
# Adds psycopg2-binary so the PostgreSQL backend store works without
# compiling from source.
#
# Image decision:
#   The official ghcr.io/mlflow/mlflow image provides the mlflow server
#   binary but does not bundle psycopg2.  We add psycopg2-binary here so
#   MLFLOW_BACKEND_STORE_URI can point at a PostgreSQL (RDS) database.
#
# Build context: repository root (for consistency with other services).
#
# Server command is set via ECS task-definition "command" field using
# `sh -c` so that environment variables (MLFLOW_BACKEND_STORE_URI,
# MLFLOW_ARTIFACT_ROOT) are expanded at container start time.

FROM ghcr.io/mlflow/mlflow:latest

# psycopg2-binary for RDS PostgreSQL backend store
RUN pip install --no-cache-dir psycopg2-binary

EXPOSE 5000

# CMD is overridden by the ECS task definition "command" field.
# Fallback for local docker run (requires env vars to be set externally):
CMD ["sh", "-c", \
     "mlflow server --host 0.0.0.0 --port 5000 \
      --backend-store-uri $MLFLOW_BACKEND_STORE_URI \
      --default-artifact-root $MLFLOW_ARTIFACT_ROOT"]
