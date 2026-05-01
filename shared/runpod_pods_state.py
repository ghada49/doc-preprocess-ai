"""
RunPod GPU pod bookkeeping for S3 (ops/runpod-pods.json).

Version 2 schema (preferred):
  {"version": 2, "pods": [{"role", "pod_id", "created_at", "source"}, ...]}

Legacy version 1 (backward compatible):
  {"iep0": "<pod_id>", "iep1a": "<pod_id>", "iep1b": "<pod_id>"}

Scale-up appends new pod records; scale-down terminates every known pod_id and
clears or rewrites the file.  Multiple pods per role are supported.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

RUNPOD_STATE_VERSION = 2
RUNPOD_ROLES = ("iep0", "iep1a", "iep1b")


def iso_now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_runpod_pods_state(data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """
    Return a normalized list of pod entry dicts from raw JSON (v1, v2, or empty).
    Each entry has keys: role, pod_id, created_at (str), source (str).
    """
    if not data:
        return []
    pods_out: list[dict[str, Any]] = []

    if isinstance(data.get("pods"), list):
        for item in data["pods"]:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").strip()
            pod_id = str(item.get("pod_id") or "").strip()
            if role not in RUNPOD_ROLES or not pod_id:
                continue
            pods_out.append(
                {
                    "role": role,
                    "pod_id": pod_id,
                    "created_at": str(item.get("created_at") or "").strip(),
                    "source": str(item.get("source") or "").strip(),
                }
            )
        return pods_out

    # Legacy v1: top-level iep0 / iep1a / iep1b strings
    for role in RUNPOD_ROLES:
        pid = data.get(role)
        if not pid:
            continue
        pod_id = str(pid).strip()
        if pod_id:
            pods_out.append(
                {
                    "role": role,
                    "pod_id": pod_id,
                    "created_at": "",
                    "source": "legacy_v1",
                }
            )
    return pods_out


def serialize_runpod_pods_state_v2(pods: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": RUNPOD_STATE_VERSION, "pods": pods}


def unique_pod_ids_for_termination(state: Mapping[str, Any] | None) -> list[str]:
    """All distinct pod_id values to terminate (stable order: first seen)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in parse_runpod_pods_state(state):
        pid = entry["pod_id"]
        if pid not in seen:
            seen.add(pid)
            ordered.append(pid)
    return ordered


def latest_pod_id_per_role(state: Mapping[str, Any] | None) -> dict[str, str]:
    """
    For Grafana/service probes: one pod id per role (newest by created_at ISO).
    Legacy rows with empty created_at sort before any ISO timestamp (oldest).
    """
    best_ts: dict[str, str] = {}
    best_id: dict[str, str] = {}
    for entry in parse_runpod_pods_state(state):
        role = entry["role"]
        pod_id = entry["pod_id"]
        ts = entry.get("created_at") or ""
        cur = best_ts.get(role)
        if cur is None or ts > (cur or ""):
            best_ts[role] = ts
            best_id[role] = pod_id
    return best_id


def merge_append_and_remove(
    existing: Mapping[str, Any] | None,
    *,
    append: list[dict[str, Any]],
    remove_pod_ids: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """
    Load existing state (v1 or v2), drop entries whose pod_id is in remove_pod_ids,
    append new entries (skip duplicate pod_id), return v2 dict.
    Entries in append whose pod_id was superseded (listed in remove_pod_ids) are skipped.
    """
    remove = frozenset(remove_pod_ids or ())
    base = parse_runpod_pods_state(existing)
    kept = [e for e in base if e["pod_id"] not in remove]
    seen_ids = {e["pod_id"] for e in kept}
    for entry in append:
        role = str(entry.get("role") or "").strip()
        pod_id = str(entry.get("pod_id") or "").strip()
        if role not in RUNPOD_ROLES or not pod_id:
            continue
        if pod_id in remove:
            continue
        if pod_id in seen_ids:
            continue
        seen_ids.add(pod_id)
        kept.append(
            {
                "role": role,
                "pod_id": pod_id,
                "created_at": str(entry.get("created_at") or "").strip() or iso_now_utc(),
                "source": str(entry.get("source") or "").strip() or "unknown",
            }
        )
    return serialize_runpod_pods_state_v2(kept)


def json_dumps_state(state: dict[str, Any]) -> bytes:
    return json.dumps(state, indent=2, sort_keys=False).encode("utf-8")


def json_loads_state(raw: bytes | str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}
