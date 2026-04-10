#!/usr/bin/env python3
"""Upload test image to MinIO for Google cleanup validation"""

import sys
from minio import Minio

# MinIO client - connects to minio service in docker
client = Minio(
    "minio:9000",  # Docker internal hostname
    access_key="libraryai",
    secret_key="libraryaipassword123",
    secure=False,
)

# Upload test image
bucket_name = "libraryai"
object_name = "uploads/google-cleanup-test.tif"
file_path = "test_data/sample_book.tif"

try:
    # Ensure bucket exists
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
        print(f"Created bucket: {bucket_name}")

    # Upload file
    result = client.fput_object(bucket_name, object_name, file_path)
    print(f"✅ Uploaded {file_path} to s3://{bucket_name}/{object_name}")
    print(f"   ETag: {result.etag}")
except Exception as e:
    print(f"❌ Upload failed: {e}")
    sys.exit(1)
