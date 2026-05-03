"""
Unit tests for trailing JobPage delivery vs active processing rates.

``compute_trailing_page_rates_from_aggregates`` encodes the same formulas as
``trailing_terminal_page_rate_metrics`` after SQL aggregates are fetched.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from services.eep.app.admin.dashboard import (
    _terminal_trailing_window_base_filters,
    compute_trailing_page_rates_from_aggregates,
    trailing_terminal_page_rate_metrics,
)
from services.eep.app.db.models import Job, JobPage
from services.eep.app.db.session import SessionLocal


def test_wall_clock_delivery_rate_120_over_24h() -> None:
    wall, active = compute_trailing_page_rates_from_aggregates(
        hours=24,
        n_terminal_in_window=120,
        n_terminal_with_processing_ms=0,
        sum_processing_time_ms=0.0,
    )
    assert wall == 5.0
    assert active is None


def test_active_processing_rate_120_over_six_worker_hours() -> None:
    six_hours_ms = 6.0 * 3_600_000.0
    wall, active = compute_trailing_page_rates_from_aggregates(
        hours=24,
        n_terminal_in_window=120,
        n_terminal_with_processing_ms=120,
        sum_processing_time_ms=six_hours_ms,
    )
    assert wall == 5.0
    assert active == 20.0


def test_active_rate_null_when_processing_ms_incomplete() -> None:
    wall, active = compute_trailing_page_rates_from_aggregates(
        hours=24,
        n_terminal_in_window=120,
        n_terminal_with_processing_ms=119,
        sum_processing_time_ms=3_600_000.0,
    )
    assert wall == 5.0
    assert active is None


def test_active_rate_null_when_sum_ms_zero() -> None:
    wall, active = compute_trailing_page_rates_from_aggregates(
        hours=24,
        n_terminal_in_window=10,
        n_terminal_with_processing_ms=10,
        sum_processing_time_ms=0.0,
    )
    assert wall == round(10 / 24.0, 4)
    assert active is None


def test_zero_terminal_wall_zero_active_unavailable() -> None:
    wall, active = compute_trailing_page_rates_from_aggregates(
        hours=24,
        n_terminal_in_window=0,
        n_terminal_with_processing_ms=0,
        sum_processing_time_ms=0.0,
    )
    assert wall == 0.0
    assert active is None


def test_hours_non_positive_returns_wall_zero_active_none() -> None:
    wall, active = compute_trailing_page_rates_from_aggregates(
        hours=0,
        n_terminal_in_window=50,
        n_terminal_with_processing_ms=50,
        sum_processing_time_ms=3_600_000.0,
    )
    assert wall == 0.0
    assert active is None


def test_trailing_filter_sql_includes_status_updated_bounds() -> None:
    now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    base = _terminal_trailing_window_base_filters(now=now, hours=24)
    stmt = select(func.count()).select_from(JobPage).where(*base)
    compiled = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    )
    lowered = compiled.lower()
    assert "status_updated_at" in lowered
    assert lowered.count("status_updated_at") >= 2


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL DATABASE_URL required for ORM integration",
)
def test_pg_trailing_metrics_ignore_rows_outside_window() -> None:
    """Rows with ``status_updated_at`` before the window must not affect counts."""
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    window_start = now - timedelta(hours=24)
    job_id = f"test-job-{uuid4().hex[:12]}"
    db: Session = SessionLocal()
    try:
        db.add(
            Job(
                job_id=job_id,
                collection_id="c",
                material_type="book",
                pipeline_mode="layout",
                ptiff_qa_mode="auto_continue",
                policy_version="pv",
                status="done",
                page_count=130,
                accepted_count=3,
                review_count=0,
                failed_count=0,
                pending_human_correction_count=0,
                shadow_mode=False,
            )
        )
        old_ts = window_start - timedelta(hours=3)
        per_ms = (6.0 * 3_600_000.0) / 120.0
        # Outside window — must be ignored
        db.add(
            JobPage(
                page_id=f"{uuid4().hex}",
                job_id=job_id,
                page_number=1,
                sub_page_index=None,
                status="accepted",
                input_image_uri="s3://x/1",
                processing_time_ms=per_ms,
                status_updated_at=old_ts,
            )
        )
        for i in range(120):
            db.add(
                JobPage(
                    page_id=f"{uuid4().hex}",
                    job_id=job_id,
                    page_number=i + 2,
                    sub_page_index=None,
                    status="accepted",
                    input_image_uri="s3://x/1",
                    processing_time_ms=per_ms,
                    status_updated_at=now - timedelta(minutes=i),
                )
            )
        db.commit()

        wall, active = trailing_terminal_page_rate_metrics(db, hours=24, now=now)
        assert wall == 5.0
        assert active == 20.0
    finally:
        db.query(JobPage).filter(JobPage.job_id == job_id).delete(synchronize_session=False)
        db.query(Job).filter(Job.job_id == job_id).delete(synchronize_session=False)
        db.commit()
        db.close()
