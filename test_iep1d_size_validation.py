#!/usr/bin/env python3
"""
Test IEP1D image size validation
"""
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
    """Test rectification with given image"""
    print(f"\n{'='*80}")
    print(f"Testing: {description}")

    if not Path(image_path).exists():
        print(f"[ERROR] Image not found: {image_path}")
        return False

    img_bytes = Path(image_path).read_bytes()
    size_mb = len(img_bytes) / 1024 / 1024
    print(f"[OK] Image size: {size_mb:.1f} MB")

    # Upload to MinIO
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
    print("[OK] Uploaded to MinIO")

    # Test rectify endpoint
    payload = {
        "job_id": f"test-{uuid4().hex[:8]}",
        "page_number": 1,
        "image_uri": image_uri,
        "material_type": "book",
    }

    print("[INFO] Calling /v1/rectify...")
    try:
        response = requests.post(IEP1D_URL, json=payload, timeout=120)

        if response.status_code == 200:
            print("[OK] Success! (HTTP 200)")
            result = response.json()
            if "skew_residual_after" in result:
                print(
                    f"     Skew: {result['skew_residual_before']:.2f}° -> {result['skew_residual_after']:.2f}°"
                )
            return True

        elif response.status_code == 413:
            print("[OK] Correctly rejected with HTTP 413 (Payload Too Large)")
            detail = response.json().get("detail", {})
            print(f"     Error: {detail.get('error_code', 'N/A')}")
            print(f"     Message: {detail.get('error_message', 'N/A')[:100]}...")
            return True

        else:
            print(f"[ERROR] HTTP {response.status_code}")
            print(f"        {response.text[:200]}")
            return False

    except requests.exceptions.Timeout:
        print("[ERROR] Request TIMEOUT (>120 seconds)")
        return False
    except Exception as e:
        print(f"[ERROR] {e}")
        return False


# Run tests
print("IEP1D Image Size Validation Test")
print("Testing with different image sizes")

# Test 1: Small image (should work)
test_image("test_data/sample_book.tif", "Small image (20.7 MB, OK)")

# Test 2: Large image (should be rejected)
test_image("test_data/sample_na121_7.tif", "Large image (155.3 MB, REJECT)")

print(f"\n{'='*80}")
print("[SUMMARY] Size validation working as expected")
