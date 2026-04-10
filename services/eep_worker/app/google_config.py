"""
services/eep_worker/app/google_config.py
-----------------------------------------
P2.2 — Google Document AI configuration loading and startup validation
for the EEP worker.

Reads Google Document AI settings from environment variables.  The service
account credentials are never supplied as inline env-var JSON — they must be
present as a mounted file (never read by this module; only the path is checked).

Kubernetes deployment (ops must apply externally if no K8s manifests in repo):
  Secret name:   google-documentai-sa
  Mount path:    /var/secrets/google          (readOnly: true)
  Key file path: /var/secrets/google/key.json (the GOOGLE_CREDENTIALS_PATH default)

  Example volume/volumeMount snippet (add to eep-worker Deployment):
    volumes:
      - name: google-documentai-sa
        secret:
          secretName: google-documentai-sa
    volumeMounts:
      - name: google-documentai-sa
        mountPath: /var/secrets/google
        readOnly: true

Docker Compose local dev:
  Bind-mount a local service account JSON file:
    volumes:
      - /path/to/local/key.json:/var/secrets/google/key.json:ro
  Or set GOOGLE_CREDENTIALS_PATH to any accessible path.

Exported:
    GoogleWorkerState       — dataclass: enabled flag + loaded config + optional client
    load_google_config      — read env vars → GoogleDocumentAIConfig
    validate_google_startup — full startup check; returns GoogleWorkerState
    initialize_google       — called once from main._lifespan; stores validated state
    get_google_worker_state — accessor for the process-lifetime state

Dependency pattern
------------------
``main.py`` calls ``initialize_google()`` during lifespan — that is the only
coupling between the two modules.  Code that *uses* Google (layout adjudication
in P3) imports ``get_google_worker_state()`` from this module, or better still
receives the ``CallGoogleDocumentAI`` client as an explicit parameter so that
adjudication logic stays fully decoupled from how/where the client was obtained:

    # task runner (P3):
    state = get_google_worker_state()
    result = await run_layout_adjudication(..., google_client=state.client)

    # adjudication function signature:
    async def run_layout_adjudication(
        ...,
        google_client: CallGoogleDocumentAI | None,
    ) -> AdjudicationResult: ...
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from services.eep.app.google.document_ai import CallGoogleDocumentAI, GoogleDocumentAIConfig

logger = logging.getLogger(__name__)

__all__ = [
    "GoogleWorkerState",
    "load_google_config",
    "validate_google_startup",
    "initialize_google",
    "get_google_worker_state",
]


# ── State dataclass ────────────────────────────────────────────────────────────


@dataclass
class GoogleWorkerState:
    """
    Runtime state of the Google Document AI integration in the EEP worker.

    Set once during lifespan by ``initialize_google()`` and readable via
    ``get_google_worker_state()``.  Do not import this state from main.py;
    always obtain it through ``get_google_worker_state()``.

    For adjudication functions, prefer receiving ``state.client`` as an
    explicit parameter rather than calling ``get_google_worker_state()``
    inside the function — this keeps adjudication logic testable in isolation.

    Fields:
        enabled  — True only when config is valid, credentials file exists, and
                   the client was instantiated without error.  False means
                   Google calls must NOT be attempted for this process lifetime.
        config   — The loaded GoogleDocumentAIConfig (always set after startup;
                   only None in the pre-startup default).
        client   — Initialized CallGoogleDocumentAI instance ready for use.
                   None when enabled=False.
    """

    enabled: bool
    config: GoogleDocumentAIConfig | None
    client: CallGoogleDocumentAI | None


# ── Module-level state (owned here, not in main.py) ───────────────────────────

_DISABLED_DEFAULT = GoogleWorkerState(enabled=False, config=None, client=None)
_state: GoogleWorkerState = _DISABLED_DEFAULT


# ── Env-var helpers ────────────────────────────────────────────────────────────


def _parse_bool(raw: str | None, default: bool) -> bool:
    """Return True when *raw* is one of 'true', '1', 'yes' (case-insensitive)."""
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes")


def _parse_int(raw: str | None, default: int, name: str) -> int:
    """Return int(*raw*), falling back to *default* on parse failure."""
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning(
            "google_config: %s=%r is not a valid integer — using default %d",
            name,
            raw,
            default,
        )
        return default


# ── Config loader ──────────────────────────────────────────────────────────────


def load_google_config() -> GoogleDocumentAIConfig:
    """
    Read Google Document AI settings from environment variables.

    All settings have safe defaults (``enabled=False``, empty strings for IDs,
    conservative timeouts) so the worker starts cleanly with no configuration.

    Never reads credentials file contents — only the path is tracked here.

    Environment variables:
        GOOGLE_ENABLED                  — "true"/"1"/"yes" to enable (default: false)
        GOOGLE_PROJECT_ID               — GCP project ID (required when enabled)
        GOOGLE_LOCATION                 — GCP region, e.g. "us" (default: "us")
        GOOGLE_PROCESSOR_ID_LAYOUT      — Layout processor ID (required when enabled)
        GOOGLE_PROCESSOR_ID_CLEANUP     — Cleanup processor ID (optional; future use)
        GOOGLE_TIMEOUT_LAYOUT_SECONDS   — Layout call timeout seconds (default: 90)
        GOOGLE_TIMEOUT_CLEANUP_SECONDS  — Cleanup call timeout seconds (default: 120)
        GOOGLE_MAX_RETRIES              — Max transient-error retries (default: 2)
        GOOGLE_FALLBACK_ON_TIMEOUT      — Legacy config flag; IEP2 now falls back
                                          to local display output on timeout
        GOOGLE_CREDENTIALS_PATH         — Path to service account JSON file
                                          (default: /var/secrets/google/key.json)

    Returns:
        GoogleDocumentAIConfig populated from the current environment.
    """
    return GoogleDocumentAIConfig(
        enabled=_parse_bool(os.environ.get("GOOGLE_ENABLED"), default=False),
        project_id=os.environ.get("GOOGLE_PROJECT_ID", ""),
        location=os.environ.get("GOOGLE_LOCATION", "us"),
        processor_id_layout=os.environ.get("GOOGLE_PROCESSOR_ID_LAYOUT", ""),
        processor_id_cleanup=os.environ.get("GOOGLE_PROCESSOR_ID_CLEANUP", ""),
        timeout_layout_seconds=_parse_int(
            os.environ.get("GOOGLE_TIMEOUT_LAYOUT_SECONDS"),
            default=90,
            name="GOOGLE_TIMEOUT_LAYOUT_SECONDS",
        ),
        timeout_cleanup_seconds=_parse_int(
            os.environ.get("GOOGLE_TIMEOUT_CLEANUP_SECONDS"),
            default=120,
            name="GOOGLE_TIMEOUT_CLEANUP_SECONDS",
        ),
        max_retries=_parse_int(
            os.environ.get("GOOGLE_MAX_RETRIES"),
            default=2,
            name="GOOGLE_MAX_RETRIES",
        ),
        fallback_on_timeout=_parse_bool(os.environ.get("GOOGLE_FALLBACK_ON_TIMEOUT"), default=True),
        credentials_file=os.environ.get("GOOGLE_CREDENTIALS_PATH", "/var/secrets/google/key.json"),
    )


# ── Startup validator + process-lifetime accessor ─────────────────────────────


def validate_google_startup() -> GoogleWorkerState:
    """
    Load and validate Google Document AI configuration at worker startup.

    When Google is enabled, performs three checks in order:
      1. Config fields valid — required IDs present, timeouts positive.
      2. Credentials file exists — path must resolve to a regular file.
      3. Client instantiation succeeds — ``CallGoogleDocumentAI(config)`` must
         not raise.

    On any failure the Google integration is disabled with a clear WARNING log
    so the rest of the worker pipeline continues normally.

    Logs:
      - Whether Google is enabled/disabled
      - Project ID, location, and whether each processor ID is configured
      - Whether the credentials file was found (never logs file contents)
      - Client init outcome and key timeout/retry settings

    Returns:
        GoogleWorkerState with ``enabled=True`` when all checks pass, or
        ``enabled=False`` on any failure.
    """
    config = load_google_config()

    # ── Disabled path ──────────────────────────────────────────────────────────
    if not config.enabled:
        logger.info(
            "Google Document AI: disabled (GOOGLE_ENABLED is not 'true'); "
            "Google fallback will not be available this run"
        )
        return GoogleWorkerState(enabled=False, config=config, client=None)

    # ── Log non-secret settings ────────────────────────────────────────────────
    logger.info(
        "Google Document AI: enabled — project=%r location=%r "
        "layout_processor_configured=%s cleanup_processor_configured=%s",
        config.project_id,
        config.location,
        bool(config.processor_id_layout),
        bool(config.processor_id_cleanup),
    )

    # ── 1. Validate config fields ──────────────────────────────────────────────
    valid, msg = config.validate()
    if not valid:
        logger.warning(
            "Google Document AI: disabling — config validation failed: %s. "
            "Set GOOGLE_PROJECT_ID and GOOGLE_PROCESSOR_ID_LAYOUT, "
            "or set GOOGLE_ENABLED=false to suppress this warning.",
            msg,
        )
        return GoogleWorkerState(enabled=False, config=config, client=None)

    # ── 2. Check credentials file exists ──────────────────────────────────────
    creds_path = config.credentials_file
    creds_found = os.path.isfile(creds_path)
    logger.info(
        "Google Document AI: credentials file %r — %s",
        creds_path,
        "found" if creds_found else "NOT FOUND",
    )
    if not creds_found:
        logger.warning(
            "Google Document AI: disabling — credentials file not found at %r. "
            "K8s: mount Secret 'google-documentai-sa' at /var/secrets/google "
            "(readOnly: true). "
            "Docker Compose: bind-mount a service account JSON to "
            "/var/secrets/google/key.json, or override GOOGLE_CREDENTIALS_PATH.",
            creds_path,
        )
        return GoogleWorkerState(enabled=False, config=config, client=None)

    # ── 3. Instantiate client ──────────────────────────────────────────────────
    try:
        client = CallGoogleDocumentAI(config)
        logger.info(
            "Google Document AI: client ready — "
            "layout_timeout=%ds cleanup_timeout=%ds "
            "max_retries=%d fallback_on_timeout=%s",
            config.timeout_layout_seconds,
            config.timeout_cleanup_seconds,
            config.max_retries,
            config.fallback_on_timeout,
        )
        return GoogleWorkerState(enabled=True, config=config, client=client)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Google Document AI: disabling — client initialization failed: %s",
            exc,
        )
        return GoogleWorkerState(enabled=False, config=config, client=None)


def initialize_google() -> None:
    """
    Run startup validation and store the result as the process-lifetime state.

    Called exactly once from ``services.eep_worker.app.main._lifespan``.
    All other code reads state via ``get_google_worker_state()`` — never by
    importing from ``main``.
    """
    global _state
    _state = validate_google_startup()


def get_google_worker_state() -> GoogleWorkerState:
    """
    Return the Google Document AI worker state for this process.

    Safe to call at any time; returns the disabled default before
    ``initialize_google()`` has been called (e.g. in tests).

    Prefer passing ``state.client`` explicitly to adjudication functions
    rather than calling this inside those functions, so they remain
    independently testable without process-level startup.
    """
    return _state
