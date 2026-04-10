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
from io import BytesIO
from typing import Any

from shared.schemas.layout import Region, RegionType
from shared.schemas.ucf import BoundingBox

logger = logging.getLogger(__name__)

__all__ = [
    "GoogleDocumentAIConfig",
    "CallGoogleDocumentAI",
    "convert_image_bytes_to_pdf",
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
        text         — OCR text content extracted from textBlock.text (Strategy 2 only);
                       None for entity-based and page-level strategies
    """

    type_: str
    bounding_poly: Any  # google.cloud.documentai_v1.types.BoundingPoly
    confidence: float
    text: str | None = None


def _bounding_poly_has_geometry(bounding_poly: Any) -> bool:
    """Return True when a BoundingPoly carries any usable vertices."""
    if bounding_poly is None:
        return False

    try:
        if list(getattr(bounding_poly, "normalized_vertices", [])):
            return True
    except Exception:
        pass

    try:
        if list(getattr(bounding_poly, "vertices", [])):
            return True
    except Exception:
        pass

    return False


def _summarize_layout_response(document: Any, pages: list[Any]) -> dict[str, Any]:
    """Extract response-shape diagnostics for audit/debugging."""
    try:
        text_length = len(getattr(document, "text", "") or "") if document else 0
    except Exception:
        text_length = 0

    try:
        document_layout = getattr(document, "document_layout", None) if document else None
        blocks = list(getattr(document_layout, "blocks", [])) if document_layout else []
    except Exception:
        blocks = []

    return {
        "document_layout_block_count": len(blocks),
        "pages_count": len(pages),
        "text_length": text_length,
        "document_layout_blocks_have_geometry": any(
            _bounding_poly_has_geometry(getattr(block, "bounding_box", None)) for block in blocks
        ),
    }


def _derive_empty_reason(
    *,
    canonical_region_count: int,
    document_layout_block_count: int,
    pages_count: int,
    text_length: int,
    document_layout_blocks_have_geometry: bool,
) -> str | None:
    """Explain an empty successful Google layout result when possible."""
    if canonical_region_count > 0:
        return None

    if document_layout_block_count > 0 and not document_layout_blocks_have_geometry:
        return "semantic_blocks_without_geometry"

    if document_layout_block_count == 0 and pages_count == 0 and text_length == 0:
        return "no_layout_content_returned"

    return None


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


def convert_image_bytes_to_pdf(image_bytes: bytes) -> bytes:
    """
    Convert a single image into a one-page PDF without changing its pixel geometry.

    The PDF is written at 72 DPI so the page's MediaBox width/height numerically
    match the source pixel width/height. Google Layout Parser reports page-space
    coordinates, so this 1:1 numeric mapping keeps downstream bounding boxes in
    the same coordinate system as the original image.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("Pillow is required for image-to-PDF conversion") from exc

    with Image.open(BytesIO(image_bytes)) as source_image:
        source_image.load()
        width, height = source_image.size
        if width <= 0 or height <= 0:
            raise ValueError(f"Invalid image dimensions: {width}x{height}")

        working_image = source_image
        if working_image.mode in {"RGBA", "LA"} or (
            working_image.mode == "P" and "transparency" in working_image.info
        ):
            flattened = Image.new("RGBA", working_image.size, (255, 255, 255, 255))
            working_image = Image.alpha_composite(
                flattened,
                working_image.convert("RGBA"),
            ).convert("RGB")
        elif working_image.mode != "RGB":
            working_image = working_image.convert("RGB")

        pdf_buffer = BytesIO()
        # Force a 1:1 numeric mapping between source pixels and PDF page units.
        working_image.save(pdf_buffer, format="PDF", resolution=72.0)

    logger.debug(
        "Converting image to PDF for Layout Parser: original_dimensions=%dx%d pdf_dimensions=%dx%d",
        width,
        height,
        width,
        height,
    )
    return pdf_buffer.getvalue()


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
            from google.api_core.client_options import ClientOptions
            from google.cloud import documentai_v1  # noqa: F401

            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_file
            api_endpoint = _build_api_endpoint(self.config.location)
            self._client = documentai_v1.DocumentProcessorServiceClient(
                client_options=ClientOptions(api_endpoint=api_endpoint)
            )
            self._credentials_valid = True
            logger.info(
                "CallGoogleDocumentAI: client initialized successfully (endpoint=%s)",
                api_endpoint,
            )
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

        # Capture source pixel dimensions before PDF conversion.
        # Layout Parser v2+ may return pages=[] when returnBoundingBoxes is
        # enabled; in that case block.bounding_box.normalizedVertices are in
        # [0, 1] and we need the original image size to denormalize correctly.
        self._source_width: int | None = None
        self._source_height: int | None = None
        if image_bytes and mime_type.lower().startswith("image/"):
            try:
                from PIL import Image

                with Image.open(BytesIO(image_bytes)) as _src:
                    self._source_width, self._source_height = _src.size
            except Exception:
                pass  # leave None; _call_google_api_sync falls back to 1000×1000

        prepared_bytes = image_bytes
        prepared_mime_type = mime_type
        if image_bytes and mime_type.lower().startswith("image/"):
            try:
                prepared_bytes = convert_image_bytes_to_pdf(image_bytes)
                prepared_mime_type = "application/pdf"
            except Exception as exc:
                logger.exception(
                    "process_layout: failed to convert image to PDF before Google call: %s",
                    exc,
                )
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
                    image_bytes=prepared_bytes,
                    mime_type=prepared_mime_type,
                    timeout_sec=self.config.timeout_layout_seconds,
                )

                elapsed_ms = (time.time() - start_time) * 1000
                logger.info(
                    "process_layout: success after %d attempt(s), job_id=%s, "
                    "regions=%d, blocks=%d, pages=%d, text_length=%d, "
                    "blocks_have_geometry=%s, empty_reason=%s, elapsed=%.0fms",
                    attempt,
                    job_id or "unknown",
                    result.get("region_count", 0),
                    result.get("document_layout_block_count", 0),
                    result.get("pages_count", 0),
                    result.get("text_length", 0),
                    result.get("document_layout_blocks_have_geometry", False),
                    result.get("empty_reason"),
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
        Process image for artifact cleanup using Google Document AI.

        Calls the cleanup processor (processor_id_cleanup) and extracts
        the rendered page image from the response.  Returns None when:
          - processor_id_cleanup is not configured
          - credentials are unavailable
          - the API returns no rendered page image
          - any transient error exhausts retries
          - any permanent error occurs

        Args:
            image_bytes — Raw image bytes (PNG or TIFF)
            job_id      — Job ID for logging (optional)

        Returns:
            Cleaned image bytes, or None on any failure.
        """
        if not self.config.processor_id_cleanup:
            logger.debug(
                "process_cleanup: processor_id_cleanup not configured, skipping (job_id=%s)",
                job_id or "unknown",
            )
            return None

        if not await self._lazy_init():
            return None

        mime_type = _detect_mime_type(image_bytes)
        start_time = time.time()
        attempt = 0
        last_error: Exception | None = None

        while attempt <= self.config.max_retries:
            attempt += 1
            try:
                logger.debug(
                    "process_cleanup: attempt %d/%d, job_id=%s",
                    attempt,
                    self.config.max_retries + 1,
                    job_id or "unknown",
                )

                loop = asyncio.get_event_loop()
                cleaned = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        self._call_google_cleanup_sync,
                        image_bytes,
                        mime_type,
                    ),
                    timeout=self.config.timeout_cleanup_seconds,
                )

                elapsed_ms = (time.time() - start_time) * 1000
                logger.info(
                    "process_cleanup: success after %d attempt(s), job_id=%s, "
                    "has_image=%s, elapsed=%.0fms",
                    attempt,
                    job_id or "unknown",
                    cleaned is not None,
                    elapsed_ms,
                )
                return cleaned

            except TimeoutError as e:
                last_error = e
                logger.warning(
                    "process_cleanup: timeout on attempt %d (job_id=%s)",
                    attempt,
                    job_id or "unknown",
                )
                if attempt <= self.config.max_retries:
                    wait_sec = 2 ** (attempt - 1)
                    await asyncio.sleep(wait_sec)
                continue

            except GoogleAPITransientError as e:
                last_error = e
                logger.warning(
                    "process_cleanup: transient error on attempt %d: %s",
                    attempt,
                    e,
                )
                if attempt <= self.config.max_retries:
                    wait_sec = 2 ** (attempt - 1)
                    await asyncio.sleep(wait_sec)
                continue

            except GoogleAPIPermanentError as e:
                logger.error(
                    "process_cleanup: permanent error (job_id=%s): %s",
                    job_id or "unknown",
                    e,
                )
                return None

        elapsed_ms = (time.time() - start_time) * 1000
        logger.error(
            "process_cleanup: exhausted max retries (%d), job_id=%s, "
            "last_error=%s, elapsed=%.0fms",
            self.config.max_retries + 1,
            job_id or "unknown",
            last_error,
            elapsed_ms,
        )
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
              - document_layout_block_count: int
              - pages_count: int
              - text_length: int
              - document_layout_blocks_have_geometry: bool
              - empty_reason: str | None
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

            # Layout Parser: explicitly request page-level fields.
            # The Layout Parser does not populate document.pages by default.
            # Adding them to the field_mask instructs the API to include
            # paragraph/block/table spatial data in the response, which is
            # required for positional bbox resolution in Strategy 2/3.
            from google.protobuf import field_mask_pb2

            _layout_field_mask = field_mask_pb2.FieldMask(
                paths=[
                    "text",
                    "entities",
                    "document_layout",
                    "pages.dimension",
                    "pages.paragraphs",
                    "pages.blocks",
                    "pages.tables",
                    "pages.visual_elements",
                ]
            )

            _process_options = documentai_v1.ProcessOptions(
                layout_config=documentai_v1.ProcessOptions.LayoutConfig(
                    return_bounding_boxes=True,
                    enable_image_annotation=True,
                    enable_table_annotation=True,
                )
            )

            if image_bytes:
                request = documentai_v1.ProcessRequest(
                    name=processor_name,
                    raw_document=documentai_v1.RawDocument(
                        content=image_bytes,
                        mime_type=mime_type,
                    ),
                    field_mask=_layout_field_mask,
                    process_options=_process_options,
                )
            else:
                request = documentai_v1.ProcessRequest(
                    name=processor_name,
                    gcs_document=documentai_v1.GcsDocument(gcs_uri=image_uri),
                    field_mask=_layout_field_mask,
                    process_options=_process_options,
                )

            response = self._client.process_document(request=request)

            # ── Capture raw SDK response as JSON (for debugging / audit) ──────
            _raw_response_json: str | None = None
            try:
                # proto-plus: type(obj).to_json(obj) serializes all fields
                _raw_response_json = type(response).to_json(response)
            except Exception as _ser_exc:
                try:
                    # fallback: protobuf MessageToJson on the underlying _pb
                    from google.protobuf.json_format import MessageToJson

                    _raw_response_json = MessageToJson(response._pb)
                except Exception:
                    logger.debug(
                        "_call_google_api_sync: could not serialize raw response: %s", _ser_exc
                    )

            document = response.document
            pages = list(document.pages) if document and document.pages else []
            if not pages:
                logger.warning(
                    "_call_google_api_sync: document.pages is empty after API call "
                    "(job_id=%s) — Layout Parser processor did not return page-level "
                    "spatial data; bbox resolution via page.paragraphs will be skipped",
                    getattr(self, "_job_id", "unknown"),
                )

            # Resolve page dimensions for bbox denormalization.
            # Priority 1: Google's reported page dimensions (pages non-empty).
            # Priority 2: Source image dimensions captured before PDF conversion
            #             (Layout Parser v2+ with returnBoundingBoxes returns
            #             pages=[] but block.bounding_box.normalizedVertices is
            #             in [0, 1] relative to the original image size).
            # Priority 3: Conservative 1000×1000 fallback.
            page_width = 1000
            page_height = 1000
            if pages and hasattr(pages[0], "dimension") and pages[0].dimension:
                w = int(pages[0].dimension.width)
                h = int(pages[0].dimension.height)
                # Image inputs are wrapped in a 72 DPI single-page PDF whose MediaBox
                # matches the source pixel dimensions, so Google page coordinates can
                # continue to be interpreted as original image coordinates.
                if w > 0:
                    page_width = w
                if h > 0:
                    page_height = h
            elif getattr(self, "_source_width", None) and getattr(self, "_source_height", None):
                page_width = self._source_width  # type: ignore[assignment]
                page_height = self._source_height  # type: ignore[assignment]
                logger.debug(
                    "_call_google_api_sync: pages empty — using source image dimensions "
                    "%dx%d for normalizedVertices denormalization",
                    page_width,
                    page_height,
                )

            diagnostics = _summarize_layout_response(document, pages)
            elements = _extract_elements_from_response(document, pages)
            diagnostics["empty_reason"] = _derive_empty_reason(
                canonical_region_count=len(elements),
                document_layout_block_count=diagnostics["document_layout_block_count"],
                pages_count=diagnostics["pages_count"],
                text_length=diagnostics["text_length"],
                document_layout_blocks_have_geometry=diagnostics[
                    "document_layout_blocks_have_geometry"
                ],
            )

            return {
                "raw_response": response,
                "raw_response_json": _raw_response_json,
                "pages": pages,
                "elements": elements,
                "page_width": page_width,
                "page_height": page_height,
                "region_count": len(elements),
                **diagnostics,
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

            raw_text = getattr(element, "text", None)
            region_text = raw_text.strip() if isinstance(raw_text, str) and raw_text.strip() else None

            regions.append(
                Region(
                    id=f"r{len(regions)}",
                    type=canonical_type,
                    bbox=bbox,
                    confidence=confidence,
                    text=region_text,
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

    def _call_google_cleanup_sync(
        self,
        image_bytes: bytes,
        mime_type: str,
    ) -> bytes | None:
        """
        Synchronous call to Google Document AI cleanup processor (runs in thread pool).

        Sends image_bytes to the cleanup processor and returns the rendered page
        image from the response if the processor emits one.

        Returns:
            bytes of the cleaned/rendered page image, or None if the processor
            response contains no image content.

        Raises:
            GoogleAPIPermanentError — credentials missing, bad config, 4xx errors
            GoogleAPITransientError — network errors, timeouts, 429/5xx errors
        """
        try:
            from google.cloud import documentai_v1

            if not self._client:
                raise GoogleAPIPermanentError("Google client not initialized")

            processor_name = _build_processor_name(
                self.config.project_id,
                self.config.location,
                self.config.processor_id_cleanup,
            )

            request = documentai_v1.ProcessRequest(
                name=processor_name,
                raw_document=documentai_v1.RawDocument(
                    content=image_bytes,
                    mime_type=mime_type,
                ),
            )

            response = self._client.process_document(request=request)
            document = response.document

            # Extract rendered page image from the response if the processor emits one
            if document and document.pages:
                page = document.pages[0]
                if hasattr(page, "image") and page.image and page.image.content:
                    content = bytes(page.image.content)
                    if content:
                        return content

            return None

        except (GoogleAPITransientError, GoogleAPIPermanentError):
            raise
        except Exception as e:
            classification, reason = _classify_error(e)
            if classification == "transient":
                raise GoogleAPITransientError(reason) from e
            else:
                raise GoogleAPIPermanentError(reason) from e


# ───────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ───────────────────────────────────────────────────────────────────────────────


def _build_processor_name(project_id: str, location: str, processor_id: str) -> str:
    """Build Google Document AI processor resource name."""
    return f"projects/{project_id}/locations/{location}/processors/{processor_id}"


def _build_api_endpoint(location: str) -> str:
    """Build the regional Document AI API endpoint for a processor location."""
    normalized = (location or "us").strip().lower()
    return f"{normalized}-documentai.googleapis.com"


def _detect_mime_type(data: bytes) -> str:
    """Detect MIME type from image byte signature. Returns 'image/png' as default."""
    if len(data) >= 4:
        if data[:4] in (b"\x49\x49\x2A\x00", b"\x4D\x4D\x00\x2A"):
            return "image/tiff"
        if data[:4] == b"\x89PNG":
            return "image/png"
    return "image/png"


def _extract_elements_from_response(
    document: Any,
    pages: list[Any],
) -> list[_WrappedElement]:
    """
    Extract typed _WrappedElement list from a Google Document AI response.

    Strategy:
      1. Entity-based processor (Form Parser, Enterprise Document AI):
         document.entities with page_anchor → bounding_poly per entity.
      2. document.document_layout.blocks (Layout Parser v1+):
         The Layout Parser primary output. Each DocumentLayoutBlock carries a
         bounding_box (BoundingPoly) and a one-of kind field (text_block,
         table_block, list_block, image_block) that determines the canonical type.
         text_block.type_ further distinguishes paragraph vs section-header.
         This strategy is tried before page-level extraction; if it yields
         elements it is returned immediately.
      3. Page-element-based (older processors / Layout Parser page-level fallback):
         - page.blocks     → PARAGRAPH  (OCR/Form Parser primary)
         - page.tables     → TABLE      (always extracted)
         - page.paragraphs → PARAGRAPH  (Layout Parser page-level; only when
                             page.blocks is absent to avoid double-counting, since
                             Form Parser paragraphs are sub-elements of blocks)
         - page.visual_elements → typed by element.type_  (headers, figures, etc.)

    Returns:
        list[_WrappedElement] — empty list if nothing extractable
    """
    elements: list[_WrappedElement] = []

    # Diagnostic logging — log field counts to aid debugging when 0 elements returned
    if document and logger.isEnabledFor(logging.DEBUG):
        try:
            _dl = getattr(document, "document_layout", None)
            _dl_count = len(list(getattr(_dl, "blocks", []))) if _dl else 0
            _entity_count = len(list(getattr(document, "entities", [])))
            logger.debug(
                "_extract_elements_from_response: entities=%d document_layout_blocks=%d pages=%d",
                _entity_count,
                _dl_count,
                len(pages),
            )
        except Exception:
            pass
    if pages and logger.isEnabledFor(logging.DEBUG):
        try:
            _p0 = pages[0]
            logger.debug(
                "_extract_elements_from_response: page[0] blocks=%d tables=%d "
                "paragraphs=%d lines=%d visual_elements=%d",
                len(list(getattr(_p0, "blocks", []))),
                len(list(getattr(_p0, "tables", []))),
                len(list(getattr(_p0, "paragraphs", []))),
                len(list(getattr(_p0, "lines", []))),
                len(list(getattr(_p0, "visual_elements", []))),
            )
        except Exception:
            pass

    # ── Strategy 1: entity-based processor ──────────────────────────────────────
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
            except Exception as exc:
                logger.debug("_extract_elements_from_response: entity skip: %s", exc)
                continue
        if elements:
            return elements

    # ── Strategy 2: document.document_layout.blocks (Layout Parser v1+) ─────────
    # DocumentLayoutBlock carries content type (text_block, table_block, etc.).
    # bounding_box is defined on the proto but NOT populated by the Layout Parser.
    # Spatial information lives in document.pages[0].paragraphs[k].layout.bounding_poly.
    #
    # Bbox resolution uses positional index matching: when len(dl_blocks) == len(para_polys),
    # block[i] corresponds to para[i] (both are in reading order). When counts differ, blocks
    # are skipped and we fall through to Strategy 3.
    _dl = getattr(document, "document_layout", None) if document else None
    try:
        _dl_blocks = list(getattr(_dl, "blocks", [])) if _dl else []
    except (TypeError, AttributeError):
        _dl_blocks = []
    if _dl_blocks:
        # Collect page paragraph bounding polys in reading order for positional matching.
        _para_polys_ordered: list[Any] = []
        try:
            if pages:
                for _para in list(getattr(pages[0], "paragraphs", []) or []):
                    try:
                        _para_polys_ordered.append(_para.layout.bounding_poly)
                    except Exception:
                        _para_polys_ordered.append(None)
        except Exception:
            pass

        _can_use_positional = (
            len(_para_polys_ordered) > 0 and len(_para_polys_ordered) == len(_dl_blocks)
        )

        for _idx, block in enumerate(_dl_blocks):
            try:
                # Determine type from the one-of block kind field
                try:
                    block_kind = type(block).pb(block).WhichOneof("block")
                except Exception:
                    block_kind = None
                if block_kind == "table_block":
                    block_type = "TABLE"
                elif block_kind == "list_block":
                    block_type = "LIST_ITEM"
                elif block_kind == "image_block":
                    block_type = "FIGURE"
                elif block_kind == "text_block":
                    text_subtype = (block.text_block.type_ or "").lower()
                    if text_subtype in ("header", "section-header", "heading", "title"):
                        block_type = "SECTION_HEADER"
                    else:
                        block_type = "PARAGRAPH"
                else:
                    block_type = "PARAGRAPH"

                # Resolve bounding poly.
                # 1. Try block.bounding_box (populated if processor fills it).
                # 2. If empty, use positional match to page.paragraphs[i].
                _raw_bbox = block.bounding_box
                resolved_bbox: Any = _raw_bbox if _bounding_poly_has_geometry(_raw_bbox) else None

                if resolved_bbox is None and _can_use_positional:
                    resolved_bbox = _para_polys_ordered[_idx]

                if resolved_bbox is None:
                    logger.debug(
                        "_extract_elements_from_response: document_layout block[%d] "
                        "has no resolvable bbox, skipping",
                        _idx,
                    )
                    continue

                # Extract OCR text from textBlock when available.
                block_text: str | None = None
                if block_kind == "text_block":
                    raw_text = getattr(block.text_block, "text", None)
                    block_text = raw_text.strip() if raw_text else None

                elements.append(
                    _WrappedElement(
                        type_=block_type,
                        bounding_poly=resolved_bbox,
                        confidence=0.9,  # DocumentLayoutBlock carries no confidence score
                        text=block_text,
                    )
                )
            except Exception as exc:
                logger.debug(
                    "_extract_elements_from_response: document_layout block skip: %s", exc
                )
                continue
        if elements:
            logger.debug(
                "_extract_elements_from_response: extracted %d elements from document_layout",
                len(elements),
            )
            return elements

    # ── Strategy 3: page-element-based (older processors / page-level fallback) ─
    if not pages:
        return elements

    page = pages[0]
    # Track whether page.blocks produced any elements with a non-empty bounding poly.
    # The Layout Parser populates page.blocks but with EMPTY bboxes; in that case we
    # must fall through to page.paragraphs (which carry valid spatial information).
    blocks_had_valid_bbox = False

    # Blocks → PARAGRAPH (OCR / Form Parser primary structure)
    if hasattr(page, "blocks") and page.blocks:
        for block in page.blocks:
            try:
                layout = block.layout
                bpoly = layout.bounding_poly
                # Skip blocks with empty bounding poly (Layout Parser fills blocks but
                # leaves bboxes empty; paragraphs carry the actual spatial coordinates).
                if not _bounding_poly_has_geometry(bpoly):
                    continue
                blocks_had_valid_bbox = True
                elements.append(
                    _WrappedElement(
                        type_="PARAGRAPH",
                        bounding_poly=bpoly,
                        confidence=float(getattr(layout, "confidence", 0.5)),
                    )
                )
            except Exception as exc:
                logger.debug("_extract_elements_from_response: page block skip: %s", exc)
                continue

    # Tables → TABLE (always extracted regardless of blocks/paragraphs)
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
            except Exception as exc:
                logger.debug("_extract_elements_from_response: page table skip: %s", exc)
                continue

    # Paragraphs — Layout Parser page-level structure.
    # Used when page.blocks is absent (Form Parser/OCR has no blocks → fall back here)
    # OR when page.blocks had entries but all had empty bboxes (Layout Parser case).
    # Not used when blocks produced valid elements: for Form Parser, paragraphs are
    # sub-elements of blocks and extracting both would double-count.
    if not blocks_had_valid_bbox and hasattr(page, "paragraphs") and page.paragraphs:
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
            except Exception as exc:
                logger.debug("_extract_elements_from_response: page paragraph skip: %s", exc)
                continue

    # Visual elements — Layout Parser places headers, figures, and other structural
    # elements here when they are separate from paragraph flow.
    try:
        _ve_list = list(page.visual_elements) if getattr(page, "visual_elements", None) else []
    except (TypeError, AttributeError):
        _ve_list = []
    for ve in _ve_list:
        try:
            layout = ve.layout
            ve_type = getattr(ve, "type_", None) or "FIGURE"
            elements.append(
                _WrappedElement(
                    type_=ve_type,
                    bounding_poly=layout.bounding_poly,
                    confidence=float(getattr(layout, "confidence", 0.5)),
                )
            )
        except Exception as exc:
            logger.debug("_extract_elements_from_response: visual_element skip: %s", exc)
            continue

    logger.debug(
        "_extract_elements_from_response: page-level total=%d "
        "(blocks_had_valid_bbox=%s)",
        len(elements),
        blocks_had_valid_bbox,
    )
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
              - document_layout_block_count: int
              - pages_count: int
              - text_length: int
              - document_layout_blocks_have_geometry: bool
              - empty_reason: str | None

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
                    "document_layout_block_count": 0,
                    "pages_count": 0,
                    "text_length": 0,
                    "document_layout_blocks_have_geometry": False,
                    "empty_reason": None,
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
        empty_reason = _derive_empty_reason(
            canonical_region_count=len(regions),
            document_layout_block_count=int(result.get("document_layout_block_count", 0)),
            pages_count=int(result.get("pages_count", 0)),
            text_length=int(result.get("text_length", 0)),
            document_layout_blocks_have_geometry=bool(
                result.get("document_layout_blocks_have_geometry", False)
            ),
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
                "document_layout_block_count": int(result.get("document_layout_block_count", 0)),
                "pages_count": int(result.get("pages_count", 0)),
                "text_length": int(result.get("text_length", 0)),
                "document_layout_blocks_have_geometry": bool(
                    result.get("document_layout_blocks_have_geometry", False)
                ),
                "empty_reason": empty_reason,
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
                "document_layout_block_count": 0,
                "pages_count": 0,
                "text_length": 0,
                "document_layout_blocks_have_geometry": False,
                "empty_reason": None,
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
                    "error": "Google cleanup returned no result (processor not configured or no image in response)",
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
