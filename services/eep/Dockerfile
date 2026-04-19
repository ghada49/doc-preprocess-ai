FROM python:3.11-slim

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

# Install the shared runtime dependency set needed by the EEP API and the
# modules it imports at startup and request time.
RUN uv pip install --system \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "prometheus-client>=0.20.0" \
    "alembic>=1.13" \
    "boto3>=1.34" \
    "httpx>=0.27" \
    "sqlalchemy>=2.0" \
    "psycopg2-binary>=2.9" \
    "redis>=5.0" \
    "opencv-python-headless>=4.9" \
    "numpy>=1.26" \
    "Pillow>=10.0" \
    "python-jose[cryptography]>=3.3" \
    "python-multipart>=0.0.9" \
    "PyYAML>=6.0" \
    "bcrypt>=4.0"

# Source code is volume-mounted in development (see docker-compose.yml)
# CMD is supplied by docker-compose.yml
