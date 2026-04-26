"""
tests/test_v1_status.py
-----------------------
Contract tests for GET /v1/status.

Verifies:
  - HTTP 200
  - Body exactly {"status": "ok", "service": "eep"}
  - No auth required (the endpoint is public)
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.eep.app.main import app

_client = TestClient(app, raise_server_exceptions=False)


def test_v1_status_200() -> None:
    r = _client.get("/v1/status")
    assert r.status_code == 200


def test_v1_status_body() -> None:
    r = _client.get("/v1/status")
    assert r.json() == {"status": "ok", "service": "eep"}


def test_v1_status_no_auth_required() -> None:
    """The endpoint must be reachable without an Authorization header."""
    r = _client.get("/v1/status")
    # Must not return 401 or 403
    assert r.status_code not in (401, 403)
