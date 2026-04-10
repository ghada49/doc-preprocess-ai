#!/usr/bin/env python3
"""
Test large image with detailed response streaming and logging
"""
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import boto3
import requests

# Configuration
MINIO_ENDPOINT = "http://localhost:9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"
MINIO_BUCKET = "test-artifacts"
IEP1D_URL = "http://localhost:8003/v1/rectify"

SAMPLE_IMAGE = Path("test_data/sample_na121_7.tif")

print("=" * 80)
print("IEP1D Large Image Stream Test")
print("=" * 80)

if not SAMPLE_IMAGE.exists():
    print(f"[ERROR] Image not found: {SAMPLE_IMAGE}")
    sys.exit(1)

img_bytes = SAMPLE_IMAGE.read_bytes()
size_mb = len(img_bytes) / 1024 / 1024
print(f"[OK] Image: {size_mb:.1f} MB")

# Upload to MinIO
print("\n[INFO] Uploading to MinIO...")
s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
    region_name="us-east-1",
)

try:
    s3.head_bucket(Bucket=MINIO_BUCKET)
except:
    s3.create_bucket(Bucket=MINIO_BUCKET)

test_key = f"test-rectify/{uuid4().hex}/input.tif"
s3.put_object(Bucket=MINIO_BUCKET, Key=test_key, Body=img_bytes)
image_uri = f"s3://{MINIO_BUCKET}/{test_key}"
print(f"[OK] Uploaded: {image_uri}")

# Create request payload
job_id = f"test-stream-{uuid4().hex[:8]}"
payload = {"job_id": job_id, "page_number": 1, "image_uri": image_uri, "material_type": "book"}

# Call with streaming and detailed logging
print(f"\n[INFO] Posting to {IEP1D_URL}")
print(f"      Job ID: {job_id}")
print("      Timeout: 600 seconds")
print("      Using requests with stream=True...")

t0 = time.time()
try:
    # Use stream=True to see if we get headers quickly
    response = requests.post(IEP1D_URL, json=payload, timeout=600, stream=True)
    t_first_byte = time.time()
    print(f"\n[OK] First response received! ({t_first_byte-t0:.1f}s)")
    print(f"     Status: {response.status_code}")
    print(f"     Headers: {dict(response.headers)}")

    # Read full response
    content = response.content
    t_done = time.time()
    print(f"[OK] Full response read ({t_done-t_first_byte:.1f}s)")
    print(f"     Size: {len(content)} bytes")

    if response.status_code == 200:
        result = response.json()
        print(f"\n[OK] Result:\n{json.dumps(result, indent=2)}")
    else:
        print(f"\n[ERROR] HTTP {response.status_code}: {content.decode()[:500]}")

except requests.exceptions.Timeout as e:
    t_timeout = time.time()
    print(f"\n[ERROR] Request TIMEOUT after {t_timeout-t0:.1f}s")
    print(f"        {e}")
except Exception as e:
    t_err = time.time()
    print(f"\n[ERROR] Request failed after {t_err-t0:.1f}s: {e}")
    import traceback

    traceback.print_exc()
