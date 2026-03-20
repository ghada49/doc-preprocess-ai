"""
services/eep/app/db/models.py
------------------------------
SQLAlchemy 2.0 ORM models for the EEP service.

Six core tables (spec Section 13):
  User, Job, JobPage, PageLineage, ServiceInvocation, QualityGateLog

All column names, types, nullability, and defaults mirror the migration in
services/eep/migrations/versions/0001_p1_core_tables.py exactly.
MLOps models are intentionally absent — they belong to Phase 8 (Packet 8.1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    """Shared declarative base for all EEP ORM models."""


# ── users ──────────────────────────────────────────────────────────────────────


class User(Base):
    """
    Registered system user.

    role CHECK: 'user' | 'admin'
    """

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(Text(), primary_key=True)
    username: Mapped[str] = mapped_column(Text(), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(Text(), nullable=False)
    role: Mapped[str] = mapped_column(Text(), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ── jobs ───────────────────────────────────────────────────────────────────────


class Job(Base):
    """
    Top-level processing job.

    material_type CHECK: 'book' | 'newspaper' | 'archival_document'
    pipeline_mode CHECK: 'preprocess' | 'layout'
    ptiff_qa_mode CHECK: 'manual' | 'auto_continue'
    status CHECK: 'queued' | 'running' | 'done' | 'failed'
    """

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(Text(), primary_key=True)
    collection_id: Mapped[str] = mapped_column(Text(), nullable=False)
    material_type: Mapped[str] = mapped_column(Text(), nullable=False)
    pipeline_mode: Mapped[str] = mapped_column(Text(), nullable=False, default="layout")
    ptiff_qa_mode: Mapped[str] = mapped_column(Text(), nullable=False, default="manual")
    policy_version: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False, default="queued")
    page_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    review_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    pending_human_correction_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0
    )
    shadow_mode: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    created_by: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ── job_pages ──────────────────────────────────────────────────────────────────


class JobPage(Base):
    """
    Per-page processing record.

    status CHECK: all 10 PageState values including 'ptiff_qa_pending'.
    acceptance_decision CHECK: 'accepted' | 'review' | 'failed' | NULL.
    """

    __tablename__ = "job_pages"

    page_id: Mapped[str] = mapped_column(Text(), primary_key=True)
    job_id: Mapped[str] = mapped_column(Text(), ForeignKey("jobs.job_id"), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    sub_page_index: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    status: Mapped[str] = mapped_column(Text(), nullable=False, default="queued")
    routing_path: Mapped[str | None] = mapped_column(Text(), nullable=True)
    escalated_to_gpu: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    input_image_uri: Mapped[str] = mapped_column(Text(), nullable=False)
    output_image_uri: Mapped[str | None] = mapped_column(Text(), nullable=True)
    quality_summary: Mapped[Any] = mapped_column(JSONB, nullable=True)
    layout_consensus_result: Mapped[Any] = mapped_column(JSONB, nullable=True)
    acceptance_decision: Mapped[str | None] = mapped_column(Text(), nullable=True)
    review_reasons: Mapped[Any] = mapped_column(JSONB, nullable=True)
    processing_time_ms: Mapped[float | None] = mapped_column(Float(), nullable=True)
    status_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    output_layout_uri: Mapped[str | None] = mapped_column(Text(), nullable=True)

    __table_args__ = (UniqueConstraint("job_id", "page_number", "sub_page_index"),)


# ── page_lineage ───────────────────────────────────────────────────────────────


class PageLineage(Base):
    """
    Complete audit trail for every page.

    preprocessed_artifact_state / layout_artifact_state CHECK:
        'pending' | 'confirmed' | 'recovery_failed'  (default 'pending').
    ptiff_ssim: offline-only; MUST NOT influence routing decisions.
    """

    __tablename__ = "page_lineage"

    lineage_id: Mapped[str] = mapped_column(Text(), primary_key=True)
    job_id: Mapped[str] = mapped_column(Text(), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    sub_page_index: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    correlation_id: Mapped[str] = mapped_column(Text(), nullable=False)
    input_image_uri: Mapped[str] = mapped_column(Text(), nullable=False)
    input_image_hash: Mapped[str | None] = mapped_column(Text(), nullable=True)
    otiff_uri: Mapped[str] = mapped_column(Text(), nullable=False)
    reference_ptiff_uri: Mapped[str | None] = mapped_column(Text(), nullable=True)
    ptiff_ssim: Mapped[float | None] = mapped_column(Float(), nullable=True)
    iep1a_used: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    iep1b_used: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    selected_geometry_model: Mapped[str | None] = mapped_column(Text(), nullable=True)
    structural_agreement: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    iep1d_used: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    material_type: Mapped[str] = mapped_column(Text(), nullable=False)
    routing_path: Mapped[str | None] = mapped_column(Text(), nullable=True)
    policy_version: Mapped[str] = mapped_column(Text(), nullable=False)
    acceptance_decision: Mapped[str | None] = mapped_column(Text(), nullable=True)
    acceptance_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    gate_results: Mapped[Any] = mapped_column(JSONB, nullable=True)
    total_processing_ms: Mapped[float | None] = mapped_column(Float(), nullable=True)
    shadow_eval_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    cleanup_retry_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    preprocessed_artifact_state: Mapped[str] = mapped_column(
        Text(), nullable=False, default="pending"
    )
    layout_artifact_state: Mapped[str] = mapped_column(Text(), nullable=False, default="pending")
    output_image_uri: Mapped[str | None] = mapped_column(Text(), nullable=True)
    parent_page_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    split_source: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    human_corrected: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    human_correction_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    human_correction_fields: Mapped[Any] = mapped_column(JSONB, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(Text(), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("job_id", "page_number", "sub_page_index"),)


# ── service_invocations ────────────────────────────────────────────────────────


class ServiceInvocation(Base):
    """
    Per-invocation record for every IEP call made during processing.

    status CHECK: 'success' | 'error' | 'timeout' | 'skipped'
    id: SERIAL (auto-incrementing integer PK)
    """

    __tablename__ = "service_invocations"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    lineage_id: Mapped[str] = mapped_column(
        Text(), ForeignKey("page_lineage.lineage_id"), nullable=False
    )
    service_name: Mapped[str] = mapped_column(Text(), nullable=False)
    service_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    model_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    model_source: Mapped[str | None] = mapped_column(Text(), nullable=True)
    invoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_time_ms: Mapped[float | None] = mapped_column(Float(), nullable=True)
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    metrics: Mapped[Any] = mapped_column(JSONB, nullable=True)
    config_snapshot: Mapped[Any] = mapped_column(JSONB, nullable=True)


# ── quality_gate_log ───────────────────────────────────────────────────────────


class QualityGateLog(Base):
    """
    Immutable record of every quality gate decision.

    gate_type CHECK:
        'geometry_selection' | 'geometry_selection_post_rectification' |
        'artifact_validation' | 'artifact_validation_final' | 'layout'
    route_decision CHECK:
        'accepted' | 'rectification' | 'pending_human_correction' | 'review'
    """

    __tablename__ = "quality_gate_log"

    gate_id: Mapped[str] = mapped_column(Text(), primary_key=True)
    job_id: Mapped[str] = mapped_column(Text(), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    gate_type: Mapped[str] = mapped_column(Text(), nullable=False)
    iep1a_geometry: Mapped[Any] = mapped_column(JSONB, nullable=True)
    iep1b_geometry: Mapped[Any] = mapped_column(JSONB, nullable=True)
    structural_agreement: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    selected_model: Mapped[str | None] = mapped_column(Text(), nullable=True)
    selection_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    sanity_check_results: Mapped[Any] = mapped_column(JSONB, nullable=True)
    split_confidence: Mapped[Any] = mapped_column(JSONB, nullable=True)
    tta_variance: Mapped[Any] = mapped_column(JSONB, nullable=True)
    artifact_validation_score: Mapped[float | None] = mapped_column(Float(), nullable=True)
    route_decision: Mapped[str] = mapped_column(Text(), nullable=False)
    review_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    processing_time_ms: Mapped[float | None] = mapped_column(Float(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
