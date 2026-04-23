FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.4.18 /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN uv pip install --system \
    "boto3>=1.34" \
    "sqlalchemy>=2.0" \
    "psycopg2-binary>=2.9" \
    "PyYAML>=6.0"

# source code is volume-mounted in docker compose
# CMD is supplied by orchestrator
