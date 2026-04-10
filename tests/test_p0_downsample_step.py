"""
tests/test_p0_downsample_step.py
---------------------------------
Pre-IEP0 downsampling stage tests.

Covers:
  1. Image with any dimension > 2096 is resized so all dims <= 2096.
  2. Image with both dims <= 2096 is stored at original resolution (scale=1.0).
  3. Image exactly at the 2096 boundary is not resized.
  4. Aspect ratio is preserved (within integer rounding) for wide and tall images.
  5. Source artifact URI is never written to — only output URI receives a write.
  6. All DownsampleResult metadata fields are populated and correct.
  7. scale_factor matches the actual ratio applied.
  8. DownsampleResult is serialisable via dataclasses.asdict().
  9. Written TIFF is decodable and has the expected dimensions.
  10. Exceptions from storage.put_bytes() propagate unchanged.
  11. Custom max_dimension_px overrides the 2096 default.
  12. cv2.imencode failure raises ValueError.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from services.eep_worker.app.downsample_step import DownsampleResult, run_downsample_step
from shared.io.storage import LocalFileBackend


# ── helpers ─────────────────────────────────────────────────────────────────────


def _make_image(h: int, w: int) -> np.ndarray:
    """Return a solid-grey uint8 BGR image of the requested size."""
    return np.full((h, w, 3), 128, dtype=np.uint8)


def _file_uri(path: Path) -> str:
    return f"file://{path}"


# ── Resize / no-resize decision ─────────────────────────────────────────────────


class TestResizeDecision:
    def test_wide_image_is_resized(self, tmp_path: Path) -> None:
        """Width > 2096 triggers a resize so new_width <= 2096."""
        img = _make_image(h=1000, w=4000)
        out_uri = _file_uri(tmp_path / "out.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src/page.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        assert result.downsampled_width <= 2096
        assert result.scale_factor < 1.0

    def test_tall_image_is_resized(self, tmp_path: Path) -> None:
        """Height > 2096 triggers a resize so new_height <= 2096."""
        img = _make_image(h=5000, w=800)
        out_uri = _file_uri(tmp_path / "out.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src/page.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        assert result.downsampled_height <= 2096
        assert result.scale_factor < 1.0

    def test_both_dims_within_limit_no_resize(self, tmp_path: Path) -> None:
        """Image with both dims <= 2096 is stored at original resolution."""
        img = _make_image(h=800, w=600)
        out_uri = _file_uri(tmp_path / "out.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src/page.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        assert result.scale_factor == 1.0
        assert result.downsampled_width == 600
        assert result.downsampled_height == 800
        assert result.original_width == 600
        assert result.original_height == 800

    def test_image_exactly_at_limit_not_resized(self, tmp_path: Path) -> None:
        """Image with max dim exactly equal to 2096 is not resized (scale == 1.0)."""
        img = _make_image(h=2096, w=1500)
        out_uri = _file_uri(tmp_path / "out.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src/page.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        assert result.scale_factor == 1.0
        assert result.downsampled_height == 2096
        assert result.downsampled_width == 1500

    def test_resized_dims_never_exceed_limit(self, tmp_path: Path) -> None:
        """After resize, both width and height must be <= max_dimension_px."""
        img = _make_image(h=3000, w=4000)
        out_uri = _file_uri(tmp_path / "out.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src/page.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        assert result.downsampled_width <= 2096
        assert result.downsampled_height <= 2096


# ── Aspect ratio ─────────────────────────────────────────────────────────────────


class TestAspectRatio:
    """Aspect ratio must be preserved within integer-rounding tolerance."""

    def test_aspect_ratio_preserved_wide(self, tmp_path: Path) -> None:
        orig_h, orig_w = 1000, 4000
        img = _make_image(h=orig_h, w=orig_w)
        out_uri = _file_uri(tmp_path / "out.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        orig_ratio = orig_w / orig_h
        new_ratio = result.downsampled_width / result.downsampled_height
        assert abs(orig_ratio - new_ratio) < 0.02

    def test_aspect_ratio_preserved_tall(self, tmp_path: Path) -> None:
        orig_h, orig_w = 5000, 800
        img = _make_image(h=orig_h, w=orig_w)
        out_uri = _file_uri(tmp_path / "out.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        orig_ratio = orig_w / orig_h
        new_ratio = result.downsampled_width / result.downsampled_height
        assert abs(orig_ratio - new_ratio) < 0.02

    def test_aspect_ratio_preserved_square(self, tmp_path: Path) -> None:
        img = _make_image(h=3000, w=3000)
        out_uri = _file_uri(tmp_path / "out.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        # Square must stay square
        assert result.downsampled_width == result.downsampled_height


# ── Metadata correctness ─────────────────────────────────────────────────────────


class TestMetadata:
    def test_all_fields_populated(self, tmp_path: Path) -> None:
        orig_h, orig_w = 3000, 2500
        img = _make_image(h=orig_h, w=orig_w)
        src_uri = "file://original/source.tiff"
        out_uri = _file_uri(tmp_path / "ds.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri=src_uri,
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        assert result.source_artifact_uri == src_uri
        assert result.downsampled_artifact_uri == out_uri
        assert result.original_width == orig_w
        assert result.original_height == orig_h
        assert result.downsampled_width > 0
        assert result.downsampled_height > 0
        assert 0.0 < result.scale_factor <= 1.0
        assert result.processing_time_ms >= 0.0

    def test_scale_factor_matches_resize_ratio(self, tmp_path: Path) -> None:
        """scale_factor is exactly min(2096/w, 2096/h) rounded to 6dp."""
        # W is the constraining dimension: 4192 / 2096 = 0.5
        img = _make_image(h=1000, w=4192)
        out_uri = _file_uri(tmp_path / "out.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        assert abs(result.scale_factor - 0.5) < 1e-4

    def test_result_is_dataclass_serialisable(self, tmp_path: Path) -> None:
        """DownsampleResult can be serialised via dataclasses.asdict()."""
        img = _make_image(h=1000, w=2500)
        out_uri = _file_uri(tmp_path / "ds.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://source.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        d = dataclasses.asdict(result)
        assert isinstance(d, dict)
        expected_keys = {
            "source_artifact_uri",
            "downsampled_artifact_uri",
            "original_width",
            "original_height",
            "downsampled_width",
            "downsampled_height",
            "scale_factor",
            "processing_time_ms",
        }
        assert expected_keys <= d.keys()


# ── Artifact integrity ─────────────────────────────────────────────────────────


class TestArtifactIntegrity:
    def test_output_tiff_is_written(self, tmp_path: Path) -> None:
        """The downsampled TIFF exists on disk after the call."""
        img = _make_image(h=1000, w=3000)
        out_path = tmp_path / "output.tiff"

        run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src.tiff",
            output_uri=_file_uri(out_path),
            storage=LocalFileBackend(),
        )

        assert out_path.exists()
        assert out_path.stat().st_size > 0

    def test_written_tiff_is_decodable(self, tmp_path: Path) -> None:
        """Written TIFF bytes can be decoded back to a valid numpy array with the expected shape."""
        orig_h, orig_w = 500, 3000
        img = _make_image(h=orig_h, w=orig_w)
        out_path = tmp_path / "output.tiff"
        out_uri = _file_uri(out_path)

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
        )

        tiff_bytes = out_path.read_bytes()
        buf = np.frombuffer(tiff_bytes, dtype=np.uint8)
        decoded = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        assert decoded is not None
        assert decoded.shape[0] == result.downsampled_height
        assert decoded.shape[1] == result.downsampled_width

    def test_source_artifact_uri_never_written(self) -> None:
        """storage.put_bytes is called exactly once, for the output URI only."""
        img = _make_image(h=1000, w=3000)
        src_uri = "s3://mybucket/original/1.tiff"
        out_uri = "s3://mybucket/downsampled/1.tiff"

        mock_storage = MagicMock()
        run_downsample_step(
            full_res_image=img,
            source_artifact_uri=src_uri,
            output_uri=out_uri,
            storage=mock_storage,
        )

        mock_storage.put_bytes.assert_called_once()
        call_uri = mock_storage.put_bytes.call_args[0][0]
        assert call_uri == out_uri

    def test_storage_failure_propagates(self) -> None:
        """Exceptions from storage.put_bytes() propagate unchanged."""
        img = _make_image(h=200, w=300)
        mock_storage = MagicMock()
        mock_storage.put_bytes.side_effect = OSError("S3 write failed")

        with pytest.raises(OSError, match="S3 write failed"):
            run_downsample_step(
                full_res_image=img,
                source_artifact_uri="s3://bucket/src.tiff",
                output_uri="s3://bucket/out.tiff",
                storage=mock_storage,
            )

    def test_encode_failure_raises_value_error(self) -> None:
        """If cv2.imencode returns False, ValueError is raised."""
        img = _make_image(h=200, w=300)
        mock_storage = MagicMock()

        with patch("services.eep_worker.app.downsample_step.cv2.imencode", return_value=(False, None)):
            with pytest.raises(ValueError, match="cv2.imencode"):
                run_downsample_step(
                    full_res_image=img,
                    source_artifact_uri="file://src.tiff",
                    output_uri="file://out.tiff",
                    storage=mock_storage,
                )


# ── Custom max_dimension_px ───────────────────────────────────────────────────


class TestCustomMaxDimension:
    def test_custom_limit_triggers_resize(self, tmp_path: Path) -> None:
        """Custom max_dimension_px < image dims causes a resize."""
        img = _make_image(h=500, w=1000)
        out_uri = _file_uri(tmp_path / "custom.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
            max_dimension_px=200,
        )

        assert result.downsampled_width <= 200
        assert result.downsampled_height <= 200
        assert result.scale_factor < 1.0

    def test_custom_limit_no_resize_when_within_bound(self, tmp_path: Path) -> None:
        """Image smaller than the custom limit is not resized."""
        img = _make_image(h=50, w=100)
        out_uri = _file_uri(tmp_path / "custom.tiff")

        result = run_downsample_step(
            full_res_image=img,
            source_artifact_uri="file://src.tiff",
            output_uri=out_uri,
            storage=LocalFileBackend(),
            max_dimension_px=200,
        )

        assert result.scale_factor == 1.0
        assert result.downsampled_width == 100
        assert result.downsampled_height == 50
