"""
services/iep2b/app/class_mapping.py
-------------------------------------
IEP2B native-to-canonical class mapping (Packet 6.4).

DocLayout-YOLO uses a DocStructBench-aligned class vocabulary.  Before
returning a LayoutDetectResponse every detected native class is mapped to
LibraryAI's canonical 5-class RegionType schema; any class with no canonical
equivalent is excluded from the output.

NATIVE_TO_CANONICAL maps lowercase native class names to RegionType or None.
None means "exclude this region entirely".

Phase 12 note: The exact native class names depend on the pretrained
checkpoint loaded.  Verify this table against the model card and class-index
file shipped with the production DocLayout-YOLO weights before deployment.
Mapping errors here propagate directly to the consensus gate's matching
accuracy, so correctness is critical.
"""

from __future__ import annotations

from shared.schemas.layout import RegionType

# ---------------------------------------------------------------------------
# Native class name → canonical RegionType  (None = exclude)
# ---------------------------------------------------------------------------
# Keys are lowercase to allow case-insensitive lookup via map_native_class().
# Covers the DocStructBench vocabulary and common DocLayout-YOLO checkpoints.

NATIVE_TO_CANONICAL: dict[str, RegionType | None] = {
    # ── text / paragraph variants ────────────────────────────────────────────
    "text": RegionType.text_block,
    "plain text": RegionType.text_block,
    "paragraph": RegionType.text_block,
    "body text": RegionType.text_block,
    "list": RegionType.text_block,
    "list_item": RegionType.text_block,
    "list-item": RegionType.text_block,
    "abstract": RegionType.text_block,
    "reference": RegionType.text_block,
    "references": RegionType.text_block,
    "footnote": RegionType.text_block,
    "footer": RegionType.text_block,
    "page-footer": RegionType.text_block,
    "page_footer": RegionType.text_block,
    # ── title / heading variants ─────────────────────────────────────────────
    "title": RegionType.title,
    "section-header": RegionType.title,
    "section_header": RegionType.title,
    "header": RegionType.title,
    "page-header": RegionType.title,
    "page_header": RegionType.title,
    "headline": RegionType.title,
    # ── table ────────────────────────────────────────────────────────────────
    "table": RegionType.table,
    # ── figure / image variants ──────────────────────────────────────────────
    "figure": RegionType.image,
    "image": RegionType.image,
    "picture": RegionType.image,
    # ── caption variants ─────────────────────────────────────────────────────
    "caption": RegionType.caption,
    "figure_caption": RegionType.caption,
    "figure-caption": RegionType.caption,
    "table_caption": RegionType.caption,
    "table-caption": RegionType.caption,
    "table_footnote": RegionType.caption,
    "table-footnote": RegionType.caption,
    "formula_caption": RegionType.caption,
    "formula-caption": RegionType.caption,
    # ── exclude — no canonical equivalent ────────────────────────────────────
    "abandon": None,  # DocLayout-YOLO noise / background class
    "isolate_formula": None,  # standalone mathematical formula
    "isolate-formula": None,
    "formula": None,
    "equation": None,
    "algorithm": None,
    "code": None,
    "page_number": None,
    "page-number": None,
    "toc": None,  # table of contents
}


def map_native_class(native_class: str) -> RegionType | None:
    """
    Map a native DocLayout-YOLO class label to a canonical RegionType.

    Lookup is case-insensitive.  Returns None for unknown or explicitly
    excluded classes; callers must discard regions that map to None.

    Args:
        native_class: Raw class label from the DocLayout-YOLO model output.

    Returns:
        Canonical RegionType if a mapping exists, None otherwise.
    """
    return NATIVE_TO_CANONICAL.get(native_class.strip().lower())
