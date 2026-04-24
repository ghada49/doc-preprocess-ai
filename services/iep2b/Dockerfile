# =============================================================================
# IEP2B - DocLayout-YOLO layout detection service
# =============================================================================
#
# Build modes
#   Base (default):
#     docker build -t libraryai/iep2b:base -f services/iep2b/Dockerfile .
#
#   Real model:
#     docker build --build-arg ENABLE_REAL_MODEL=true \
#         -t libraryai/iep2b:real \
#         -f services/iep2b/Dockerfile .
#
# Approved serving images must bake the exact checkpoint and version sidecar into:
#   /opt/models/iep2b/doclayout_yolo_docstructbench_imgsz1024.pt
#   /opt/models/iep2b/doclayout_yolo_docstructbench_imgsz1024.pt.version
#
# Local development may mount a replacement file and point
# IEP2B_LOCAL_WEIGHTS_PATH at it, but runtime serving never downloads
# weights on startup.
#
# =============================================================================

FROM python:3.11-slim-bookworm

ARG ENABLE_REAL_MODEL=false

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
        libgomp1 \
        libgl1 \
        libglib2.0-0; \
    fi; \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.4.18 /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_HTTP_TIMEOUT=120

RUN pip install --no-cache-dir --upgrade pip wheel "setuptools<81"

RUN uv pip install --system \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "prometheus-client>=0.20.0" \
    "boto3>=1.34"

COPY services/iep2b/requirements-real.txt /tmp/requirements-real-iep2b.txt
COPY models/iep2b/ /opt/models/iep2b/

RUN if [ "$ENABLE_REAL_MODEL" = "true" ]; then \
      pip install --no-cache-dir \
        "torch==2.2.2" \
        "torchvision==0.17.2" \
        --index-url https://download.pytorch.org/whl/cpu \
      && pip install --no-cache-dir \
        -r /tmp/requirements-real-iep2b.txt; \
    fi

COPY shared/ ./shared/
COPY services/iep2b/ ./services/iep2b/

EXPOSE 8005
CMD ["uvicorn", "services.iep2b.app.main:app", "--host", "0.0.0.0", "--port", "8005"]
