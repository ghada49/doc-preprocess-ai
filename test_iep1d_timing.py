#!/usr/bin/env python3
"""
Debug IEP1D test with detailed timing
"""
import json
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


def test_image(image_path, description):
    print(f"\n{'='*80}")
    print(f"Testing: {description}")
    print(f"{'='*80}")

    # Load image
    t0 = time.time()
    if not Path(image_path).exists():
        print(f"[ERROR] Image not found: {image_path}")
        return False

    img_bytes = Path(image_path).read_bytes()
    size_mb = len(img_bytes) / 1024 / 1024
    print(f"[OK] Image loaded: {size_mb:.1f} MB")
    t1 = time.time()
    print(f"   Load time: {t1-t0:.2f}s")

    # Upload to MinIO
    t0 = time.time()
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

        test_key = f"test-rectify/{uuid4().hex}/input.tif"
        s3.put_object(Bucket=MINIO_BUCKET, Key=test_key, Body=img_bytes)
        t1 = time.time()
        print(f"[OK] Uploaded to MinIO: s3://{MINIO_BUCKET}/{test_key}")
        print(f"   Upload time: {t1-t0:.2f}s")

        image_uri = f"s3://{MINIO_BUCKET}/{test_key}"
    except Exception as e:
        print(f"[ERROR] MinIO upload failed: {e}")
        return False

    # Call rectify
    t0 = time.time()
    payload = {
        "job_id": f"test-{uuid4().hex[:8]}",
        "page_number": 1,
        "image_uri": image_uri,
        "material_type": "book",
    }

    print("\n[INFO] Calling /v1/rectify...")
    print(f"   Payload: {json.dumps(payload, indent=2)}")
    print("   Timeout: 300 seconds")

    try:
        t_request_start = time.time()
        response = requests.post(IEP1D_URL, json=payload, timeout=300)
        t_request_end = time.time()
        request_time = t_request_end - t_request_start

        print(f"[OK] Response received ({request_time:.1f}s, HTTP {response.status_code})")

        if response.status_code != 200:
            print(f"[ERROR] HTTP Error: {response.status_code}")
            print(f"   Body: {response.text[:500]}")
            return False

        result = response.json()
        t1 = time.time()
        print("[OK] Rectification completed!")
        print(f"   Total time: {t1-t0:.2f}s")
        print("\n[INFO] Result:")
        print(json.dumps(result, indent=2))
        return True

    except requests.exceptions.Timeout:
        t1 = time.time()
        print(f"[ERROR] Request TIMEOUT after {t1-t0:.1f}s")
        return False
    except Exception as e:
        t1 = time.time()
        print(f"[ERROR] Request failed ({t1-t0:.1f}s): {e}")
        return False


# Run tests
print("IEP1D Timing Test Suite")
print("Testing with different image sizes")

# Test 1: Small image (should work)
test_image("test_data/sample_book.tif", "Small image (20.7 MB)")

# Test 2: Large image (problematic)
test_image("test_data/sample_na121_7.tif", "Large image (155.3 MB)")

print(f"\n{'='*80}")
print("Test complete")
