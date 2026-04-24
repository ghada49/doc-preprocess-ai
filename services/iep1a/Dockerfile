# pytorch/pytorch includes CUDA 11.8 + cuDNN 8 + Python 3.10 (3.11 not available in this tag)
# Service-specific ML model deps (ultralytics, detectron2, etc.) are installed in Phase 2/6
FROM pytorch/pytorch:2.2.2-cuda11.8-cudnn8-runtime

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.4.18 /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ENV UV_HTTP_TIMEOUT=300

# Install runtime deps: FastAPI + YOLOv8 segmentation
RUN uv pip install --system \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "prometheus-client>=0.20.0" \
    "ultralytics>=8.1.0" \
    "boto3>=1.34.0" \
    "numpy>=1.26,<2" \
    && pip uninstall -y opencv-python 2>/dev/null; \
    uv pip install --system "opencv-python-headless>=4.9.0" "numpy>=1.26,<2"

COPY shared/ ./shared/
COPY services/iep1a/ ./services/iep1a/
COPY models/iep1a/ ./models/iep1a/

ENV IEP1A_MODELS_DIR=/app/models/iep1a

EXPOSE 8001
CMD ["uvicorn", "services.iep1a.app.main:app", "--host", "0.0.0.0", "--port", "8001"]
