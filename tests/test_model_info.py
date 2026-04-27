"""
tests/test_model_info.py
------------------------
Tests for the GET /model-info endpoint on iep1a and iep1b.

Covers:
  - Endpoint exists and returns 200
  - Required fields are present in the response
  - reload_count starts at 0 on a fresh module state
  - reloaded_since_startup is False when no reload has occurred
  - reloaded_since_startup is True after reload_models() is called
  - reload_count increments correctly
  - last_reload_at is None before first reload
  - last_reload_at is an ISO-format string after reload
  - version_tag is always null (honest unknown)
  - mock_mode reflects IEP1A_MOCK_MODE / IEP1B_MOCK_MODE env var
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── iep1a ─────────────────────────────────────────────────────────────────────


@pytest.fixture()
def iep1a_fresh():
    """
    Return a TestClient for a mini iep1a app with a clean reload-tracking state.
    Resets module-level counters before each test so tests are independent.
    """
    import services.iep1a.app.inference as inf_mod

    # Reset module-level state
    inf_mod._loaded_models.clear()
    inf_mod._reload_count = 0
    inf_mod._last_reload_wall = None
    inf_mod._last_version_tag = None

    app = FastAPI()

    from services.iep1a.app.main import model_info
    app.get("/model-info")(model_info)

    return TestClient(app, raise_server_exceptions=False)


class TestIep1aModelInfo:
    def test_endpoint_returns_200(self, iep1a_fresh: TestClient) -> None:
        resp = iep1a_fresh.get("/model-info")
        assert resp.status_code == 200

    def test_required_fields_present(self, iep1a_fresh: TestClient) -> None:
        data = iep1a_fresh.get("/model-info").json()
        required = {
            "service",
            "mock_mode",
            "models_dir",
            "loaded_models",
            "reload_count",
            "last_reload_at",
            "reloaded_since_startup",
            "version_tag",
        }
        assert required.issubset(data.keys())

    def test_service_is_iep1a(self, iep1a_fresh: TestClient) -> None:
        data = iep1a_fresh.get("/model-info").json()
        assert data["service"] == "iep1a"

    def test_reload_count_starts_at_zero(self, iep1a_fresh: TestClient) -> None:
        data = iep1a_fresh.get("/model-info").json()
        assert data["reload_count"] == 0

    def test_reloaded_since_startup_false_initially(self, iep1a_fresh: TestClient) -> None:
        data = iep1a_fresh.get("/model-info").json()
        assert data["reloaded_since_startup"] is False

    def test_last_reload_at_none_initially(self, iep1a_fresh: TestClient) -> None:
        data = iep1a_fresh.get("/model-info").json()
        assert data["last_reload_at"] is None

    def test_version_tag_is_null(self, iep1a_fresh: TestClient) -> None:
        data = iep1a_fresh.get("/model-info").json()
        assert data["version_tag"] is None

    def test_loaded_models_lists_all_materials(self, iep1a_fresh: TestClient) -> None:
        data = iep1a_fresh.get("/model-info").json()
        materials = {entry["material"] for entry in data["loaded_models"]}
        assert materials == {"book", "newspaper", "microfilm"}

    def test_loaded_models_entries_have_required_keys(self, iep1a_fresh: TestClient) -> None:
        data = iep1a_fresh.get("/model-info").json()
        for entry in data["loaded_models"]:
            assert "material" in entry
            assert "weight_file" in entry
            assert "weight_path" in entry
            assert "cached" in entry

    def test_cached_false_before_load(self, iep1a_fresh: TestClient) -> None:
        data = iep1a_fresh.get("/model-info").json()
        assert all(not entry["cached"] for entry in data["loaded_models"])

    def test_reload_count_increments_after_reload(self, iep1a_fresh: TestClient) -> None:
        import services.iep1a.app.inference as inf_mod
        inf_mod.reload_models()
        data = iep1a_fresh.get("/model-info").json()
        assert data["reload_count"] == 1

    def test_reloaded_since_startup_true_after_reload(self, iep1a_fresh: TestClient) -> None:
        import services.iep1a.app.inference as inf_mod
        inf_mod.reload_models()
        data = iep1a_fresh.get("/model-info").json()
        assert data["reloaded_since_startup"] is True

    def test_last_reload_at_set_after_reload(self, iep1a_fresh: TestClient) -> None:
        import services.iep1a.app.inference as inf_mod
        inf_mod.reload_models()
        data = iep1a_fresh.get("/model-info").json()
        assert data["last_reload_at"] is not None
        # Must be an ISO 8601 string parseable by datetime
        from datetime import datetime
        datetime.fromisoformat(data["last_reload_at"])

    def test_mock_mode_false_by_default(self, iep1a_fresh: TestClient, monkeypatch) -> None:
        monkeypatch.delenv("IEP1A_MOCK_MODE", raising=False)
        data = iep1a_fresh.get("/model-info").json()
        assert data["mock_mode"] is False

    def test_mock_mode_true_when_env_set(self, iep1a_fresh: TestClient, monkeypatch) -> None:
        monkeypatch.setenv("IEP1A_MOCK_MODE", "true")
        data = iep1a_fresh.get("/model-info").json()
        assert data["mock_mode"] is True

    def test_multiple_reloads_counted(self, iep1a_fresh: TestClient) -> None:
        import services.iep1a.app.inference as inf_mod
        inf_mod.reload_models()
        inf_mod.reload_models()
        inf_mod.reload_models()
        data = iep1a_fresh.get("/model-info").json()
        assert data["reload_count"] == 3

    def test_version_tag_set_after_reload_with_tag(self, iep1a_fresh: TestClient) -> None:
        import services.iep1a.app.inference as inf_mod
        inf_mod.reload_models(version_tag="v2.3.0")
        data = iep1a_fresh.get("/model-info").json()
        assert data["version_tag"] == "v2.3.0"

    def test_version_tag_updated_on_subsequent_reload(self, iep1a_fresh: TestClient) -> None:
        import services.iep1a.app.inference as inf_mod
        inf_mod.reload_models(version_tag="v2.3.0")
        inf_mod.reload_models(version_tag="v2.4.0")
        data = iep1a_fresh.get("/model-info").json()
        assert data["version_tag"] == "v2.4.0"

    def test_version_tag_none_when_reload_called_without_tag(self, iep1a_fresh: TestClient) -> None:
        import services.iep1a.app.inference as inf_mod
        inf_mod.reload_models()
        data = iep1a_fresh.get("/model-info").json()
        assert data["version_tag"] is None

    def test_version_tag_empty_string_treated_as_none(self, iep1a_fresh: TestClient) -> None:
        import services.iep1a.app.inference as inf_mod
        inf_mod.reload_models(version_tag="")
        data = iep1a_fresh.get("/model-info").json()
        assert data["version_tag"] is None


# ── iep1b ─────────────────────────────────────────────────────────────────────


@pytest.fixture()
def iep1b_fresh():
    """
    Return a TestClient for a mini iep1b app with a clean reload-tracking state.
    """
    import services.iep1b.app.inference as inf_mod

    inf_mod._loaded_models.clear()
    inf_mod._reload_count = 0
    inf_mod._last_reload_wall = None
    inf_mod._last_version_tag = None

    app = FastAPI()

    from services.iep1b.app.main import model_info
    app.get("/model-info")(model_info)

    return TestClient(app, raise_server_exceptions=False)


class TestIep1bModelInfo:
    def test_endpoint_returns_200(self, iep1b_fresh: TestClient) -> None:
        resp = iep1b_fresh.get("/model-info")
        assert resp.status_code == 200

    def test_service_is_iep1b(self, iep1b_fresh: TestClient) -> None:
        data = iep1b_fresh.get("/model-info").json()
        assert data["service"] == "iep1b"

    def test_version_tag_is_null(self, iep1b_fresh: TestClient) -> None:
        data = iep1b_fresh.get("/model-info").json()
        assert data["version_tag"] is None

    def test_reload_count_starts_at_zero(self, iep1b_fresh: TestClient) -> None:
        data = iep1b_fresh.get("/model-info").json()
        assert data["reload_count"] == 0

    def test_reloaded_since_startup_false_initially(self, iep1b_fresh: TestClient) -> None:
        data = iep1b_fresh.get("/model-info").json()
        assert data["reloaded_since_startup"] is False

    def test_loaded_models_lists_all_materials(self, iep1b_fresh: TestClient) -> None:
        data = iep1b_fresh.get("/model-info").json()
        materials = {entry["material"] for entry in data["loaded_models"]}
        assert materials == {"book", "newspaper", "microfilm"}

    def test_reload_count_increments_after_reload(self, iep1b_fresh: TestClient) -> None:
        import services.iep1b.app.inference as inf_mod
        inf_mod.reload_models()
        data = iep1b_fresh.get("/model-info").json()
        assert data["reload_count"] == 1

    def test_reloaded_since_startup_true_after_reload(self, iep1b_fresh: TestClient) -> None:
        import services.iep1b.app.inference as inf_mod
        inf_mod.reload_models()
        data = iep1b_fresh.get("/model-info").json()
        assert data["reloaded_since_startup"] is True

    def test_last_reload_at_set_after_reload(self, iep1b_fresh: TestClient) -> None:
        import services.iep1b.app.inference as inf_mod
        inf_mod.reload_models()
        data = iep1b_fresh.get("/model-info").json()
        assert data["last_reload_at"] is not None
        from datetime import datetime
        datetime.fromisoformat(data["last_reload_at"])

    def test_mock_mode_false_by_default(self, iep1b_fresh: TestClient, monkeypatch) -> None:
        monkeypatch.delenv("IEP1B_MOCK_MODE", raising=False)
        data = iep1b_fresh.get("/model-info").json()
        assert data["mock_mode"] is False

    def test_version_tag_set_after_reload_with_tag(self, iep1b_fresh: TestClient) -> None:
        import services.iep1b.app.inference as inf_mod
        inf_mod.reload_models(version_tag="v1.5.0")
        data = iep1b_fresh.get("/model-info").json()
        assert data["version_tag"] == "v1.5.0"

    def test_version_tag_none_before_reload(self, iep1b_fresh: TestClient) -> None:
        data = iep1b_fresh.get("/model-info").json()
        assert data["version_tag"] is None

    def test_version_tag_updated_on_subsequent_reload(self, iep1b_fresh: TestClient) -> None:
        import services.iep1b.app.inference as inf_mod
        inf_mod.reload_models(version_tag="v1.5.0")
        inf_mod.reload_models(version_tag="v1.6.0")
        data = iep1b_fresh.get("/model-info").json()
        assert data["version_tag"] == "v1.6.0"

    def test_version_tag_empty_string_treated_as_none(self, iep1b_fresh: TestClient) -> None:
        import services.iep1b.app.inference as inf_mod
        inf_mod.reload_models(version_tag="")
        data = iep1b_fresh.get("/model-info").json()
        assert data["version_tag"] is None
