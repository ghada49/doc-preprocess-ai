#!/usr/bin/env python
"""
Run a live Google Document AI request-shape matrix against the same PDF payload.

Writes a structured comparison to .tmp/google_layout_request_matrix.json.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from google.api_core.client_options import ClientOptions
from google.auth.transport.requests import AuthorizedSession
from google.cloud import documentai
from google.cloud import documentai_v1
from google.oauth2 import service_account
from google.protobuf import field_mask_pb2

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.eep.app.google.document_ai import (  # noqa: E402
    CallGoogleDocumentAI,
    GoogleDocumentAIConfig,
    _bounding_poly_has_geometry,
    _derive_empty_reason,
    _extract_elements_from_response,
    _summarize_layout_response,
)

CURRENT_FIELD_MASK_PATHS = [
    "text",
    "entities",
    "document_layout",
    "pages.dimension",
    "pages.paragraphs",
    "pages.blocks",
    "pages.tables",
    "pages.visual_elements",
]


def _load_env() -> dict[str, str]:
    env_path = _REPO_ROOT / ".env"
    data: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
        os.environ[key.strip()] = value.strip()
    return data


def _page_dimensions(pages: list[Any]) -> tuple[int, int]:
    page_width = 1000
    page_height = 1000
    if pages and getattr(pages[0], "dimension", None):
        width = int(getattr(pages[0].dimension, "width", 0) or 0)
        height = int(getattr(pages[0].dimension, "height", 0) or 0)
        if width > 0:
            page_width = width
        if height > 0:
            page_height = height
    return page_width, page_height


def _block_geometry_count(document: Any) -> int:
    try:
        document_layout = getattr(document, "document_layout", None)
        blocks = list(getattr(document_layout, "blocks", [])) if document_layout else []
    except Exception:
        return 0
    return sum(1 for block in blocks if _bounding_poly_has_geometry(getattr(block, "bounding_box", None)))


def _analyze_document(document: Any) -> dict[str, Any]:
    pages = list(getattr(document, "pages", []) or [])
    diagnostics = _summarize_layout_response(document, pages)
    elements = _extract_elements_from_response(document, pages)
    page_width, page_height = _page_dimensions(pages)
    mapper = CallGoogleDocumentAI(GoogleDocumentAIConfig(enabled=False))
    canonical_regions = mapper._map_google_to_canonical(elements, page_width, page_height)

    return {
        **diagnostics,
        "document_layout_blocks_with_geometry_count": _block_geometry_count(document),
        "extracted_element_count": len(elements),
        "canonical_region_count": len(canonical_regions),
        "canonical_regions_can_be_produced": len(canonical_regions) > 0,
        "empty_reason": _derive_empty_reason(
            canonical_region_count=len(canonical_regions),
            document_layout_block_count=diagnostics["document_layout_block_count"],
            pages_count=diagnostics["pages_count"],
            text_length=diagnostics["text_length"],
            document_layout_blocks_have_geometry=diagnostics[
                "document_layout_blocks_have_geometry"
            ],
        ),
    }


async def _call_process_document(
    client: Any,
    request: Any,
    *,
    timeout_sec: int,
) -> Any:
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, lambda: client.process_document(request=request)),
        timeout=timeout_sec,
    )


def _build_request(
    module: Any,
    *,
    name: str,
    payload: bytes,
    mime_type: str,
    field_mask: Any = None,
    process_options: Any = None,
) -> Any:
    kwargs: dict[str, Any] = {
        "name": name,
        "raw_document": module.RawDocument(content=payload, mime_type=mime_type),
    }
    if field_mask is not None:
        kwargs["field_mask"] = field_mask
    if process_options is not None:
        kwargs["process_options"] = process_options
    return module.ProcessRequest(**kwargs)


def _current_field_mask() -> field_mask_pb2.FieldMask:
    return field_mask_pb2.FieldMask(paths=list(CURRENT_FIELD_MASK_PATHS))


def _minimal_field_mask() -> field_mask_pb2.FieldMask:
    return field_mask_pb2.FieldMask(paths=["document_layout"])


def _discover_processor_version(client: Any, processor_name: str) -> tuple[str | None, str | None]:
    try:
        versions = list(client.list_processor_versions(parent=processor_name))
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)

    if not versions:
        return None, "no processor versions returned"

    for version in versions:
        state = str(getattr(version, "state", ""))
        if "DEPLOYED" in state.upper():
            return str(version.name), None

    return str(versions[0].name), None


async def _run_client_variant(
    *,
    label: str,
    description: str,
    client: Any,
    request: Any,
    timeout_sec: int,
    processor_resource: str,
    transport: str,
    field_mask_paths: list[str] | None,
    sample_style: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = await _call_process_document(client, request, timeout_sec=timeout_sec)
        analysis = _analyze_document(response.document)
        return {
            "variant": label,
            "description": description,
            "transport": transport,
            "processor_resource": processor_resource,
            "field_mask_paths": field_mask_paths,
            "sample_style": sample_style,
            "success": True,
            "error": None,
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 2),
            **analysis,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "variant": label,
            "description": description,
            "transport": transport,
            "processor_resource": processor_resource,
            "field_mask_paths": field_mask_paths,
            "sample_style": sample_style,
            "success": False,
            "error": str(exc),
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 2),
            "document_layout_block_count": 0,
            "pages_count": 0,
            "text_length": 0,
            "document_layout_blocks_have_geometry": False,
            "document_layout_blocks_with_geometry_count": 0,
            "extracted_element_count": 0,
            "canonical_region_count": 0,
            "canonical_regions_can_be_produced": False,
            "empty_reason": None,
        }


async def _run_rest_variant(
    *,
    env: dict[str, str],
    payload: bytes,
    mime_type: str,
    processor_resource: str,
    timeout_sec: int,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        credentials = service_account.Credentials.from_service_account_file(
            env["GOOGLE_CREDENTIALS_PATH"],
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        session = AuthorizedSession(credentials)
        endpoint = f"https://{env.get('GOOGLE_LOCATION', 'us').strip().lower()}-documentai.googleapis.com/v1/{processor_resource}:process"
        response = session.post(
            endpoint,
            json={
                "rawDocument": {
                    "content": base64.b64encode(payload).decode("ascii"),
                    "mimeType": mime_type,
                },
                "fieldMask": ",".join(CURRENT_FIELD_MASK_PATHS),
            },
            timeout=timeout_sec,
        )
        response.raise_for_status()
        payload_json = response.json()
        document = documentai_v1.Document.from_json(json.dumps(payload_json.get("document", {})))
        analysis = _analyze_document(document)
        return {
            "variant": "rest_equivalent_current_request",
            "description": "REST-equivalent request with the current production field_mask",
            "transport": "rest",
            "processor_resource": processor_resource,
            "field_mask_paths": list(CURRENT_FIELD_MASK_PATHS),
            "sample_style": False,
            "success": True,
            "error": None,
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 2),
            **analysis,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "variant": "rest_equivalent_current_request",
            "description": "REST-equivalent request with the current production field_mask",
            "transport": "rest",
            "processor_resource": processor_resource,
            "field_mask_paths": list(CURRENT_FIELD_MASK_PATHS),
            "sample_style": False,
            "success": False,
            "error": str(exc),
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 2),
            "document_layout_block_count": 0,
            "pages_count": 0,
            "text_length": 0,
            "document_layout_blocks_have_geometry": False,
            "document_layout_blocks_with_geometry_count": 0,
            "extracted_element_count": 0,
            "canonical_region_count": 0,
            "canonical_regions_can_be_produced": False,
            "empty_reason": None,
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run a live Google layout request matrix.")
    parser.add_argument(
        "--pdf",
        default=str(_REPO_ROOT / ".tmp" / "google_layout_request.pdf"),
        help="Path to the prepared PDF payload",
    )
    parser.add_argument(
        "--output",
        default=str(_REPO_ROOT / ".tmp" / "google_layout_request_matrix.json"),
        help="Output JSON path",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=90,
        help="Per-request timeout in seconds",
    )
    args = parser.parse_args()

    env = _load_env()
    payload = Path(args.pdf).read_bytes()
    mime_type = "application/pdf"
    location = env.get("GOOGLE_LOCATION", "us").strip().lower()
    processor_id = env["GOOGLE_PROCESSOR_ID_LAYOUT"]
    project_id = env["GOOGLE_PROJECT_ID"]
    processor_resource = (
        f"projects/{project_id}/locations/{location}/processors/{processor_id}"
    )

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = env["GOOGLE_CREDENTIALS_PATH"]
    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
    client = documentai_v1.DocumentProcessorServiceClient(client_options=opts)

    results: list[dict[str, Any]] = []

    current_request = _build_request(
        documentai_v1,
        name=processor_resource,
        payload=payload,
        mime_type=mime_type,
        field_mask=_current_field_mask(),
    )
    results.append(
        await _run_client_variant(
            label="current_production_request",
            description="Current production Python client request with the production field_mask",
            client=client,
            request=current_request,
            timeout_sec=args.timeout_sec,
            processor_resource=processor_resource,
            transport="python_client",
            field_mask_paths=list(CURRENT_FIELD_MASK_PATHS),
        )
    )

    no_field_mask_request = _build_request(
        documentai_v1,
        name=processor_resource,
        payload=payload,
        mime_type=mime_type,
    )
    results.append(
        await _run_client_variant(
            label="no_field_mask",
            description="Same request without a field_mask",
            client=client,
            request=no_field_mask_request,
            timeout_sec=args.timeout_sec,
            processor_resource=processor_resource,
            transport="python_client",
            field_mask_paths=None,
        )
    )

    minimal_field_mask_request = _build_request(
        documentai_v1,
        name=processor_resource,
        payload=payload,
        mime_type=mime_type,
        field_mask=_minimal_field_mask(),
    )
    results.append(
        await _run_client_variant(
            label="minimal_field_mask",
            description="Same request with a minimal field_mask containing only document_layout",
            client=client,
            request=minimal_field_mask_request,
            timeout_sec=args.timeout_sec,
            processor_resource=processor_resource,
            transport="python_client",
            field_mask_paths=["document_layout"],
        )
    )

    processor_version_name, processor_version_error = _discover_processor_version(
        client, processor_resource
    )
    if processor_version_name:
        version_request = _build_request(
            documentai_v1,
            name=processor_version_name,
            payload=payload,
            mime_type=mime_type,
            field_mask=_current_field_mask(),
        )
        results.append(
            await _run_client_variant(
                label="explicit_processor_version",
                description="Current production request against an explicit processor version",
                client=client,
                request=version_request,
                timeout_sec=args.timeout_sec,
                processor_resource=processor_version_name,
                transport="python_client",
                field_mask_paths=list(CURRENT_FIELD_MASK_PATHS),
            )
        )
    else:
        results.append(
            {
                "variant": "explicit_processor_version",
                "description": "Current production request against an explicit processor version",
                "transport": "python_client",
                "processor_resource": processor_resource,
                "field_mask_paths": list(CURRENT_FIELD_MASK_PATHS),
                "sample_style": False,
                "success": False,
                "error": processor_version_error or "processor version unavailable",
                "elapsed_ms": 0.0,
                "document_layout_block_count": 0,
                "pages_count": 0,
                "text_length": 0,
                "document_layout_blocks_have_geometry": False,
                "document_layout_blocks_with_geometry_count": 0,
                "extracted_element_count": 0,
                "canonical_region_count": 0,
                "canonical_regions_can_be_produced": False,
                "empty_reason": None,
                "skipped": True,
            }
        )

    sample_client = documentai.DocumentProcessorServiceClient(client_options=opts)
    sample_style_request = _build_request(
        documentai,
        name=sample_client.processor_path(project_id, location, processor_id),
        payload=payload,
        mime_type=mime_type,
        process_options=documentai.ProcessOptions(
            individual_page_selector=documentai.ProcessOptions.IndividualPageSelector(
                pages=[1]
            )
        ),
    )
    results.append(
        await _run_client_variant(
            label="sample_client_library_style",
            description="Python client request shaped like Google's sample, with page selector and no field_mask",
            client=sample_client,
            request=sample_style_request,
            timeout_sec=args.timeout_sec,
            processor_resource=sample_client.processor_path(project_id, location, processor_id),
            transport="python_client",
            field_mask_paths=None,
            sample_style=True,
        )
    )

    needs_rest = not any(
        row.get("canonical_regions_can_be_produced") or row.get("document_layout_blocks_have_geometry")
        for row in results
        if row.get("success")
    )
    if needs_rest:
        results.append(
            await _run_rest_variant(
                env=env,
                payload=payload,
                mime_type=mime_type,
                processor_resource=processor_resource,
                timeout_sec=args.timeout_sec,
            )
        )

    output = {
        "pdf_path": str(Path(args.pdf)),
        "processor_resource": processor_resource,
        "timeout_sec": args.timeout_sec,
        "results": results,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(str(output_path))
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
