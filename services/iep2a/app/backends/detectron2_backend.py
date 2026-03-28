"""
services/iep2a/app/backends/detectron2_backend.py
---------------------------------------------------
IEP2A Detectron2 backend.

Wraps the existing model.py / inference.py / postprocess.py machinery with
no behavior change.  All Detectron2-specific env vars remain intact:

    IEP2A_USE_REAL_MODEL         "true" to enable real inference
    IEP2A_WEIGHTS_PATH           baked-in checkpoint (default: /opt/models/iep2a/model_final.pth)
    IEP2A_LOCAL_WEIGHTS_PATH     mounted local dev override
    IEP2A_CONFIG_PATH            Detectron2 config override
    IEP2A_SCORE_THRESH           detection confidence threshold (default: 0.5)
    IEP2A_DEVICE                 "cuda" or "cpu"
    IEP2A_NUM_CLASSES            number of classes in the weights (default: 5)
    IEP2A_MODEL_VERSION          optional override; sidecar file is authoritative

This backend is selected when IEP2A_LAYOUT_BACKEND=detectron2 (default).
"""

from __future__ import annotations

import logging

from .base import BackendResult, ImageLoadError, LayoutBackend

logger = logging.getLogger(__name__)


class Detectron2Backend(LayoutBackend):
    """
    IEP2A Detectron2 layout backend.

    Delegates all model loading and inference to the pre-existing
    model.py / inference.py / postprocess.py modules unchanged.
    """

    def initialize(self) -> None:
        """Warm up the Detectron2 predictor at startup."""
        from services.iep2a.app.model import initialize_model_if_configured

        logger.info("IEP2A backend: initializing Detectron2")
        initialize_model_if_configured()

    def is_ready(self) -> bool:
        """Return True only after a successful Detectron2 model load."""
        from services.iep2a.app.model import is_real_model_loaded

        return is_real_model_loaded()

    def detect(self, image_uri: str) -> BackendResult:
        """Run Detectron2 inference; apply postprocessing; return BackendResult."""
        from services.iep2a.app.inference import (
            PUBLAYNET_CLASS_MAP,
            load_image_from_uri,
            raw_detections_to_regions,
            run_detectron2,
        )
        from services.iep2a.app.model import get_loaded_model_version, get_predictor
        from services.iep2a.app.postprocess import postprocess_regions

        predictor = get_predictor()  # raises RuntimeError if not loaded
        try:
            image = load_image_from_uri(image_uri)
        except Exception as exc:
            raise ImageLoadError(f"Cannot load image from {image_uri!r}: {exc}") from exc
        h, w = image.shape[:2]
        raw = run_detectron2(predictor, image)
        raw_regions = raw_detections_to_regions(raw, PUBLAYNET_CLASS_MAP)
        regions, col_struct = postprocess_regions(
            raw_regions,
            page_width=float(w),
            page_height=float(h),
        )

        return BackendResult(
            regions=regions,
            column_structure=col_struct,
            model_version=get_loaded_model_version(),
            detector_type="detectron2",
        )
