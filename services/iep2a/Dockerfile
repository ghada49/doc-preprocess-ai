FROM python:3.11-slim-bookworm

ARG ENABLE_REAL_MODEL=false
# Set ENABLE_PADDLE_BACKEND=true to install PaddleOCR alongside (or instead of) Detectron2.
# ENABLE_REAL_MODEL and ENABLE_PADDLE_BACKEND are independent; both can be true.
ARG ENABLE_PADDLE_BACKEND=false

RUN set -eux; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list; \
    fi; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    for i in 1 2 3; do \
      apt-get update && break || sleep 5; \
    done; \
    apt-get install -y --no-install-recommends --fix-missing \
      -o Acquire::Retries=5 \
      -o Acquire::http::Timeout=30 \
      -o Acquire::https::Timeout=30 \
      curl; \
    if [ "$ENABLE_REAL_MODEL" = "true" ]; then \
      apt-get install -y --no-install-recommends --fix-missing \
        -o Acquire::Retries=5 \
        -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30 \
        build-essential \
        cmake \
        git \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        ninja-build; \
    fi; \
    if [ "$ENABLE_PADDLE_BACKEND" = "true" ] && [ "$ENABLE_REAL_MODEL" != "true" ]; then \
      apt-get install -y --no-install-recommends --fix-missing \
        -o Acquire::Retries=5 \
        -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30 \
        libgl1 \
        libglib2.0-0; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir --upgrade pip wheel "setuptools<81"

RUN pip install --no-cache-dir \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "prometheus-client>=0.20.0"

COPY services/iep2a/requirements-real.txt /tmp/requirements-real-iep2a.txt
COPY models/iep2a/ /opt/models/iep2a/

RUN if [ "$ENABLE_REAL_MODEL" = "true" ]; then \
      pip install --no-cache-dir \
        "torch==2.2.2" \
        "torchvision==0.17.2" \
        --index-url https://download.pytorch.org/whl/cpu \
      && pip install --no-cache-dir "layoutparser>=0.3.4" \
      && pip install --no-cache-dir -r /tmp/requirements-real-iep2a.txt \
      && pip install --no-cache-dir --no-build-isolation \
        "detectron2@git+https://github.com/facebookresearch/detectron2.git"; \
    fi

# PaddleOCR layout backend (IEP2A_LAYOUT_BACKEND=paddleocr).
# paddlepaddle: CPU-only wheel.  For GPU inference use paddlepaddle-gpu instead.
# paddleocr[doc-parser]>=3.0.0 provides the official LayoutDetection API used
# for PP-DocLayoutV2 layout analysis.
# opencv-python-headless is already present via requirements-real; installed here
# when ENABLE_PADDLE_BACKEND=true without ENABLE_REAL_MODEL=true.
COPY services/iep2a/requirements-paddle.txt /tmp/requirements-paddle-iep2a.txt
RUN if [ "$ENABLE_PADDLE_BACKEND" = "true" ]; then \
      pip install --no-cache-dir -r /tmp/requirements-paddle-iep2a.txt; \
    fi

# Approved serving images must bake the exact PubLayNet checkpoint into
# /opt/models/iep2a/model_final.pth and the matching version sidecar into
# /opt/models/iep2a/model_final.pth.version. Local development may still
# mount a replacement file via IEP2A_LOCAL_WEIGHTS_PATH.
#
# Optional PaddleOCR backend support uses the official PP-DocLayoutV2 model
# directory baked into /opt/models/iep2a/paddle/PP-DocLayoutV2 with the matching
# sidecar /opt/models/iep2a/paddle/PP-DocLayoutV2.version. Local development may
# mount a replacement directory under /dev-models/iep2a and point
# IEP2A_PADDLE_LOCAL_MODEL_DIR at it.
#
# Source code is volume-mounted in development (see docker-compose.yml).
# CMD is supplied by docker-compose.yml.
