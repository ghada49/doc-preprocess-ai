"""
services.eep.app.google
-----------------------
Google Cloud integration for Document AI layout analysis and artifact cleanup.

Exports:
    CallGoogleDocumentAI    — main client for Google Document AI API calls
    GoogleDocumentAIConfig  — configuration and credentials
    run_google_layout_analysis — public API for IEP2 adjudication fallback
    run_google_cleanup         — public API for IEP1 rescue (stub)
"""

from .document_ai import (
    CallGoogleDocumentAI,
    GoogleDocumentAIConfig,
    run_google_cleanup,
    run_google_layout_analysis,
)

__all__ = [
    "CallGoogleDocumentAI",
    "GoogleDocumentAIConfig",
    "run_google_layout_analysis",
    "run_google_cleanup",
]
