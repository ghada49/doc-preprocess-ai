"""
Drain monitor for LibraryAI batch scale-down.

Polls Redis queue depths and DB active job/page states until all processing
activity is confirmed finished, then exits 0. Exits non-zero if the timeout
is reached and --force is not set.

Queue keys (from shared/schemas/queue.py):
  libraryai:page_tasks                  — pending page tasks
  libraryai:page_tasks:processing       — in-flight page tasks
  libraryai:shadow_tasks                — pending shadow evaluations
  libraryai:shadow_tasks:processing     — in-flight shadow evaluations
  libraryai:page_tasks:dead_letter      — exhausted retries (warning only)

DB checks:
  jobs.status IN ('queued', 'running') with at least one active/processable page
  job_pages.status IN ('queued', 'preprocessing', 'rectification',
                       'layout_detection', 'semantic_norm')

Scale-down is safe when ALL of:
  - pending queue depth == 0
  - processing queue depth == 0
  - shadow pending depth == 0
  - shadow processing depth == 0
  - DB active job count == 0
  - DB active page count == 0

Page state classification (shared/state_machine.py):
  Processable / active — scale-down must wait:
    queued, preprocessing, rectification, layout_detection, semantic_norm

  Human-review waiting — scale-down is ALLOWED (these states are intentionally
  excluded from PAGE_ACTIVE_STATES so infrastructure is not kept running for
  pages waiting for human input):
    ptiff_qa_pending, pending_human_correction

  Terminal — no further processing:
    accepted, review, failed, split

--check-only mode (for scheduled_window workflow):
  Performs a single check (no polling). Exits 0 if processable work exists
  (NOT yet drained), exits 1 if already drained (nothing to do). Useful for
  the scheduled-window.yml workflow to decide whether to trigger scale-up.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any


QUEUE_PAGE_TASKS = "libraryai:page_tasks"
QUEUE_PAGE_TASKS_PROCESSING = "libraryai:page_tasks:processing"
QUEUE_SHADOW_TASKS = "libraryai:shadow_tasks"
QUEUE_SHADOW_TASKS_PROCESSING = "libraryai:shadow_tasks:processing"
QUEUE_DEAD_LETTER = "libraryai:page_tasks:dead_letter"


def _redis_client(redis_url: str):
    import redis
    return redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5)


def _db_engine(database_url: str):
    import psycopg2
    return psycopg2.connect(database_url, connect_timeout=10)


def _check_queues(r) -> dict[str, int]:
    return {
        "pending":           r.llen(QUEUE_PAGE_TASKS),
        "processing":        r.llen(QUEUE_PAGE_TASKS_PROCESSING),
        "shadow_pending":    r.llen(QUEUE_SHADOW_TASKS),
        "shadow_processing": r.llen(QUEUE_SHADOW_TASKS_PROCESSING),
        "dead_letter":       r.llen(QUEUE_DEAD_LETTER),
    }


PAGE_ACTIVE_STATES = (
    "queued",
    "preprocessing",
    "rectification",
    "layout_detection",
    "semantic_norm",
)
PAGE_HUMAN_REVIEW_STATES = ("ptiff_qa_pending", "pending_human_correction")
PAGE_TERMINAL_STATES = ("accepted", "review", "failed", "split")

DEFAULT_SAMPLE_LIMIT = 10
DEFAULT_STALE_SECONDS = 900
DEFAULT_LAYOUT_STALE_SECONDS = 180

NORMAL_PROCESSING_SERVICES = (
    "libraryai-iep1d",
    "libraryai-iep1e",
    "libraryai-iep2a-v2",
    "libraryai-iep2b",
    "libraryai-eep-worker",
    "libraryai-eep-recovery",
    "libraryai-shadow-worker",
)


def _check_db(conn) -> tuple[int, int]:
    with conn.cursor() as cur:
        # active_jobs: jobs still in a non-terminal status that have at least one
        # processable page.  Uses job_id (the actual PK column) not id.
        cur.execute(
            """
            SELECT COUNT(DISTINCT j.job_id)
            FROM jobs j
            JOIN job_pages p ON p.job_id = j.job_id
            WHERE j.status IN ('queued', 'running')
              AND p.status = ANY(%s)
            """,
            (list(PAGE_ACTIVE_STATES),),
        )
        active_jobs = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM job_pages WHERE status = ANY(%s)",
            (list(PAGE_ACTIVE_STATES),),
        )
        active_pages = cur.fetchone()[0]
    return active_jobs, active_pages


def _fetch_all_dicts(cur) -> list[dict[str, Any]]:
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    return value


def _page_age_seconds(row: dict[str, Any]) -> float | None:
    value = row.get("age_seconds")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stale_timeout_for_status(status: str | None) -> int:
    if status == "layout_detection":
        return int(os.getenv("DRAIN_LAYOUT_STALE_SECONDS", str(DEFAULT_LAYOUT_STALE_SECONDS)))
    return int(os.getenv("DRAIN_STALE_SECONDS", str(DEFAULT_STALE_SECONDS)))


def _is_stale_page(row: dict[str, Any]) -> bool:
    age = _page_age_seconds(row)
    if age is None:
        return False
    return age > _stale_timeout_for_status(row.get("status"))


def _review_reason(row: dict[str, Any]) -> Any:
    reasons = row.get("review_reasons")
    if reasons:
        return reasons
    return row.get("latest_review_reason")


def _is_automatable_page_status(status: str | None) -> bool:
    return status in PAGE_ACTIVE_STATES


def _is_human_review_only_status(status: str | None) -> bool:
    return status in PAGE_HUMAN_REVIEW_STATES


def _db_page_sample_query(where_sql: str, *, limit: int) -> str:
    return f"""
        SELECT
          p.job_id,
          p.page_id,
          p.page_number,
          p.sub_page_index,
          p.status,
          p.review_reasons,
          q.latest_review_reason,
          p.status_updated_at,
          p.created_at,
          EXTRACT(EPOCH FROM (now() - COALESCE(p.status_updated_at, p.created_at))) AS age_seconds
        FROM job_pages p
        LEFT JOIN LATERAL (
          SELECT review_reason AS latest_review_reason
          FROM quality_gate_log q
          WHERE q.job_id = p.job_id
            AND q.page_number = p.page_number
          ORDER BY q.created_at DESC
          LIMIT 1
        ) q ON TRUE
        WHERE {where_sql}
        ORDER BY COALESCE(p.status_updated_at, p.created_at) ASC NULLS FIRST
        LIMIT {int(limit)}
    """


def _check_db_details(conn, sample_limit: int = DEFAULT_SAMPLE_LIMIT) -> dict[str, Any]:
    active_jobs, active_pages = _check_db(conn)
    details: dict[str, Any] = {
        "active_jobs_count": int(active_jobs),
        "active_pages_count": int(active_pages),
        "active_page_samples": [],
        "human_review_only_pages_count": 0,
        "human_review_only_page_samples": [],
        "status_counts": {},
        "nonterminal_jobs_count": 0,
        "human_review_only_jobs_count": 0,
        "split_parent_with_children_count": 0,
        "split_parent_without_children_count": 0,
    }
    with conn.cursor() as cur:
        cur.execute(
            _db_page_sample_query("p.status = ANY(%s)", limit=sample_limit),
            (list(PAGE_ACTIVE_STATES),),
        )
        details["active_page_samples"] = _fetch_all_dicts(cur)

        cur.execute(
            "SELECT status, COUNT(*) FROM job_pages GROUP BY status ORDER BY status"
        )
        details["status_counts"] = {status: int(count) for status, count in cur.fetchall()}

        cur.execute(
            "SELECT COUNT(*) FROM job_pages WHERE status = ANY(%s)",
            (list(PAGE_HUMAN_REVIEW_STATES),),
        )
        details["human_review_only_pages_count"] = int(cur.fetchone()[0])

        cur.execute(
            _db_page_sample_query("p.status = ANY(%s)", limit=sample_limit),
            (list(PAGE_HUMAN_REVIEW_STATES),),
        )
        details["human_review_only_page_samples"] = _fetch_all_dicts(cur)

        cur.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')")
        details["nonterminal_jobs_count"] = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT COUNT(DISTINCT j.job_id)
            FROM jobs j
            WHERE j.status IN ('queued', 'running')
              AND EXISTS (
                SELECT 1 FROM job_pages p
                WHERE p.job_id = j.job_id
                  AND p.status = ANY(%s)
              )
              AND NOT EXISTS (
                SELECT 1 FROM job_pages p
                WHERE p.job_id = j.job_id
                  AND p.status = ANY(%s)
              )
            """,
            (list(PAGE_HUMAN_REVIEW_STATES), list(PAGE_ACTIVE_STATES)),
        )
        details["human_review_only_jobs_count"] = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT COUNT(*)
            FROM job_pages p
            WHERE p.status = 'split'
              AND EXISTS (
                SELECT 1
                FROM job_pages c
                WHERE c.job_id = p.job_id
                  AND c.page_number = p.page_number
                  AND c.sub_page_index IS NOT NULL
              )
            """
        )
        details["split_parent_with_children_count"] = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT COUNT(*)
            FROM job_pages p
            WHERE p.status = 'split'
              AND NOT EXISTS (
                SELECT 1
                FROM job_pages c
                WHERE c.job_id = p.job_id
                  AND c.page_number = p.page_number
                  AND c.sub_page_index IS NOT NULL
              )
            """
        )
        details["split_parent_without_children_count"] = int(cur.fetchone()[0])

    return details


def _lookup_page_details(conn, page_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not page_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              p.job_id,
              p.page_id,
              p.page_number,
              p.sub_page_index,
              p.status,
              p.review_reasons,
              q.latest_review_reason,
              p.status_updated_at,
              p.created_at,
              EXTRACT(EPOCH FROM (now() - COALESCE(p.status_updated_at, p.created_at))) AS age_seconds
            FROM job_pages p
            LEFT JOIN LATERAL (
              SELECT review_reason AS latest_review_reason
              FROM quality_gate_log q
              WHERE q.job_id = p.job_id
                AND q.page_number = p.page_number
              ORDER BY q.created_at DESC
              LIMIT 1
            ) q ON TRUE
            WHERE p.page_id = ANY(%s)
            LIMIT {len(page_ids)}
            """,
            (page_ids,),
        )
        return {str(row["page_id"]): row for row in _fetch_all_dicts(cur)}


def _queue_all_items(r, key: str) -> list[str]:
    try:
        return list(r.lrange(key, 0, -1))
    except Exception:
        return []


def _parse_json_item(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _redis_item_summary(
    queue_name: str,
    raw: str,
    page_lookup: dict[str, dict[str, Any]],
    *,
    processing_queue: bool,
    r: Any,
) -> dict[str, Any]:
    parsed = _parse_json_item(raw)
    if parsed is None:
        return {
            "source": "redis",
            "queue": queue_name,
            "reason": "redis_unparseable_item",
            "automatable_work": True,
            "human_review_only_work": False,
            "raw_prefix": raw[:200],
        }

    page_id = parsed.get("page_id")
    page = page_lookup.get(str(page_id)) if page_id is not None else None
    db_status = page.get("status") if page else None
    task_id = parsed.get("task_id")
    live_heartbeat = False
    claim_owner = None
    if processing_queue and task_id:
        try:
            live_heartbeat = bool(r.exists(f"libraryai:page_tasks:heartbeat:{task_id}"))
        except Exception:
            live_heartbeat = False
        try:
            claim_owner = r.hget("libraryai:page_tasks:claims", task_id)
        except Exception:
            claim_owner = None

    automatable = _is_automatable_page_status(db_status)
    human_review_only = _is_human_review_only_status(db_status)
    reason = "redis_item_with_automatable_db_page"
    if page is None:
        reason = "redis_item_without_matching_db_page"
        automatable = True
    elif human_review_only:
        reason = "redis_item_for_human_review_only_page"
    elif db_status in PAGE_TERMINAL_STATES:
        reason = "redis_item_for_terminal_page"
    elif processing_queue and automatable and _is_stale_page(page) and not live_heartbeat:
        reason = (
            "stale_processing_item_no_live_worker_claim"
            if not claim_owner
            else "stale_processing_item_no_live_worker_heartbeat"
        )

    return {
        "source": "redis",
        "queue": queue_name,
        "reason": reason,
        "task_id": task_id,
        "job_id": parsed.get("job_id") or (page or {}).get("job_id"),
        "page_id": page_id,
        "page_number": parsed.get("page_number") or (page or {}).get("page_number"),
        "sub_page_index": parsed.get("sub_page_index") or (page or {}).get("sub_page_index"),
        "retry_count": parsed.get("retry_count"),
        "db_status": db_status,
        "review_reason": _review_reason(page or {}),
        "age_seconds": _page_age_seconds(page or {}),
        "live_heartbeat": live_heartbeat,
        "claim_owner": claim_owner,
        "live_claim": bool(claim_owner),
        "automatable_work": bool(automatable),
        "human_review_only_work": bool(human_review_only),
    }


def _check_redis_details(
    r,
    conn,
    queues: dict[str, int],
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    queue_specs = (
        ("pending", QUEUE_PAGE_TASKS, False),
        ("processing", QUEUE_PAGE_TASKS_PROCESSING, True),
    )
    raw_by_queue: dict[str, list[str]] = {
        name: _queue_all_items(r, key)
        for name, key, _processing_queue in queue_specs
    }

    page_ids: list[str] = []
    for raw_items in raw_by_queue.values():
        for raw in raw_items:
            parsed = _parse_json_item(raw)
            page_id = parsed.get("page_id") if parsed else None
            if page_id:
                page_ids.append(str(page_id))
    page_lookup = _lookup_page_details(conn, sorted(set(page_ids)))

    blockers: list[dict[str, Any]] = []
    nonblocking_samples: list[dict[str, Any]] = []
    for name, key, processing_queue in queue_specs:
        for raw in raw_by_queue[name]:
            item = _redis_item_summary(name, raw, page_lookup, processing_queue=processing_queue, r=r)
            if item["automatable_work"]:
                blockers.append(item)
            else:
                nonblocking_samples.append(item)

    shadow_blockers: list[dict[str, Any]] = []
    if queues["shadow_pending"] > 0:
        shadow_blockers.append(
            {
                "source": "redis",
                "queue": "shadow_pending",
                "reason": "redis_shadow_pending_items",
                "count": queues["shadow_pending"],
                "automatable_work": True,
                "human_review_only_work": False,
            }
        )
    if queues["shadow_processing"] > 0:
        shadow_blockers.append(
            {
                "source": "redis",
                "queue": "shadow_processing",
                "reason": "redis_shadow_processing_items",
                "count": queues["shadow_processing"],
                "automatable_work": True,
                "human_review_only_work": False,
            }
        )

    active_redis_items_count = (
        sum(1 for item in blockers if item.get("queue") in {"pending", "processing"})
        + queues["shadow_pending"]
        + queues["shadow_processing"]
    )
    return {
        "active_redis_items_count": int(active_redis_items_count),
        "redis_blocker_samples": blockers[:sample_limit] + shadow_blockers,
        "redis_nonblocking_samples": nonblocking_samples[:sample_limit],
    }


def _check_ecs_details() -> dict[str, Any]:
    cluster = os.getenv("ECS_CLUSTER", "").strip()
    if not cluster:
        return {
            "available": False,
            "active_ecs_tasks_count": 0,
            "reason": "ECS_CLUSTER_not_set",
            "services": [],
        }
    service_names = [
        item.strip()
        for item in os.getenv("DRAIN_ECS_SERVICES", ",".join(NORMAL_PROCESSING_SERVICES)).split(",")
        if item.strip()
    ]
    try:
        import boto3

        kwargs: dict[str, str] = {}
        region = os.getenv("AWS_REGION", "").strip()
        if region:
            kwargs["region_name"] = region
        ecs = boto3.client("ecs", **kwargs)
        response = ecs.describe_services(cluster=cluster, services=service_names)
        services = []
        active_count = 0
        for service in response.get("services", []):
            running = int(service.get("runningCount", 0) or 0)
            pending = int(service.get("pendingCount", 0) or 0)
            desired = int(service.get("desiredCount", 0) or 0)
            active_count += running + pending
            services.append(
                {
                    "service_name": service.get("serviceName"),
                    "desired_count": desired,
                    "running_count": running,
                    "pending_count": pending,
                    "status": service.get("status"),
                }
            )
        return {
            "available": True,
            "active_ecs_tasks_count": active_count,
            "services": services,
        }
    except Exception as exc:
        return {
            "available": False,
            "active_ecs_tasks_count": 0,
            "reason": f"{exc.__class__.__name__}: {exc}",
            "services": [],
        }


def _collect_snapshot(r, conn) -> dict[str, Any]:
    sample_limit = int(os.getenv("DRAIN_SAMPLE_LIMIT", str(DEFAULT_SAMPLE_LIMIT)))
    queues = _check_queues(r)
    db = _check_db_details(conn, sample_limit=sample_limit)
    redis_details = _check_redis_details(r, conn, queues, sample_limit=sample_limit)
    ecs = _check_ecs_details()
    blockers: list[dict[str, Any]] = []
    for row in db["active_page_samples"]:
        blockers.append(
            {
                "source": "postgres",
                "reason": (
                    "stale_db_automatable_page_no_sampled_worker_claim"
                    if _is_stale_page(row)
                    else "db_automatable_page"
                ),
                "job_id": row.get("job_id"),
                "page_id": row.get("page_id"),
                "page_number": row.get("page_number"),
                "sub_page_index": row.get("sub_page_index"),
                "status": row.get("status"),
                "review_reason": _review_reason(row),
                "age_seconds": _page_age_seconds(row),
                "automatable_work": True,
                "human_review_only_work": False,
            }
        )
    blockers.extend(redis_details["redis_blocker_samples"])

    return {
        "queues": queues,
        "db": db,
        "redis": redis_details,
        "ecs": ecs,
        "active_jobs_count": db["active_jobs_count"],
        "active_pages_count": db["active_pages_count"],
        "active_redis_items_count": redis_details["active_redis_items_count"],
        "active_ecs_tasks_count": ecs["active_ecs_tasks_count"],
        "blocking_reasons": blockers,
    }


def _is_snapshot_drained(snapshot: dict[str, Any]) -> bool:
    return (
        snapshot["active_pages_count"] == 0
        and snapshot["active_redis_items_count"] == 0
    )


def _is_drained(queues: dict[str, int], active_jobs: int, active_pages: int) -> bool:
    # active_pages is the authoritative signal: if no pages are in a processable
    # state and queues are empty, the drain is complete.  active_jobs is reported
    # for visibility but NOT required to be zero — job status rows lag slightly
    # behind page state transitions and checking them independently causes
    # spurious "not drained" results (active_jobs>0 while active_pages=0).
    return (
        queues["pending"] == 0
        and queues["processing"] == 0
        and queues["shadow_pending"] == 0
        and queues["shadow_processing"] == 0
        and active_pages == 0
    )


def _status_line(queues: dict[str, int], active_jobs: int, active_pages: int) -> str:
    parts = [
        f"pending={queues['pending']}",
        f"processing={queues['processing']}",
        f"shadow_pending={queues['shadow_pending']}",
        f"shadow_processing={queues['shadow_processing']}",
        f"dead_letter={queues['dead_letter']}",
        f"active_jobs_db={active_jobs}",
        f"active_pages_db={active_pages}",
    ]
    if queues["dead_letter"] > 0:
        parts.append(f"WARNING:dead_letter={queues['dead_letter']}_pages_exhausted_retries")
    return " ".join(parts)


def _snapshot_status_line(snapshot: dict[str, Any]) -> str:
    queues = snapshot["queues"]
    parts = [
        f"pending={queues['pending']}",
        f"processing={queues['processing']}",
        f"shadow_pending={queues['shadow_pending']}",
        f"shadow_processing={queues['shadow_processing']}",
        f"dead_letter={queues['dead_letter']}",
        f"active_jobs_count={snapshot['active_jobs_count']}",
        f"active_pages_count={snapshot['active_pages_count']}",
        f"active_redis_items_count={snapshot['active_redis_items_count']}",
        f"active_ecs_tasks_count={snapshot['active_ecs_tasks_count']}",
        f"active_jobs_db={snapshot['active_jobs_count']}",
        f"active_pages_db={snapshot['active_pages_count']}",
    ]
    if queues["dead_letter"] > 0:
        parts.append(f"WARNING:dead_letter={queues['dead_letter']}_pages_exhausted_retries")
    return " ".join(parts)


def _diagnostic_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    db = snapshot["db"]
    return {
        "active_jobs_count": snapshot["active_jobs_count"],
        "active_pages_count": snapshot["active_pages_count"],
        "active_redis_items_count": snapshot["active_redis_items_count"],
        "active_ecs_tasks_count": snapshot["active_ecs_tasks_count"],
        "blocking_reasons": snapshot["blocking_reasons"],
        "status_counts": db["status_counts"],
        "human_review_only_pages_count": db["human_review_only_pages_count"],
        "human_review_only_page_samples": db["human_review_only_page_samples"],
        "nonterminal_jobs_count": db["nonterminal_jobs_count"],
        "human_review_only_jobs_count": db["human_review_only_jobs_count"],
        "split_parent_with_children_count": db["split_parent_with_children_count"],
        "split_parent_without_children_count": db["split_parent_without_children_count"],
        "redis_nonblocking_samples": snapshot["redis"]["redis_nonblocking_samples"],
        "ecs": snapshot["ecs"],
    }


def _print_diagnostics(snapshot: dict[str, Any]) -> None:
    payload = _json_safe(_diagnostic_payload(snapshot))
    print("DRAIN-DIAGNOSTICS " + json.dumps(payload, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout-minutes", type=int,
        default=int(os.getenv("DRAIN_TIMEOUT_MINUTES", "120")),
        help="Maximum minutes to wait for drain before giving up",
    )
    parser.add_argument(
        "--poll-interval-seconds", type=int,
        default=int(os.getenv("DRAIN_POLL_INTERVAL_SECONDS", "30")),
        help="Seconds between each drain check",
    )
    parser.add_argument(
        "--force", type=lambda v: v.lower() == "true",
        default=os.getenv("DRAIN_FORCE", "false").lower() == "true",
        help="If true, exit 0 even if drain timed out (unsafe — jobs may be stuck)",
    )
    # Single-shot modes (no polling):
    #
    # --assert-drained
    #   Exit 0  → IS drained (queues empty, no processable DB work).
    #             Safe to stop processing infrastructure.
    #   Exit 1  → NOT drained (active work remains, do NOT stop).
    #   Used by: scale-down-auto.yml to decide whether to trigger scale-down.
    #
    # --assert-has-work
    #   Exit 0  → HAS processable work (queues or DB active pages non-zero).
    #             Scale-up is warranted.
    #   Exit 1  → NO work (already drained, do not start infrastructure).
    #   Used by: scheduled-window.yml to decide whether to trigger scale-up.
    #
    # Exactly one of these flags may be provided. Both are mutually exclusive
    # with each other and with --force/--timeout-minutes/--poll-interval-seconds.
    single_shot_group = parser.add_mutually_exclusive_group()
    single_shot_group.add_argument(
        "--assert-drained", action="store_true", default=False,
        help=(
            "Single-shot: exit 0 if drained (safe to stop), "
            "exit 1 if active work remains. "
            "Used by scale-down-auto.yml."
        ),
    )
    single_shot_group.add_argument(
        "--assert-has-work", action="store_true", default=False,
        help=(
            "Single-shot: exit 0 if processable work exists (scale-up warranted), "
            "exit 1 if already drained (nothing to start). "
            "Used by scheduled-window.yml."
        ),
    )
    args = parser.parse_args()

    redis_url = os.environ.get("REDIS_URL", "").strip()
    database_url = os.environ.get("DATABASE_URL", "").strip()

    if not redis_url:
        print("ERROR: REDIS_URL is not set", flush=True)
        return 1
    if not database_url:
        print("ERROR: DATABASE_URL is not set", flush=True)
        return 1

    r = _redis_client(redis_url)
    conn = _db_engine(database_url)

    # ── single-shot modes (no polling) ────────────────────────────────────────
    if args.assert_drained or args.assert_has_work:
        try:
            snapshot = _collect_snapshot(r, conn)
            drained = _is_snapshot_drained(snapshot)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            status_line = _snapshot_status_line(snapshot)

            if args.assert_drained:
                if drained:
                    print(f"[{now}] ASSERT-DRAINED: YES — safe to stop. {status_line}", flush=True)
                    return 0  # exit 0 = IS drained, safe to stop
                else:
                    print(f"[{now}] ASSERT-DRAINED: NO — active work remains. {status_line}", flush=True)
                    _print_diagnostics(snapshot)
                    return 1  # exit 1 = NOT drained, do not stop
            else:  # assert_has_work
                if not drained:
                    print(f"[{now}] ASSERT-HAS-WORK: YES — processable work exists. {status_line}", flush=True)
                    _print_diagnostics(snapshot)
                    return 0  # exit 0 = HAS work, scale-up warranted
                else:
                    print(f"[{now}] ASSERT-HAS-WORK: NO — already drained. {status_line}", flush=True)
                    return 1  # exit 1 = NO work, do not start
        except Exception as exc:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(
                f"[{now}] ERROR: drain monitor check failed before a drain decision: "
                f"{exc.__class__.__name__}: {exc}",
                flush=True,
            )
            return 2
        finally:
            try:
                conn.close()
            except Exception:
                pass

    deadline = time.monotonic() + args.timeout_minutes * 60
    attempt = 0

    print(
        f"Drain monitor started — timeout={args.timeout_minutes}m "
        f"poll={args.poll_interval_seconds}s force={args.force}",
        flush=True,
    )

    try:
        while True:
            attempt += 1
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                snapshot = _collect_snapshot(r, conn)
            except Exception as exc:
                print(
                    f"[{now}] ERROR: drain monitor check failed before a drain decision: "
                    f"{exc.__class__.__name__}: {exc}",
                    flush=True,
                )
                return 2
            queues = snapshot["queues"]
            status = _snapshot_status_line(snapshot)

            if _is_snapshot_drained(snapshot):
                result = {
                    "status": "drained",
                    "attempt": attempt,
                    "timestamp": now,
                    **queues,
                    "active_jobs_db": snapshot["active_jobs_count"],
                    "active_pages_db": snapshot["active_pages_count"],
                    "active_redis_items_count": snapshot["active_redis_items_count"],
                    "active_ecs_tasks_count": snapshot["active_ecs_tasks_count"],
                }
                print(f"[{now}] DRAINED: {status}", flush=True)
                print(json.dumps(result), flush=True)
                return 0

            remaining = deadline - time.monotonic()
            print(
                f"[{now}] attempt={attempt} remaining={remaining:.0f}s {status}",
                flush=True,
            )

            if remaining <= 0:
                result = {
                    "status": "timeout",
                    "attempt": attempt,
                    "timestamp": now,
                    **queues,
                    "active_jobs_db": snapshot["active_jobs_count"],
                    "active_pages_db": snapshot["active_pages_count"],
                    "active_redis_items_count": snapshot["active_redis_items_count"],
                    "active_ecs_tasks_count": snapshot["active_ecs_tasks_count"],
                }
                print(
                    f"[{now}] TIMEOUT after {args.timeout_minutes}m — "
                    f"queues not empty. {status}",
                    flush=True,
                )
                _print_diagnostics(snapshot)
                print(json.dumps(result), flush=True)
                if args.force:
                    print(
                        "WARNING: --force=true — proceeding with scale-down "
                        "despite non-empty queues. Some jobs may be left in processing state.",
                        flush=True,
                    )
                    return 0
                return 1

            sleep_secs = min(args.poll_interval_seconds, remaining)
            time.sleep(sleep_secs)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
