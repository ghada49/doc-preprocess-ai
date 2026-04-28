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
            queues = _check_queues(r)
            active_jobs, active_pages = _check_db(conn)
            drained = _is_drained(queues, active_jobs, active_pages)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            status_line = _status_line(queues, active_jobs, active_pages)

            if args.assert_drained:
                if drained:
                    print(f"[{now}] ASSERT-DRAINED: YES — safe to stop. {status_line}", flush=True)
                    return 0  # exit 0 = IS drained, safe to stop
                else:
                    print(f"[{now}] ASSERT-DRAINED: NO — active work remains. {status_line}", flush=True)
                    return 1  # exit 1 = NOT drained, do not stop
            else:  # assert_has_work
                if not drained:
                    print(f"[{now}] ASSERT-HAS-WORK: YES — processable work exists. {status_line}", flush=True)
                    return 0  # exit 0 = HAS work, scale-up warranted
                else:
                    print(f"[{now}] ASSERT-HAS-WORK: NO — already drained. {status_line}", flush=True)
                    return 1  # exit 1 = NO work, do not start
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
