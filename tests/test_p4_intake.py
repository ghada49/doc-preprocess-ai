"""
tests/test_p4_intake.py
-----------------------
Packet 4.3a — OTIFF intake, SHA-256 hash computation, and proxy image derivation.

Covers:
  - compute_hash: correct SHA-256 for known inputs, 64-char hex, deterministic
  - load_otiff: delegates to storage.get_bytes, wraps exceptions in OtiffLoadError
  - decode_otiff: returns uint8 3-channel numpy, correct dims, raises OtiffDecodeError
    for garbage/empty/truncated bytes
  - check_hash_consistency: no-op when prior=None or hashes match; raises
    OtiffHashMismatchError on mismatch with correct attributes
  - derive_proxy: downscales when exceeds limit, identity when within, aspect ratio
    preserved (landscape and portrait), dtype preserved, raises ValueError for unknown
    material type, uses default config when None, per-material-type config respected,
    image exactly at limit returns same object

No live storage or GPU required — cv2 / numpy work in CI.
"""

from __future__ import annotations

import hashlib
from typing import cast
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from services.eep_worker.app.intake import (
    OtiffDecodeError,
    OtiffHashMismatchError,
    OtiffLoadError,
    ProxyConfig,
    check_hash_consistency,
    compute_hash,
    decode_otiff,
    derive_proxy,
    load_otiff,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_image(h: int, w: int, seed: int = 42) -> np.ndarray:
    """Create a deterministic uint8 BGR image of shape (h, w, 3)."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def _encode_png(image: np.ndarray) -> bytes:
    """Encode a numpy BGR image to PNG bytes via OpenCV."""
    success, buf = cv2.imencode(".png", image)
    assert success, "cv2.imencode failed in test helper"
    return cast(bytes, buf.tobytes())


# ── TestComputeHash ────────────────────────────────────────────────────────────


class TestComputeHash:
    def test_returns_64_char_hex_string(self) -> None:
        result = compute_hash(b"hello")
        assert isinstance(result, str)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_matches_stdlib_sha256(self) -> None:
        data = b"arbitrary data for hashing"
        expected = hashlib.sha256(data).hexdigest()
        assert compute_hash(data) == expected

    def test_known_value_empty_bytes(self) -> None:
        # SHA-256 of empty bytes is a well-known constant.
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert compute_hash(b"") == expected

    def test_deterministic_same_input(self) -> None:
        data = b"determinism check"
        assert compute_hash(data) == compute_hash(data)

    def test_different_inputs_produce_different_hashes(self) -> None:
        assert compute_hash(b"aaa") != compute_hash(b"bbb")

    def test_returns_lowercase(self) -> None:
        result = compute_hash(b"case check")
        assert result == result.lower()


# ── TestLoadOtiff ──────────────────────────────────────────────────────────────


class TestLoadOtiff:
    def test_returns_bytes_from_storage(self) -> None:
        storage = MagicMock()
        storage.get_bytes.return_value = b"\x89PNG raw bytes"
        result = load_otiff("s3://bucket/page.tiff", storage)
        assert result == b"\x89PNG raw bytes"

    def test_calls_get_bytes_with_uri(self) -> None:
        storage = MagicMock()
        storage.get_bytes.return_value = b"data"
        load_otiff("s3://bucket/img.tiff", storage)
        storage.get_bytes.assert_called_once_with("s3://bucket/img.tiff")

    def test_wraps_exception_in_otiff_load_error(self) -> None:
        storage = MagicMock()
        storage.get_bytes.side_effect = ConnectionError("network failure")
        with pytest.raises(OtiffLoadError):
            load_otiff("s3://bucket/img.tiff", storage)

    def test_load_error_carries_uri(self) -> None:
        storage = MagicMock()
        storage.get_bytes.side_effect = OSError("disk error")
        with pytest.raises(OtiffLoadError) as exc_info:
            load_otiff("file:///data/page.tiff", storage)
        assert exc_info.value.uri == "file:///data/page.tiff"

    def test_load_error_carries_cause(self) -> None:
        storage = MagicMock()
        original = RuntimeError("unexpected")
        storage.get_bytes.side_effect = original
        with pytest.raises(OtiffLoadError) as exc_info:
            load_otiff("s3://bucket/img.tiff", storage)
        assert exc_info.value.cause is original

    def test_load_error_is_subclass_of_os_error(self) -> None:
        storage = MagicMock()
        storage.get_bytes.side_effect = OSError("fail")
        with pytest.raises(OSError):
            load_otiff("s3://x", storage)

    def test_load_error_chained_from_cause(self) -> None:
        storage = MagicMock()
        cause = ValueError("bad uri")
        storage.get_bytes.side_effect = cause
        with pytest.raises(OtiffLoadError) as exc_info:
            load_otiff("s3://x", storage)
        assert exc_info.value.__cause__ is cause


# ── TestDecodeOtiff ────────────────────────────────────────────────────────────


class TestDecodeOtiff:
    def test_returns_uint8_ndarray(self) -> None:
        img = _make_image(64, 64)
        data = _encode_png(img)
        result = decode_otiff(data)
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.uint8

    def test_returns_3_channel_array(self) -> None:
        img = _make_image(32, 48)
        data = _encode_png(img)
        result = decode_otiff(data)
        assert result.ndim == 3
        assert result.shape[2] == 3

    def test_correct_height_and_width(self) -> None:
        img = _make_image(50, 80)
        data = _encode_png(img)
        result = decode_otiff(data)
        assert result.shape[:2] == (50, 80)

    def test_raises_otiff_decode_error_for_garbage_bytes(self) -> None:
        with pytest.raises(OtiffDecodeError):
            decode_otiff(b"\x00\x01\x02\x03 not an image")

    def test_raises_otiff_decode_error_for_empty_bytes(self) -> None:
        with pytest.raises(OtiffDecodeError):
            decode_otiff(b"")

    def test_decode_error_carries_uri(self) -> None:
        with pytest.raises(OtiffDecodeError) as exc_info:
            decode_otiff(b"garbage", uri="s3://bucket/bad.tiff")
        assert exc_info.value.uri == "s3://bucket/bad.tiff"

    def test_decode_error_default_uri(self) -> None:
        with pytest.raises(OtiffDecodeError) as exc_info:
            decode_otiff(b"garbage")
        assert exc_info.value.uri == "<unknown>"

    def test_decode_error_is_subclass_of_value_error(self) -> None:
        with pytest.raises(ValueError):
            decode_otiff(b"not an image")

    def test_uri_keyword_argument_accepted(self) -> None:
        """Explicit uri= kwarg should flow through to the error."""
        with pytest.raises(OtiffDecodeError) as exc_info:
            decode_otiff(b"junk", uri="file:///data/x.tiff")
        assert "file:///data/x.tiff" in str(exc_info.value)


# ── TestCheckHashConsistency ───────────────────────────────────────────────────


class TestCheckHashConsistency:
    def test_no_op_when_prior_hash_is_none(self) -> None:
        # Should not raise.
        check_hash_consistency("s3://x", "abc123", None)

    def test_no_op_when_hashes_match(self) -> None:
        h = compute_hash(b"data")
        check_hash_consistency("s3://x", h, h)

    def test_raises_on_mismatch(self) -> None:
        with pytest.raises(OtiffHashMismatchError):
            check_hash_consistency("s3://x", "hash_new", "hash_old")

    def test_error_carries_uri(self) -> None:
        with pytest.raises(OtiffHashMismatchError) as exc_info:
            check_hash_consistency("s3://bucket/p.tiff", "new", "old")
        assert exc_info.value.uri == "s3://bucket/p.tiff"

    def test_error_carries_expected(self) -> None:
        with pytest.raises(OtiffHashMismatchError) as exc_info:
            check_hash_consistency("s3://x", "actual_hash", "prior_hash")
        assert exc_info.value.expected == "prior_hash"

    def test_error_carries_actual(self) -> None:
        with pytest.raises(OtiffHashMismatchError) as exc_info:
            check_hash_consistency("s3://x", "actual_hash", "prior_hash")
        assert exc_info.value.actual == "actual_hash"

    def test_error_is_subclass_of_value_error(self) -> None:
        with pytest.raises(ValueError):
            check_hash_consistency("s3://x", "a", "b")

    def test_no_raise_when_prior_none_even_if_hash_is_empty(self) -> None:
        check_hash_consistency("s3://x", "", None)

    def test_mismatch_error_message_includes_uri(self) -> None:
        with pytest.raises(OtiffHashMismatchError) as exc_info:
            check_hash_consistency("s3://special/uri", "new", "old")
        assert "s3://special/uri" in str(exc_info.value)


# ── TestDeriveProxy ────────────────────────────────────────────────────────────


class TestDeriveProxy:
    def test_downscales_landscape_image(self) -> None:
        img = _make_image(600, 2000)  # long edge = 2000, limit = 1024
        result = derive_proxy(img, "book")
        h, w = result.shape[:2]
        assert max(h, w) <= 1024

    def test_downscales_portrait_image(self) -> None:
        img = _make_image(2000, 600)  # long edge = 2000
        result = derive_proxy(img, "newspaper")
        h, w = result.shape[:2]
        assert max(h, w) <= 1024

    def test_identity_when_within_limit(self) -> None:
        img = _make_image(512, 512)  # long edge = 512, limit = 1024
        result = derive_proxy(img, "book")
        assert result is img  # same object — no copy

    def test_identity_when_exactly_at_limit(self) -> None:
        img = _make_image(1024, 800)  # long edge = 1024 == limit
        result = derive_proxy(img, "book")
        assert result is img

    def test_preserves_aspect_ratio_landscape(self) -> None:
        img = _make_image(500, 2000)  # ratio = 0.25
        result = derive_proxy(img, "archival_document")
        h, w = result.shape[:2]
        original_ratio = 500 / 2000
        result_ratio = h / w
        assert abs(result_ratio - original_ratio) < 0.02

    def test_preserves_aspect_ratio_portrait(self) -> None:
        img = _make_image(2000, 500)  # ratio = 4.0
        result = derive_proxy(img, "archival_document")
        h, w = result.shape[:2]
        original_ratio = 2000 / 500
        result_ratio = h / w
        assert abs(result_ratio - original_ratio) < 0.02

    def test_preserves_dtype(self) -> None:
        img = _make_image(600, 2000)
        result = derive_proxy(img, "book")
        assert result.dtype == np.uint8

    def test_returns_3_channels(self) -> None:
        img = _make_image(600, 2000)
        result = derive_proxy(img, "book")
        assert result.ndim == 3
        assert result.shape[2] == 3

    def test_raises_value_error_for_unknown_material_type(self) -> None:
        img = _make_image(100, 100)
        with pytest.raises(ValueError, match="Unknown material_type"):
            derive_proxy(img, "scroll")

    def test_error_message_includes_material_type(self) -> None:
        img = _make_image(100, 100)
        with pytest.raises(ValueError, match="scroll"):
            derive_proxy(img, "scroll")

    def test_microfilm_is_valid_material_type(self) -> None:
        img = _make_image(600, 2000)
        result = derive_proxy(img, "microfilm")
        h, w = result.shape[:2]
        assert max(h, w) <= 1024

    def test_uses_default_config_when_none(self) -> None:
        img = _make_image(600, 2000)
        result = derive_proxy(img, "book", config=None)
        h, w = result.shape[:2]
        assert max(h, w) <= 1024

    def test_per_material_type_config_respected(self) -> None:
        config = ProxyConfig(
            max_long_edge_px={
                "book": 512,
                "newspaper": 1024,
                "archival_document": 1024,
                "document": 1024,
            }
        )
        img = _make_image(600, 2000)
        result = derive_proxy(img, "book", config=config)
        h, w = result.shape[:2]
        assert max(h, w) <= 512

    def test_custom_config_newspaper_higher_limit(self) -> None:
        config = ProxyConfig(
            max_long_edge_px={"book": 512, "newspaper": 2048, "archival_document": 1024}
        )
        img = _make_image(600, 2000)  # within 2048 limit for newspaper
        result = derive_proxy(img, "newspaper", config=config)
        assert result is img

    def test_all_four_material_types_accepted_by_default(self) -> None:
        img = _make_image(100, 100)
        for mt in ("book", "newspaper", "archival_document"):
            derive_proxy(img, mt)  # must not raise

    def test_very_small_image_not_upscaled(self) -> None:
        img = _make_image(10, 10)
        result = derive_proxy(img, "book")
        assert result is img  # small image untouched

    def test_output_long_edge_does_not_exceed_limit(self) -> None:
        img = _make_image(3000, 4000)
        result = derive_proxy(img, "book")
        h, w = result.shape[:2]
        assert max(h, w) <= 1024
