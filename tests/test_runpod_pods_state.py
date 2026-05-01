"""Unit tests for RunPod S3 bookkeeping (v1 legacy + v2 pods array)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from shared.runpod_pods_state import (
    latest_pod_id_per_role,
    merge_append_and_remove,
    parse_runpod_pods_state,
    unique_pod_ids_for_termination,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_parse_v1_legacy_three_roles() -> None:
    raw = {"iep0": "a", "iep1a": "b", "iep1b": "c"}
    pods = parse_runpod_pods_state(raw)
    assert len(pods) == 3
    roles = {p["role"]: p["pod_id"] for p in pods}
    assert roles == {"iep0": "a", "iep1a": "b", "iep1b": "c"}


def test_unique_ids_multiple_same_role_v2() -> None:
    """Scale-down must terminate every historical pod id, not only one per role."""
    raw = {
        "version": 2,
        "pods": [
            {
                "role": "iep0",
                "pod_id": "old_iep0",
                "created_at": "2026-01-01T00:00:00Z",
                "source": "run1",
            },
            {
                "role": "iep0",
                "pod_id": "new_iep0",
                "created_at": "2026-02-01T00:00:00Z",
                "source": "run2",
            },
            {
                "role": "iep1a",
                "pod_id": "only_1a",
                "created_at": "2026-01-15T00:00:00Z",
                "source": "run1",
            },
        ],
    }
    ids = unique_pod_ids_for_termination(raw)
    assert ids == ["old_iep0", "new_iep0", "only_1a"]
    latest = latest_pod_id_per_role(raw)
    assert latest["iep0"] == "new_iep0"
    assert latest["iep1a"] == "only_1a"
    assert "iep1b" not in latest


def test_merge_removes_replaced_and_skips_append_if_removed() -> None:
    existing = {
        "version": 2,
        "pods": [
            {"role": "iep0", "pod_id": "x", "created_at": "2026-01-01T00:00:00Z", "source": "a"},
        ],
    }
    append = [
        {
            "role": "iep0",
            "pod_id": "y",
            "created_at": "2026-02-01T00:00:00Z",
            "source": "replace",
        },
    ]
    merged = merge_append_and_remove(existing, append=append, remove_pod_ids=frozenset({"x"}))
    assert merged["version"] == 2
    ids = [p["pod_id"] for p in merged["pods"]]
    assert ids == ["y"]

    # Same merge call must not re-append an id listed in remove (even if it was removed from state).
    zombie = {
        "role": "iep0",
        "pod_id": "x",
        "created_at": "2026-03-01T00:00:00Z",
        "source": "bad",
    }
    merged_z = merge_append_and_remove(merged, append=[zombie], remove_pod_ids=frozenset({"x"}))
    assert [p["pod_id"] for p in merged_z["pods"]] == ["y"]

    # Appending the same removed id again should not resurrect it.
    merged2 = merge_append_and_remove(merged, append=[append[0]], remove_pod_ids=frozenset())
    assert [p["pod_id"] for p in merged2["pods"]] == ["y"]


def test_cli_list_ids_and_dry_run_multiline() -> None:
    """CLI proves multiple pods per role appear as separate termination lines."""
    script = REPO_ROOT / "scripts" / "runpod_s3_state.py"
    payload = json.dumps(
        {
            "version": 2,
            "pods": [
                {"role": "iep0", "pod_id": "p1", "created_at": "2026-01-01T00:00:00Z", "source": "s"},
                {"role": "iep0", "pod_id": "p2", "created_at": "2026-02-01T00:00:00Z", "source": "s"},
            ],
        }
    )
    r = subprocess.run(
        [sys.executable, str(script), "list-ids"],
        input=payload,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=True,
    )
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert lines == ["p1", "p2"]

    r2 = subprocess.run(
        [sys.executable, str(script), "dry-run"],
        input=payload,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=True,
    )
    out = r2.stdout
    assert "parsed_entries=2" in out
    assert "unique_pod_ids=2" in out
    assert "unique_ids_for_termination: p1,p2" in out


def test_cli_merge_stdin_env(tmp_path: Path) -> None:
    existing = json.dumps({"iep0": "legacy_only", "iep1a": "", "iep1b": ""})
    env = os.environ.copy()
    env["APPEND_JSON"] = json.dumps(
        [
            {
                "role": "iep0",
                "pod_id": "extra",
                "created_at": "2026-03-01T00:00:00Z",
                "source": "test",
            }
        ]
    )
    env["REMOVE_IDS"] = ""
    script = REPO_ROOT / "scripts" / "runpod_s3_state.py"
    r = subprocess.run(
        [sys.executable, str(script), "merge"],
        input=existing,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
    )
    data = json.loads(r.stdout)
    assert data["version"] == 2
    pids = {p["pod_id"] for p in data["pods"]}
    assert pids == {"legacy_only", "extra"}
