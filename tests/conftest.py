"""
tests/conftest.py
------------------
Shared pytest fixtures for the EEP test suite.

_bypass_require_user
---------------------
Opt-in (NOT autouse) fixture. Sets app.dependency_overrides[require_user]
to a mock admin user so that Phase-5 tests that call the main FastAPI app
directly do not fail due to authentication added in Packet 7.2.

Usage: each Phase-5 test module that imports the main app declares:
    pytestmark = pytest.mark.usefixtures("_bypass_require_user")

This keeps the bypass targeted — it runs only for the 6 modules that need
it, not for the full test suite.  P7 RBAC tests intentionally omit this
mark so that real auth enforcement is exercised.

Scope: function (applied once per test function).  P5 test classes call
app.dependency_overrides.clear() in teardown_method; the fixture
re-installs the override before the next test because it runs before
setup_method.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def _bypass_require_user() -> None:
    """Bypass require_user on the main app for Phase-5 tests that use it directly."""
    # Lazy imports keep conftest startup overhead minimal.
    from services.eep.app.auth import CurrentUser, require_user
    from services.eep.app.main import app

    _admin = CurrentUser(user_id="test-admin", role="admin")
    app.dependency_overrides[require_user] = lambda: _admin
    yield  # type: ignore[misc]
    # P5 teardown_method calls app.dependency_overrides.clear() before this
    # runs; pop is a safe no-op if the key is already gone.
    app.dependency_overrides.pop(require_user, None)
