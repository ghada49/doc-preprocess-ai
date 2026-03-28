"""
services/iep2a/app/backends/base.py
-------------------------------------
Abstract base class for IEP2A layout detection backends.

Each backend must implement:
    initialize()  — load model weights / warm up; called once at startup.
    is_ready()    — True when the backend can serve inference requests.
    detect()      — run layout detection; returns BackendResult.

BackendResult carries the normalized output that detect.py uses to
assemble a LayoutDetectResponse. All backends normalize their raw outputs
into the canonical Region / ColumnStructure schema before returning.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from shared.schemas.layout import ColumnStructure, Region


class ImageLoadError(Exception):
    """Raised when a backend cannot load the request image from image_uri."""


@dataclass
class BackendResult:
    """Normalized output from any IEP2A layout backend."""

    regions: list[Region]
    column_structure: ColumnStructure | None
    model_version: str
    #: Value for LayoutDetectResponse.detector_type
    detector_type: Literal["detectron2", "doclayout_yolo", "paddleocr"]
    warnings: list[str] = field(default_factory=list)


class LayoutBackend(ABC):
    """Abstract IEP2A layout detection backend."""

    @abstractmethod
    def initialize(self) -> None:
        """
        Load and warm up the backend.

        Called once at service startup (inside the FastAPI lifespan).
        Implementations must be idempotent (safe to call twice without side-effects).
        Should raise RuntimeError on unrecoverable initialization failure so that
        /ready reflects the failure correctly.
        """

    @abstractmethod
    def is_ready(self) -> bool:
        """Return True when the backend can serve inference requests."""

    @abstractmethod
    def detect(self, image_uri: str) -> BackendResult:
        """
        Run layout detection on the image identified by image_uri.

        Args:
            image_uri: Storage URI understood by shared.io.storage.

        Returns:
            BackendResult with canonical regions and postprocessed output.

        Raises:
            RuntimeError: when the backend is not ready.
            ImageLoadError: when image_uri cannot be loaded.
            Exception:    on image load or inference failure (caller wraps in HTTP 500).
        """
