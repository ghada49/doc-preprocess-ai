"""
services/eep/app/google/document_ai.py
--------------------------------------
Google Document AI integration for layout analysis and artifact cleanup.

Implements two public functions:
  - run_google_layout_analysis() → tuple[list[Region], dict] for IEP2 adjudication
  - run_google_cleanup() → tuple[bytes | None, dict] for IEP1 rescue

Supports credential loading from environment (GOOGLE_APPLICATION_CREDENTIALS)
or mounted Kubernetes secret (/var/secrets/google/key.json).

Handles:
  - Async API calls with configurable timeouts (90s layout, 120s cleanup)
  - Retry logic with exponential backoff (max 2 retries)
  - Error classification: transient (network, timeout, 429) vs permanent
  - Mapping of Google's native layout classes to canonical LibraryAI ontology
  - Comprehensive logging with request/response digest
  - Graceful degradation when credentials missing or Google disabled
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from shared.schemas.layout import Region, RegionType
from shared.schemas.ucf import BoundingBox

logger = logging.getLogger(__name__)

__all__ = [
    "GoogleDocumentAIConfig",
    "CallGoogleDocumentAI",
    "run_google_layout_analysis",
    "run_google_cleanup",
]


# ───────────────────────────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────────────────────────


@dataclass
class GoogleDocumentAIConfig:
    """
    Configuration for Google Document AI integration.

    Fields:
        enabled               — Enable/disable Google fallback entirely
        project_id            — GCP project ID
        location              — GCP region (e.g. "us")
        processor_id_layout   — Processor ID for layout analysis
        processor_id_cleanup  — Processor ID for artifact cleanup (future use)
        timeout_layout_seconds   — Timeout for layout API calls (default 90s)
        timeout_cleanup_seconds  — Timeout for cleanup API calls (default 120s)
        max_retries           — Max transient retries (default 2)
        fallback_on_timeout   — Legacy config flag retained for compatibility;
                                IEP2 now falls back to local display output on timeout
        credentials_file      — Path to service account key JSON
    """

    enabled: bool = True
    project_id: str = ""
    location: str = "us"
    processor_id_layout: str = ""
    processor_id_cleanup: str = ""
    timeout_layout_seconds: int = 90
    timeout_cleanup_seconds: int = 120
    max_retries: int = 2
    fallback_on_timeout: bool = True
    credentials_file: str = field(
        default_factory=lambda: os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS", "/var/secrets/google/key.json"
        )
    )

    def validate(self) -> tuple[bool, str]:
        """
        Validate configuration.

        Returns:
            (is_valid, message) — if not valid, message explains why
        """
        if not self.enabled:
            return True, "Google Document AI disabled in config"

        if not self.project_id:
            return False, "project_id not set"
        if not self.processor_id_layout:
            return False, "processor_id_layout not set"
        if self.timeout_layout_seconds <= 0:
            return False, "timeout_layout_seconds must be > 0"
        if self.timeout_cleanup_seconds <= 0:
            return False, "timeout_cleanup_seconds must be > 0"

        return True, "Config valid"


# ───────────────────────────────────────────────────────────────────────────────
# Google → Canonical Mapping
# ───────────────────────────────────────────────────────────────────────────────

# Google Document AI element types and their canonical mappings.
#
# Google's native types include:
#   PAGE_BREAK, SECTION_HEADER, PARAGRAPH, TABLE, FORM_FIELD, IMAGE,
#   CAPTION, EQUATION, LIST_ITEM, PAGE_NUMBER, FOOTER, HEADER, etc.
#
# Mapped to None means "skip this element" (not a content region).
# Unknown types not in this dict are mapped conservatively to text_block.

_GOOGLE_TO_CANONICAL: dict[str, RegionType | None] = {
    "PARAGRAPH": RegionType.text_block,
    "SECTION_HEADER": RegionType.title,
    "TITLE": RegionType.title,
    "HEADING": RegionType.title,
    "SUBTITLE": RegionType.title,
    "TABLE": RegionType.table,
    "FORM_FIELD": RegionType.text_block,  # Conservative: treat as text
    "IMAGE": RegionType.image,
    "PICTURE": RegionType.image,
    "PHOTO": RegionType.image,
    "FIGURE": RegionType.image,
    "CAPTION": RegionType.caption,
    "FOOTER": RegionType.text_block,  # Conservative: footer is metadata
    "HEADER": RegionType.text_block,  # Conservative: header is metadata
    "PAGE_BREAK": None,  # Skip: not a content region
    "PAGE_NUMBER": None,  # Skip: not a content region
    "FOOTNOTE": RegionType.text_block,
    "ENDNOTE": RegionType.text_block,
    "LIST_ITEM": RegionType.text_block,
    "EQUATION": RegionType.text_block,
}


# ───────────────────────────────────────────────────────────────────────────────
# Internal element wrapper
# ───────────────────────────────────────────────────────────────────────────────


@dataclass
class _WrappedElement:
    """
    Normalized representation of a Google Document AI element.

    Unifies entity-based (Form Parser) and page-element-based (Layout Parser)
    response structures so that _map_google_to_canonical() works for both.

    Fields:
        type_        — Google element type string (e.g. "PARAGRAPH", "TABLE")
        bounding_poly — Google BoundingPoly object with normalized_vertices or vertices
        confidence   — detection confidence in [0, 1]
    """

    type_: str
    bounding_poly: Any  # google.cloud.documentai_v1.types.BoundingPoly
    confidence: float


# ───────────────────────────────────────────────────────────────────────────────
# Error Classification
# ───────────────────────────────────────────────────────────────────────────────


class GoogleAPIError(Exception):
    """Base exception for Google API errors."""

    pass


class GoogleAPITransientError(GoogleAPIError):
    """Transient error (retry eligible)."""

    pass


class GoogleAPIPermanentError(GoogleAPIError):
    """Permanent error (do not retry)."""

    pass


def _classify_error(error: Exception, http_status: int | None = None) -> tuple[str, str]:
    """
    Classify an error as 'transient' or 'permanent'.

    Returns:
        (classification, reason) — e.g. ('transient', 'Request timeout after 90s')
    """
    if isinstance(error, asyncio.TimeoutError):
        return "transient", f"Request timeout: {error}"

    if http_status is not None:
        if http_status == 429:
            return "transient", f"Rate limited (HTTP {http_status})"
        if 500 <= http_status < 600:
            return "transient", f"Server error (HTTP {http_status})"
        if http_status in (401, 403):
            return "permanent", f"Auth failure (HTTP {http_status})"
        if http_status == 404:
            return "permanent", f"Processor not found (HTTP {http_status})"
        if http_status == 400:
            return "permanent", f"Bad request (HTTP {http_status})"

    error_str = str(error).lower()
    if "timeout" in error_str or "deadline" in error_str:
        return "transient", f"Timeout: {error}"
    if "unauthenticated" in error_str or "permission" in error_str:
        return "permanent", f"Auth error: {error}"
    if "not found" in error_str:
        return "permanent", f"Resource not found: {error}"

    # Default to transient for unknown errors (safer to retry)
    return "transient", f"Unknown error: {error}"


# ───────────────────────────────────────────────────────────────────────────────
# Main Client
# ───────────────────────────────────────────────────────────────────────────────


class CallGoogleDocumentAI:
    """
    Wrapper for Google Document AI API calls.

    Handles:
      - Credential loading from env or K8s secret
      - Async document processing with retries
      - Error classification and logging
      - Mapping of Google's native types to canonical ontology
    """

    def __init__(self, config: GoogleDocumentAIConfig):
        """
        Initialize the Google Document AI client.

        Args:
            config — GoogleDocumentAIConfig instance

        Note:
            Credentials are loaded lazily on first API call, not in __init__.
            This allows graceful degradation if credentials are missing.
        """
        self.config = config
        self._client: Any = None
        self._credentials_valid = False
        self._init_error: str | None = None

    async def _lazy_init(self) -> bool:
        """
        Initialize the Google Cloud DocumentAI client (lazy).

        Loads credentials from environment or mounted K8s secret.
        Returns True if initialization successful, False otherwise.
        Credentials are cached to avoid repeated initialization attempts.
        """
        if self._client is not None:
            return self._credentials_valid

        if self._init_error is not None:
            logger.debug("_lazy_init: cached init error, skipping: %s", self._init_error)
            return False

        creds_file = self.config.credentials_file
        if not os.path.exists(creds_file):
            msg = (
                f"Google credentials file not found at {creds_file}. "
                f"Expected GOOGLE_APPLICATION_CREDENTIALS={creds_file} or "
                f"/var/secrets/google/key.json (K8s Secret mount)"
            )
            self._init_error = msg
            logger.warning("CallGoogleDocumentAI._lazy_init: %s", msg)
            return False

        try:
            from google.cloud import documentai_v1  # noqa: F401

            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_file
            self._client = documentai_v1.DocumentProcessorServiceClient()
            self._credentials_valid = True
            logger.info("CallGoogleDocumentAI: client initialized successfully")
            return True

        except ImportError as e:
            msg = f"google-cloud-documentai package not installed: {e}. " f"Add to requirements.txt"
            self._init_error = msg
            logger.error("CallGoogleDocumentAI._lazy_init: %s", msg)
            return False

        except Exception as e:
            msg = f"Failed to initialize Google Document AI client: {e}"
            self._init_error = msg
            logger.error("CallGoogleDocumentAI._lazy_init: %s", msg)
            return False

    async def process_layout(
        self,
        image_uri: str,
        material_type: str,
        job_id: str | None = None,
        image_bytes: bytes | None = None,
        mime_type: str = "image/png",
    ) -> dict[str, Any] | None:
        """
        Process a page image for layout analysis using Google Document AI.

        Args:
            image_uri    — GCS URI or placeholder (used when image_bytes is None)
            material_type — "book" | "newspaper" | "archival_document"
            job_id       — Job ID for logging (optional)
            image_bytes  — Raw image bytes (preferred over image_uri)
            mime_type    — MIME type for image_bytes (default "image/png")

        Returns:
            dict with keys:
              - pages: list of Google page objects
              - elements: list[_WrappedElement] ready for _map_google_to_canonical
              - page_width: int
              - page_height: int
              - region_count: int
              - raw_response: complete Google API response (for audit)
            Or None on any error (transient exhausted, permanent, credentials missing).
        """
        if not self.config.enabled:
            logger.debug("process_layout: Google Document AI disabled in config")
            return None

        if not await self._lazy_init():
            return None

        start_time = time.time()
        attempt = 0
        last_error: Exception | None = None

        while attempt <= self.config.max_retries:
            attempt += 1
            try:
                logger.debug(
                    "process_layout: attempt %d/%d, job_id=%s, image_uri=%s",
                    attempt,
                    self.config.max_retries + 1,
                    job_id or "unknown",
                    image_uri,
                )

                result = await self._call_google_api_with_timeout(
                    image_uri=image_uri,
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                    timeout_sec=self.config.timeout_layout_seconds,
                )

                elapsed_ms = (time.time() - start_time) * 1000
                logger.info(
                    "process_layout: success after %d attempt(s), job_id=%s, "
                    "regions=%d, elapsed=%.0fms",
                    attempt,
                    job_id or "unknown",
                    result.get("region_count", 0),
                    elapsed_ms,
                )
                return result

            except (TimeoutError, GoogleAPITransientError) as e:
                last_error = e
                _, reason = _classify_error(e)
                logger.warning(
                    "process_layout: transient error on attempt %d: %s",
                    attempt,
                    reason,
                )
                if attempt <= self.config.max_retries:
                    wait_sec = 2 ** (attempt - 1)
                    logger.debug("process_layout: retrying after %ds", wait_sec)
                    await asyncio.sleep(wait_sec)
                continue

            except GoogleAPIPermanentError as e:
                _, reason = _classify_error(e)
                logger.error(
                    "process_layout: permanent error on attempt %d, job_id=%s: %s",
                    attempt,
                    job_id or "unknown",
                    reason,
                )
                return None

        elapsed_ms = (time.time() - start_time) * 1000
        logger.error(
            "process_layout: exhausted max retries (%d), job_id=%s, "
            "last_error=%s, elapsed=%.0fms",
            self.config.max_retries + 1,
            job_id or "unknown",
            last_error,
            elapsed_ms,
        )
        return None

    async def process_cleanup(
        self,
        image_bytes: bytes,
        job_id: str | None = None,
    ) -> bytes | None:
        """
        Process image for artifact cleanup using Google Document AI (stub).

        Reserved for IEP1 rescue. Returns None until processor_id_cleanup
        is provisioned and this method is fully implemented.

        Args:
            image_bytes — Raw image bytes
            job_id      — Job ID for logging (optional)

        Returns:
            Cleaned image bytes, or None if not implemented / failed
        """
        logger.debug("process_cleanup: not yet implemented (reserved for IEP1 rescue)")
        return None

    async def _call_google_api_with_timeout(
        self,
        image_uri: str,
        image_bytes: bytes | None,
        mime_type: str,
        timeout_sec: int,
    ) -> dict[str, Any]:
        """
        Call Google Document AI with timeout.

        Raises:
            GoogleAPITransientError — retryable errors (timeout, network, 429)
            GoogleAPIPermanentError — permanent errors (auth, bad request, etc.)
        """
        try:
            result = await asyncio.wait_for(
                self._call_google_api_inner(
                    image_uri=image_uri,
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                ),
                timeout=timeout_sec,
            )
            return result
        except TimeoutError as e:
            raise GoogleAPITransientError(f"Layout analysis timeout after {timeout_sec}s") from e

    async def _call_google_api_inner(
        self,
        image_uri: str,
        image_bytes: bytes | None,
        mime_type: str,
    ) -> dict[str, Any]:
        """Inner Google API call (runs in asyncio executor to avoid blocking)."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            self._call_google_api_sync,
            image_uri,
            image_bytes,
            mime_type,
        )
        return result

    def _call_google_api_sync(
        self,
        image_uri: str,
        image_bytes: bytes | None,
        mime_type: str,
    ) -> dict[str, Any]:
        """
        Synchronous call to Google Document AI API (runs in thread pool).

        Returns:
            dict with:
              - raw_response: full Google API response object
              - pages: list of Document.Page objects
              - elements: list[_WrappedElement] ready for _map_google_to_canonical
              - page_width: int (from Google response or default 1000)
              - page_height: int (from Google response or default 1000)
              - region_count: int
        """
        try:
            from google.cloud import documentai_v1

            if not self._client:
                raise GoogleAPIPermanentError("Google client not initialized")

            processor_name = _build_processor_name(
                self.config.project_id,
                self.config.location,
                self.config.processor_id_layout,
            )

            if image_bytes:
                request = documentai_v1.ProcessRequest(
                    name=processor_name,
                    raw_document=documentai_v1.RawDocument(
                        content=image_bytes,
                        mime_type=mime_type,
                    ),
                )
            else:
                request = documentai_v1.ProcessRequest(
                    name=processor_name,
                    gcs_document=documentai_v1.GcsDocument(gcs_uri=image_uri),
                )

            response = self._client.process_document(request=request)
            document = response.document
            pages = list(document.pages) if document and document.pages else []

            # Extract page dimensions from first page
            page_width = 1000
            page_height = 1000
            if pages and hasattr(pages[0], "dimension") and pages[0].dimension:
                w = int(pages[0].dimension.width)
                h = int(pages[0].dimension.height)
                if w > 0:
                    page_width = w
                if h > 0:
                    page_height = h

            elements = _extract_elements_from_response(document, pages)

            return {
                "raw_response": response,
                "pages": pages,
                "elements": elements,
                "page_width": page_width,
                "page_height": page_height,
                "region_count": len(elements),
            }

        except (GoogleAPITransientError, GoogleAPIPermanentError):
            raise
        except Exception as e:
            classification, reason = _classify_error(e)
            if classification == "transient":
                raise GoogleAPITransientError(reason) from e
            else:
                raise GoogleAPIPermanentError(reason) from e

    def _map_google_to_canonical(
        self,
        google_elements: list[Any],
        page_width: int,
        page_height: int,
    ) -> list[Region]:
        """
        Map Google Document AI elements to canonical Region schema.

        Accepts both _WrappedElement instances (production) and duck-typed
        objects with .type_, .bounding_poly, .confidence (tests).

        Args:
            google_elements — list of elements with .type_, .bounding_poly, .confidence
            page_width      — page width in pixels
            page_height     — page height in pixels

        Returns:
            list of canonical Region objects (r0, r1, r2, ...)

        Algorithm:
          1. For each element, look up type_ in _GOOGLE_TO_CANONICAL
          2. If mapped to None (PAGE_BREAK etc.), skip
          3. If not in dict (truly unknown), map conservatively to text_block
          4. Extract bbox; skip element if bbox is invalid
          5. Clamp confidence to [0, 1]; default to 0.5 if non-numeric
        """
        regions: list[Region] = []
        unmapped_types: set[str] = set()

        for idx, element in enumerate(google_elements):
            google_type = getattr(element, "type_", "UNKNOWN")

            if google_type in _GOOGLE_TO_CANONICAL:
                canonical_type = _GOOGLE_TO_CANONICAL[google_type]
                if canonical_type is None:
                    # Explicitly skipped (PAGE_BREAK, PAGE_NUMBER)
                    logger.debug("_map_google_to_canonical: skipping type '%s'", google_type)
                    continue
            else:
                # Unknown type → conservative text_block
                canonical_type = RegionType.text_block
                unmapped_types.add(google_type)

            try:
                bbox = self._extract_bbox(element, page_width, page_height)
            except ValueError as e:
                logger.warning(
                    "_map_google_to_canonical: invalid bbox for element %d (%s): %s, skipping",
                    idx,
                    google_type,
                    e,
                )
                continue

            raw_conf = getattr(element, "confidence", None)
            if not isinstance(raw_conf, int | float):
                confidence = 0.5
            else:
                confidence = max(0.0, min(1.0, float(raw_conf)))

            regions.append(
                Region(
                    id=f"r{len(regions)}",
                    type=canonical_type,
                    bbox=bbox,
                    confidence=confidence,
                )
            )

        for utype in unmapped_types:
            logger.warning(
                "_map_google_to_canonical: unknown Google type '%s' → mapped to text_block",
                utype,
            )

        logger.info(
            "_map_google_to_canonical: %d elements → %d canonical regions; " "unmapped types: %s",
            len(google_elements),
            len(regions),
            unmapped_types or "none",
        )
        return regions

    def _extract_bbox(
        self,
        element: Any,
        page_width: int,
        page_height: int,
    ) -> BoundingBox:
        """
        Extract BoundingBox from a Google element.

        Google bounding_poly has either:
          - normalized_vertices: points normalized to [0, 1]
          - vertices: pixel coordinates

        Returns:
            BoundingBox in pixel coordinates, clamped to page bounds

        Raises:
            ValueError if bbox cannot be extracted or is degenerate
        """
        try:
            bounding_poly = element.bounding_poly
        except AttributeError:
            raise ValueError("element has no bounding_poly")

        if bounding_poly is None:
            raise ValueError("bounding_poly is None")

        vertices = None
        if hasattr(bounding_poly, "normalized_vertices") and bounding_poly.normalized_vertices:
            vertices = [
                (v.x * page_width, v.y * page_height) for v in bounding_poly.normalized_vertices
            ]
        elif hasattr(bounding_poly, "vertices") and bounding_poly.vertices:
            vertices = [(v.x, v.y) for v in bounding_poly.vertices]

        if not vertices or len(vertices) < 2:
            raise ValueError("bounding_poly has insufficient vertices")

        xs = [v[0] for v in vertices]
        ys = [v[1] for v in vertices]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)

        if x_min >= x_max or y_min >= y_max:
            raise ValueError(f"degenerate bbox: x=[{x_min},{x_max}] y=[{y_min},{y_max}]")

        # Clamp to page bounds
        x_min = max(0.0, x_min)
        y_min = max(0.0, y_min)
        x_max = min(float(page_width), x_max)
        y_max = min(float(page_height), y_max)

        return BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


# ───────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ───────────────────────────────────────────────────────────────────────────────


def _build_processor_name(project_id: str, location: str, processor_id: str) -> str:
    """Build Google Document AI processor resource name."""
    return f"projects/{project_id}/locations/{location}/processors/{processor_id}"


def _extract_elements_from_response(
    document: Any,
    pages: list[Any],
) -> list[_WrappedElement]:
    """
    Extract typed _WrappedElement list from a Google Document AI response.

    Strategy:
      1. Entity-based processor (e.g. Form Parser, Enterprise Document AI):
         If document.entities is non-empty and entities have page_anchor,
         extract entities — each has type_ and bounding_poly.
      2. Page-element-based processor (Layout Parser):
         Extract from typed page collections:
           - page.blocks  → type "PARAGRAPH"
           - page.tables  → type "TABLE"
           - page.paragraphs → type "PARAGRAPH" (fallback if no blocks)

    Returns:
        list[_WrappedElement] — empty list if nothing extractable
    """
    elements: list[_WrappedElement] = []

    # Strategy 1: entity-based processor
    if document and hasattr(document, "entities") and document.entities:
        for entity in document.entities:
            try:
                page_anchor = getattr(entity, "page_anchor", None)
                if not page_anchor or not page_anchor.page_refs:
                    continue
                bpoly = page_anchor.page_refs[0].bounding_poly
                elements.append(
                    _WrappedElement(
                        type_=getattr(entity, "type_", "PARAGRAPH"),
                        bounding_poly=bpoly,
                        confidence=float(getattr(entity, "confidence", 0.5)),
                    )
                )
            except Exception:
                continue
        if elements:
            return elements

    # Strategy 2: page-element-based processor (Layout Parser)
    if not pages:
        return elements

    page = pages[0]

    # Blocks → PARAGRAPH
    if hasattr(page, "blocks") and page.blocks:
        for block in page.blocks:
            try:
                layout = block.layout
                elements.append(
                    _WrappedElement(
                        type_="PARAGRAPH",
                        bounding_poly=layout.bounding_poly,
                        confidence=float(getattr(layout, "confidence", 0.5)),
                    )
                )
            except Exception:
                continue

    # Tables → TABLE
    if hasattr(page, "tables") and page.tables:
        for table in page.tables:
            try:
                layout = table.layout
                elements.append(
                    _WrappedElement(
                        type_="TABLE",
                        bounding_poly=layout.bounding_poly,
                        confidence=float(getattr(layout, "confidence", 0.5)),
                    )
                )
            except Exception:
                continue

    # If no blocks/tables, fall back to paragraph-level elements
    if not elements and hasattr(page, "paragraphs") and page.paragraphs:
        for para in page.paragraphs:
            try:
                layout = para.layout
                elements.append(
                    _WrappedElement(
                        type_="PARAGRAPH",
                        bounding_poly=layout.bounding_poly,
                        confidence=float(getattr(layout, "confidence", 0.5)),
                    )
                )
            except Exception:
                continue

    return elements


# ───────────────────────────────────────────────────────────────────────────────
# Public API Functions
# ───────────────────────────────────────────────────────────────────────────────


async def run_google_layout_analysis(
    image_bytes: bytes,
    image_uri: str | None = None,
    material_type: str = "document",
    job_id: str | None = None,
    mime_type: str = "image/png",
    config: GoogleDocumentAIConfig | None = None,
) -> tuple[list[Region], dict[str, Any]]:
    """
    Run Google Document AI layout analysis on an image.

    Main entry point for IEP2 adjudication fallback.

    Args:
        image_bytes   — Raw image bytes (PNG, JPEG, etc.)
        image_uri     — GCS URI (optional; used when image_bytes is None)
        material_type — "book" | "newspaper" | "archival_document"
        job_id        — Job ID for logging
        mime_type     — MIME type for image_bytes (default "image/png")
        config        — GoogleDocumentAIConfig; uses defaults if None

    Returns:
        (regions, metadata) where:
          - regions: list[Region] — canonical layout regions (empty if failed)
          - metadata: dict:
              - success: bool
              - error: str | None
              - google_response_time_ms: float
              - region_count: int
              - fallback_used: bool
              - source: "google_document_ai" | "none"

    Never raises. Returns ([], {"success": False, ...}) on any error.
    """
    if config is None:
        config = GoogleDocumentAIConfig()

    start_time = time.time()
    client = CallGoogleDocumentAI(config)

    try:
        result = await client.process_layout(
            image_uri=image_uri or "unknown",
            material_type=material_type,
            job_id=job_id,
            image_bytes=image_bytes,
            mime_type=mime_type,
        )

        if result is None:
            return (
                [],
                {
                    "success": False,
                    "error": "Google Document AI returned None",
                    "google_response_time_ms": (time.time() - start_time) * 1000,
                    "region_count": 0,
                    "fallback_used": True,
                    "source": "none",
                },
            )

        page_width: int = result.get("page_width", 1000)
        page_height: int = result.get("page_height", 1000)
        elements: list[Any] = result.get("elements", [])

        regions = client._map_google_to_canonical(
            google_elements=elements,
            page_width=page_width,
            page_height=page_height,
        )

        return (
            regions,
            {
                "success": True,
                "error": None,
                "google_response_time_ms": (time.time() - start_time) * 1000,
                "region_count": len(regions),
                "fallback_used": True,
                "source": "google_document_ai",
            },
        )

    except Exception as e:
        logger.exception("run_google_layout_analysis: unexpected error: %s", e)
        return (
            [],
            {
                "success": False,
                "error": str(e),
                "google_response_time_ms": (time.time() - start_time) * 1000,
                "region_count": 0,
                "fallback_used": True,
                "source": "none",
            },
        )


async def run_google_cleanup(
    image_bytes: bytes,
    job_id: str | None = None,
    config: GoogleDocumentAIConfig | None = None,
) -> tuple[bytes | None, dict[str, Any]]:
    """
    Run Google Document AI artifact cleanup on an image.

    Reserved for IEP1 rescue: improve image quality before re-running
    IEP1 geometry detection on a difficult page.

    Args:
        image_bytes — Raw image bytes to clean up
        job_id      — Job ID for logging (optional)
        config      — GoogleDocumentAIConfig; uses defaults if None

    Returns:
        (cleaned_bytes, metadata) where:
          - cleaned_bytes: bytes | None — improved image (None until implemented)
          - metadata: dict:
              - success: bool
              - error: str | None
              - google_response_time_ms: float
              - implemented: bool — False until processor_id_cleanup is active

    Never raises. Returns (None, {"success": False, ...}) on any error.

    Note:
        Currently a stub. Will be activated when processor_id_cleanup is
        configured in GoogleDocumentAIConfig and the cleanup processor
        is provisioned in GCP.
    """
    if config is None:
        config = GoogleDocumentAIConfig()

    start_time = time.time()
    client = CallGoogleDocumentAI(config)

    try:
        cleaned = await client.process_cleanup(image_bytes=image_bytes, job_id=job_id)
        elapsed_ms = (time.time() - start_time) * 1000

        if cleaned is None:
            return (
                None,
                {
                    "success": False,
                    "error": "cleanup not yet implemented",
                    "google_response_time_ms": elapsed_ms,
                    "implemented": False,
                },
            )

        return (
            cleaned,
            {
                "success": True,
                "error": None,
                "google_response_time_ms": elapsed_ms,
                "implemented": True,
            },
        )

    except Exception as e:
        logger.exception("run_google_cleanup: unexpected error: %s", e)
        return (
            None,
            {
                "success": False,
                "error": str(e),
                "google_response_time_ms": (time.time() - start_time) * 1000,
                "implemented": False,
            },
        )
