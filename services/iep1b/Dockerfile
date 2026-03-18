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

# Install minimal runtime deps needed for health/metrics endpoints
# ML model deps (ultralytics, detectron2, etc.) are added in Phase 2/6
RUN uv pip install --system \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "prometheus-client>=0.20.0"

# Source code is volume-mounted in development (see docker-compose.yml)
# CMD is supplied by docker-compose.yml
