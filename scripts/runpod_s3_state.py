#!/usr/bin/env python3
"""
CLI for merging RunPod pod bookkeeping (used by GitHub Actions scale-up / scale-down).

  Merge append (stdin = existing JSON from S3):
    APPEND_JSON='[{"role":"iep0","pod_id":"...","created_at":"...","source":"..."}]'
    REMOVE_IDS='id1,id2'   # optional
    aws s3 cp s3://bucket/ops/runpod-pods.json - 2>/dev/null | \\
      python3 scripts/runpod_s3_state.py merge

  Emit unique pod IDs for termination (stdin = existing JSON):
    aws s3 cp s3://bucket/ops/runpod-pods.json - | \\
      python3 scripts/runpod_s3_state.py list-ids

  Dry-run: print parsed pod count and ids (stdin = JSON file content)
    python3 scripts/runpod_s3_state.py dry-run < /tmp/state.json
"""

from __future__ import annotations

import json
import os
import sys

# Repo root on path
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from shared.runpod_pods_state import (  # noqa: E402
    json_dumps_state,
    json_loads_state,
    merge_append_and_remove,
    parse_runpod_pods_state,
    unique_pod_ids_for_termination,
)


def _cmd_merge() -> None:
    raw_in = sys.stdin.read()
    existing = json_loads_state(raw_in)
    append_raw = os.environ.get("APPEND_JSON", "[]")
    append = json.loads(append_raw)
    if not isinstance(append, list):
        raise SystemExit("APPEND_JSON must be a JSON array")
    remove_raw = os.environ.get("REMOVE_IDS", "").strip()
    remove = frozenset(x.strip() for x in remove_raw.split(",") if x.strip())
    merged = merge_append_and_remove(existing, append=append, remove_pod_ids=remove)
    sys.stdout.buffer.write(json_dumps_state(merged))


def _cmd_list_ids() -> None:
    raw_in = sys.stdin.read()
    existing = json_loads_state(raw_in)
    for pid in unique_pod_ids_for_termination(existing):
        print(pid)


def _cmd_dry_run() -> None:
    raw_in = sys.stdin.read()
    existing = json_loads_state(raw_in)
    pods = parse_runpod_pods_state(existing)
    ids = unique_pod_ids_for_termination(existing)
    print(f"parsed_entries={len(pods)} unique_pod_ids={len(ids)}")
    for p in pods:
        print(f"  role={p['role']} pod_id={p['pod_id']} created_at={p['created_at']!r} source={p['source']!r}")
    print("unique_ids_for_termination:", ",".join(ids))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "merge"
    if cmd == "merge":
        _cmd_merge()
    elif cmd == "list-ids":
        _cmd_list_ids()
    elif cmd == "dry-run":
        _cmd_dry_run()
    else:
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
