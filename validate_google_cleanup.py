#!/usr/bin/env python3
"""
Test script to create a real job in the running LibraryAI system
and monitor the Google cleanup fallback execution.
"""

import json
import requests
import sys
import time
from pathlib import Path

# Configuration
EEP_API = "http://localhost:8000"
COLLECTION_ID = "google-cleanup-validation"
MATERIAL_TYPE_VALUE = "book"
TEST_IMAGE = Path("test_data/sample_book.tif")
PAGE_INPUT_URI = "s3://libraryai/uploads/google-cleanup-test.tif"
TEST_USER = "testuser"
TEST_PASSWORD = "testpass123"

# Global session with auth
session = requests.Session()

def authenticate():
    """Get an auth token"""
    print("Authenticating...")

    payload = {
        "username": TEST_USER,
        "password": TEST_PASSWORD,
    }

    response = requests.post(
        f"{EEP_API}/v1/auth/token",
        data=payload,  # Note: form data, not JSON
    )

    if response.status_code not in [200, 201]:
        print(f"⚠️  Auth failed: {response.status_code}")
        print(f"   Response: {response.text}")
        print(f"   Trying without auth (might fail)...")
        return False

    data = response.json()
    token = data.get("access_token")
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})
        print(f"✅ Authenticated as {TEST_USER}")
        return True

    return False

def create_job():
    """Create a test job that exercises the full rescue path"""

    # Minimal job creation payload
    payload = {
        "collection_id": COLLECTION_ID,
        "material_type": MATERIAL_TYPE_VALUE,
        "pages": [
            {
                "page_number": 1,
                "input_uri": PAGE_INPUT_URI,
            }
        ],
        "pipeline_mode": "layout",
        "ptiff_qa_mode": "manual",
        "policy_version": "v1",
    }

    print(f"Creating job with payload: {json.dumps(payload, indent=2)}")

    # Create the job
    response = session.post(
        f"{EEP_API}/v1/jobs",
        json=payload,
        headers={"Content-Type": "application/json"},
    )

    if response.status_code != 201:
        print(f"❌ Job creation failed: {response.status_code}")
        print(f"Response: {response.text}")
        return None

    data = response.json()
    job_id = data.get("job_id")
    print(f"✅ Job created: {job_id}")
    print(f"   Status: {data.get('status')}")
    print(f"   Pages: {data.get('page_count')}")

    return job_id

def get_job_status(job_id):
    """Get job status"""
    response = session.get(f"{EEP_API}/v1/jobs/{job_id}")
    if response.status_code != 200:
        print(f"❌ Failed to get job status: {response.status_code}")
        return None
    return response.json()

def main():
    print("=" * 80)
    print("Google Cleanup Fallback Runtime Validation")
    print("=" * 80)
    print()

    # Authenticate
    print(f"[0/4] Authenticating...")
    authenticate()
    print()

    # Check if EEP is reachable
    print(f"[1/4] Checking EEP API connectivity...")
    try:
        response = requests.get(f"{EEP_API}/health", timeout=5)
        if response.status_code == 200:
            print(f"✅ EEP API is online")
        else:
            print(f"⚠️  EEP API returned {response.status_code}")
    except Exception as e:
        print(f"❌ Cannot reach EEP: {e}")
        sys.exit(1)

    print()
    print(f"[2/4] Creating a test job...")
    job_id = create_job()
    if not job_id:
        sys.exit(1)

    print()
    print(f"[3/4] Waiting for job to process...")
    print(f"      (monitoring for Google cleanup invocation...)")

    # Poll job status for a while
    start_time = time.time()
    timeout_seconds = 60

    while time.time() - start_time < timeout_seconds:
        status = get_job_status(job_id)
        if not status:
            time.sleep(2)
            continue

        page_status = status.get("pages", [{}])[0].get("status", "unknown")
        print(f"   Job status: {status.get('status')}, Page status: {page_status}")

        # Check for completion or rescue path
        if page_status in ["terminal_accepted", "terminal_rejected", "pending_human_correction"]:
            print(f"✅ Page reached terminal state: {page_status}")
            break

        time.sleep(5)

    if time.time() - start_time >= timeout_seconds:
        print(f"⏱️  Timeout waiting for job completion")

    print()
    print(f"[4/4] Final job status:")
    final_status = get_job_status(job_id)
    if final_status:
        print(json.dumps(final_status, indent=2))

    print()
    print("=" * 80)
    print("Now check worker logs for Google cleanup messages:")
    print("  docker compose logs --follow eep-worker | grep -i google")
    print("=" * 80)

if __name__ == "__main__":
    main()
