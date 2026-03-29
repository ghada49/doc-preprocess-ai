"""
tests/test_p6_iep2b_class_mapping.py
---------------------------------------
Packet 6.4 — IEP2B native-to-canonical class mapping unit tests.

Tests NATIVE_TO_CANONICAL table and map_native_class function.

Covers:
  NATIVE_TO_CANONICAL table:
    - All 5 canonical RegionType values are reachable via at least one mapping
    - No mapping produces a RegionType value outside the canonical schema
    - Only RegionType instances or None appear as values

  map_native_class:
    - Known text-variant classes map to RegionType.text_block
    - Known title-variant classes map to RegionType.title
    - "table" maps to RegionType.table
    - Figure/image variants map to RegionType.image
    - Caption variants map to RegionType.caption
    - Explicitly excluded classes ("abandon", "isolate_formula", etc.) → None
    - Completely unknown class → None
    - Lookup is case-insensitive
    - Leading/trailing whitespace is stripped before lookup
"""

from __future__ import annotations

import pytest

from services.iep2b.app.class_mapping import NATIVE_TO_CANONICAL, map_native_class
from shared.schemas.layout import RegionType

_ALL_CANONICAL: frozenset[RegionType] = frozenset(RegionType)


# ---------------------------------------------------------------------------
# NATIVE_TO_CANONICAL table invariants
# ---------------------------------------------------------------------------


class TestNativeToCanonicalTable:
    def test_all_canonical_types_reachable(self) -> None:
        """Every canonical RegionType must be reachable via at least one mapping."""
        mapped_types = {v for v in NATIVE_TO_CANONICAL.values() if v is not None}
        assert (
            mapped_types == _ALL_CANONICAL
        ), f"Missing canonical types: {_ALL_CANONICAL - mapped_types}"

    def test_no_non_canonical_values(self) -> None:
        """Table values must be RegionType members or None — no stray strings."""
        for key, value in NATIVE_TO_CANONICAL.items():
            assert value is None or isinstance(
                value, RegionType
            ), f"NATIVE_TO_CANONICAL[{key!r}] = {value!r} is not a RegionType or None"

    def test_all_keys_are_lowercase(self) -> None:
        """Keys must be lowercase so case-insensitive lookup works correctly."""
        for key in NATIVE_TO_CANONICAL:
            assert key == key.lower(), f"Key {key!r} is not lowercase"

    def test_table_is_non_empty(self) -> None:
        assert len(NATIVE_TO_CANONICAL) > 0


# ---------------------------------------------------------------------------
# map_native_class — text_block mappings
# ---------------------------------------------------------------------------


class TestMapToTextBlock:
    @pytest.mark.parametrize(
        "native",
        [
            "text",
            "plain text",
            "paragraph",
            "body text",
            "list",
            "abstract",
            "reference",
            "references",
            "footnote",
            "footer",
        ],
    )
    def test_text_variant_maps_to_text_block(self, native: str) -> None:
        assert (
            map_native_class(native) == RegionType.text_block
        ), f"{native!r} should map to text_block"


# ---------------------------------------------------------------------------
# map_native_class — title mappings
# ---------------------------------------------------------------------------


class TestMapToTitle:
    @pytest.mark.parametrize(
        "native",
        ["title", "section-header", "section_header", "header", "headline"],
    )
    def test_title_variant_maps_to_title(self, native: str) -> None:
        assert map_native_class(native) == RegionType.title, f"{native!r} should map to title"


# ---------------------------------------------------------------------------
# map_native_class — table
# ---------------------------------------------------------------------------


class TestMapToTable:
    def test_table_maps_to_table(self) -> None:
        assert map_native_class("table") == RegionType.table


# ---------------------------------------------------------------------------
# map_native_class — image mappings
# ---------------------------------------------------------------------------


class TestMapToImage:
    @pytest.mark.parametrize("native", ["figure", "image", "picture"])
    def test_image_variant_maps_to_image(self, native: str) -> None:
        assert map_native_class(native) == RegionType.image, f"{native!r} should map to image"


# ---------------------------------------------------------------------------
# map_native_class — caption mappings
# ---------------------------------------------------------------------------


class TestMapToCaption:
    @pytest.mark.parametrize(
        "native",
        [
            "caption",
            "figure_caption",
            "figure-caption",
            "table_caption",
            "table-caption",
            "table_footnote",
            "table-footnote",
        ],
    )
    def test_caption_variant_maps_to_caption(self, native: str) -> None:
        assert map_native_class(native) == RegionType.caption, f"{native!r} should map to caption"


# ---------------------------------------------------------------------------
# map_native_class — excluded classes return None
# ---------------------------------------------------------------------------


class TestExcludedClasses:
    @pytest.mark.parametrize(
        "native",
        [
            "abandon",
            "isolate_formula",
            "isolate-formula",
            "formula",
            "equation",
            "algorithm",
            "code",
            "page_number",
            "page-number",
            "toc",
        ],
    )
    def test_excluded_class_returns_none(self, native: str) -> None:
        assert map_native_class(native) is None, f"{native!r} should be excluded (map to None)"

    def test_completely_unknown_class_returns_none(self) -> None:
        assert map_native_class("totally_unknown_class_xyz") is None

    def test_empty_string_returns_none(self) -> None:
        assert map_native_class("") is None


# ---------------------------------------------------------------------------
# map_native_class — case-insensitivity and whitespace stripping
# ---------------------------------------------------------------------------


class TestLookupNormalization:
    def test_uppercase_input_accepted(self) -> None:
        assert map_native_class("TEXT") == RegionType.text_block

    def test_mixed_case_input_accepted(self) -> None:
        assert map_native_class("Title") == RegionType.title

    def test_all_caps_figure_accepted(self) -> None:
        assert map_native_class("FIGURE") == RegionType.image

    def test_leading_whitespace_stripped(self) -> None:
        assert map_native_class("  table") == RegionType.table

    def test_trailing_whitespace_stripped(self) -> None:
        assert map_native_class("table  ") == RegionType.table

    def test_surrounding_whitespace_stripped(self) -> None:
        assert map_native_class("  caption  ") == RegionType.caption
