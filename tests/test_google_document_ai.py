"""
tests/test_google_document_ai.py
-------------------------------
Unit tests for Google Document AI integration module (services/eep/app/google/document_ai.py).

Test coverage:
  - GoogleDocumentAIConfig validation
  - Error classification (transient vs permanent)
  - _WrappedElement dataclass
  - _extract_elements_from_response (entity-based + page-element-based)
  - Type mapping (Google → canonical)
  - Bounding box extraction (normalized, pixel, clamping, errors)
  - _map_google_to_canonical (full pipeline)
  - Async API calls with mock Google responses
  - Retry logic with exponential backoff
  - Timeout handling
  - Graceful degradation when credentials missing or Google disabled
  - run_google_layout_analysis (public API)
  - run_google_cleanup (public API)
"""

import logging
import re
from io import BytesIO
from typing import Any, NoReturn
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from services.eep.app.google.document_ai import (
    CallGoogleDocumentAI,
    GoogleAPIPermanentError,
    GoogleAPITransientError,
    GoogleDocumentAIConfig,
    _classify_error,
    _extract_elements_from_response,
    _WrappedElement,
    convert_image_bytes_to_pdf,
    run_google_cleanup,
    run_google_layout_analysis,
)
from shared.schemas.layout import RegionType

# ───────────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────────


def _make_bpoly(x0: float, y0: float, x1: float, y1: float, normalized: bool = True) -> Mock:
    """Build a mock Google bounding_poly."""
    v0 = Mock(x=x0, y=y0)
    v1 = Mock(x=x1, y=y1)
    poly = Mock()
    if normalized:
        poly.normalized_vertices = [v0, v1]
        poly.vertices = []
    else:
        poly.normalized_vertices = []
        poly.vertices = [v0, v1]
    return poly


def _make_wrapped(
    type_: str = "PARAGRAPH",
    x0: float = 0.1,
    y0: float = 0.1,
    x1: float = 0.9,
    y1: float = 0.5,
    confidence: float = 0.9,
) -> _WrappedElement:
    """Build a _WrappedElement with normalized vertices."""
    return _WrappedElement(
        type_=type_,
        bounding_poly=_make_bpoly(x0, y0, x1, y1, normalized=True),
        confidence=confidence,
    )


def _make_image_bytes(
    width: int,
    height: int,
    *,
    image_format: str = "PNG",
    mode: str = "RGB",
) -> bytes:
    """Build image bytes with stable dimensions for PDF conversion tests."""
    color = (255, 0, 0) if mode == "RGB" else 255
    image = Image.new(mode, (width, height), color=color)
    buffer = BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def _extract_pdf_media_box(pdf_bytes: bytes) -> tuple[float, float]:
    """Extract the first page MediaBox width/height from Pillow-generated PDF bytes."""
    match = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([0-9.]+)\s+([0-9.]+)\s*\]", pdf_bytes)
    assert match is not None, "PDF MediaBox not found"
    return float(match.group(1)), float(match.group(2))


def _extract_pdf_page_count(pdf_bytes: bytes) -> int:
    """Extract the page count from Pillow-generated PDF bytes."""
    match = re.search(rb"/Count\s+(\d+)", pdf_bytes)
    assert match is not None, "PDF page count not found"
    return int(match.group(1))


def _extract_pdf_draw_size(pdf_bytes: bytes) -> tuple[float, float]:
    """Extract the draw matrix size used for the embedded page image."""
    match = re.search(
        rb"q\s+([0-9.]+)\s+0\s+0\s+([0-9.]+)\s+0\s+0\s+cm\s+/image\s+Do\s+Q",
        pdf_bytes,
    )
    assert match is not None, "PDF image draw matrix not found"
    return float(match.group(1)), float(match.group(2))


# ───────────────────────────────────────────────────────────────────────────────
# Test: GoogleDocumentAIConfig Validation
# ───────────────────────────────────────────────────────────────────────────────


class TestConvertImageBytesToPdf:
    """Test the image-to-PDF conversion helper used by Layout Parser."""

    def test_creates_valid_single_page_pdf_without_scaling(self) -> None:
        """Converted PDF preserves source dimensions and image draw size."""
        pdf_bytes = convert_image_bytes_to_pdf(_make_image_bytes(123, 456))

        assert pdf_bytes.startswith(b"%PDF-")
        assert _extract_pdf_page_count(pdf_bytes) == 1
        assert _extract_pdf_media_box(pdf_bytes) == pytest.approx((123.0, 456.0))
        assert _extract_pdf_draw_size(pdf_bytes) == pytest.approx((123.0, 456.0))

    def test_pdf_page_dimensions_keep_coordinates_aligned_with_image_pixels(self) -> None:
        """Converted PDF dimensions keep bbox mapping aligned with original pixels."""
        client = CallGoogleDocumentAI(GoogleDocumentAIConfig(enabled=False))
        pdf_bytes = convert_image_bytes_to_pdf(_make_image_bytes(400, 600))
        pdf_width, pdf_height = _extract_pdf_media_box(pdf_bytes)

        regions = client._map_google_to_canonical(
            [_make_wrapped("PARAGRAPH", x0=0.1, y0=0.2, x1=0.9, y1=0.8)],
            int(pdf_width),
            int(pdf_height),
        )

        assert regions[0].bbox.x_min == pytest.approx(40.0)
        assert regions[0].bbox.y_min == pytest.approx(120.0)
        assert regions[0].bbox.x_max == pytest.approx(360.0)
        assert regions[0].bbox.y_max == pytest.approx(480.0)

    def test_tiff_input_converts_to_valid_single_page_pdf(self) -> None:
        """TIFF bytes produce a valid single-page PDF with matching dimensions."""
        pdf_bytes = convert_image_bytes_to_pdf(_make_image_bytes(200, 300, image_format="TIFF"))

        assert pdf_bytes.startswith(b"%PDF-")
        assert _extract_pdf_page_count(pdf_bytes) == 1
        assert _extract_pdf_media_box(pdf_bytes) == pytest.approx((200.0, 300.0))

    def test_jpeg_input_converts_to_valid_single_page_pdf(self) -> None:
        """JPEG bytes produce a valid single-page PDF with matching dimensions."""
        pdf_bytes = convert_image_bytes_to_pdf(_make_image_bytes(150, 250, image_format="JPEG"))

        assert pdf_bytes.startswith(b"%PDF-")
        assert _extract_pdf_page_count(pdf_bytes) == 1
        assert _extract_pdf_media_box(pdf_bytes) == pytest.approx((150.0, 250.0))


class TestGoogleDocumentAIConfigValidation:
    """Test config validation."""

    def test_disabled_config_always_valid(self) -> None:
        """Disabled config is always valid regardless of other fields."""
        config = GoogleDocumentAIConfig(enabled=False)
        is_valid, msg = config.validate()
        assert is_valid
        assert "disabled" in msg.lower()

    def test_missing_project_id(self) -> None:
        """Missing project_id returns invalid."""
        config = GoogleDocumentAIConfig(enabled=True, project_id="")
        is_valid, msg = config.validate()
        assert not is_valid
        assert "project_id" in msg.lower()

    def test_missing_processor_id_layout(self) -> None:
        """Missing processor_id_layout returns invalid."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test-project",
            processor_id_layout="",
        )
        is_valid, msg = config.validate()
        assert not is_valid
        assert "processor_id_layout" in msg.lower()

    def test_zero_layout_timeout_invalid(self) -> None:
        """timeout_layout_seconds = 0 is invalid."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test-project",
            processor_id_layout="proc-123",
            timeout_layout_seconds=0,
        )
        is_valid, msg = config.validate()
        assert not is_valid
        assert "timeout" in msg.lower()

    def test_negative_cleanup_timeout_invalid(self) -> None:
        """timeout_cleanup_seconds < 0 is invalid."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test-project",
            processor_id_layout="proc-123",
            timeout_cleanup_seconds=-1,
        )
        is_valid, msg = config.validate()
        assert not is_valid
        assert "timeout" in msg.lower()

    def test_valid_config(self) -> None:
        """Properly configured config is valid."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test-project",
            processor_id_layout="proc-layout",
            processor_id_cleanup="proc-cleanup",
            timeout_layout_seconds=90,
            timeout_cleanup_seconds=120,
        )
        is_valid, msg = config.validate()
        assert is_valid

    def test_credentials_file_defaults_to_env_or_k8s(self) -> None:
        """credentials_file defaults to env var or K8s path."""
        config = GoogleDocumentAIConfig()
        assert config.credentials_file  # non-empty

    def test_processor_id_cleanup_not_required_for_valid(self) -> None:
        """processor_id_cleanup is optional — not required for valid config."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test-project",
            processor_id_layout="proc-layout",
            processor_id_cleanup="",  # empty is fine
        )
        is_valid, _ = config.validate()
        assert is_valid


# ───────────────────────────────────────────────────────────────────────────────
# Test: Error Classification
# ───────────────────────────────────────────────────────────────────────────────


class TestErrorClassification:
    """Test error classification logic."""

    def test_timeout_is_transient(self) -> None:
        """asyncio.TimeoutError → transient."""
        error = TimeoutError("Request timeout")
        classification, reason = _classify_error(error)
        assert classification == "transient"
        assert "timeout" in reason.lower()

    def test_http_429_is_transient(self) -> None:
        """HTTP 429 (rate limit) → transient."""
        error = Exception("Rate limited")
        classification, reason = _classify_error(error, http_status=429)
        assert classification == "transient"
        assert "rate" in reason.lower()

    def test_http_5xx_is_transient(self) -> None:
        """HTTP 5xx (server error) → transient."""
        for status in [500, 502, 503, 504]:
            classification, reason = _classify_error(Exception(), http_status=status)
            assert classification == "transient"

    def test_http_401_is_permanent(self) -> None:
        """HTTP 401 (unauthorized) → permanent."""
        classification, reason = _classify_error(Exception(), http_status=401)
        assert classification == "permanent"
        assert "auth" in reason.lower()

    def test_http_403_is_permanent(self) -> None:
        """HTTP 403 (forbidden) → permanent."""
        classification, reason = _classify_error(Exception(), http_status=403)
        assert classification == "permanent"

    def test_http_404_is_permanent(self) -> None:
        """HTTP 404 (not found) → permanent."""
        classification, reason = _classify_error(Exception(), http_status=404)
        assert classification == "permanent"
        assert "not found" in reason.lower()

    def test_http_400_is_permanent(self) -> None:
        """HTTP 400 (bad request) → permanent."""
        classification, reason = _classify_error(Exception(), http_status=400)
        assert classification == "permanent"

    def test_permission_in_message_is_permanent(self) -> None:
        """'permission' in error message → permanent."""
        classification, reason = _classify_error(Exception("Permission denied"))
        assert classification == "permanent"

    def test_unauthenticated_in_message_is_permanent(self) -> None:
        """'unauthenticated' in error message → permanent."""
        classification, reason = _classify_error(Exception("UNAUTHENTICATED"))
        assert classification == "permanent"

    def test_not_found_in_message_is_permanent(self) -> None:
        """'not found' in error message → permanent."""
        classification, reason = _classify_error(Exception("Resource not found"))
        assert classification == "permanent"

    def test_timeout_in_message_is_transient(self) -> None:
        """'timeout' in error message (non-asyncio) → transient."""
        classification, reason = _classify_error(Exception("Connection timeout"))
        assert classification == "transient"

    def test_deadline_in_message_is_transient(self) -> None:
        """'deadline' in error message → transient."""
        classification, reason = _classify_error(Exception("Deadline exceeded"))
        assert classification == "transient"

    def test_unknown_error_defaults_to_transient(self) -> None:
        """Unknown errors default to transient (safer to retry)."""
        classification, reason = _classify_error(Exception("Some random error"))
        assert classification == "transient"

    def test_returns_tuple_with_two_strings(self) -> None:
        """Return value is always (str, str)."""
        result = _classify_error(Exception("test"))
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], str)


# ───────────────────────────────────────────────────────────────────────────────
# Test: _WrappedElement
# ───────────────────────────────────────────────────────────────────────────────


class TestWrappedElement:
    """Test _WrappedElement dataclass."""

    def test_creation(self) -> None:
        """_WrappedElement stores type_, bounding_poly, confidence."""
        bpoly = Mock()
        elem = _WrappedElement(type_="PARAGRAPH", bounding_poly=bpoly, confidence=0.8)
        assert elem.type_ == "PARAGRAPH"
        assert elem.bounding_poly is bpoly
        assert elem.confidence == 0.8

    def test_table_type(self) -> None:
        """TABLE type is stored correctly."""
        elem = _WrappedElement(type_="TABLE", bounding_poly=Mock(), confidence=0.95)
        assert elem.type_ == "TABLE"

    def test_zero_confidence(self) -> None:
        """Confidence 0.0 is valid."""
        elem = _WrappedElement(type_="IMAGE", bounding_poly=Mock(), confidence=0.0)
        assert elem.confidence == 0.0

    def test_full_confidence(self) -> None:
        """Confidence 1.0 is valid."""
        elem = _WrappedElement(type_="CAPTION", bounding_poly=Mock(), confidence=1.0)
        assert elem.confidence == 1.0


# ───────────────────────────────────────────────────────────────────────────────
# Test: _extract_elements_from_response
# ───────────────────────────────────────────────────────────────────────────────


class TestExtractElementsFromResponse:
    """Test _extract_elements_from_response helper."""

    def test_empty_document_returns_empty(self) -> None:
        """None document and no pages → empty list."""
        result = _extract_elements_from_response(None, [])
        assert result == []

    def test_no_pages_returns_empty(self) -> None:
        """Non-None document with no pages → empty list."""
        doc = Mock()
        doc.entities = []
        result = _extract_elements_from_response(doc, [])
        assert result == []

    def test_entity_based_processor(self) -> None:
        """Entity-based processor: extracts from document.entities."""
        bpoly = _make_bpoly(0.1, 0.1, 0.9, 0.5)
        page_ref = Mock()
        page_ref.bounding_poly = bpoly
        page_anchor = Mock()
        page_anchor.page_refs = [page_ref]
        entity = Mock()
        entity.type_ = "TITLE"
        entity.confidence = 0.9
        entity.page_anchor = page_anchor

        doc = Mock()
        doc.entities = [entity]

        result = _extract_elements_from_response(doc, [])
        assert len(result) == 1
        assert result[0].type_ == "TITLE"
        assert result[0].confidence == 0.9

    def test_entity_based_skips_entity_without_page_anchor(self) -> None:
        """Entities without page_anchor are skipped."""
        entity = Mock()
        entity.type_ = "PARAGRAPH"
        entity.confidence = 0.8
        entity.page_anchor = None

        doc = Mock()
        doc.entities = [entity]

        result = _extract_elements_from_response(doc, [])
        assert result == []

    def test_page_element_blocks(self) -> None:
        """Page-level blocks → PARAGRAPH type."""
        bpoly = _make_bpoly(0.0, 0.0, 1.0, 0.5)
        layout = Mock()
        layout.bounding_poly = bpoly
        layout.confidence = 0.85
        block = Mock()
        block.layout = layout

        page = Mock()
        page.blocks = [block]
        page.tables = []
        page.paragraphs = []

        doc = Mock()
        doc.entities = []

        result = _extract_elements_from_response(doc, [page])
        assert len(result) == 1
        assert result[0].type_ == "PARAGRAPH"
        assert result[0].confidence == 0.85

    def test_page_element_tables(self) -> None:
        """Page-level tables → TABLE type."""
        bpoly = _make_bpoly(0.1, 0.5, 0.9, 0.9)
        layout = Mock()
        layout.bounding_poly = bpoly
        layout.confidence = 0.9
        table = Mock()
        table.layout = layout

        page = Mock()
        page.blocks = []
        page.tables = [table]
        page.paragraphs = []

        doc = Mock()
        doc.entities = []

        result = _extract_elements_from_response(doc, [page])
        assert len(result) == 1
        assert result[0].type_ == "TABLE"

    def test_page_element_paragraphs_fallback(self) -> None:
        """No blocks/tables → falls back to paragraphs."""
        bpoly = _make_bpoly(0.0, 0.0, 0.5, 0.3)
        layout = Mock()
        layout.bounding_poly = bpoly
        layout.confidence = 0.75
        para = Mock()
        para.layout = layout

        page = Mock()
        page.blocks = []
        page.tables = []
        page.paragraphs = [para]

        doc = Mock()
        doc.entities = []

        result = _extract_elements_from_response(doc, [page])
        assert len(result) == 1
        assert result[0].type_ == "PARAGRAPH"

    def test_entity_strategy_takes_precedence_over_page_elements(self) -> None:
        """Entity strategy wins when entities have page_anchor."""
        # Build valid entity
        bpoly = _make_bpoly(0.1, 0.1, 0.9, 0.5)
        page_ref = Mock()
        page_ref.bounding_poly = bpoly
        page_anchor = Mock()
        page_anchor.page_refs = [page_ref]
        entity = Mock()
        entity.type_ = "TABLE"
        entity.confidence = 0.99
        entity.page_anchor = page_anchor

        # Also build page blocks
        block_layout = Mock()
        block_layout.bounding_poly = _make_bpoly(0.0, 0.0, 1.0, 1.0)
        block_layout.confidence = 0.5
        block = Mock()
        block.layout = block_layout

        page = Mock()
        page.blocks = [block]
        page.tables = []
        page.paragraphs = []

        doc = Mock()
        doc.entities = [entity]

        result = _extract_elements_from_response(doc, [page])
        # Should use entities, not page blocks
        assert len(result) == 1
        assert result[0].type_ == "TABLE"

    def test_mixed_blocks_and_tables(self) -> None:
        """Blocks and tables are both extracted."""
        block_layout = Mock()
        block_layout.bounding_poly = _make_bpoly(0.0, 0.0, 1.0, 0.4)
        block_layout.confidence = 0.8
        block = Mock()
        block.layout = block_layout

        table_layout = Mock()
        table_layout.bounding_poly = _make_bpoly(0.0, 0.5, 1.0, 0.9)
        table_layout.confidence = 0.9
        table = Mock()
        table.layout = table_layout

        page = Mock()
        page.blocks = [block]
        page.tables = [table]
        page.paragraphs = []

        doc = Mock()
        doc.entities = []

        result = _extract_elements_from_response(doc, [page])
        assert len(result) == 2
        types = {e.type_ for e in result}
        assert "PARAGRAPH" in types
        assert "TABLE" in types

    def test_document_layout_blocks_extracted_as_strategy2(self) -> None:
        """document_layout blocks with non-empty bounding_box use it directly."""
        from google.cloud import documentai_v1

        bpoly = documentai_v1.BoundingPoly(
            normalized_vertices=[
                documentai_v1.NormalizedVertex(x=0.1, y=0.1),
                documentai_v1.NormalizedVertex(x=0.9, y=0.1),
                documentai_v1.NormalizedVertex(x=0.9, y=0.5),
                documentai_v1.NormalizedVertex(x=0.1, y=0.5),
            ]
        )
        text_block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock.LayoutTextBlock(
            type_="paragraph", text="hello"
        )
        block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock(
            text_block=text_block,
            bounding_box=bpoly,
        )
        dl = documentai_v1.Document.DocumentLayout(blocks=[block])
        doc = documentai_v1.Document(document_layout=dl)

        result = _extract_elements_from_response(doc, [])
        assert len(result) == 1
        assert result[0].type_ == "PARAGRAPH"
        # proto-plus copies message on attribute access; compare normalized_vertices by value
        assert list(result[0].bounding_poly.normalized_vertices) == list(bpoly.normalized_vertices)

    def test_document_layout_blocks_with_geometry_and_no_pages_map_to_canonical_regions(self) -> None:
        """Block-only responses still yield canonical regions when block geometry exists."""
        from google.cloud import documentai_v1

        client = CallGoogleDocumentAI(GoogleDocumentAIConfig(enabled=False))
        bpoly = documentai_v1.BoundingPoly(
            normalized_vertices=[
                documentai_v1.NormalizedVertex(x=0.1, y=0.1),
                documentai_v1.NormalizedVertex(x=0.9, y=0.1),
                documentai_v1.NormalizedVertex(x=0.9, y=0.5),
                documentai_v1.NormalizedVertex(x=0.1, y=0.5),
            ]
        )
        block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock(
            text_block=documentai_v1.Document.DocumentLayout.DocumentLayoutBlock.LayoutTextBlock(
                type_="paragraph",
                text="hello",
            ),
            bounding_box=bpoly,
        )
        doc = documentai_v1.Document(
            document_layout=documentai_v1.Document.DocumentLayout(blocks=[block])
        )

        elements = _extract_elements_from_response(doc, [])
        regions = client._map_google_to_canonical(elements, page_width=1000, page_height=1000)

        assert len(regions) == 1
        assert regions[0].type == RegionType.text_block
        assert regions[0].bbox.x_min == pytest.approx(100.0)
        assert regions[0].bbox.y_min == pytest.approx(100.0)
        assert regions[0].bbox.x_max == pytest.approx(900.0)
        assert regions[0].bbox.y_max == pytest.approx(500.0)

    def test_document_layout_blocks_resolve_bbox_from_page_paragraphs_positional(self) -> None:
        """When bounding_box is empty, bbox is resolved by positional match to page.paragraphs."""
        from google.cloud import documentai_v1

        # Build a paragraph bounding poly (what page.paragraphs provides)
        para_bpoly = documentai_v1.BoundingPoly(
            normalized_vertices=[
                documentai_v1.NormalizedVertex(x=0.0, y=0.2),
                documentai_v1.NormalizedVertex(x=1.0, y=0.2),
                documentai_v1.NormalizedVertex(x=1.0, y=0.4),
                documentai_v1.NormalizedVertex(x=0.0, y=0.4),
            ]
        )
        para_layout = documentai_v1.Document.Page.Layout(bounding_poly=para_bpoly, confidence=0.95)
        page_para = documentai_v1.Document.Page.Paragraph(layout=para_layout)
        page = documentai_v1.Document.Page(paragraphs=[page_para])

        # document_layout block with EMPTY bounding_box (real Layout Parser behavior)
        text_block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock.LayoutTextBlock(
            type_="paragraph", text="hello world"
        )
        block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock(
            text_block=text_block,
            # bounding_box left empty — positional match (1 block, 1 paragraph) resolves it
        )
        dl = documentai_v1.Document.DocumentLayout(blocks=[block])
        doc = documentai_v1.Document(document_layout=dl, pages=[page])

        result = _extract_elements_from_response(doc, [page])
        assert len(result) == 1
        assert result[0].type_ == "PARAGRAPH"
        # bbox resolved from page.paragraphs[0] via positional match
        assert list(result[0].bounding_poly.normalized_vertices) == list(
            para_bpoly.normalized_vertices
        )

    def test_document_layout_block_without_bbox_and_no_page_paragraphs_is_skipped(self) -> None:
        """document_layout block with empty bbox and no page paragraphs is silently skipped."""
        from google.cloud import documentai_v1

        text_block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock.LayoutTextBlock(
            type_="paragraph", text="orphan"
        )
        block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock(
            text_block=text_block,
            # empty bounding_box, no text_anchor → cannot resolve
        )
        dl = documentai_v1.Document.DocumentLayout(blocks=[block])
        doc = documentai_v1.Document(document_layout=dl)

        result = _extract_elements_from_response(doc, [])
        assert result == []

    def test_document_layout_table_block_mapped_to_table(self) -> None:
        """document_layout table_block → TABLE type."""
        from google.cloud import documentai_v1

        bpoly = documentai_v1.BoundingPoly(
            normalized_vertices=[
                documentai_v1.NormalizedVertex(x=0.0, y=0.5),
                documentai_v1.NormalizedVertex(x=1.0, y=0.5),
                documentai_v1.NormalizedVertex(x=1.0, y=1.0),
                documentai_v1.NormalizedVertex(x=0.0, y=1.0),
            ]
        )
        table_block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock.LayoutTableBlock()
        block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock(
            table_block=table_block,
            bounding_box=bpoly,
        )
        dl = documentai_v1.Document.DocumentLayout(blocks=[block])
        doc = documentai_v1.Document(document_layout=dl)

        result = _extract_elements_from_response(doc, [])
        assert len(result) == 1
        assert result[0].type_ == "TABLE"

    def test_document_layout_text_block_header_mapped_to_section_header(self) -> None:
        """document_layout text_block with type_='section-header' → SECTION_HEADER."""
        from google.cloud import documentai_v1

        bpoly = documentai_v1.BoundingPoly(
            normalized_vertices=[
                documentai_v1.NormalizedVertex(x=0.0, y=0.0),
                documentai_v1.NormalizedVertex(x=1.0, y=0.0),
                documentai_v1.NormalizedVertex(x=1.0, y=0.1),
                documentai_v1.NormalizedVertex(x=0.0, y=0.1),
            ]
        )
        text_block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock.LayoutTextBlock(
            type_="section-header", text="Chapter 1"
        )
        block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock(
            text_block=text_block,
            bounding_box=bpoly,
        )
        dl = documentai_v1.Document.DocumentLayout(blocks=[block])
        doc = documentai_v1.Document(document_layout=dl)

        result = _extract_elements_from_response(doc, [])
        assert len(result) == 1
        assert result[0].type_ == "SECTION_HEADER"

    def test_document_layout_takes_priority_over_page_blocks(self) -> None:
        """document_layout strategy is used when blocks are present; page-level blocks ignored."""
        from google.cloud import documentai_v1

        bpoly = documentai_v1.BoundingPoly(
            normalized_vertices=[
                documentai_v1.NormalizedVertex(x=0.0, y=0.0),
                documentai_v1.NormalizedVertex(x=1.0, y=1.0),
            ]
        )
        text_block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock.LayoutTextBlock(
            type_="paragraph"
        )
        block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock(
            text_block=text_block,
            bounding_box=bpoly,
        )
        dl = documentai_v1.Document.DocumentLayout(blocks=[block])
        doc = documentai_v1.Document(document_layout=dl)

        # page also has a block — should be ignored since document_layout wins
        page_layout = Mock()
        page_layout.bounding_poly = _make_bpoly(0.0, 0.0, 1.0, 1.0)
        page_layout.confidence = 0.5
        page_block = Mock()
        page_block.layout = page_layout
        page = Mock()
        page.blocks = [page_block]
        page.tables = []
        page.paragraphs = []

        result = _extract_elements_from_response(doc, [page])
        # Only 1 element from document_layout, not 2
        assert len(result) == 1


# ───────────────────────────────────────────────────────────────────────────────
# Test: source-dimension fallback for pages=[] responses (IEP2 / Layout Parser v2+)
# ───────────────────────────────────────────────────────────────────────────────


class TestSourceDimensionFallback:
    """
    Verify that block.bounding_box.normalizedVertices are correctly denormalized
    when document.pages is empty (Layout Parser v2+ with returnBoundingBoxes=True).
    """

    def _make_doc_with_blocks(self, num_blocks: int = 1, add_vertices: bool = True):
        """Build a documentai_v1.Document with document_layout blocks only (no pages)."""
        from google.cloud import documentai_v1

        blocks = []
        for i in range(num_blocks):
            bbox_kwargs: dict = {}
            if add_vertices:
                bbox_kwargs["normalized_vertices"] = [
                    documentai_v1.NormalizedVertex(x=0.1, y=0.2),
                    documentai_v1.NormalizedVertex(x=0.8, y=0.2),
                    documentai_v1.NormalizedVertex(x=0.8, y=0.6),
                    documentai_v1.NormalizedVertex(x=0.1, y=0.6),
                ]
            bpoly = documentai_v1.BoundingPoly(**bbox_kwargs)
            block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock(
                text_block=documentai_v1.Document.DocumentLayout.DocumentLayoutBlock.LayoutTextBlock(
                    type_="paragraph", text=f"block {i}"
                ),
                bounding_box=bpoly,
            )
            blocks.append(block)
        dl = documentai_v1.Document.DocumentLayout(blocks=blocks)
        return documentai_v1.Document(document_layout=dl)

    def test_blocks_with_normalized_vertices_yield_non_empty_regions(self) -> None:
        """normalizedVertices in blocks → non-empty Region[] even when pages=[]."""
        doc = self._make_doc_with_blocks(num_blocks=3, add_vertices=True)
        elements = _extract_elements_from_response(doc, [])
        assert len(elements) == 3

        client = CallGoogleDocumentAI(GoogleDocumentAIConfig(enabled=False))
        regions = client._map_google_to_canonical(elements, page_width=800, page_height=1200)
        assert len(regions) == 3

    def test_correct_bbox_math_from_normalized_vertices_with_source_dims(self) -> None:
        """
        normalizedVertices * (source_width, source_height) → correct pixel bbox.
        Vertices: x∈[0.1, 0.8], y∈[0.2, 0.6], page 800×1200.
        Expected: x_min=80, y_min=240, x_max=640, y_max=720.
        """
        doc = self._make_doc_with_blocks(num_blocks=1, add_vertices=True)
        elements = _extract_elements_from_response(doc, [])
        assert len(elements) == 1

        client = CallGoogleDocumentAI(GoogleDocumentAIConfig(enabled=False))
        regions = client._map_google_to_canonical(elements, page_width=800, page_height=1200)
        assert len(regions) == 1
        bbox = regions[0].bbox
        assert bbox.x_min == pytest.approx(80.0)   # 0.1 * 800
        assert bbox.y_min == pytest.approx(240.0)  # 0.2 * 1200
        assert bbox.x_max == pytest.approx(640.0)  # 0.8 * 800
        assert bbox.y_max == pytest.approx(720.0)  # 0.6 * 1200

    def test_pages_empty_uses_source_dimensions_from_instance(self) -> None:
        """_call_google_api_sync uses _source_width/_source_height when pages=[]."""
        from unittest.mock import Mock, patch

        from google.cloud import documentai_v1

        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="proj",
            location="us",
            processor_id_layout="proc",
            credentials_file="/fake/key.json",
        )
        client = CallGoogleDocumentAI(config)
        # Inject source dimensions as if process_layout set them from PIL
        client._source_width = 1600
        client._source_height = 2400

        bpoly = documentai_v1.BoundingPoly(
            normalized_vertices=[
                documentai_v1.NormalizedVertex(x=0.0, y=0.0),
                documentai_v1.NormalizedVertex(x=1.0, y=1.0),
            ]
        )
        block = documentai_v1.Document.DocumentLayout.DocumentLayoutBlock(
            text_block=documentai_v1.Document.DocumentLayout.DocumentLayoutBlock.LayoutTextBlock(
                type_="paragraph"
            ),
            bounding_box=bpoly,
        )
        doc = documentai_v1.Document(
            document_layout=documentai_v1.Document.DocumentLayout(blocks=[block])
        )
        mock_response = Mock()
        mock_response.document = doc
        mock_api_client = Mock()
        mock_api_client.process_document.return_value = mock_response
        client._client = mock_api_client

        result = client._call_google_api_sync(
            image_uri=None,
            image_bytes=b"%PDF-fake",
            mime_type="application/pdf",
        )

        # Source dims used as page dimensions since pages=[]
        assert result["page_width"] == 1600
        assert result["page_height"] == 2400
        assert len(result["elements"]) == 1

    def test_blocks_without_geometry_yield_empty_with_diagnostics(self) -> None:
        """document_layout blocks with empty bounding_box → empty elements, diagnostics populated."""
        doc = self._make_doc_with_blocks(num_blocks=4, add_vertices=False)
        elements = _extract_elements_from_response(doc, [])
        assert elements == []

        # Diagnostics should still report 4 blocks with no geometry
        from services.eep.app.google.document_ai import _summarize_layout_response

        diag = _summarize_layout_response(doc, [])
        assert diag["document_layout_block_count"] == 4
        assert diag["pages_count"] == 0
        assert diag["document_layout_blocks_have_geometry"] is False


# ───────────────────────────────────────────────────────────────────────────────
# Test: Bounding Box Extraction
# ───────────────────────────────────────────────────────────────────────────────


class TestBoundingBoxExtraction:
    """Test _extract_bbox from Google elements."""

    @pytest.fixture
    def client(self) -> "CallGoogleDocumentAI":
        return CallGoogleDocumentAI(GoogleDocumentAIConfig(enabled=False))

    def test_extract_from_normalized_vertices(self, client: CallGoogleDocumentAI) -> None:
        """Normalized vertices [0,1] → pixel coordinates."""
        element = _make_wrapped(x0=0.1, y0=0.2, x1=0.9, y1=0.8)
        bbox = client._extract_bbox(element, page_width=1000, page_height=1000)
        assert bbox.x_min == pytest.approx(100.0)
        assert bbox.y_min == pytest.approx(200.0)
        assert bbox.x_max == pytest.approx(900.0)
        assert bbox.y_max == pytest.approx(800.0)

    def test_extract_from_pixel_vertices(self, client: CallGoogleDocumentAI) -> None:
        """Pixel vertices → used directly."""
        element = _WrappedElement(
            type_="PARAGRAPH",
            bounding_poly=_make_bpoly(100, 200, 900, 800, normalized=False),
            confidence=0.9,
        )
        bbox = client._extract_bbox(element, page_width=1000, page_height=1000)
        assert bbox.x_min == 100
        assert bbox.y_min == 200
        assert bbox.x_max == 900
        assert bbox.y_max == 800

    def test_four_corner_vertices(self, client: CallGoogleDocumentAI) -> None:
        """4-corner polygon → min/max extraction."""
        poly = Mock()
        poly.normalized_vertices = [
            Mock(x=0.1, y=0.1),
            Mock(x=0.9, y=0.1),
            Mock(x=0.9, y=0.9),
            Mock(x=0.1, y=0.9),
        ]
        poly.vertices = []
        element = _WrappedElement(type_="PARAGRAPH", bounding_poly=poly, confidence=0.8)
        bbox = client._extract_bbox(element, page_width=1000, page_height=1000)
        assert bbox.x_min == pytest.approx(100.0)
        assert bbox.y_min == pytest.approx(100.0)
        assert bbox.x_max == pytest.approx(900.0)
        assert bbox.y_max == pytest.approx(900.0)

    def test_bbox_clamped_to_page_bounds(self, client: CallGoogleDocumentAI) -> None:
        """Out-of-bounds vertices are clamped to page."""
        element = _make_wrapped(x0=-0.1, y0=-0.1, x1=1.1, y1=1.1)
        bbox = client._extract_bbox(element, page_width=1000, page_height=1000)
        assert bbox.x_min == 0.0
        assert bbox.y_min == 0.0
        assert bbox.x_max == 1000.0
        assert bbox.y_max == 1000.0

    def test_degenerate_bbox_x_raises(self, client: CallGoogleDocumentAI) -> None:
        """x_min == x_max (zero-width) raises ValueError."""
        element = _make_wrapped(x0=0.5, y0=0.1, x1=0.5, y1=0.9)  # same x → zero width
        with pytest.raises(ValueError):
            client._extract_bbox(element, page_width=1000, page_height=1000)

    def test_degenerate_bbox_y_raises(self, client: CallGoogleDocumentAI) -> None:
        """y_min == y_max (zero-height) raises ValueError."""
        element = _make_wrapped(x0=0.1, y0=0.5, x1=0.9, y1=0.5)  # same y → zero height
        with pytest.raises(ValueError):
            client._extract_bbox(element, page_width=1000, page_height=1000)

    def test_missing_bounding_poly_raises(self, client: CallGoogleDocumentAI) -> None:
        """AttributeError on bounding_poly access → ValueError."""
        element = Mock(spec=[])  # no attributes → AttributeError on access
        with pytest.raises(ValueError):
            client._extract_bbox(element, page_width=1000, page_height=1000)

    def test_none_bounding_poly_raises(self, client: CallGoogleDocumentAI) -> None:
        """bounding_poly = None raises ValueError."""
        element = _WrappedElement(type_="PARAGRAPH", bounding_poly=None, confidence=0.5)
        with pytest.raises(ValueError):
            client._extract_bbox(element, page_width=1000, page_height=1000)

    def test_single_vertex_raises(self, client: CallGoogleDocumentAI) -> None:
        """Single vertex is insufficient → ValueError."""
        poly = Mock()
        poly.normalized_vertices = [Mock(x=0.5, y=0.5)]
        poly.vertices = []
        element = _WrappedElement(type_="PARAGRAPH", bounding_poly=poly, confidence=0.5)
        with pytest.raises(ValueError):
            client._extract_bbox(element, page_width=1000, page_height=1000)

    def test_empty_vertices_raises(self, client: CallGoogleDocumentAI) -> None:
        """No vertices in either field → ValueError."""
        poly = Mock()
        poly.normalized_vertices = []
        poly.vertices = []
        element = _WrappedElement(type_="PARAGRAPH", bounding_poly=poly, confidence=0.5)
        with pytest.raises(ValueError):
            client._extract_bbox(element, page_width=1000, page_height=1000)

    def test_different_page_dimensions(self, client: CallGoogleDocumentAI) -> None:
        """Normalized vertices scale correctly for non-square pages."""
        element = _make_wrapped(x0=0.0, y0=0.0, x1=1.0, y1=1.0)
        bbox = client._extract_bbox(element, page_width=800, page_height=1200)
        assert bbox.x_max == 800.0
        assert bbox.y_max == 1200.0


# ───────────────────────────────────────────────────────────────────────────────
# Test: Type Mapping (_map_google_to_canonical)
# ───────────────────────────────────────────────────────────────────────────────


class TestTypeMappingGoogleToCanonical:
    """Test _map_google_to_canonical."""

    @pytest.fixture
    def client(self) -> "CallGoogleDocumentAI":
        return CallGoogleDocumentAI(GoogleDocumentAIConfig(enabled=False))

    def test_paragraph_to_text_block(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("PARAGRAPH")], 1000, 1000)
        assert len(regions) == 1
        assert regions[0].type == RegionType.text_block

    def test_section_header_to_title(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("SECTION_HEADER")], 1000, 1000)
        assert regions[0].type == RegionType.title

    def test_title_to_title(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("TITLE")], 1000, 1000)
        assert regions[0].type == RegionType.title

    def test_heading_to_title(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("HEADING")], 1000, 1000)
        assert regions[0].type == RegionType.title

    def test_subtitle_to_title(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("SUBTITLE")], 1000, 1000)
        assert regions[0].type == RegionType.title

    def test_table_to_table(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("TABLE")], 1000, 1000)
        assert regions[0].type == RegionType.table

    def test_image_to_image(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("IMAGE")], 1000, 1000)
        assert regions[0].type == RegionType.image

    def test_picture_to_image(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("PICTURE")], 1000, 1000)
        assert regions[0].type == RegionType.image

    def test_photo_to_image(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("PHOTO")], 1000, 1000)
        assert regions[0].type == RegionType.image

    def test_figure_to_image(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("FIGURE")], 1000, 1000)
        assert regions[0].type == RegionType.image

    def test_caption_to_caption(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("CAPTION")], 1000, 1000)
        assert regions[0].type == RegionType.caption

    def test_footer_to_text_block(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("FOOTER")], 1000, 1000)
        assert regions[0].type == RegionType.text_block

    def test_header_to_text_block(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("HEADER")], 1000, 1000)
        assert regions[0].type == RegionType.text_block

    def test_footnote_to_text_block(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("FOOTNOTE")], 1000, 1000)
        assert regions[0].type == RegionType.text_block

    def test_list_item_to_text_block(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("LIST_ITEM")], 1000, 1000)
        assert regions[0].type == RegionType.text_block

    def test_equation_to_text_block(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("EQUATION")], 1000, 1000)
        assert regions[0].type == RegionType.text_block

    def test_form_field_to_text_block(self, client: CallGoogleDocumentAI) -> None:
        regions = client._map_google_to_canonical([_make_wrapped("FORM_FIELD")], 1000, 1000)
        assert regions[0].type == RegionType.text_block

    def test_page_break_skipped(self, client: CallGoogleDocumentAI) -> None:
        """PAGE_BREAK → None mapping → region excluded."""
        regions = client._map_google_to_canonical([_make_wrapped("PAGE_BREAK")], 1000, 1000)
        assert len(regions) == 0

    def test_page_number_skipped(self, client: CallGoogleDocumentAI) -> None:
        """PAGE_NUMBER → None mapping → region excluded."""
        regions = client._map_google_to_canonical([_make_wrapped("PAGE_NUMBER")], 1000, 1000)
        assert len(regions) == 0

    def test_unknown_type_maps_to_text_block(self, client: CallGoogleDocumentAI) -> None:
        """Truly unknown type → conservative text_block."""
        regions = client._map_google_to_canonical([_make_wrapped("FUTURE_TYPE_XYZ")], 1000, 1000)
        assert len(regions) == 1
        assert regions[0].type == RegionType.text_block

    def test_sequential_region_ids(self, client: CallGoogleDocumentAI) -> None:
        """Region IDs are sequential: r0, r1, r2, ..."""
        elements = [
            _make_wrapped("PARAGRAPH", x0=i * 0.1, y0=0.0, x1=i * 0.1 + 0.09, y1=0.1)
            for i in range(4)
        ]
        regions = client._map_google_to_canonical(elements, 1000, 1000)
        assert [r.id for r in regions] == ["r0", "r1", "r2", "r3"]

    def test_skipped_elements_dont_gap_ids(self, client: CallGoogleDocumentAI) -> None:
        """IDs stay sequential even when elements are skipped."""
        elements = [
            _make_wrapped("PARAGRAPH"),
            _make_wrapped("PAGE_BREAK"),  # skipped
            _make_wrapped("TABLE", x0=0.1, y0=0.6, x1=0.9, y1=0.9),
        ]
        regions = client._map_google_to_canonical(elements, 1000, 1000)
        assert len(regions) == 2
        assert regions[0].id == "r0"
        assert regions[1].id == "r1"

    def test_confidence_preserved(self, client: CallGoogleDocumentAI) -> None:
        """Confidence value is preserved from element."""
        element = _make_wrapped("PARAGRAPH", confidence=0.73)
        regions = client._map_google_to_canonical([element], 1000, 1000)
        assert regions[0].confidence == pytest.approx(0.73)

    def test_missing_confidence_defaults_to_0_5(self, client: CallGoogleDocumentAI) -> None:
        """Non-numeric confidence defaults to 0.5."""
        element = _make_wrapped("PARAGRAPH")
        element.confidence = "bad"  # type: ignore
        regions = client._map_google_to_canonical([element], 1000, 1000)
        assert regions[0].confidence == 0.5

    def test_confidence_clamped_below(self, client: CallGoogleDocumentAI) -> None:
        """Confidence below 0 is clamped to 0."""
        element = _make_wrapped("PARAGRAPH", confidence=-0.5)
        regions = client._map_google_to_canonical([element], 1000, 1000)
        assert regions[0].confidence == 0.0

    def test_confidence_clamped_above(self, client: CallGoogleDocumentAI) -> None:
        """Confidence above 1 is clamped to 1."""
        element = _make_wrapped("PARAGRAPH", confidence=1.5)
        regions = client._map_google_to_canonical([element], 1000, 1000)
        assert regions[0].confidence == 1.0

    def test_invalid_bbox_skips_element(self, client: CallGoogleDocumentAI) -> None:
        """Element with degenerate bbox is skipped without crashing."""
        bad_element = _make_wrapped("PARAGRAPH", x0=0.5, y0=0.1, x1=0.5, y1=0.9)  # zero width
        good_element = _make_wrapped("TABLE", x0=0.0, y0=0.5, x1=0.8, y1=0.9)
        regions = client._map_google_to_canonical([bad_element, good_element], 1000, 1000)
        assert len(regions) == 1
        assert regions[0].type == RegionType.table
        assert regions[0].id == "r0"

    def test_empty_input_returns_empty(self, client: CallGoogleDocumentAI) -> None:
        """Empty element list → empty region list."""
        regions = client._map_google_to_canonical([], 1000, 1000)
        assert regions == []

    def test_region_ids_match_pattern(self, client: CallGoogleDocumentAI) -> None:
        """All region IDs match ^r\\d+$."""
        elements = [_make_wrapped("PARAGRAPH") for _ in range(5)]
        regions = client._map_google_to_canonical(elements, 1000, 1000)
        import re

        for region in regions:
            assert re.match(r"^r\d+$", region.id)

    def test_bbox_correct_pixel_values(self, client: CallGoogleDocumentAI) -> None:
        """Normalized bbox [0.1, 0.2, 0.9, 0.8] on 1000×1000 → correct pixels."""
        element = _make_wrapped("PARAGRAPH", x0=0.1, y0=0.2, x1=0.9, y1=0.8)
        regions = client._map_google_to_canonical([element], 1000, 1000)
        assert regions[0].bbox.x_min == pytest.approx(100.0)
        assert regions[0].bbox.y_min == pytest.approx(200.0)
        assert regions[0].bbox.x_max == pytest.approx(900.0)
        assert regions[0].bbox.y_max == pytest.approx(800.0)


# ───────────────────────────────────────────────────────────────────────────────
# Test: Async API Calls (with mocks)
# ───────────────────────────────────────────────────────────────────────────────


class TestAsyncAPICallsWithMocks:
    """Test async API calls with mocked Google responses."""

    @pytest.mark.asyncio
    async def test_process_layout_disabled_returns_none(self) -> None:
        """process_layout returns None when config.enabled=False."""
        config = GoogleDocumentAIConfig(enabled=False)
        client = CallGoogleDocumentAI(config)
        result = await client.process_layout(image_uri="gs://bucket/img.png", material_type="book")
        assert result is None

    @pytest.mark.asyncio
    async def test_process_layout_missing_credentials_returns_none(self) -> None:
        """process_layout returns None when credentials file missing."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test-project",
            processor_id_layout="proc-123",
            credentials_file="/nonexistent/path/key.json",
        )
        client = CallGoogleDocumentAI(config)
        result = await client.process_layout(image_uri="gs://bucket/img.png", material_type="book")
        assert result is None

    @pytest.mark.asyncio
    async def test_retry_on_transient_then_success(self) -> None:
        """First attempt transient error, second attempt succeeds."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
            max_retries=2,
        )
        client = CallGoogleDocumentAI(config)
        call_count = 0

        async def mock_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GoogleAPITransientError("Network timeout")
            return {
                "pages": [],
                "elements": [],
                "page_width": 1000,
                "page_height": 1000,
                "region_count": 0,
            }

        with patch.object(client, "_call_google_api_with_timeout", side_effect=mock_call):
            with patch.object(client, "_lazy_init", return_value=True):
                result = await client.process_layout(
                    image_uri="gs://b/img.png", material_type="book"
                )
                assert result is not None
                assert call_count == 2

    @pytest.mark.asyncio
    async def test_exhausted_retries_returns_none(self) -> None:
        """Exhausting all retries returns None."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
            max_retries=1,
        )
        client = CallGoogleDocumentAI(config)

        async def always_transient(*args: Any, **kwargs: Any) -> NoReturn:
            raise GoogleAPITransientError("Always fails")

        with patch.object(client, "_call_google_api_with_timeout", side_effect=always_transient):
            with patch.object(client, "_lazy_init", return_value=True):
                with patch("asyncio.sleep", return_value=None):
                    result = await client.process_layout(
                        image_uri="gs://b/img.png", material_type="book"
                    )
                    assert result is None

    @pytest.mark.asyncio
    async def test_permanent_error_no_retry(self) -> None:
        """Permanent error returns None immediately (no retry)."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
            max_retries=2,
        )
        client = CallGoogleDocumentAI(config)
        call_count = 0

        async def permanent_fail(*args: Any, **kwargs: Any) -> NoReturn:
            nonlocal call_count
            call_count += 1
            raise GoogleAPIPermanentError("Auth failed")

        with patch.object(client, "_call_google_api_with_timeout", side_effect=permanent_fail):
            with patch.object(client, "_lazy_init", return_value=True):
                result = await client.process_layout(
                    image_uri="gs://b/img.png", material_type="book"
                )
                assert result is None
                assert call_count == 1  # No retry

    @pytest.mark.asyncio
    async def test_mime_type_passed_through(self) -> None:
        """mime_type parameter is passed to _call_google_api_with_timeout."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
        )
        client = CallGoogleDocumentAI(config)
        captured_kwargs: dict[str, Any] = {}

        async def capture_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_kwargs.update(kwargs)
            return {
                "pages": [],
                "elements": [],
                "page_width": 1000,
                "page_height": 1000,
                "region_count": 0,
            }

        with patch.object(client, "_call_google_api_with_timeout", side_effect=capture_call):
            with patch.object(client, "_lazy_init", return_value=True):
                await client.process_layout(
                    image_uri="gs://b/img.png",
                    material_type="book",
                    mime_type="image/jpeg",
                )
                assert captured_kwargs.get("mime_type") == "image/jpeg"

    @pytest.mark.asyncio
    async def test_image_inputs_are_converted_to_pdf_before_google_call(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Image inputs are wrapped in a one-page PDF before the Google request."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
        )
        client = CallGoogleDocumentAI(config)
        captured_kwargs: dict[str, Any] = {}

        async def capture_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_kwargs.update(kwargs)
            return {
                "pages": [],
                "elements": [],
                "page_width": 321,
                "page_height": 654,
                "region_count": 0,
            }

        with patch.object(client, "_call_google_api_with_timeout", side_effect=capture_call):
            with patch.object(client, "_lazy_init", return_value=True):
                with caplog.at_level(logging.DEBUG):
                    await client.process_layout(
                        image_uri="gs://b/img.png",
                        material_type="book",
                        image_bytes=_make_image_bytes(321, 654),
                        mime_type="image/png",
                    )

        assert captured_kwargs["mime_type"] == "application/pdf"
        assert captured_kwargs["image_bytes"].startswith(b"%PDF-")
        assert _extract_pdf_media_box(captured_kwargs["image_bytes"]) == pytest.approx(
            (321.0, 654.0)
        )
        assert "Converting image to PDF for Layout Parser" in caplog.text

    @pytest.mark.asyncio
    async def test_non_image_inputs_skip_pdf_conversion(self) -> None:
        """Existing PDF inputs pass through unchanged."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
        )
        client = CallGoogleDocumentAI(config)
        captured_kwargs: dict[str, Any] = {}
        pdf_bytes = b"%PDF-1.4\nexisting-pdf"

        async def capture_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_kwargs.update(kwargs)
            return {
                "pages": [],
                "elements": [],
                "page_width": 1000,
                "page_height": 1000,
                "region_count": 0,
            }

        with patch.object(client, "_call_google_api_with_timeout", side_effect=capture_call):
            with patch.object(client, "_lazy_init", return_value=True):
                await client.process_layout(
                    image_uri="gs://b/doc.pdf",
                    material_type="book",
                    image_bytes=pdf_bytes,
                    mime_type="application/pdf",
                )

        assert captured_kwargs["mime_type"] == "application/pdf"
        assert captured_kwargs["image_bytes"] == pdf_bytes

    @pytest.mark.asyncio
    async def test_tiff_mime_type_triggers_pdf_conversion(self) -> None:
        """image/tiff inputs are converted to PDF and mime_type is updated."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
        )
        client = CallGoogleDocumentAI(config)
        captured_kwargs: dict[str, Any] = {}

        async def capture_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_kwargs.update(kwargs)
            return {"pages": [], "elements": [], "page_width": 200, "page_height": 300, "region_count": 0}

        with patch.object(client, "_call_google_api_with_timeout", side_effect=capture_call):
            with patch.object(client, "_lazy_init", return_value=True):
                await client.process_layout(
                    image_uri="gs://b/img.tiff",
                    material_type="book",
                    image_bytes=_make_image_bytes(200, 300, image_format="TIFF"),
                    mime_type="image/tiff",
                )

        assert captured_kwargs["mime_type"] == "application/pdf"
        assert captured_kwargs["image_bytes"].startswith(b"%PDF-")

    @pytest.mark.asyncio
    async def test_jpeg_mime_type_triggers_pdf_conversion(self) -> None:
        """image/jpeg inputs are converted to PDF and mime_type is updated."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
        )
        client = CallGoogleDocumentAI(config)
        captured_kwargs: dict[str, Any] = {}

        async def capture_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_kwargs.update(kwargs)
            return {"pages": [], "elements": [], "page_width": 150, "page_height": 250, "region_count": 0}

        with patch.object(client, "_call_google_api_with_timeout", side_effect=capture_call):
            with patch.object(client, "_lazy_init", return_value=True):
                await client.process_layout(
                    image_uri="gs://b/img.jpg",
                    material_type="book",
                    image_bytes=_make_image_bytes(150, 250, image_format="JPEG"),
                    mime_type="image/jpeg",
                )

        assert captured_kwargs["mime_type"] == "application/pdf"
        assert captured_kwargs["image_bytes"].startswith(b"%PDF-")

    def test_layout_process_request_includes_page_field_mask(self) -> None:
        """_call_google_api_sync sends a field_mask with pages.paragraphs and document_layout."""
        from google.cloud import documentai_v1

        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="proj",
            processor_id_layout="proc",
            credentials_file="/fake/key.json",
        )
        client = CallGoogleDocumentAI(config)

        # Build a minimal valid response mock using a real documentai_v1.Document
        mock_response = Mock()
        mock_response.document = documentai_v1.Document(text="content")
        mock_api_client = Mock()
        mock_api_client.process_document.return_value = mock_response
        client._client = mock_api_client

        client._call_google_api_sync(
            image_uri=None,
            image_bytes=b"%PDF-fake",
            mime_type="application/pdf",
        )

        assert mock_api_client.process_document.called
        call_kwargs = mock_api_client.process_document.call_args.kwargs
        request = call_kwargs.get("request") or mock_api_client.process_document.call_args.args[0]

        # ProcessRequest must carry a field_mask with the expected paths
        fm_paths = set(request.field_mask.paths)
        assert "pages.paragraphs" in fm_paths, f"pages.paragraphs missing from field_mask: {fm_paths}"
        assert "document_layout" in fm_paths, f"document_layout missing: {fm_paths}"
        assert "text" in fm_paths, f"text missing: {fm_paths}"
        assert "pages.dimension" in fm_paths, f"pages.dimension missing: {fm_paths}"

    def test_call_google_api_sync_includes_raw_response_json_key(self) -> None:
        """_call_google_api_sync always returns dict with 'raw_response_json' key."""
        from google.cloud import documentai_v1

        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="proj",
            location="us",
            processor_id_layout="proc",
            credentials_file="/fake/key.json",
        )
        client = CallGoogleDocumentAI(config)

        mock_response = Mock()
        mock_response.document = documentai_v1.Document(text="hello")
        mock_api_client = Mock()
        mock_api_client.process_document.return_value = mock_response
        client._client = mock_api_client

        result = client._call_google_api_sync(
            image_uri=None,
            image_bytes=b"%PDF-fake",
            mime_type="application/pdf",
        )

        # Key must always be present (value may be None if serialization fails on Mock)
        assert "raw_response_json" in result

    @pytest.mark.asyncio
    async def test_process_cleanup_returns_none_stub(self) -> None:
        """process_cleanup is a stub — always returns None."""
        config = GoogleDocumentAIConfig(enabled=False)
        client = CallGoogleDocumentAI(config)
        result = await client.process_cleanup(image_bytes=b"fake-image")
        assert result is None

    @pytest.mark.asyncio
    async def test_lazy_init_caches_error(self) -> None:
        """_lazy_init caches error — does not re-attempt after first failure."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            credentials_file="/nonexistent/path.json",
        )
        client = CallGoogleDocumentAI(config)

        # First call sets _init_error
        result1 = await client._lazy_init()
        assert result1 is False
        assert client._init_error is not None

        # Second call returns False from cache (no file system re-check)
        result2 = await client._lazy_init()
        assert result2 is False

    @pytest.mark.asyncio
    async def test_lazy_init_import_error_graceful(self) -> None:
        """_lazy_init handles ImportError (google-cloud-documentai not installed)."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            credentials_file="/fake/existing.json",
        )
        client = CallGoogleDocumentAI(config)

        with patch("os.path.exists", return_value=True):
            with patch("builtins.__import__", side_effect=ImportError("no module")):
                result = await client._lazy_init()
                assert result is False
                assert client._init_error is not None


# ───────────────────────────────────────────────────────────────────────────────
# Test: run_google_layout_analysis (public API)
# ───────────────────────────────────────────────────────────────────────────────


class TestRunGoogleLayoutAnalysis:
    """Test run_google_layout_analysis public API function."""

    @pytest.mark.asyncio
    async def test_disabled_config_returns_failure(self) -> None:
        """Disabled config → success=False, empty regions."""
        config = GoogleDocumentAIConfig(enabled=False)
        regions, metadata = await run_google_layout_analysis(
            image_bytes=b"fake",
            config=config,
        )
        assert regions == []
        assert metadata["success"] is False
        assert metadata["region_count"] == 0

    @pytest.mark.asyncio
    async def test_metadata_structure(self) -> None:
        """Metadata always has all required keys."""
        config = GoogleDocumentAIConfig(enabled=False)
        regions, metadata = await run_google_layout_analysis(
            image_bytes=b"fake",
            config=config,
        )
        required_keys = {
            "success",
            "error",
            "google_response_time_ms",
            "region_count",
            "fallback_used",
            "source",
            "document_layout_block_count",
            "pages_count",
            "text_length",
            "document_layout_blocks_have_geometry",
            "empty_reason",
        }
        assert required_keys.issubset(metadata.keys())

    @pytest.mark.asyncio
    async def test_never_raises_on_disabled(self) -> None:
        """Never raises even with minimal config."""
        regions, metadata = await run_google_layout_analysis(
            image_bytes=b"x",
            config=GoogleDocumentAIConfig(enabled=False),
        )
        assert isinstance(regions, list)
        assert isinstance(metadata, dict)

    @pytest.mark.asyncio
    async def test_never_raises_on_missing_credentials(self) -> None:
        """Never raises when credentials are missing."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/nonexistent/path.json",
        )
        regions, metadata = await run_google_layout_analysis(
            image_bytes=b"fake",
            config=config,
        )
        assert isinstance(regions, list)
        assert metadata["success"] is False

    @pytest.mark.asyncio
    async def test_success_path_returns_regions(self) -> None:
        """Successful API call → regions from mapped elements."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
        )
        # Mock process_layout to return wrapped elements
        elements = [
            _make_wrapped("PARAGRAPH", x0=0.0, y0=0.0, x1=0.5, y1=0.3),
            _make_wrapped("TABLE", x0=0.0, y0=0.4, x1=1.0, y1=0.9),
        ]
        mock_result = {
            "raw_response": None,
            "pages": [],
            "elements": elements,
            "page_width": 1000,
            "page_height": 1000,
            "region_count": 2,
        }

        with patch(
            "services.eep.app.google.document_ai.CallGoogleDocumentAI.process_layout",
            return_value=mock_result,
        ):
            regions, metadata = await run_google_layout_analysis(
                image_bytes=b"fake",
                config=config,
            )

        assert len(regions) == 2
        assert metadata["success"] is True
        assert metadata["region_count"] == 2
        assert metadata["source"] == "google_document_ai"
        assert metadata["document_layout_block_count"] == 0
        assert metadata["pages_count"] == 0
        assert metadata["text_length"] == 0
        assert metadata["document_layout_blocks_have_geometry"] is False
        assert metadata["empty_reason"] is None

    @pytest.mark.asyncio
    async def test_empty_semantic_blocks_without_geometry_exposes_diagnostics(self) -> None:
        """Block-only responses without geometry remain empty-success with explicit diagnostics."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
        )
        mock_result = {
            "raw_response": None,
            "pages": [],
            "elements": [],
            "page_width": 1000,
            "page_height": 1000,
            "region_count": 0,
            "document_layout_block_count": 9,
            "pages_count": 0,
            "text_length": 0,
            "document_layout_blocks_have_geometry": False,
            "empty_reason": "semantic_blocks_without_geometry",
        }

        with patch(
            "services.eep.app.google.document_ai.CallGoogleDocumentAI.process_layout",
            return_value=mock_result,
        ):
            regions, metadata = await run_google_layout_analysis(
                image_bytes=b"fake",
                config=config,
            )

        assert regions == []
        assert metadata["success"] is True
        assert metadata["region_count"] == 0
        assert metadata["document_layout_block_count"] == 9
        assert metadata["pages_count"] == 0
        assert metadata["text_length"] == 0
        assert metadata["document_layout_blocks_have_geometry"] is False
        assert metadata["empty_reason"] == "semantic_blocks_without_geometry"

    @pytest.mark.asyncio
    async def test_none_result_returns_failure(self) -> None:
        """process_layout returning None → success=False."""
        config = GoogleDocumentAIConfig(
            enabled=True,
            project_id="test",
            processor_id_layout="proc",
            credentials_file="/fake/path.json",
        )
        with patch(
            "services.eep.app.google.document_ai.CallGoogleDocumentAI.process_layout",
            return_value=None,
        ):
            regions, metadata = await run_google_layout_analysis(
                image_bytes=b"fake",
                config=config,
            )
        assert regions == []
        assert metadata["success"] is False
        assert metadata["source"] == "none"

    @pytest.mark.asyncio
    async def test_mime_type_jpeg(self) -> None:
        """mime_type='image/jpeg' is accepted without error."""
        config = GoogleDocumentAIConfig(enabled=False)
        regions, metadata = await run_google_layout_analysis(
            image_bytes=b"fake",
            mime_type="image/jpeg",
            config=config,
        )
        assert isinstance(regions, list)

    @pytest.mark.asyncio
    async def test_response_time_present_and_positive(self) -> None:
        """google_response_time_ms is a positive float."""
        config = GoogleDocumentAIConfig(enabled=False)
        _, metadata = await run_google_layout_analysis(image_bytes=b"x", config=config)
        assert metadata["google_response_time_ms"] >= 0

    @pytest.mark.asyncio
    async def test_default_config_used_when_none(self) -> None:
        """run_google_layout_analysis creates default config when None passed."""
        # Should not raise; will fail gracefully (no credentials)
        regions, metadata = await run_google_layout_analysis(image_bytes=b"x", config=None)
        assert isinstance(regions, list)
        assert isinstance(metadata, dict)

    @pytest.mark.asyncio
    async def test_fallback_used_always_true(self) -> None:
        """fallback_used is always True (Google is always the fallback)."""
        config = GoogleDocumentAIConfig(enabled=False)
        _, metadata = await run_google_layout_analysis(image_bytes=b"x", config=config)
        assert metadata["fallback_used"] is True


# ───────────────────────────────────────────────────────────────────────────────
# Test: run_google_cleanup (public API)
# ───────────────────────────────────────────────────────────────────────────────


class TestRunGoogleCleanup:
    """Test run_google_cleanup public API function."""

    @pytest.mark.asyncio
    async def test_stub_returns_none_bytes(self) -> None:
        """Stub implementation returns (None, metadata)."""
        config = GoogleDocumentAIConfig(enabled=False)
        cleaned, metadata = await run_google_cleanup(image_bytes=b"image", config=config)
        assert cleaned is None

    @pytest.mark.asyncio
    async def test_stub_returns_not_implemented(self) -> None:
        """Metadata indicates not yet implemented."""
        config = GoogleDocumentAIConfig(enabled=False)
        _, metadata = await run_google_cleanup(image_bytes=b"image", config=config)
        assert metadata["implemented"] is False
        assert metadata["success"] is False

    @pytest.mark.asyncio
    async def test_metadata_structure(self) -> None:
        """Metadata always has required keys."""
        config = GoogleDocumentAIConfig(enabled=False)
        _, metadata = await run_google_cleanup(image_bytes=b"image", config=config)
        required_keys = {"success", "error", "google_response_time_ms", "implemented"}
        assert required_keys.issubset(metadata.keys())

    @pytest.mark.asyncio
    async def test_never_raises(self) -> None:
        """run_google_cleanup never raises."""
        cleaned, metadata = await run_google_cleanup(
            image_bytes=b"x",
            config=GoogleDocumentAIConfig(enabled=False),
        )
        assert isinstance(metadata, dict)

    @pytest.mark.asyncio
    async def test_default_config_when_none(self) -> None:
        """Uses default config when None passed."""
        cleaned, metadata = await run_google_cleanup(image_bytes=b"x", config=None)
        assert isinstance(metadata, dict)

    @pytest.mark.asyncio
    async def test_response_time_non_negative(self) -> None:
        """google_response_time_ms >= 0."""
        config = GoogleDocumentAIConfig(enabled=False)
        _, metadata = await run_google_cleanup(image_bytes=b"x", config=config)
        assert metadata["google_response_time_ms"] >= 0

    @pytest.mark.asyncio
    async def test_future_success_path(self) -> None:
        """When process_cleanup returns bytes, metadata reflects success."""
        config = GoogleDocumentAIConfig(enabled=False)
        with patch(
            "services.eep.app.google.document_ai.CallGoogleDocumentAI.process_cleanup",
            return_value=b"cleaned-image-bytes",
        ):
            cleaned, metadata = await run_google_cleanup(image_bytes=b"raw", config=config)
        assert cleaned == b"cleaned-image-bytes"
        assert metadata["success"] is True
        assert metadata["implemented"] is True
        assert metadata["error"] is None
