#!/usr/bin/env python3
"""Batch-process a large TIFF with Google Document AI.

This script is intended for files that exceed the 40 MB synchronous limit,
such as ``test_data/sample_book.tif``. It uploads the local TIFF to GCS,
submits a ``batch_process_documents`` request, downloads the JSON results,
and exports any returned ``pages[].image.content`` bytes for inspection.

Typical usage:

    $env:GOOGLE_APPLICATION_CREDENTIALS="C:\\secrets\\documentai.json"
    python scripts/documentai_batch_process_large_tiff.py `
        --bucket your-gcs-bucket

By default, the script reads:
    - project from GOOGLE_PROJECT_ID
    - location from GOOGLE_LOCATION (defaults to "us")
    - processor from GOOGLE_PROCESSOR_ID_LAYOUT
    - input file from test_data/sample_book.tif
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Iterable

try:
    from google.api_core.client_options import ClientOptions
    from google.cloud import documentai
    from google.cloud import storage
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "Missing Google Cloud dependencies. Install:\n"
        "  pip install google-cloud-documentai google-cloud-storage"
    ) from exc


DEFAULT_INPUT_FILE = Path("test_data/sample_book3.tif")
DEFAULT_LOCAL_OUTPUT_DIR = Path(".tmp") / "documentai"
DEFAULT_FIELD_MASK = "text,pages.image,pages.transforms,pages.dimension"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-process a large TIFF with Google Document AI."
    )
    parser.add_argument(
        "--project-id",
        default=_env("GOOGLE_PROJECT_ID"),
        help="Google Cloud project id. Defaults to GOOGLE_PROJECT_ID.",
    )
    parser.add_argument(
        "--location",
        default=_env("GOOGLE_LOCATION", "us"),
        help="Processor location, usually 'us' or 'eu'. Defaults to GOOGLE_LOCATION or 'us'.",
    )
    parser.add_argument(
        "--processor-id",
        default=_env("GOOGLE_PROCESSOR_ID_LAYOUT"),
        help="Document AI processor id. Defaults to GOOGLE_PROCESSOR_ID_LAYOUT.",
    )
    parser.add_argument(
        "--processor-version-id",
        default=None,
        help="Optional processor version id. If omitted, the processor default version is used.",
    )
    parser.add_argument(
        "--input-file",
        type=Path,
        default=DEFAULT_INPUT_FILE,
        help=f"Local TIFF to upload. Defaults to {DEFAULT_INPUT_FILE}.",
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="GCS bucket used for both the uploaded source file and the batch output.",
    )
    parser.add_argument(
        "--input-prefix",
        default="document-ai-inputs",
        help="GCS prefix for the uploaded TIFF.",
    )
    parser.add_argument(
        "--output-prefix",
        default="document-ai-outputs",
        help="GCS prefix where Document AI writes JSON output.",
    )
    parser.add_argument(
        "--mime-type",
        default="image/tiff",
        help="Input MIME type. Defaults to image/tiff.",
    )
    parser.add_argument(
        "--field-mask",
        default=DEFAULT_FIELD_MASK,
        help=(
            "DocumentOutputConfig field mask. Defaults to "
            f"'{DEFAULT_FIELD_MASK}'."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=7200,
        help="How long to wait for the long-running batch operation.",
    )
    parser.add_argument(
        "--upload-timeout-seconds",
        type=int,
        default=1800,
        help="Timeout for uploading the local file to GCS. Defaults to 1800 seconds.",
    )
    parser.add_argument(
        "--upload-chunk-size-mb",
        type=int,
        default=8,
        help="Chunk size in MB for resumable GCS uploads. Defaults to 8.",
    )
    parser.add_argument(
        "--local-output-dir",
        type=Path,
        default=DEFAULT_LOCAL_OUTPUT_DIR,
        help=f"Directory for downloaded JSON and exported page images. Defaults to {DEFAULT_LOCAL_OUTPUT_DIR}.",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Assume the input TIFF already exists in GCS and do not upload it again.",
    )
    parser.add_argument(
        "--gcs-input-uri",
        default=None,
        help="Explicit gs:// input URI to process. If set, local upload is skipped.",
    )
    parser.add_argument(
        "--layout-return-images",
        action="store_true",
        help="Enable Layout Parser return_images/return_bounding_boxes options.",
    )
    return parser.parse_args()


def _env(key: str, default: str | None = None) -> str | None:
    import os

    return os.environ.get(key, default)


def validate_args(args: argparse.Namespace) -> None:
    missing = []
    if not args.project_id:
        missing.append("--project-id or GOOGLE_PROJECT_ID")
    if not args.processor_id:
        missing.append("--processor-id or GOOGLE_PROCESSOR_ID_LAYOUT")
    if not args.gcs_input_uri and not args.input_file.exists():
        raise SystemExit(f"Input file not found: {args.input_file}")
    if missing:
        raise SystemExit("Missing required configuration: " + ", ".join(missing))


def parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Expected gs:// URI, got: {uri}")
    body = uri[5:]
    bucket, _, prefix = body.partition("/")
    if not bucket:
        raise ValueError(f"Invalid gs:// URI: {uri}")
    return bucket, prefix


def build_client(location: str) -> documentai.DocumentProcessorServiceClient:
    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
    return documentai.DocumentProcessorServiceClient(client_options=opts)


def upload_file(
    storage_client: storage.Client,
    local_path: Path,
    bucket_name: str,
    object_name: str,
    timeout_seconds: int,
    chunk_size_mb: int,
) -> str:
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    blob.chunk_size = chunk_size_mb * 1024 * 1024
    print(
        f"Uploading {local_path} -> gs://{bucket_name}/{object_name} "
        f"(timeout={timeout_seconds}s, chunk_size={chunk_size_mb}MB)"
    )
    blob.upload_from_filename(
        str(local_path),
        content_type="image/tiff",
        timeout=timeout_seconds,
    )
    return f"gs://{bucket_name}/{object_name}"


def build_process_options(
    layout_return_images: bool,
) -> documentai.ProcessOptions | None:
    if not layout_return_images:
        return None
    return documentai.ProcessOptions(
        layout_config=documentai.ProcessOptions.LayoutConfig(
            return_images=True,
            return_bounding_boxes=True,
        )
    )


def build_processor_name(
    client: documentai.DocumentProcessorServiceClient,
    project_id: str,
    location: str,
    processor_id: str,
    processor_version_id: str | None,
) -> str:
    if processor_version_id:
        return client.processor_version_path(
            project_id, location, processor_id, processor_version_id
        )
    return client.processor_path(project_id, location, processor_id)


def run_batch(
    client: documentai.DocumentProcessorServiceClient,
    project_id: str,
    location: str,
    processor_id: str,
    processor_version_id: str | None,
    gcs_input_uri: str,
    gcs_output_uri: str,
    mime_type: str,
    field_mask: str,
    timeout_seconds: int,
    layout_return_images: bool,
) -> documentai.BatchProcessMetadata:
    name = build_processor_name(
        client=client,
        project_id=project_id,
        location=location,
        processor_id=processor_id,
        processor_version_id=processor_version_id,
    )
    input_config = documentai.BatchDocumentsInputConfig(
        gcs_documents=documentai.GcsDocuments(
            documents=[
                documentai.GcsDocument(gcs_uri=gcs_input_uri, mime_type=mime_type)
            ]
        )
    )
    output_config = documentai.DocumentOutputConfig(
        gcs_output_config=documentai.DocumentOutputConfig.GcsOutputConfig(
            gcs_uri=gcs_output_uri,
            field_mask=field_mask,
        )
    )
    process_options = build_process_options(layout_return_images)

    request = documentai.BatchProcessRequest(
        name=name,
        input_documents=input_config,
        document_output_config=output_config,
        process_options=process_options,
    )

    print(f"Submitting batch request for {gcs_input_uri}")
    operation = client.batch_process_documents(request=request)
    print(f"Operation: {operation.operation.name}")
    print("Waiting for batch processing to complete...")
    operation.result(timeout=timeout_seconds)

    metadata = documentai.BatchProcessMetadata(operation.metadata)
    if metadata.state != documentai.BatchProcessMetadata.State.SUCCEEDED:
        raise RuntimeError(f"Batch process failed: {metadata.state_message}")
    return metadata


def list_output_json_blobs(
    storage_client: storage.Client,
    output_gcs_destination: str,
) -> Iterable[storage.Blob]:
    bucket_name, prefix = parse_gs_uri(output_gcs_destination)
    return storage_client.list_blobs(bucket_name, prefix=prefix)


def save_blob_to_local(blob: storage.Blob, base_dir: Path) -> Path:
    local_path = base_dir / blob.name
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(blob.download_as_bytes())
    return local_path


def export_page_images(
    document: documentai.Document,
    base_dir: Path,
    source_name: str,
) -> list[Path]:
    exported: list[Path] = []
    for index, page in enumerate(document.pages, start=1):
        page_image = getattr(page, "image", None)
        content = getattr(page_image, "content", b"") if page_image else b""
        mime_type = getattr(page_image, "mime_type", "") if page_image else ""
        if not content:
            continue
        suffix = mime_suffix(mime_type)
        local_path = base_dir / f"{source_name}.page-{index}{suffix}"
        local_path.write_bytes(content)
        exported.append(local_path)
    return exported


def mime_suffix(mime_type: str) -> str:
    if mime_type == "image/png":
        return ".png"
    if mime_type in {"image/tiff", "image/tif"}:
        return ".tif"
    if mime_type == "image/jpeg":
        return ".jpg"
    return ".bin"


def summarize_document(document: documentai.Document) -> dict[str, object]:
    image_pages = 0
    page_summaries: list[dict[str, object]] = []

    for page in document.pages:
        image_content = getattr(getattr(page, "image", None), "content", b"")
        has_image = bool(image_content)
        if has_image:
            image_pages += 1
        page_summaries.append(
            {
                "page_number": page.page_number,
                "has_image_content": has_image,
                "image_mime_type": getattr(getattr(page, "image", None), "mime_type", ""),
                "transforms": len(getattr(page, "transforms", [])),
                "dimension": {
                    "width": getattr(getattr(page, "dimension", None), "width", 0),
                    "height": getattr(getattr(page, "dimension", None), "height", 0),
                    "unit": getattr(getattr(page, "dimension", None), "unit", ""),
                },
            }
        )

    return {
        "page_count": len(document.pages),
        "text_length": len(document.text),
        "pages_with_image_content": image_pages,
        "pages": page_summaries,
    }


def main() -> int:
    args = parse_args()
    validate_args(args)

    run_id = uuid.uuid4().hex[:12]
    local_root = args.local_output_dir / run_id
    json_dir = local_root / "json"
    image_dir = local_root / "page_images"
    json_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)

    storage_client = storage.Client(project=args.project_id)
    client = build_client(args.location)

    if args.gcs_input_uri:
        gcs_input_uri = args.gcs_input_uri
    else:
        object_name = f"{args.input_prefix.rstrip('/')}/{run_id}/{args.input_file.name}"
        if args.skip_upload:
            gcs_input_uri = f"gs://{args.bucket}/{object_name}"
        else:
            gcs_input_uri = upload_file(
                storage_client=storage_client,
                local_path=args.input_file,
                bucket_name=args.bucket,
                object_name=object_name,
                timeout_seconds=args.upload_timeout_seconds,
                chunk_size_mb=args.upload_chunk_size_mb,
            )

    gcs_output_uri = f"gs://{args.bucket}/{args.output_prefix.rstrip('/')}/{run_id}/"

    print("Starting batch process with:")
    print(f"  project_id: {args.project_id}")
    print(f"  location: {args.location}")
    print(f"  processor_id: {args.processor_id}")
    print(f"  input: {gcs_input_uri}")
    print(f"  output: {gcs_output_uri}")
    print(f"  local_output: {local_root.resolve()}")

    metadata = run_batch(
        client=client,
        project_id=args.project_id,
        location=args.location,
        processor_id=args.processor_id,
        processor_version_id=args.processor_version_id,
        gcs_input_uri=gcs_input_uri,
        gcs_output_uri=gcs_output_uri,
        mime_type=args.mime_type,
        field_mask=args.field_mask,
        timeout_seconds=args.timeout_seconds,
        layout_return_images=args.layout_return_images,
    )

    print("Batch processing succeeded.")
    print(f"State message: {metadata.state_message or '<empty>'}")

    found_json = 0
    exported_images = 0

    for process in metadata.individual_process_statuses:
        print(f"Processed source: {process.input_gcs_source}")
        print(f"Output prefix: {process.output_gcs_destination}")
        blobs = list(list_output_json_blobs(storage_client, process.output_gcs_destination))
        if not blobs:
            print("  No output blobs found under this destination.")
            continue

        for blob in blobs:
            if not blob.name.endswith(".json"):
                continue
            found_json += 1
            local_json = save_blob_to_local(blob, json_dir)
            document = documentai.Document.from_json(
                local_json.read_bytes(),
                ignore_unknown_fields=True,
            )
            summary = summarize_document(document)
            exported = export_page_images(
                document=document,
                base_dir=image_dir,
                source_name=Path(blob.name).stem,
            )
            exported_images += len(exported)

            print(f"  JSON: {local_json}")
            print(
                "  Summary: "
                f"pages={summary['page_count']}, "
                f"text_length={summary['text_length']}, "
                f"pages_with_image_content={summary['pages_with_image_content']}"
            )
            for image_path in exported:
                print(f"  Exported image: {image_path}")

    print()
    print("Done.")
    print(f"Downloaded JSON files: {found_json}")
    print(f"Exported page images: {exported_images}")
    print(f"Local output directory: {local_root.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
