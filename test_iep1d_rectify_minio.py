#!/usr/bin/env python3
"""
Test IEP1D rectification by uploading test image to MinIO via boto3
"""
import json
import sys
from pathlib import Path
from uuid import uuid4

import boto3
import requests

# Configuration
SAMPLE_IMAGE = Path("test_data/sample_book3.tif")  # Large image test
IEP1D_URL = "http://localhost:8003/v1/rectify"
REQUEST_TIMEOUT = 300  # 5 minutes for large images
MINIO_ENDPOINT = "http://localhost:9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"
MINIO_BUCKET = "test-artifacts"

print("=" * 80)
print("IEP1D Rectification Test with MinIO (boto3)")
print("=" * 80)

# 1. Check image exists
if not SAMPLE_IMAGE.exists():
    print(f"❌ Image not found: {SAMPLE_IMAGE}")
    sys.exit(1)

print(f"✅ Image found: {SAMPLE_IMAGE}")
img_bytes = SAMPLE_IMAGE.read_bytes()
print(f"   File size: {len(img_bytes) / 1024 / 1024:.1f} MB")

# 2. Check IEP1D service
print("\nChecking IEP1D service...")
try:
    health = requests.get("http://localhost:8003/health", timeout=5).json()
    ready = requests.get("http://localhost:8003/ready", timeout=5).json()
    print(f"   Health: {health}")
    print(f"   Ready: {ready}")
    if ready.get("status") != "ready":
        print("❌ IEP1D model not ready!")
        sys.exit(1)
except Exception as e:
    print(f"❌ Failed to connect to IEP1D: {e}")
    sys.exit(1)

# 3. Upload image to MinIO
print("\nUploading image to MinIO...")
try:
    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
    )

    # Create bucket if needed
    try:
        s3.head_bucket(Bucket=MINIO_BUCKET)
    except:
        s3.create_bucket(Bucket=MINIO_BUCKET)
        print(f"   Created bucket: {MINIO_BUCKET}")

    # Upload file
    test_key = f"test-rectify/{uuid4().hex}/input.tif"
    s3.put_object(Bucket=MINIO_BUCKET, Key=test_key, Body=img_bytes)
    print(f"   Uploaded to: s3://{MINIO_BUCKET}/{test_key}")

    image_uri = f"s3://{MINIO_BUCKET}/{test_key}"

except Exception as e:
    print(f"❌ MinIO upload failed: {e}")
    import traceback

    traceback.print_exc()
    sys.exit(1)

# 4. Call rectify endpoint
print("\nCalling /v1/rectify endpoint...")
job_id = f"test-{uuid4().hex[:8]}"
page_number = 1

payload = {
    "job_id": job_id,
    "page_number": page_number,
    "image_uri": image_uri,
    "material_type": "book",
}

print(f"   Job ID: {job_id}")
print(f"   Image URI: {image_uri}")
print(f"   Material: {payload['material_type']}")
print(f"\n   Processing with {REQUEST_TIMEOUT}s timeout...")
print("   (Large images may take several minutes)")

try:
    import time

    start = time.time()
    response = requests.post(IEP1D_URL, json=payload, timeout=REQUEST_TIMEOUT)
    elapsed = time.time() - start

    response.raise_for_status()
    result = response.json()
    print(f"\n✅ Rectification succeeded! ({elapsed:.1f}s)")
    print("\nFull Result:")
    print(json.dumps(result, indent=2))

    # Extract key metrics
    if "rectified_image_uri" in result:
        print("\n📦 Rectified output stored at:")
        print(f"   {result['rectified_image_uri']}")

    if "metrics" in result:
        print("\n📊 Rectification Metrics:")
        metrics = result["metrics"]
        for key, value in metrics.items():
            if isinstance(value, float):
                print(f"   {key}: {value:.6f}")
            else:
                print(f"   {key}: {value}")

except requests.exceptions.Timeout:
    print(f"❌ Request timed out after {REQUEST_TIMEOUT} seconds")
    print("   The image may be too large or the service is slow")
    sys.exit(1)
except requests.exceptions.RequestException as e:
    print(f"❌ Request failed: {e}")
    if hasattr(e, "response") and e.response is not None:
        print(f"   Status: {e.response.status_code}")
        print(f"   Response: {e.response.text}")
    sys.exit(1)

print("\n" + "=" * 80)
print("✅ IEP1D Rectification Test Complete")
print("=" * 80)
