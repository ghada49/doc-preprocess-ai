"""
scripts/seed_test_page.py
-------------------------
Seed a page directly into pending_human_correction for testing
the correction workspace (crop, deskew, split, etc.)

Supports either:
- --image-path   -> uploads the local file to MinIO and stores the s3:// URI
- --artifact-uri -> stores the given URI as-is (must be a valid s3:// URI)

Usage:
    python -m scripts.seed_test_page \
        --job-id <job_id> \
        --page-number 1 \
        --image-path /app/test_data/sample_na121_7.tif

OR:
    python -m scripts.seed_test_page \
        --job-id <job_id> \
        --page-number 1 \
        --artifact-uri s3://libraryai/jobs/<job_id>/input/otiff/1/sample.tif

Optional:
    --user-id <owner_user_id>
    --username <owner_username>

Reads DATABASE_URL and S3_* env vars from the environment.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


def validate_s3_uri(uri: str) -> None:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Expected s3:// URI, got '{parsed.scheme}://'.")
    if not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"Invalid s3 URI: {uri!r}")


def upload_to_minio(local_path: str, job_id: str, page_number: int) -> str:
    """Upload a local file to MinIO and return its s3:// URI."""
    try:
        import boto3
    except ImportError:
        print("[!] boto3 is required. Install it with: pip install boto3")
        sys.exit(1)

    bucket = os.environ.get("S3_BUCKET_NAME", "libraryai")
    endpoint = os.environ.get("S3_ENDPOINT_URL", "http://minio:9000")
    access_key = os.environ.get("S3_ACCESS_KEY") or os.environ.get("S3_ACCESS_KEY_ID", "minioadmin")
    secret_key = os.environ.get("S3_SECRET_KEY") or os.environ.get(
        "S3_SECRET_ACCESS_KEY", "minioadmin"
    )

    filename = os.path.basename(local_path)
    s3_key = f"jobs/{job_id}/input/otiff/{page_number}/{filename}"
    s3_uri = f"s3://{bucket}/{s3_key}"

    print(f"[+] Uploading {local_path} -> {s3_uri} ...")
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    with open(local_path, "rb") as fh:
        s3.put_object(Bucket=bucket, Key=s3_key, Body=fh)
    print("[+] Upload complete.")
    return s3_uri


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed test page for correction workspace")

    parser.add_argument("--job-id", required=True, help="Existing job_id")
    parser.add_argument("--page-number", type=int, required=True, help="Page number (e.g. 1)")

    parser.add_argument(
        "--image-path",
        default=None,
        help="Local image path — uploaded to MinIO, s3:// URI stored in DB",
    )
    parser.add_argument(
        "--artifact-uri",
        default=None,
        help="Existing s3:// artifact URI to store directly",
    )

    parser.add_argument("--user-id", default=None, help="Expected owner user_id of the job")
    parser.add_argument("--username", default=None, help="Expected owner username of the job")

    parser.add_argument("--db-url", default=None)

    args = parser.parse_args()

    if args.user_id and args.username:
        print("[!] Use either --user-id or --username, not both.")
        sys.exit(1)

    if bool(args.image_path) == bool(args.artifact_uri):
        print("[!] Provide exactly one of --image-path or --artifact-uri.")
        sys.exit(1)

    if args.image_path:
        if not os.path.exists(args.image_path):
            print(f"[!] Image path does not exist: {args.image_path}")
            sys.exit(1)
        artifact_uri = upload_to_minio(args.image_path, args.job_id, args.page_number)
    else:
        try:
            validate_s3_uri(args.artifact_uri)
        except ValueError as exc:
            print(f"[!] {exc}")
            sys.exit(1)
        artifact_uri = args.artifact_uri

    db_url: str = args.db_url or os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://libraryai:changeme@localhost:5432/libraryai",
    )
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")

    engine = create_engine(db_url)

    with Session(engine) as session:
        expected_user_id = args.user_id

        if args.username:
            user_row = session.execute(
                text("SELECT user_id FROM users WHERE username = :u"),
                {"u": args.username},
            ).fetchone()

            if not user_row:
                print(f"[!] Username '{args.username}' not found.")
                sys.exit(1)

            expected_user_id = str(user_row[0])

        job = session.execute(
            text(
                "SELECT job_id, created_by, material_type, policy_version"
                " FROM jobs WHERE job_id = :jid"
            ),
            {"jid": args.job_id},
        ).fetchone()

        if not job:
            print(f"[!] Job {args.job_id} not found.")
            sys.exit(1)

        job_owner_id = str(job[1]) if job[1] is not None else None
        job_material_type = str(job[2]) if job[2] is not None else "document"
        job_policy_version = str(job[3]) if job[3] is not None else "v0-seed"

        if expected_user_id and job_owner_id != expected_user_id:
            print("[!] Ownership mismatch.")
            print(f"    Job owner user_id : {job_owner_id}")
            print(f"    Expected user_id  : {expected_user_id}")
            sys.exit(1)

        existing_page = session.execute(
            text(
                """
                SELECT page_id
                FROM job_pages
                WHERE job_id = :jid AND page_number = :pn
                """
            ),
            {"jid": args.job_id, "pn": args.page_number},
        ).fetchone()

        if existing_page:
            page_id = str(existing_page[0])

            session.execute(
                text(
                    """
                    UPDATE job_pages
                    SET status = 'pending_human_correction',
                        review_reasons = '["manual_test"]'::jsonb,
                        input_image_uri = :uri,
                        output_image_uri = :uri
                    WHERE page_id = :pid
                    """
                ),
                {
                    "uri": artifact_uri,
                    "pid": page_id,
                },
            )

            print("[+] Updated existing page -> pending_human_correction")

        else:
            page_id = str(uuid.uuid4())

            session.execute(
                text(
                    """
                    INSERT INTO job_pages (
                        page_id,
                        job_id,
                        page_number,
                        status,
                        review_reasons,
                        input_image_uri,
                        output_image_uri
                    )
                    VALUES (
                        :pid,
                        :jid,
                        :pn,
                        'pending_human_correction',
                        '["manual_test"]'::jsonb,
                        :uri,
                        :uri
                    )
                    """
                ),
                {
                    "pid": page_id,
                    "jid": args.job_id,
                    "pn": args.page_number,
                    "uri": artifact_uri,
                },
            )

            print("[+] Created new test page")

        # Upsert page_lineage — required by correction apply/reject endpoints.
        # ON CONFLICT covers the re-seed case (same job+page run twice).
        session.execute(
            text(
                """
                INSERT INTO page_lineage (
                    lineage_id,
                    job_id,
                    page_number,
                    sub_page_index,
                    correlation_id,
                    input_image_uri,
                    otiff_uri,
                    output_image_uri,
                    material_type,
                    policy_version,
                    preprocessed_artifact_state,
                    layout_artifact_state,
                    human_corrected,
                    created_at
                )
                VALUES (
                    gen_random_uuid(),
                    :jid,
                    :pn,
                    NULL,
                    :corr_id,
                    :uri,
                    :uri,
                    :uri,
                    :material_type,
                    :policy_version,
                    'confirmed',
                    'pending',
                    false,
                    NOW()
                )
                ON CONFLICT (job_id, page_number, sub_page_index)
                DO UPDATE SET
                    otiff_uri                   = EXCLUDED.otiff_uri,
                    output_image_uri            = EXCLUDED.output_image_uri,
                    preprocessed_artifact_state = 'confirmed'
                """
            ),
            {
                "jid": args.job_id,
                "pn": args.page_number,
                "uri": artifact_uri,
                "corr_id": f"seed-{args.job_id}-p{args.page_number}",
                "material_type": job_material_type,
                "policy_version": job_policy_version,
            },
        )

        print("[+] Upserted page_lineage row")

        session.commit()

    print("\n=== READY FOR TEST ===")
    print(f"Job ID       : {args.job_id}")
    print(f"Page Number  : {args.page_number}")
    print(f"Page ID      : {page_id}")
    print(f"Job owner    : {job_owner_id}")
    print(f"Artifact URI : {artifact_uri}")
    print("Lineage      : upserted (otiff_uri + output_image_uri = artifact_uri)")
    print("\nOpen in browser:")
    print(f"/queue/{args.job_id}/{args.page_number}/workspace")


if __name__ == "__main__":
    main()
