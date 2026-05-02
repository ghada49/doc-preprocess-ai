FROM python:3.11-slim-bookworm

# Set ENABLE_REAL_MODEL=true to install PaddleOCR for real inference.
# Without it the service runs in mock mode (IEP1E_MOCK_MODE=true is default).
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
        libgl1 \
        libglib2.0-0 \
        libgomp1; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IEP1E_MODEL_DIR=/opt/models/iep1e

RUN pip install --no-cache-dir --upgrade pip wheel "setuptools<81"

RUN pip install --no-cache-dir \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "prometheus-client>=0.20.0" \
    "boto3>=1.34" \
    "numpy>=1.24" \
    "opencv-python-headless>=4.8"

# Install PaddleOCR only when building the real-model image.
# In the default (stub) image, IEP1E_MOCK_MODE=true keeps all requests working.
#
# PaddleOCR is pinned to <3.0 because 3.x removed lang="arabic" support and
# restructured the model registry — our build_ocr_engine() targets the 2.x API.
RUN if [ "$ENABLE_REAL_MODEL" = "true" ]; then \
      pip install --no-cache-dir \
        "paddlepaddle>=2.6,<3.0" \
        "paddleocr>=2.7.0,<3.0"; \
    fi

# Bake the PaddleOCR CLS (text-direction classifier) model at build time.
# PaddleOCR 2.x downloads cls to its default cache (/root/.paddleocr/whl/cls/...)
# even when use_angle_cls=False and cls_model_dir is specified — the cls_model_dir
# kwarg is only honoured when use_angle_cls=True. We populate the exact cache path
# PaddleOCR checks so it finds the files and skips the ~30 MB runtime download.
RUN if [ "$ENABLE_REAL_MODEL" = "true" ]; then \
      mkdir -p /root/.paddleocr/whl/cls/ch_ppocr_mobile_v2.0_cls_infer && \
      curl -fL "https://paddleocr.bj.bcebos.com/dygraph_v2.0/ch/ch_ppocr_mobile_v2.0_cls_infer.tar" \
        | tar -xf - -C /root/.paddleocr/whl/cls/ch_ppocr_mobile_v2.0_cls_infer --strip-components=1; \
    fi

# Bake Arabic OCR det/rec model weights into the image at build time so the container
# starts instantly with no runtime internet dependency — mirrors the iep2a
# convention of COPYing pre-downloaded weights under /opt/models/<service>.
#
# To refresh det/rec weights:
#   1. Delete models/iep1e/det/ and models/iep1e/rec/
#   2. Re-run the one-time download script:
#        docker run --rm -v "$PWD/models/iep1e:/opt/models/iep1e" \
#          python:3.11-slim-bookworm bash -c "
#            apt-get update -qq && apt-get install -y -qq curl tar;
#            mkdir -p /opt/models/iep1e/det /opt/models/iep1e/rec;
#            curl -L https://paddleocr.bj.bcebos.com/PP-OCRv3/multilingual/Multilingual_PP-OCRv3_det_infer.tar \
#              | tar -xf - -C /opt/models/iep1e/det --strip-components=1;
#            curl -L https://paddleocr.bj.bcebos.com/PP-OCRv3/multilingual/arabic_PP-OCRv3_rec_infer.tar \
#              | tar -xf - -C /opt/models/iep1e/rec --strip-components=1"
#   3. Rebuild: ENABLE_REAL_MODEL=true docker compose build iep1e
# (CLS weights are baked into /root/.paddleocr/whl/cls/ in the build step above.)
COPY models/iep1e/ /opt/models/iep1e/
COPY shared/ ./shared/
COPY services/iep1e/ ./services/iep1e/

EXPOSE 8007

CMD ["uvicorn", "services.iep1e.app.main:app", "--host", "0.0.0.0", "--port", "8007"]
