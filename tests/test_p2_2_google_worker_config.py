"""
tests/test_p2_2_google_worker_config.py
----------------------------------------
P2.2 — Focused unit tests for Google Document AI config loading and
startup validation in the EEP worker.

Tests are fully isolated via monkeypatching — no real GCP credentials or
filesystem state required.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.eep_worker.app.google_config import (
    GoogleWorkerState,
    _parse_bool,
    _parse_int,
    get_google_worker_state,
    initialize_google,
    load_google_config,
    validate_google_startup,
)

# ── _parse_bool ────────────────────────────────────────────────────────────────


class TestParseBool:
    def test_true_values(self) -> None:
        for v in ("true", "True", "TRUE", "1", "yes", "YES"):
            assert _parse_bool(v, default=False) is True

    def test_false_values(self) -> None:
        for v in ("false", "False", "0", "no", "off", ""):
            assert _parse_bool(v, default=True) is False

    def test_none_returns_default_true(self) -> None:
        assert _parse_bool(None, default=True) is True

    def test_none_returns_default_false(self) -> None:
        assert _parse_bool(None, default=False) is False


# ── _parse_int ─────────────────────────────────────────────────────────────────


class TestParseInt:
    def test_valid_integer(self) -> None:
        assert _parse_int("90", default=30, name="X") == 90

    def test_none_returns_default(self) -> None:
        assert _parse_int(None, default=42, name="X") == 42

    def test_invalid_returns_default_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            result = _parse_int("notanint", default=5, name="MY_VAR")
        assert result == 5
        assert "MY_VAR" in caplog.text

    def test_whitespace_stripped(self) -> None:
        assert _parse_int("  60  ", default=0, name="X") == 60


# ── load_google_config ─────────────────────────────────────────────────────────


class TestLoadGoogleConfig:
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Remove all GOOGLE_* vars from the test environment."""
        for key in list(os.environ.keys()):
            if key.startswith("GOOGLE_"):
                monkeypatch.delenv(key, raising=False)

    def test_defaults_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        cfg = load_google_config()
        assert cfg.enabled is False
        assert cfg.project_id == ""
        assert cfg.location == "us"
        assert cfg.processor_id_layout == ""
        assert cfg.processor_id_cleanup == ""
        assert cfg.timeout_layout_seconds == 90
        assert cfg.timeout_cleanup_seconds == 120
        assert cfg.max_retries == 2
        assert cfg.fallback_on_timeout is True
        assert cfg.credentials_file == "/var/secrets/google/key.json"

    def test_enabled_true_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_ENABLED", "true")
        cfg = load_google_config()
        assert cfg.enabled is True

    def test_all_settings_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_PROJECT_ID", "my-project")
        monkeypatch.setenv("GOOGLE_LOCATION", "eu")
        monkeypatch.setenv("GOOGLE_PROCESSOR_ID_LAYOUT", "abc123")
        monkeypatch.setenv("GOOGLE_PROCESSOR_ID_CLEANUP", "def456")
        monkeypatch.setenv("GOOGLE_TIMEOUT_LAYOUT_SECONDS", "45")
        monkeypatch.setenv("GOOGLE_TIMEOUT_CLEANUP_SECONDS", "60")
        monkeypatch.setenv("GOOGLE_MAX_RETRIES", "3")
        monkeypatch.setenv("GOOGLE_FALLBACK_ON_TIMEOUT", "false")
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", "/tmp/key.json")

        cfg = load_google_config()
        assert cfg.enabled is True
        assert cfg.project_id == "my-project"
        assert cfg.location == "eu"
        assert cfg.processor_id_layout == "abc123"
        assert cfg.processor_id_cleanup == "def456"
        assert cfg.timeout_layout_seconds == 45
        assert cfg.timeout_cleanup_seconds == 60
        assert cfg.max_retries == 3
        assert cfg.fallback_on_timeout is False
        assert cfg.credentials_file == "/tmp/key.json"

    def test_credentials_path_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", "/custom/path/key.json")
        cfg = load_google_config()
        assert cfg.credentials_file == "/custom/path/key.json"

    def test_malformed_int_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_TIMEOUT_LAYOUT_SECONDS", "banana")
        cfg = load_google_config()
        assert cfg.timeout_layout_seconds == 90  # default


# ── validate_google_startup ────────────────────────────────────────────────────


class TestValidateGoogleStartup:
    def _base_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set up a fully valid Google env."""
        monkeypatch.setenv("GOOGLE_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_PROJECT_ID", "my-project")
        monkeypatch.setenv("GOOGLE_LOCATION", "us")
        monkeypatch.setenv("GOOGLE_PROCESSOR_ID_LAYOUT", "proc-layout-001")
        monkeypatch.setenv("GOOGLE_PROCESSOR_ID_CLEANUP", "")
        monkeypatch.setenv("GOOGLE_TIMEOUT_LAYOUT_SECONDS", "90")
        monkeypatch.setenv("GOOGLE_TIMEOUT_CLEANUP_SECONDS", "120")
        monkeypatch.setenv("GOOGLE_MAX_RETRIES", "2")
        monkeypatch.setenv("GOOGLE_FALLBACK_ON_TIMEOUT", "true")
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", "/var/secrets/google/key.json")

    def test_disabled_returns_disabled_state(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("GOOGLE_ENABLED", "false")
        import logging

        with caplog.at_level(logging.INFO):
            state = validate_google_startup()
        assert state.enabled is False
        assert state.client is None
        assert "disabled" in caplog.text.lower()

    def test_disabled_state_has_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_ENABLED", "false")
        state = validate_google_startup()
        assert state.config is not None
        assert state.config.enabled is False

    def test_enabled_missing_project_id_disables(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Config validation failure (missing project_id) disables Google."""
        monkeypatch.setenv("GOOGLE_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_PROJECT_ID", "")
        monkeypatch.setenv("GOOGLE_PROCESSOR_ID_LAYOUT", "proc-001")
        import logging

        with caplog.at_level(logging.WARNING):
            state = validate_google_startup()
        assert state.enabled is False
        assert "project_id" in caplog.text.lower() or "config" in caplog.text.lower()

    def test_enabled_missing_processor_id_disables(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Config validation failure (missing processor_id_layout) disables Google."""
        monkeypatch.setenv("GOOGLE_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_PROJECT_ID", "my-project")
        monkeypatch.setenv("GOOGLE_PROCESSOR_ID_LAYOUT", "")
        import logging

        with caplog.at_level(logging.WARNING):
            state = validate_google_startup()
        assert state.enabled is False
        assert "processor_id_layout" in caplog.text.lower() or "config" in caplog.text.lower()

    def test_credentials_file_missing_disables(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing credentials file disables Google with a clear warning."""
        self._base_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", "/nonexistent/path/key.json")
        import logging

        with caplog.at_level(logging.WARNING):
            state = validate_google_startup()
        assert state.enabled is False
        assert "/nonexistent/path/key.json" in caplog.text or "NOT FOUND" in caplog.text

    def test_credentials_file_missing_logs_mount_hint(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warning log mentions how to fix the credentials mount."""
        self._base_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", "/does/not/exist.json")
        import logging

        with caplog.at_level(logging.WARNING):
            validate_google_startup()
        # Should mention the K8s Secret name and/or mount path in the warning
        assert "google-documentai-sa" in caplog.text or "/var/secrets/google" in caplog.text

    def test_client_init_failure_disables(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Client initialization exception disables Google gracefully."""
        # Write a real (but dummy) credentials file so the path check passes
        creds_file = tmp_path / "key.json"
        creds_file.write_text("{}")
        self._base_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", str(creds_file))

        import logging

        with (
            patch(
                "services.eep_worker.app.google_config.CallGoogleDocumentAI",
                side_effect=RuntimeError("simulated init failure"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            state = validate_google_startup()

        assert state.enabled is False
        assert "simulated init failure" in caplog.text

    def test_fully_valid_config_returns_enabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When all checks pass, state.enabled is True and client is set."""
        creds_file = tmp_path / "key.json"
        creds_file.write_text("{}")
        self._base_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", str(creds_file))

        mock_client = MagicMock()
        with patch(
            "services.eep_worker.app.google_config.CallGoogleDocumentAI",
            return_value=mock_client,
        ):
            state = validate_google_startup()

        assert state.enabled is True
        assert state.client is mock_client
        assert state.config is not None
        assert state.config.project_id == "my-project"

    def test_fully_valid_logs_project_and_location(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Non-secret settings are logged when Google is enabled."""
        creds_file = tmp_path / "key.json"
        creds_file.write_text("{}")
        self._base_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", str(creds_file))

        import logging

        with (
            patch(
                "services.eep_worker.app.google_config.CallGoogleDocumentAI",
                return_value=MagicMock(),
            ),
            caplog.at_level(logging.INFO),
        ):
            validate_google_startup()

        assert "my-project" in caplog.text
        assert "us" in caplog.text

    def test_credentials_content_never_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Secret file contents must never appear in log output."""
        secret_content = '{"private_key": "SUPER_SECRET_KEY_VALUE_12345"}'
        creds_file = tmp_path / "key.json"
        creds_file.write_text(secret_content)
        self._base_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", str(creds_file))

        import logging

        with (
            patch(
                "services.eep_worker.app.google_config.CallGoogleDocumentAI",
                return_value=MagicMock(),
            ),
            caplog.at_level(logging.DEBUG),
        ):
            validate_google_startup()

        assert "SUPER_SECRET_KEY_VALUE_12345" not in caplog.text
        assert "private_key" not in caplog.text


# ── GoogleWorkerState dataclass ────────────────────────────────────────────────


class TestGoogleWorkerState:
    def test_disabled_state_construction(self) -> None:
        state = GoogleWorkerState(enabled=False, config=None, client=None)
        assert state.enabled is False
        assert state.config is None
        assert state.client is None

    def test_enabled_state_construction(self) -> None:
        mock_config = MagicMock()
        mock_client = MagicMock()
        state = GoogleWorkerState(enabled=True, config=mock_config, client=mock_client)
        assert state.enabled is True
        assert state.config is mock_config
        assert state.client is mock_client


# ── initialize_google / get_google_worker_state ───────────────────────────────


class TestInitializeGoogleAndAccessor:
    """
    Verify that initialize_google() stores its result where
    get_google_worker_state() can retrieve it, and that the two are the
    only coupling between main.py and downstream adjudication code.
    """

    def test_get_returns_disabled_before_init(self) -> None:
        """Before initialize_google() the accessor returns a disabled state."""
        import services.eep_worker.app.google_config as gc

        original = gc._state
        try:
            gc._state = gc._DISABLED_DEFAULT
            state = get_google_worker_state()
            assert state.enabled is False
        finally:
            gc._state = original

    def test_initialize_stores_validated_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """initialize_google() stores the validate_google_startup() result."""
        creds_file = tmp_path / "key.json"
        creds_file.write_text("{}")
        monkeypatch.setenv("GOOGLE_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_PROJECT_ID", "proj")
        monkeypatch.setenv("GOOGLE_PROCESSOR_ID_LAYOUT", "proc")
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", str(creds_file))

        import services.eep_worker.app.google_config as gc

        original = gc._state
        try:
            with patch(
                "services.eep_worker.app.google_config.CallGoogleDocumentAI",
                return_value=MagicMock(),
            ):
                initialize_google()
            stored = get_google_worker_state()
            assert stored.enabled is True
            assert stored.config is not None
        finally:
            gc._state = original  # restore so other tests are unaffected

    def test_initialize_stores_disabled_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """initialize_google() stores a disabled state when config is invalid."""
        monkeypatch.setenv("GOOGLE_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_PROJECT_ID", "")  # missing — will fail validation

        import services.eep_worker.app.google_config as gc

        original = gc._state
        try:
            initialize_google()
            assert get_google_worker_state().enabled is False
        finally:
            gc._state = original
