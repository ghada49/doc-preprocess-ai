# ruff: noqa: E402

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

torch = pytest.importorskip("torch")

import services.iep1d.app.model as model_mod
import services.iep1d.app.rectify as rectify_mod
from services.iep1d.app.rectify import router
from services.iep1d.app.uvdoc import UVDocConfig, UVDocNet, UVDocRectifier
from shared.middleware import configure_observability


def _gradient_image(height: int = 240, width: int = 320) -> np.ndarray:
    x = np.tile(np.linspace(0, 255, width, dtype=np.uint8), (height, 1))
    y = np.tile(np.linspace(0, 255, height, dtype=np.uint8).reshape(height, 1), (1, width))
    return np.dstack([x, y, 255 - x])


@pytest.fixture
def workspace_tmp_path() -> Iterator[Path]:
    tmp_path = Path.cwd() / "test_tmp" / "iep1d" / uuid4().hex
    tmp_path.mkdir(parents=True, exist_ok=True)
    try:
        yield tmp_path
    finally:
        rmtree(tmp_path, ignore_errors=True)


def _write_tiff(path: Path, image: np.ndarray) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    success, buf = cv2.imencode(".tiff", image)
    assert success, "cv2.imencode failed in test helper"
    path.write_bytes(bytes(buf.tobytes()))
    return f"file://{path.as_posix()}"


def _read_image(uri: str) -> np.ndarray:
    path = Path(uri[len("file://") :])
    raw = path.read_bytes()
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, f"Failed to decode image at {uri!r}"
    return image


def _write_zero_checkpoint(path: Path) -> None:
    model = UVDocNet()
    zero_state = {name: torch.zeros_like(value) for name, value in model.state_dict().items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"model_state": zero_state}, path)
    path.with_suffix(path.suffix + ".version").write_text("uvdoc-zero-test", encoding="utf-8")


def _legacy_checkpoint_state_dict(model: UVDocNet) -> dict[str, torch.Tensor]:
    legacy_state = {}
    for name, value in model.state_dict().items():
        legacy_name = name
        for current, legacy in (
            ("out_point_positions_2d", "out_point_positions2D"),
            ("out_point_positions_3d", "out_point_positions3D"),
            ("bridge_1.", "bridge_1.0."),
            ("bridge_2.", "bridge_2.0."),
            ("bridge_3.", "bridge_3.0."),
        ):
            legacy_name = legacy_name.replace(current, legacy)
        legacy_state[legacy_name] = value.clone()
    return legacy_state


def _build_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        model_mod.initialize_model_if_configured()
        yield

    app = FastAPI(lifespan=lifespan)
    configure_observability(
        app, service_name="iep1d-test", health_checks=[model_mod.is_model_ready]
    )
    app.include_router(router)
    return app


@pytest.fixture(autouse=True)
def _reset_model_state(monkeypatch: pytest.MonkeyPatch) -> None:
    model_mod.reset_for_testing()
    for env_name in (
        "IEP1D_WEIGHTS_PATH",
        "IEP1D_LOCAL_WEIGHTS_PATH",
        "IEP1D_MODEL_VERSION",
        "IEP1D_DEVICE",
    ):
        monkeypatch.delenv(env_name, raising=False)
    yield
    model_mod.reset_for_testing()


class TestReadiness:
    def test_ready_is_503_when_checkpoint_missing(
        self,
        workspace_tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        missing_path = workspace_tmp_path / "missing" / "best_model.pkl"
        monkeypatch.setenv("IEP1D_WEIGHTS_PATH", str(missing_path))
        monkeypatch.setenv("IEP1D_DEVICE", "cpu")

        with TestClient(_build_app()) as client:
            ready = client.get("/ready")
            assert ready.status_code == 503

            status = client.get("/v1/model-status")
            assert status.status_code == 200
            body = status.json()
            assert body["ready"] is False
            assert str(missing_path) in (body["weights_path"] or "")
            assert "not found" in (body["error"] or "")


class TestCheckpointKeyRemapping:
    def test_remap_supports_legacy_uvdoc_checkpoint_naming(self) -> None:
        model = UVDocNet()
        legacy_state = _legacy_checkpoint_state_dict(model)

        remapped = UVDocRectifier._remap_checkpoint_keys(legacy_state)

        assert "bridge_1.0.0.weight" in legacy_state
        assert "bridge_1.0.weight" in remapped
        assert "out_point_positions2D.0.weight" in legacy_state
        assert "out_point_positions_2d.0.weight" in remapped
        assert set(remapped.keys()) == set(model.state_dict().keys())

        model.load_state_dict(remapped, strict=True)


class TestRectifierImplementation:
    def test_chunked_identity_map_preserves_image(self) -> None:
        image = _gradient_image(height=37, width=53)
        y_coords = torch.linspace(-1.0, 1.0, steps=3)
        x_coords = torch.linspace(-1.0, 1.0, steps=4)
        grid_y, grid_x = torch.meshgrid(y_coords, x_coords, indexing="ij")
        identity_map = torch.stack((grid_x, grid_y), dim=0).unsqueeze(0)

        rectified = UVDocRectifier._apply_point_position_map(
            image,
            identity_map,
            chunk_rows=7,
        )

        assert rectified.shape == image.shape
        assert np.allclose(rectified, image, atol=1)

    def test_rectify_resizes_image_before_tensor_conversion(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen_shapes: list[tuple[int, ...]] = []
        original_to_tensor = UVDocRectifier._numpy_bgr_to_tensor

        def spy_to_tensor(image_bgr: np.ndarray) -> torch.Tensor:
            seen_shapes.append(tuple(image_bgr.shape))
            return original_to_tensor(image_bgr)

        def passthrough_remap(
            image_bgr: np.ndarray,
            point_positions_2d: torch.Tensor,
            *,
            chunk_rows: int,
        ) -> np.ndarray:
            return image_bgr

        rectifier = UVDocRectifier(
            UVDocNet(),
            device=torch.device("cpu"),
            config=UVDocConfig(),
        )
        monkeypatch.setattr(UVDocRectifier, "_numpy_bgr_to_tensor", staticmethod(spy_to_tensor))
        monkeypatch.setattr(
            UVDocRectifier, "_apply_point_position_map", staticmethod(passthrough_remap)
        )

        source_image = _gradient_image(height=1200, width=800)
        rectified = rectifier.rectify(source_image)

        assert rectified.shape == source_image.shape
        assert seen_shapes == [(rectifier._config.input_height, rectifier._config.input_width, 3)]


class TestRectifyEndpoint:
    def test_rectify_persists_real_artifact(
        self,
        workspace_tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        checkpoint_path = workspace_tmp_path / "weights" / "best_model.pkl"
        _write_zero_checkpoint(checkpoint_path)
        monkeypatch.setenv("IEP1D_LOCAL_WEIGHTS_PATH", str(checkpoint_path))
        monkeypatch.setenv("IEP1D_DEVICE", "cpu")

        input_uri = _write_tiff(
            workspace_tmp_path / "jobs" / "job-123" / "output" / "1.tiff",
            _gradient_image(),
        )

        payload = {
            "job_id": "job-123",
            "page_number": 1,
            "image_uri": input_uri,
            "material_type": "book",
        }

        with TestClient(_build_app()) as client:
            assert client.get("/ready").status_code == 200

            response = client.post("/v1/rectify", json=payload)
            assert response.status_code == 200
            body = response.json()

        rectified_uri = body["rectified_image_uri"]
        rectified_path = Path(rectified_uri[len("file://") :])
        assert rectified_uri != input_uri
        assert "/rectified/" in rectified_uri
        assert rectified_path.exists()

        source_image = _read_image(input_uri)
        rectified_image = _read_image(rectified_uri)
        assert rectified_image.shape == source_image.shape
        assert not np.array_equal(rectified_image, source_image)
        assert 0.0 <= body["rectification_confidence"] <= 1.0
        assert body["processing_time_ms"] >= 0.0
        assert isinstance(body["warnings"], list)

    def test_rectify_returns_failure_when_model_inference_raises(
        self,
        workspace_tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        checkpoint_path = workspace_tmp_path / "weights" / "best_model.pkl"
        _write_zero_checkpoint(checkpoint_path)
        monkeypatch.setenv("IEP1D_LOCAL_WEIGHTS_PATH", str(checkpoint_path))
        monkeypatch.setenv("IEP1D_DEVICE", "cpu")

        input_uri = _write_tiff(
            workspace_tmp_path / "input" / "page.tiff",
            _gradient_image(),
        )

        class BrokenRectifier:
            def rectify(self, image: np.ndarray) -> np.ndarray:
                raise RuntimeError("synthetic inference failure")

        payload = {
            "job_id": "job-123",
            "page_number": 1,
            "image_uri": input_uri,
            "material_type": "book",
        }

        with TestClient(_build_app()) as client:
            monkeypatch.setattr(rectify_mod, "get_rectifier", lambda: BrokenRectifier())
            response = client.post("/v1/rectify", json=payload)

        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["error_code"] == "rectification_failed"
        assert "synthetic inference failure" in detail["error_message"]
