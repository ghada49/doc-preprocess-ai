#!/usr/bin/env python3
"""
Test IEP1D rectification on sample_book.tif
"""
import json
import sys
from pathlib import Path
from uuid import uuid4

import cv2
import requests

# Configuration
SAMPLE_IMAGE = Path("test_data/sample_book2.jpeg")
IEP1D_URL = "http://localhost:8003/v1/rectify"
MINIO_URL = "http://localhost:9000"
MINIO_KEY = "minioadmin"
MINIO_SECRET = "minioadmin"

print("=" * 80)
print("IEP1D Rectification Test")
print("=" * 80)

# 1. Check image exists
if not SAMPLE_IMAGE.exists():
    print(f"❌ Image not found: {SAMPLE_IMAGE}")
    sys.exit(1)

print(f"✅ Image found: {SAMPLE_IMAGE}")
img = cv2.imread(str(SAMPLE_IMAGE), cv2.IMREAD_COLOR)
print(f"   Size: {img.shape[0]}x{img.shape[1]} pixels")
print(f"   File size: {SAMPLE_IMAGE.stat().st_size / 1024 / 1024:.1f} MB")

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

# 3. Call rectify endpoint
print("\nCalling /v1/rectify endpoint...")
job_id = f"test-{uuid4().hex[:8]}"
page_number = 1

payload = {
    "job_id": job_id,
    "page_number": page_number,
    "image_uri": f"file://{SAMPLE_IMAGE.absolute()}",
    "material_type": "book",
}

print(f"   Job ID: {job_id}")
print(f"   Material: {payload['material_type']}")

try:
    response = requests.post(IEP1D_URL, json=payload, timeout=120)
    response.raise_for_status()
    result = response.json()
    print("\n✅ Rectification succeeded!")
    print("\nResult:")
    print(json.dumps(result, indent=2))

    # Extract key metrics
    if "rectified_image_uri" in result:
        print(f"\n📦 Rectified output stored at: {result['rectified_image_uri']}")
    if "metrics" in result:
        print("\n📊 Rectification metrics:")
        for key, value in result["metrics"].items():
            if isinstance(value, float):
                print(f"   {key}: {value:.4f}")
            else:
                print(f"   {key}: {value}")

except requests.exceptions.RequestException as e:
    print(f"❌ Request failed: {e}")
    if hasattr(e, "response") and e.response is not None:
        print(f"   Status: {e.response.status_code}")
        print(f"   Response: {e.response.text}")
    sys.exit(1)

print("\n" + "=" * 80)
