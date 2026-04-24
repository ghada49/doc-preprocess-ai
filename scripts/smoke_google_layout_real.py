#!/usr/bin/env python
"""
scripts/test_google_layout_real.py
-----------------------------------
Real-credentials smoke test for Google Document AI layout analysis.

PURPOSE
-------
Verify that the production ``run_google_layout_analysis()`` function reaches a
real Google Document AI backend, parses the response, and returns canonical
Region objects.  This script must NOT use mocks.

USAGE
-----
Set the following environment variables before running:

    GOOGLE_ENABLED=true
    GOOGLE_PROJECT_ID=<your-gcp-project>
    GOOGLE_LOCATION=us
    GOOGLE_PROCESSOR_ID_LAYOUT=<your-layout-processor-id>
    GOOGLE_CREDENTIALS_PATH=/path/to/sa-key.json   # or GOOGLE_APPLICATION_CREDENTIALS

Then run:

    python scripts/test_google_layout_real.py [image_path] [--output PATH] [--dump-request-pdf PATH]

Defaults:
    image_path   -  test_data/sample_book.tif (relative to repo root)
    --output     -  .tmp/google_layout_smoke_result.json
    --dump-request-pdf - .tmp/google_layout_request.pdf

OUTPUT
------
1. Configuration check (Step 1)
2. Image load confirmation (Step 2)
3. Raw Google API call results (Step 3)
4. Parsed canonical region summary (Step 4)
5. Full JSON saved to --output path (Step 5)

EXIT CODES
----------
0   -  Google returned a successful response (even if 0 regions)
1   -  Configuration error (missing env vars, credentials file not found)
2   -  Google API call failed (network error, auth failure, wrong processor_id)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# -- Ensure repo root is importable --------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# -- Configuration check --------------------------------------------------------


def _check_config() -> tuple[bool, dict[str, str]]:
    """
    Verify required env vars are set and credentials file exists.

    Returns:
        (ok, config_dict)  -  ok is False if any required setting is missing.
    """
    creds_path = os.environ.get(
        "GOOGLE_CREDENTIALS_PATH",
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/var/secrets/google/key.json"),
    )
    cfg = {
        "GOOGLE_ENABLED": os.environ.get("GOOGLE_ENABLED", ""),
        "GOOGLE_PROJECT_ID": os.environ.get("GOOGLE_PROJECT_ID", ""),
        "GOOGLE_LOCATION": os.environ.get("GOOGLE_LOCATION", "us"),
        "GOOGLE_PROCESSOR_ID_LAYOUT": os.environ.get("GOOGLE_PROCESSOR_ID_LAYOUT", ""),
        "GOOGLE_CREDENTIALS_PATH": creds_path,
    }

    ok = True

    enabled = cfg["GOOGLE_ENABLED"].strip().lower() in ("true", "1", "yes")
    if not enabled:
        print("  GOOGLE_ENABLED: NOT true - set GOOGLE_ENABLED=true")
        ok = False
    else:
        print(f"  GOOGLE_ENABLED: {cfg['GOOGLE_ENABLED']!r}  [OK]")

    if not cfg["GOOGLE_PROJECT_ID"]:
        print("  GOOGLE_PROJECT_ID: NOT SET  [MISSING]")
        ok = False
    else:
        print(f"  GOOGLE_PROJECT_ID: {cfg['GOOGLE_PROJECT_ID']!r}  [OK]")

    print(f"  GOOGLE_LOCATION: {cfg['GOOGLE_LOCATION']!r}")

    if not cfg["GOOGLE_PROCESSOR_ID_LAYOUT"]:
        print("  GOOGLE_PROCESSOR_ID_LAYOUT: NOT SET  [MISSING]")
        ok = False
    else:
        pid = cfg["GOOGLE_PROCESSOR_ID_LAYOUT"]
        display = pid if len(pid) <= 32 else pid[:16] + "..." + pid[-8:]
        print(f"  GOOGLE_PROCESSOR_ID_LAYOUT: {display!r}  [OK]")

    creds_exists = os.path.isfile(creds_path)
    status = "EXISTS  [OK]" if creds_exists else "NOT FOUND  [MISSING]"
    print(f"  GOOGLE_CREDENTIALS_PATH: {creds_path!r}  -  {status}")
    if not creds_exists:
        ok = False

    return ok, cfg


# -- MIME detection -------------------------------------------------------------


def _detect_mime(data: bytes) -> str:
    if len(data) >= 4:
        if data[:4] in (b"\x49\x49\x2A\x00", b"\x4D\x4D\x00\x2A"):
            return "image/tiff"
        if data[:4] == b"\x89PNG":
            return "image/png"
        if data[:2] == b"\xff\xd8":
            return "image/jpeg"
    return "image/png"


def _dump_request_pdf(image_bytes: bytes, mime_type: str, dump_path: str | None) -> None:
    """
    Save the exact PDF payload that will be sent to Google Layout Parser.

    For image inputs, this uses the same conversion helper as the production
    request path. For existing PDFs, it writes the raw bytes unchanged.
    """
    if not dump_path:
        return

    payload_bytes = image_bytes
    if mime_type.startswith("image/"):
        from services.eep.app.google.document_ai import convert_image_bytes_to_pdf

        payload_bytes = convert_image_bytes_to_pdf(image_bytes)
    elif mime_type != "application/pdf":
        print(f"  Request PDF dump skipped for unsupported MIME type: {mime_type}")
        return

    out = Path(dump_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload_bytes)
    print(f"  Request PDF: {out}")
    print(f"  PDF size:    {len(payload_bytes):,} bytes")


# -- Async analysis runner ------------------------------------------------------


def _inspect_raw_response(response: object) -> None:
    """Print response structure counts directly to stdout for diagnosis."""
    try:
        document = response.document  # type: ignore[attr-defined]
    except AttributeError:
        print("  [inspect] response has no .document attribute")
        return

    # document-level text
    try:
        text_len = len(document.text or "")
    except Exception:
        text_len = -1
    print(f"  [inspect] document.text              = {text_len} chars")

    # entities
    try:
        entity_count = len(list(document.entities))
    except Exception:
        entity_count = -1
    try:
        dl = document.document_layout
        dl_block_count = len(list(dl.blocks)) if dl else 0
    except Exception:
        dl_block_count = -1

    print(f"  [inspect] document.entities          = {entity_count}")
    print(f"  [inspect] document.document_layout   blocks = {dl_block_count}")

    # page-level
    try:
        pages = list(document.pages)
    except Exception:
        pages = []
    print(f"  [inspect] document.pages             = {len(pages)}")

    if pages:
        p = pages[0]
        for field in ("blocks", "tables", "paragraphs", "lines", "visual_elements", "tokens"):
            try:
                count = len(list(getattr(p, field, [])))
            except Exception:
                count = -1
            print(f"  [inspect] page[0].{field:<22} = {count}")

        # Show first paragraph's bbox if available
        try:
            paras = list(p.paragraphs)
            if paras:
                bpoly0 = paras[0].layout.bounding_poly
                nv0 = list(bpoly0.normalized_vertices)
                v0 = list(bpoly0.vertices)
                print(
                    f"  [inspect] page[0].paragraphs[0] bbox  "
                    f"normalized_vertices={len(nv0)} vertices={len(v0)}"
                )
                if nv0:
                    pt = nv0[0]
                    print(f"  [inspect]   first vertex x={pt.x:.4f} y={pt.y:.4f}")
        except Exception as exc:
            print(f"  [inspect] page[0].paragraphs[0] bbox inspect error: {exc}")

    # If document_layout blocks exist, show first block details
    if dl_block_count and dl_block_count > 0:
        try:
            block0 = list(dl.blocks)[0]
            pb_block = type(block0).pb(block0)
            kind = pb_block.WhichOneof("block")
            bb = block0.bounding_box
            nv = list(bb.normalized_vertices)
            v = list(bb.vertices)
            print(
                f"  [inspect] document_layout.blocks[0] kind={kind!r} "
                f"normalized_vertices={len(nv)} vertices={len(v)}"
            )
            # page_span for the first block
            try:
                ps = block0.page_span
                print(f"  [inspect] document_layout.blocks[0] page_span={ps.page_start}-{ps.page_end}")
            except Exception:
                pass
            # Text preview from text_block
            try:
                txt = (block0.text_block.text or "")[:80].replace("\n", "↵")
                print(f"  [inspect] document_layout.blocks[0] text={txt!r}")
            except Exception:
                pass
        except Exception as exc:
            print(f"  [inspect] document_layout.blocks[0] inspect error: {exc}")


async def _run(
    image_bytes: bytes,
    mime_type: str,
    cfg: dict[str, str],
    output_path: str,
) -> int:
    """
    Run ``run_google_layout_analysis()`` against the real Google backend.

    Returns exit code (0 = success, 2 = API failure).
    """
    from services.eep.app.google.document_ai import (
        CallGoogleDocumentAI,
        GoogleDocumentAIConfig,
        run_google_layout_analysis,
    )

    config = GoogleDocumentAIConfig(
        enabled=True,
        project_id=cfg["GOOGLE_PROJECT_ID"],
        location=cfg["GOOGLE_LOCATION"],
        processor_id_layout=cfg["GOOGLE_PROCESSOR_ID_LAYOUT"],
        credentials_file=cfg["GOOGLE_CREDENTIALS_PATH"],
        timeout_layout_seconds=int(os.environ.get("GOOGLE_TIMEOUT_LAYOUT_SECONDS", "90")),
        max_retries=int(os.environ.get("GOOGLE_MAX_RETRIES", "2")),
        fallback_on_timeout=True,
    )

    processor_display = (
        f"projects/{config.project_id}/locations/{config.location}"
        f"/processors/{config.processor_id_layout}"
    )
    print(f"  Processor: {processor_display}")
    print(f"  Timeout:   {config.timeout_layout_seconds}s  Max retries: {config.max_retries}")
    print("  Calling Google Document AI...")
    print()

    # Also call process_layout directly so we can inspect raw response structure
    _raw_result: dict | None = None
    try:
        _client = CallGoogleDocumentAI(config)
        await _client._lazy_init()
        _raw_result = await _client.process_layout(
            image_uri="smoke-test",
            material_type="document",
            job_id="smoke-test-inspect",
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
    except Exception:
        pass  # don't let inspect failure block the main test

    t0 = time.time()
    regions, metadata = await run_google_layout_analysis(
        image_bytes=image_bytes,
        image_uri=None,
        material_type="document",
        job_id="smoke-test",
        mime_type=mime_type,
        config=config,
    )
    elapsed_ms = (time.time() - t0) * 1000

    # -- Step 3.5a: Save raw SDK JSON ------------------------------------------
    _raw_sdk_path = _REPO_ROOT / ".tmp" / "google_layout_raw_sdk.json"
    _raw_sdk_path.parent.mkdir(parents=True, exist_ok=True)
    _raw_json_str: str | None = (_raw_result or {}).get("raw_response_json")
    if _raw_json_str:
        _raw_sdk_path.write_text(_raw_json_str, encoding="utf-8")
        print("=" * 60)
        print("STEP 3.5a  -  Raw SDK JSON saved")
        print("=" * 60)
        print(f"  Saved: {_raw_sdk_path}")
        print(f"  Size:  {len(_raw_json_str):,} chars")
        print()
    else:
        print("  [raw-dump] raw_response_json not available — serialization failed")
        print()

    # -- Step 3.5b: Raw response diagnostics -----------------------------------
    print("=" * 60)
    print("STEP 3.5b  -  Raw response diagnostics")
    print("=" * 60)
    if _raw_json_str:
        _parsed = json.loads(_raw_json_str)
        # top-level keys
        _top_keys = list(_parsed.keys())
        print(f"  top-level keys:           {_top_keys}")
        # text length
        _text = _parsed.get("document", {}).get("text", "") or ""
        print(f"  text length:              {len(_text)} chars")
        # pages count
        _pages_list = _parsed.get("document", {}).get("pages", [])
        print(f"  pages count:              {len(_pages_list)}")
        # documentLayout.blocks count
        _dl = _parsed.get("document", {}).get("documentLayout", {}) or {}
        _dl_blocks = _dl.get("blocks", [])
        print(f"  documentLayout.blocks:    {len(_dl_blocks)}")
        # blocks with boundingBox
        _with_bbox = sum(1 for b in _dl_blocks if b.get("boundingBox"))
        print(f"  blocks with boundingBox:  {_with_bbox}")
        # blocks with normalizedVertices inside boundingBox
        _with_nv = sum(
            1
            for b in _dl_blocks
            if b.get("boundingBox", {}).get("normalizedVertices")
        )
        print(f"  blocks with normalizedVertices: {_with_nv}")
        # show first block bbox if present
        if _dl_blocks and _dl_blocks[0].get("boundingBox"):
            _b0 = _dl_blocks[0]["boundingBox"]
            _nv0 = _b0.get("normalizedVertices", [])
            print(f"  blocks[0].boundingBox.normalizedVertices: {_nv0}")
    elif _raw_result and _raw_result.get("raw_response"):
        _inspect_raw_response(_raw_result["raw_response"])
    else:
        print("  [diagnostics] raw response not available")
    print()

    # -- Step 4: Results --------------------------------------------------------
    print("=" * 60)
    print("STEP 4  -  Results")
    print("=" * 60)
    print(f"  success:          {metadata['success']}")
    print(f"  region_count:     {metadata['region_count']}")
    print(f"  processing_time:  {elapsed_ms:.0f}ms")
    print(f"  source:           {metadata['source']!r}")
    if metadata.get("error"):
        print(f"  error:            {metadata['error']}")

    if not metadata["success"]:
        print()
        print("SMOKE TEST: FAILED  -  Google returned an error.")
        _save_output(output_path, regions, metadata, elapsed_ms)
        return 2

    if regions:
        r0 = regions[0]
        print()
        print("  Sample region [0]:")
        print(f"    id:   {r0.id}")
        print(f"    type: {r0.type.value}")
        print(
            f"    bbox: x_min={r0.bbox.x_min:.1f}  y_min={r0.bbox.y_min:.1f}"
            f"  x_max={r0.bbox.x_max:.1f}  y_max={r0.bbox.y_max:.1f}"
        )
        print(f"    conf: {r0.confidence:.3f}")

        type_counts: dict[str, int] = {}
        for r in regions:
            type_counts[r.type.value] = type_counts.get(r.type.value, 0) + 1
        print()
        print("  Region type histogram:")
        for rtype, count in sorted(type_counts.items()):
            print(f"    {rtype:20s}  {count}")
    else:
        print()
        print("  NOTE: Google returned 0 canonical regions for this image.")
        print("  This is a valid result (blank/unrecognised page).")

    # -- Step 5: Save JSON ------------------------------------------------------
    print()
    print("=" * 60)
    print("STEP 5  -  Save output JSON")
    print("=" * 60)
    _save_output(output_path, regions, metadata, elapsed_ms)

    print()
    print("SMOKE TEST: PASSED")
    return 0


def _save_output(output_path: str, regions: list, metadata: dict, elapsed_ms: float) -> None:
    from shared.schemas.layout import Region

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    result_dict = {
        "smoke_test": "test_google_layout_real",
        "metadata": metadata,
        "region_count": len(regions),
        "elapsed_ms": round(elapsed_ms, 2),
        "regions": [
            {
                "id": r.id,
                "type": r.type.value,
                "bbox": {
                    "x_min": r.bbox.x_min,
                    "y_min": r.bbox.y_min,
                    "x_max": r.bbox.x_max,
                    "y_max": r.bbox.y_max,
                },
                "confidence": r.confidence,
            }
            for r in regions
        ],
    }
    out.write_text(json.dumps(result_dict, indent=2))
    print(f"  Saved: {out}")


# -- Entry point ----------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real-credentials smoke test for Google Document AI layout analysis."
    )
    parser.add_argument(
        "image_path",
        nargs="?",
        default=None,
        help="Path to image file (default: test_data/sample_book.tif)",
    )
    parser.add_argument(
        "--output",
        default=str(_REPO_ROOT / ".tmp" / "google_layout_smoke_result.json"),
        help="Output JSON path (default: .tmp/google_layout_smoke_result.json)",
    )
    parser.add_argument(
        "--dump-request-pdf",
        default=str(_REPO_ROOT / ".tmp" / "google_layout_request.pdf"),
        help="Path to save the exact PDF payload sent to Google (default: .tmp/google_layout_request.pdf)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable DEBUG logging for the document_ai module to show response structure.",
    )
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s %(name)s: %(message)s",
            stream=sys.stdout,
        )
        # Suppress noisy google/grpc/urllib loggers; keep only ours
        for _noisy in ("google.auth", "google.api_core", "urllib3", "grpc"):
            logging.getLogger(_noisy).setLevel(logging.WARNING)
    else:
        logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

    print("=" * 60)
    print("Google Document AI Layout  -  Real Smoke Test")
    print("=" * 60)
    print()

    # -- Step 1: Configuration --------------------------------------------------
    print("=" * 60)
    print("STEP 1  -  Configuration verification")
    print("=" * 60)
    ok, cfg = _check_config()
    if not ok:
        print()
        print("SMOKE TEST: ABORTED  -  fix configuration and re-run.")
        sys.exit(1)

    # -- Step 2: Load image -----------------------------------------------------
    print()
    print("=" * 60)
    print("STEP 2  -  Loading image")
    print("=" * 60)

    if args.image_path:
        image_path = Path(args.image_path)
    else:
        image_path = _REPO_ROOT / "test_data" / "sample_book.tif"

    if not image_path.exists():
        print(f"  ERROR: Image not found at {image_path}")
        print("  Provide a path as argument or ensure test_data/sample_book.tif exists.")
        sys.exit(1)

    image_bytes = image_path.read_bytes()
    mime_type = _detect_mime(image_bytes)
    print(f"  Path:      {image_path}")
    print(f"  Size:      {len(image_bytes):,} bytes")
    print(f"  MIME type: {mime_type}")
    _dump_request_pdf(image_bytes, mime_type, args.dump_request_pdf)

    # -- Step 3: Call Google ----------------------------------------------------
    print()
    print("=" * 60)
    print("STEP 3  -  Calling Google Document AI (REAL API)")
    print("=" * 60)

    exit_code = asyncio.run(_run(image_bytes, mime_type, cfg, args.output))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
