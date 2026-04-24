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
    PYTHONUNBUFFERED=1 \
    UV_HTTP_TIMEOUT=120

# Install runtime deps needed for UVDoc inference + artifact I/O.
COPY services/iep1d/requirements-real.txt /tmp/requirements-real.txt
RUN uv pip install --system \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "prometheus-client>=0.20.0" \
    -r /tmp/requirements-real.txt

COPY shared/ ./shared/
COPY services/iep1d/ ./services/iep1d/
COPY models/iep1d/ /opt/models/iep1d/

EXPOSE 8003
CMD ["uvicorn", "services.iep1d.app.main:app", "--host", "0.0.0.0", "--port", "8003"]
