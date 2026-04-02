"""
tests/test_iep2a_backends.py
------------------------------
Focused tests for the IEP2A pluggable backend architecture.

Coverage:
  - backend selection via IEP2A_LAYOUT_BACKEND
  - conservative PP-DocLayoutV2 label normalization
  - real score propagation into Region.confidence
  - no fake-confidence behavior on missing scores
  - readiness failure when Paddle backend initialization fails
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient


class TestFactoryBackendSelection:
    def setup_method(self) -> None:
        import services.iep2a.app.backends.factory as factory

        factory.reset_for_testing()

    def teardown_method(self) -> None:
        import services.iep2a.app.backends.factory as factory

        factory.reset_for_testing()

    def test_factory_defaults_to_paddleocr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "true")
        monkeypatch.delenv("IEP2A_LAYOUT_BACKEND", raising=False)

        import services.iep2a.app.backends.factory as factory
        from services.iep2a.app.backends.paddleocr_backend import PaddleOCRBackend

        with patch.object(PaddleOCRBackend, "initialize", return_value=None):
            factory.initialize_backend()

        assert isinstance(factory.get_active_backend_optional(), PaddleOCRBackend)

    def test_factory_selects_paddleocr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "true")
        monkeypatch.setenv("IEP2A_LAYOUT_BACKEND", "paddleocr")

        import services.iep2a.app.backends.factory as factory
        from services.iep2a.app.backends.paddleocr_backend import PaddleOCRBackend

        with patch.object(PaddleOCRBackend, "initialize", return_value=None):
            factory.initialize_backend()

        assert isinstance(factory.get_active_backend_optional(), PaddleOCRBackend)

    def test_factory_rejects_unknown_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "true")
        monkeypatch.setenv("IEP2A_LAYOUT_BACKEND", "unknown_backend")

        import services.iep2a.app.backends.factory as factory

        with pytest.raises(ValueError, match="Unknown IEP2A_LAYOUT_BACKEND"):
            factory.initialize_backend()


class TestPaddleLabelNormalization:
    def test_expected_labels_map_conservatively(self) -> None:
        from services.iep2a.app.backends.paddleocr_backend import PADDLE_CLASS_MAP
        from shared.schemas.layout import RegionType

        assert PADDLE_CLASS_MAP["text"] == RegionType.text_block
        assert PADDLE_CLASS_MAP["paragraph_title"] == RegionType.title
        assert PADDLE_CLASS_MAP["document_title"] == RegionType.title
        assert PADDLE_CLASS_MAP["table"] == RegionType.table
        assert PADDLE_CLASS_MAP["image"] == RegionType.image
        assert PADDLE_CLASS_MAP["figure"] == RegionType.image
        assert PADDLE_CLASS_MAP["chart"] == RegionType.image
        assert PADDLE_CLASS_MAP["figure_title"] == RegionType.caption
        assert PADDLE_CLASS_MAP["figure_caption"] == RegionType.caption
        assert PADDLE_CLASS_MAP["table_caption"] == RegionType.caption
        assert PADDLE_CLASS_MAP["image_caption"] == RegionType.caption

    def test_unknown_labels_are_not_guessed(self) -> None:
        from services.iep2a.app.backends.paddleocr_backend import PADDLE_CLASS_MAP, _normalize_label

        assert _normalize_label("sidebar text") not in PADDLE_CLASS_MAP
        assert _normalize_label("footer") not in PADDLE_CLASS_MAP
        assert _normalize_label("abstract") not in PADDLE_CLASS_MAP


class TestPaddleScoreHandling:
    def test_collect_detections_uses_real_scores(self) -> None:
        from services.iep2a.app.backends.paddleocr_backend import _collect_detections

        predictions = [
            {
                "res": {
                    "boxes": [
                        {
                            "label": "text",
                            "score": 0.73,
                            "coordinate": [100.0, 100.0, 900.0, 900.0],
                        }
                    ]
                }
            }
        ]

        detections, warnings = _collect_detections(predictions)

        assert warnings == []
        assert detections == [("text", (100.0, 100.0, 900.0, 900.0), 0.73)]

    def test_missing_score_is_dropped_with_warning(self) -> None:
        from services.iep2a.app.backends.paddleocr_backend import _collect_detections

        predictions = [
            {
                "res": {
                    "boxes": [
                        {
                            "label": "text",
                            "score": 0.73,
                            "coordinate": [100.0, 100.0, 900.0, 900.0],
                        },
                        {
                            "label": "table",
                            "coordinate": [120.0, 120.0, 880.0, 880.0],
                        },
                    ]
                }
            }
        ]

        detections, warnings = _collect_detections(predictions)

        assert detections == [("text", (100.0, 100.0, 900.0, 900.0), 0.73)]
        assert any("missing/invalid scores" in warning for warning in warnings)

    def test_all_scoreless_boxes_fail_clearly(self) -> None:
        from services.iep2a.app.backends.paddleocr_backend import _collect_detections

        predictions = [
            {
                "res": {
                    "boxes": [
                        {
                            "label": "text",
                            "coordinate": [100.0, 100.0, 900.0, 900.0],
                        }
                    ]
                }
            }
        ]

        with pytest.raises(RuntimeError, match="none had valid canonical labels"):
            _collect_detections(predictions)


class TestPaddleBackendDetect:
    def _make_backend(self) -> Any:
        from services.iep2a.app.backends.paddleocr_backend import PaddleOCRBackend

        backend = PaddleOCRBackend()
        backend._ready = True
        backend._model_version = "pp-doclayoutv2-test"
        return backend

    def test_detect_propagates_real_score_to_region_confidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend = self._make_backend()
        backend._engine = SimpleNamespace(
            predict=lambda *_args, **_kwargs: iter(
                [
                    {
                        "res": {
                            "boxes": [
                                {
                                    "label": "text",
                                    "score": 0.73,
                                    "coordinate": [100.0, 100.0, 900.0, 900.0],
                                }
                            ]
                        }
                    }
                ]
            )
        )

        monkeypatch.setattr(
            "services.iep2a.app.inference.load_image_from_uri",
            lambda _uri: np.zeros((1000, 1000, 3), dtype=np.uint8),
        )

        result = backend.detect("file:///tmp/layout.png")

        assert result.detector_type == "paddleocr_pp_doclayout_v2"
        assert result.model_version == "pp-doclayoutv2-test"
        assert result.warnings == []
        assert len(result.regions) == 1
        assert result.regions[0].confidence == pytest.approx(0.73)

    def test_detect_surfaces_warning_for_dropped_scoreless_box(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend = self._make_backend()
        backend._engine = SimpleNamespace(
            predict=lambda *_args, **_kwargs: iter(
                [
                    {
                        "res": {
                            "boxes": [
                                {
                                    "label": "text",
                                    "score": 0.73,
                                    "coordinate": [100.0, 100.0, 900.0, 900.0],
                                },
                                {
                                    "label": "table",
                                    "coordinate": [120.0, 120.0, 880.0, 880.0],
                                },
                            ]
                        }
                    }
                ]
            )
        )

        monkeypatch.setattr(
            "services.iep2a.app.inference.load_image_from_uri",
            lambda _uri: np.zeros((1000, 1000, 3), dtype=np.uint8),
        )

        result = backend.detect("file:///tmp/layout.png")

        assert len(result.regions) == 1
        assert any("missing/invalid scores" in warning for warning in result.warnings)


class TestPaddleReadinessFailure:
    def teardown_method(self) -> None:
        import services.iep2a.app.backends.factory as factory

        factory.reset_for_testing()

    def test_ready_returns_503_when_paddle_backend_init_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import services.iep2a.app.backends.factory as factory
        from services.iep2a.app.backends.paddleocr_backend import PaddleOCRBackend
        from services.iep2a.app.main import app as iep2a_app

        factory.reset_for_testing()
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "true")
        monkeypatch.setenv("IEP2A_LAYOUT_BACKEND", "paddleocr")

        def _fail_initialize(self: Any) -> None:
            self._ready = False
            self._init_error = RuntimeError("paddle init failed")
            raise RuntimeError("paddle init failed")

        with patch.object(PaddleOCRBackend, "initialize", _fail_initialize):
            with TestClient(iep2a_app, raise_server_exceptions=False) as client:
                response = client.get("/ready")

        assert response.status_code == 503
        assert response.json() == {"status": "not_ready"}
