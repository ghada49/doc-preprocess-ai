"""
services/eep/app/redis_client.py
---------------------------------
Redis connection factory for the EEP service.

Connection URL is read from the ``REDIS_URL`` environment variable.
Falls back to ``redis://localhost:6379/0`` for local development.

Exports:
    get_redis  — return a ``Redis[str]`` client (decode_responses=True)
    ping_redis — return True if Redis responds to PING, False on RedisError
"""

from __future__ import annotations

import os

import redis

_REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def get_redis() -> redis.Redis:
    """
    Return a Redis client connected to ``_REDIS_URL``.

    ``decode_responses=True``: all Redis values are returned as ``str``,
    not ``bytes``.  This matches the JSON payloads stored by the queue.

    Each call constructs a new client; long-lived worker processes should
    pass the returned instance around rather than calling this repeatedly.
    """
    return redis.Redis.from_url(_REDIS_URL, decode_responses=True)


def ping_redis() -> bool:
    """Return True if Redis responds to PING, False on any ``RedisError``."""
    try:
        return bool(get_redis().ping())
    except redis.RedisError:
        return False
