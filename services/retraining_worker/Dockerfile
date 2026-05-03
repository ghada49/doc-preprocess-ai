FROM pytorch/pytorch:2.2.2-cuda11.8-cudnn8-runtime

# OpenCV (pulled in via ultralytics) needs a few system libs on Debian slim images.
# Note: libglib2.0-0 provides libgthread-2.0.so.0 (needed by cv2 import).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libglib2.0-0 \
        libxcb1 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.4.18 /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_HTTP_TIMEOUT=120

# Install the shared runtime dependency set needed by the retraining worker and
# its DB-backed task modules.
RUN uv pip install --system \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "prometheus-client>=0.20.0" \
    "boto3>=1.34" \
    "httpx>=0.27" \
    "sqlalchemy>=2.0" \
    "psycopg2-binary>=2.9" \
    "redis>=5.0" \
    "numpy>=1.26" \
    "python-jose[cryptography]>=3.3" \
    "python-multipart>=0.0.9" \
    "PyYAML>=6.0" \
    "bcrypt>=4.0" \
    "opencv-python-headless>=4.9" \
    "ultralytics>=8.2.0" \
    "mlflow>=2.11.0" \
    && uv pip uninstall --system opencv-python || true \
    && uv pip install --system --force-reinstall --no-deps "opencv-python-headless>=4.9"

COPY shared/ ./shared/
COPY services/eep/ ./services/eep/
COPY services/dataset_builder/ ./services/dataset_builder/
COPY services/retraining_worker/ ./services/retraining_worker/
COPY training/scripts/ ./training/scripts/
COPY training/preprocessing/ ./training/preprocessing/

RUN mkdir -p /tmp/prometheus_multiproc

EXPOSE 9104
CMD ["uvicorn", "services.retraining_worker.app.main:app", "--host", "0.0.0.0", "--port", "9104"]
