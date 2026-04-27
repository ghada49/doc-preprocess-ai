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
  jobs.status IN ('queued', 'running')
  job_pages.status IN ('queued', 'preprocessing', 'rectification',
                       'layout_detection', 'semantic_norm')

Scale-down is safe when ALL of:
  - pending queue depth == 0
  - processing queue depth == 0
  - shadow pending depth == 0
  - shadow processing depth == 0
  - DB active job count == 0
  - DB active page count == 0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone


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


def _check_db(conn) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
        )
        active_jobs = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM job_pages WHERE status = ANY(%s)",
            (list(PAGE_ACTIVE_STATES),),
        )
        active_pages = cur.fetchone()[0]
    return active_jobs, active_pages


def _is_drained(queues: dict[str, int], active_jobs: int, active_pages: int) -> bool:
    return (
        queues["pending"] == 0
        and queues["processing"] == 0
        and queues["shadow_pending"] == 0
        and queues["shadow_processing"] == 0
        and active_jobs == 0
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
    args = parser.parse_args()

    redis_url = os.environ.get("REDIS_URL", "").strip()
    database_url = os.environ.get("DATABASE_URL", "").strip()

    if not redis_url:
        print("ERROR: REDIS_URL is not set", flush=True)
        return 1
    if not database_url:
        print("ERROR: DATABASE_URL is not set", flush=True)
        return 1

    deadline = time.monotonic() + args.timeout_minutes * 60
    attempt = 0

    print(
        f"Drain monitor started — timeout={args.timeout_minutes}m "
        f"poll={args.poll_interval_seconds}s force={args.force}",
        flush=True,
    )

    r = _redis_client(redis_url)
    conn = _db_engine(database_url)

    try:
        while True:
            attempt += 1
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            queues = _check_queues(r)
            active_jobs, active_pages = _check_db(conn)
            status = _status_line(queues, active_jobs, active_pages)

            if _is_drained(queues, active_jobs, active_pages):
                result = {
                    "status": "drained",
                    "attempt": attempt,
                    "timestamp": now,
                    **queues,
                    "active_jobs_db": active_jobs,
                    "active_pages_db": active_pages,
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
                    "active_jobs_db": active_jobs,
                    "active_pages_db": active_pages,
                }
                print(
                    f"[{now}] TIMEOUT after {args.timeout_minutes}m — "
                    f"queues not empty. {status}",
                    flush=True,
                )
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
