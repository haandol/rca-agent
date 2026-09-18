"""Additive tables leave existing sensor_readings rows and timestamp column unchanged."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from test_service.adapters.secondary.sensor_repository.models import Base


class VitalEventIdentity(Base):
    __tablename__ = "vital_event_identity"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    admission_sequence: Mapped[int] = mapped_column(BigInteger, unique=True)
    sensor_id: Mapped[str] = mapped_column(String(128))
    schema_version: Mapped[int] = mapped_column(Integer)
    reading_id: Mapped[str] = mapped_column(String(36), unique=True)
    measurement_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sensor_readings.id", ondelete="CASCADE"), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_count: Mapped[int] = mapped_column(BigInteger, default=0)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_sqlstates: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint("schema_version IN (1,2)"),
        CheckConstraint("measurement_id IS NULL OR measurement_id = reading_id"),
    )


class VitalInbox(Base):
    __tablename__ = "vital_inbox"
    event_id: Mapped[str] = mapped_column(String(128), ForeignKey("vital_event_identity.event_id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    attempt_count: Mapped[int] = mapped_column(Integer)
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_vital_inbox_due", "next_at", "created_at"), CheckConstraint("attempt_count >= 0"))


class VitalCoordinator(Base):
    __tablename__ = "vital_coordinator"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    epoch: Mapped[str] = mapped_column(String(36))
    last_slot: Mapped[int] = mapped_column(BigInteger)
    pending_count: Mapped[int] = mapped_column(Integer)
    generated_total: Mapped[int] = mapped_column(BigInteger)
    accepted_total: Mapped[int] = mapped_column(BigInteger)
    skipped_capacity_total: Mapped[int] = mapped_column(BigInteger)
    sql_attempts_total: Mapped[int] = mapped_column(BigInteger)
    sql_failures_total: Mapped[int] = mapped_column(BigInteger)
    retries_total: Mapped[int] = mapped_column(BigInteger)
    committed_total: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint("id = 1"), CheckConstraint("pending_count BETWEEN 0 AND 86400"))
