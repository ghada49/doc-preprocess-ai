"""
services/iep2a/app/backends
----------------------------
IEP2A pluggable layout detection backends.

Selected at startup via the IEP2A_LAYOUT_BACKEND environment variable:
    detectron2 (default)  — existing Detectron2 Faster R-CNN implementation
    paddleocr             — PaddleOCR PP-DocLayoutV2 layout analysis

Public surface:
    LayoutBackend   — abstract base class (base.py)
    BackendResult   — normalized result dataclass (base.py)
    initialize_backend        — call once at startup (factory.py)
    get_active_backend        — get the initialized backend instance (factory.py)
    get_active_backend_optional — None-safe version for readiness checks (factory.py)
"""

from .base import BackendResult, LayoutBackend
from .factory import get_active_backend, get_active_backend_optional, initialize_backend

__all__ = [
    "BackendResult",
    "LayoutBackend",
    "initialize_backend",
    "get_active_backend",
    "get_active_backend_optional",
]
