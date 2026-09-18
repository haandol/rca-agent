"""One database owns generation slots, durable admission, and atomic measurement completion."""

from __future__ import annotations

import logging
import random
import re
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from test_service.adapters.secondary.sensor_repository.models import Base, SensorReadingRow
from test_service.adapters.secondary.vital_repository.models import VitalCoordinator, VitalEventIdentity, VitalInbox
from test_service.config.settings import VITAL_WORKER_SLOTS
from test_service.ports.dto.sensor import SensorReadingEntity
from test_service.ports.dto.vital import (
    Admission,
    AttemptResult,
    EventConflictError,
    InboxFullError,
    VitalEvent,
    normalize_vital_event,
)
from test_service.ports.interfaces.vital_repository import VitalRepositoryPort
from test_service.revision.write import write_statement, write_values
from test_service.services.db_observability import operation_context
from test_service.services.input_contract import observe_vital_input
from test_service.services.sensor import SensorService
from test_service.services.write_diagnostics import driver_diagnostics, log_write_error, write_contract

logger = logging.getLogger(__name__)
CAPACITY = 86_400
WORKER_SLOTS = VITAL_WORKER_SLOTS
COORDINATOR = VitalCoordinator.__table__
INBOX = VitalInbox.__table__
IDENTITY = VitalEventIdentity.__table__


def retry_delay(attempt: int, *, rng=random) -> float:
    """Equal jitter keeps retries positive and bounded without an exponent that grows with outage duration."""
    ceiling = min(60.0, 2.0 ** min(max(attempt - 1, 0), 6))
    return ceiling / 2 + rng.uniform(0, ceiling / 2)


async def initialize_vital_schema(engine: AsyncEngine) -> None:
    """Serialize additive bootstrap; never recreate old tables, modify columns, or reset populated counters."""
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(current_schema()), 918001)"))
        await conn.run_sync(Base.metadata.create_all)
        if (await conn.execute(select(COORDINATOR.c.id).where(COORDINATOR.c.id == 1))).scalar_one_or_none() is None:
            if (await conn.execute(select(func.count()).select_from(INBOX))).scalar_one() or (
                await conn.execute(select(func.count()).select_from(IDENTITY))
            ).scalar_one():
                raise RuntimeError("coordinator missing for existing event data")
            values = {column.name: 0 for column in COORDINATOR.columns}
            values.update(id=1, last_slot=-1, epoch=str(uuid.uuid4()))
            await conn.execute(insert(COORDINATOR).values(**values))


class PostgreSQLVitalRepository(VitalRepositoryPort):
    def __init__(self, engine: AsyncEngine, metrics=None):
        """Share the application's owned pool; never hold a connection while sleeping for backoff."""
        self.engine = engine
        self.metrics = metrics
        if metrics is not None and not getattr(engine.sync_engine, "_vital_starts_registered", False):

            def measurement_started(conn, cursor, statement, parameters, context, executemany):
                """Observe only the fixed measurement INSERT boundary, never its SQL parameters."""
                if statement.lstrip().upper().startswith("INSERT INTO SENSOR_READINGS "):
                    metrics.record_measurement_sql_started()

            sqlalchemy_event.listen(engine.sync_engine, "before_cursor_execute", measurement_started)
            engine.sync_engine._vital_starts_registered = True

    async def _coordinator(self, conn):
        """Serialize only short admission/counter updates; measurement SQL never holds this row lock."""
        return (await conn.execute(select(COORDINATOR).where(COORDINATOR.c.id == 1).with_for_update())).mappings().one()

    async def _admit_locked(self, conn, event: VitalEvent, coordinator, now, *, generated=False):
        """Compare existing immutable identity before capacity; duplicate reads do not lock worker-owned rows."""
        existing = (
            (await conn.execute(select(IDENTITY).where(IDENTITY.c.event_id == event.event_id))).mappings().one_or_none()
        )
        if existing is not None:
            if existing["payload_sha256"] != event.digest:
                raise EventConflictError("event ID has different content")
            if existing["measurement_id"] is None:
                pending_payload = (
                    await conn.execute(select(INBOX.c.payload).where(INBOX.c.event_id == event.event_id))
                ).scalar_one_or_none()
                if pending_payload is None or VitalEvent.from_payload(pending_payload).digest != event.digest:
                    raise RuntimeError("existing pending input integrity is unavailable")
            return Admission("COMPLETED" if existing["measurement_id"] else "PENDING", True, existing["measurement_id"])
        if coordinator["pending_count"] >= CAPACITY:
            raise InboxFullError("durable inbox is full; event not accepted")
        reading_id = str(uuid.uuid4())
        await conn.execute(
            insert(IDENTITY).values(
                event_id=event.event_id,
                payload_sha256=event.digest,
                admission_sequence=coordinator["accepted_total"] + 1,
                sensor_id=event.sensor_id,
                schema_version=event.schema_version,
                reading_id=reading_id,
                measurement_id=None,
                created_at=now,
                completed_at=None,
            )
        )
        await conn.execute(
            insert(INBOX).values(
                event_id=event.event_id, payload=event.payload(), attempt_count=0, next_at=now, created_at=now
            )
        )
        await conn.execute(
            update(COORDINATOR)
            .where(COORDINATOR.c.id == 1)
            .values(
                pending_count=COORDINATOR.c.pending_count + 1,
                accepted_total=COORDINATOR.c.accepted_total + 1,
                generated_total=COORDINATOR.c.generated_total + int(generated),
            )
        )
        return Admission("PENDING")

    async def admit(self, event: VitalEvent) -> Admission:
        """Return acceptance only after the durable identity and inbox transaction commits."""
        async with self.engine.begin() as conn:
            coordinator = await self._coordinator(conn)
            now = (await conn.execute(select(func.clock_timestamp()))).scalar_one()
            result = await self._admit_locked(conn, event, coordinator, now)
        return result

    async def generate_once(self) -> Admission:
        """One winning transaction per current database second; no old slot is replayed after downtime."""
        async with self.engine.begin() as conn:
            coordinator = await self._coordinator(conn)
            now = (await conn.execute(select(func.clock_timestamp()))).scalar_one()
            slot = int(now.timestamp())
            if slot <= coordinator["last_slot"]:
                return Admission("NO_SLOT")
            await conn.execute(update(COORDINATOR).where(COORDINATOR.c.id == 1).values(last_slot=slot))
            if coordinator["pending_count"] >= CAPACITY:
                await conn.execute(
                    update(COORDINATOR)
                    .where(COORDINATOR.c.id == 1)
                    .values(skipped_capacity_total=COORDINATOR.c.skipped_capacity_total + 1)
                )
                return Admission("CAPACITY_PAUSED")
            # Explicit synthetic subject linkage; sensor identity never becomes a patient identifier.
            value = random.uniform(105, 125) if random.random() < 0.08 else random.uniform(65, 95)
            event = normalize_vital_event(
                {
                    "event_id": str(uuid.uuid4()),
                    "sensor_id": "demo-heart-rate-sensor-001",
                    "patient_id": "P-001",
                    "schema_version": 2,
                    "reading_type": "heart_rate",
                    "value": round(value, 1),
                    "unit": "bpm",
                    "sampled_at": now.isoformat(),
                }
            )
            result = await self._admit_locked(conn, event, coordinator, now, generated=True)
        return result

    async def process_one(self, slot: int) -> AttemptResult:
        """Lock inbox before SAVEPOINT; either success/delete or failure/next_at commits before unlock."""
        if not 0 <= slot < WORKER_SLOTS:
            raise ValueError("invalid global worker slot")
        started = False
        acknowledged = False
        result = AttemptResult("NO_WORK")
        failure = None
        with operation_context("ingest"):
            try:
                async with self.engine.begin() as conn:
                    await conn.execute(text("SET LOCAL statement_timeout = '10000'"))
                    await conn.execute(text("SET LOCAL lock_timeout = '3000'"))
                    acquired = (
                        await conn.execute(
                            text("SELECT pg_try_advisory_xact_lock(hashtext(current_schema()), :slot)"),
                            {"slot": 918100 + slot},
                        )
                    ).scalar_one()
                    if not acquired:
                        return AttemptResult("BUSY")
                    pending = (
                        (
                            await conn.execute(
                                select(INBOX)
                                .where(INBOX.c.next_at <= func.clock_timestamp())
                                .order_by(INBOX.c.next_at, INBOX.c.created_at)
                                .limit(1)
                                .with_for_update(skip_locked=True)
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if pending is None:
                        return result
                    event = VitalEvent.from_payload(pending["payload"])
                    identity = (
                        (
                            await conn.execute(
                                select(IDENTITY).where(IDENTITY.c.event_id == event.event_id).with_for_update()
                            )
                        )
                        .mappings()
                        .one()
                    )
                    if identity["payload_sha256"] != event.digest or identity["measurement_id"] is not None:
                        raise RuntimeError("inbox identity integrity mismatch")
                    attempt = pending["attempt_count"] + 1
                    reading = SensorReadingEntity(
                        identity["reading_id"],
                        event.patient_id,
                        event.reading_type,
                        event.value,
                        event.unit,
                        event.measured_at,
                        is_abnormal=SensorService._is_abnormal(event.reading_type, event.value),
                    )
                    if self.metrics:
                        self.metrics.record_ingest_started(1)
                    started = True
                    observe_vital_input(event)
                    try:
                        async with conn.begin_nested():
                            await conn.execute(write_statement(), [write_values(reading)])
                    except DBAPIError as exc:
                        failure = exc
                        delay = retry_delay(attempt)
                        now = (await conn.execute(select(func.clock_timestamp()))).scalar_one()
                        states = dict(identity["failure_sqlstates"])
                        code = driver_diagnostics(exc)["sqlstate"]
                        if isinstance(code, str) and re.fullmatch(r"[0-9A-Z]{5}", code):
                            previous = states.get(code, {})
                            states[code] = {"count": previous.get("count", 0) + 1, "last_at": now.isoformat()}
                        await conn.execute(
                            update(IDENTITY)
                            .where(IDENTITY.c.event_id == event.event_id)
                            .values(
                                failure_count=identity["failure_count"] + 1,
                                last_failure_at=now,
                                failure_sqlstates=states,
                            )
                        )
                        await conn.execute(
                            update(INBOX)
                            .where(INBOX.c.event_id == event.event_id)
                            .values(attempt_count=attempt, next_at=now + timedelta(seconds=delay))
                        )
                        await conn.execute(
                            update(COORDINATOR)
                            .where(COORDINATOR.c.id == 1)
                            .values(
                                sql_attempts_total=COORDINATOR.c.sql_attempts_total + 1,
                                sql_failures_total=COORDINATOR.c.sql_failures_total + 1,
                                retries_total=COORDINATOR.c.retries_total + int(attempt > 1),
                            )
                        )
                        result = AttemptResult("RETRY", attempt > 1, delay, identity["reading_id"])
                    else:
                        now = (await conn.execute(select(func.clock_timestamp()))).scalar_one()
                        await conn.execute(
                            update(IDENTITY)
                            .where(IDENTITY.c.event_id == event.event_id)
                            .values(measurement_id=identity["reading_id"], completed_at=now)
                        )
                        await conn.execute(delete(INBOX).where(INBOX.c.event_id == event.event_id))
                        await conn.execute(
                            update(COORDINATOR)
                            .where(COORDINATOR.c.id == 1)
                            .values(
                                pending_count=COORDINATOR.c.pending_count - 1,
                                sql_attempts_total=COORDINATOR.c.sql_attempts_total + 1,
                                retries_total=COORDINATOR.c.retries_total + int(attempt > 1),
                                committed_total=COORDINATOR.c.committed_total + 1,
                            )
                        )
                        result = AttemptResult("COMMITTED", attempt > 1, reading_id=identity["reading_id"])
                acknowledged = True
                # Diagnostic work and callbacks occur only after the transaction and connection are released.
                if failure is not None:
                    log_write_error(failure)
                    logger.info(
                        "vital_retry_scheduled",
                        extra={
                            "event": "vital_retry_scheduled",
                            "reading_ref": result.reading_id,
                            "retry_delay_seconds": result.retry_delay_seconds,
                        },
                    )
                elif result.state == "COMMITTED":
                    logger.info(
                        "write_completed",
                        extra={
                            "event": "write_completed",
                            **write_contract(),
                            "count": 1,
                            "completion_semantics": "committed_rows",
                            "reading_ref": result.reading_id,
                        },
                    )
                return result
            finally:
                if started and self.metrics:
                    self.metrics.record_ingest_finished(1, failed=result.state == "RETRY", cancelled=not acknowledged)

    async def snapshot(self) -> dict:
        """Read durable counters as totals/gauges; consumers must not sum repeated snapshots."""
        async with self.engine.connect() as conn:
            return dict((await conn.execute(select(COORDINATOR).where(COORDINATOR.c.id == 1))).mappings().one())

    async def checkpoint(self) -> dict:
        """Capture a conservative cohort boundary including every pending event in one database snapshot."""
        oldest = (
            select(func.min(IDENTITY.c.admission_sequence))
            .join(INBOX, IDENTITY.c.event_id == INBOX.c.event_id)
            .scalar_subquery()
        )
        async with self.engine.connect() as conn:
            value = (
                (
                    await conn.execute(
                        select(
                            COORDINATOR.c.epoch,
                            COORDINATOR.c.accepted_total,
                            oldest.label("oldest_pending"),
                            func.clock_timestamp().label("observed_at"),
                        ).where(COORDINATOR.c.id == 1)
                    )
                )
                .mappings()
                .one()
            )
        return {
            "epoch": value["epoch"],
            "lower_exclusive": value["oldest_pending"] - 1
            if value["oldest_pending"] is not None
            else value["accepted_total"],
            "upper_inclusive": value["accepted_total"],
            "observed_at": value["observed_at"].isoformat(),
        }

    async def cohort(self, epoch: str, lower_exclusive: int, upper_inclusive: int) -> dict:
        """Verify fixed admission sequences against real measurements without returning sensor values."""
        if not 0 <= lower_exclusive <= upper_inclusive:
            raise ValueError("invalid cohort range")
        measurement = SensorReadingRow.__table__
        counts = {
            "retained_identities": 0,
            "pending_events": 0,
            "missing_measurements": 0,
            "payload_mismatches": 0,
            "matched_measurements": 0,
            "lost_pending_inputs": 0,
            "observed_42703_events": 0,
        }
        latest_42703_at = None
        async with self.engine.connect() as connection:
            conn = await connection.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                await conn.execute(text("SET LOCAL statement_timeout = '30000'"))
                current = (await conn.execute(select(COORDINATOR).where(COORDINATOR.c.id == 1))).mappings().one()
                if current["epoch"] != epoch or upper_inclusive > current["accepted_total"]:
                    return {"complete": False, "reason": "epoch or admission bound mismatch"}
                query = (
                    select(
                        IDENTITY,
                        *[column.label("stored_" + column.name) for column in measurement.columns],
                        INBOX.c.event_id.label("actual_pending_id"),
                    )
                    .outerjoin(measurement, IDENTITY.c.measurement_id == measurement.c.id)
                    .outerjoin(INBOX, IDENTITY.c.event_id == INBOX.c.event_id)
                    .where(
                        IDENTITY.c.admission_sequence > lower_exclusive,
                        IDENTITY.c.admission_sequence <= upper_inclusive,
                    )
                )
                stream = await conn.stream(query)
                async for row in stream.mappings():
                    counts["retained_identities"] += 1
                    witness = row["failure_sqlstates"].get("42703", {})
                    if witness.get("count", 0) > 0:
                        counts["observed_42703_events"] += 1
                        if latest_42703_at is None or witness["last_at"] > latest_42703_at:
                            latest_42703_at = witness["last_at"]
                    if row["actual_pending_id"] is not None:
                        counts["pending_events"] += 1
                    if row["measurement_id"] is None:
                        if row["actual_pending_id"] is None:
                            counts["lost_pending_inputs"] += 1
                        continue
                    if row["stored_id"] is None:
                        counts["missing_measurements"] += 1
                        continue
                    event = VitalEvent(
                        row["event_id"],
                        row["sensor_id"],
                        row["stored_patient_id"],
                        row["schema_version"],
                        row["stored_reading_type"],
                        row["stored_value"],
                        row["stored_unit"],
                        row["stored_timestamp"],
                    )
                    if event.digest != row["payload_sha256"] or row["reading_id"] != row["measurement_id"]:
                        counts["payload_mismatches"] += 1
                    else:
                        counts["matched_measurements"] += 1
        expected = upper_inclusive - lower_exclusive
        return {
            "epoch": epoch,
            "lower_exclusive": lower_exclusive,
            "upper_inclusive": upper_inclusive,
            "expected_events": expected,
            **counts,
            "latest_42703_at": latest_42703_at,
            "missing_identities": max(0, expected - counts["retained_identities"]),
            "complete": counts["matched_measurements"] == expected
            and counts["retained_identities"] == expected
            and counts["pending_events"] == 0
            and counts["lost_pending_inputs"] == 0,
        }
