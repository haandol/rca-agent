import logging
from datetime import datetime

from sqlalchemy import select

from test_service.adapters.secondary.sensor_repository.models import SensorReadingRow
from test_service.ports.dto.sensor import SensorReadingEntity
from test_service.ports.interfaces.database import DatabasePort
from test_service.ports.interfaces.sensor_reading_repository import SensorReadingRepositoryPort
from test_service.revision.query import fetch_patient_rows
from test_service.revision.write import write_statement, write_values
from test_service.services.db_observability import operation_context
from test_service.services.write_diagnostics import log_write_error, write_contract

logger = logging.getLogger(__name__)


class SqlAlchemySensorReadingRepository(SensorReadingRepositoryPort):
    def __init__(self, database: DatabasePort) -> None:
        """Use the database port's lifetime contract rather than owning a separate pool."""
        self._database = database

    async def save_batch(self, readings: list[SensorReadingEntity]) -> list[SensorReadingEntity]:
        """Execute bound INSERTs atomically and publish success only after commit returns."""
        if not readings:
            return []
        with operation_context("ingest"):
            try:
                async with self._database.session_context() as session:
                    await session.execute(write_statement(), [write_values(row) for row in readings])
            except Exception as exc:
                log_write_error(exc)
                raise
            logger.info(
                "write_completed",
                extra={
                    "event": "write_completed",
                    **write_contract(),
                    "count": len(readings),
                    "completion_semantics": "committed_rows",
                },
            )
        return readings

    async def find_by_patient(
        self,
        patient_id: str,
        *,
        reading_type: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 100,
    ) -> list[SensorReadingEntity]:
        """Apply identical filters and ordering before the build-selected query implementation.

        The context scope receives query errors directly and always closes.
        """
        stmt = select(SensorReadingRow).where(SensorReadingRow.patient_id == patient_id)
        if reading_type:
            stmt = stmt.where(SensorReadingRow.reading_type == reading_type)
        if from_ts:
            stmt = stmt.where(SensorReadingRow.timestamp >= from_ts)
        if to_ts:
            stmt = stmt.where(SensorReadingRow.timestamp <= to_ts)
        # A stable tiebreaker keeps row identity/order identical across query
        # plans when several readings have the same timestamp.
        stmt = stmt.order_by(SensorReadingRow.timestamp.desc(), SensorReadingRow.id.desc()).limit(limit)

        async with self._database.session_context() as session:
            return [self._to_entity(row) for row in await fetch_patient_rows(session, stmt)]

    async def find_abnormal(
        self,
        *,
        patient_id: str | None = None,
        reading_type: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 50,
    ) -> list[SensorReadingEntity]:
        """Read filtered alerts in one scope that closes promptly on return or query failure."""
        stmt = select(SensorReadingRow).where(SensorReadingRow.is_abnormal.is_(True))
        if patient_id:
            stmt = stmt.where(SensorReadingRow.patient_id == patient_id)
        if reading_type:
            stmt = stmt.where(SensorReadingRow.reading_type == reading_type)
        if from_ts:
            stmt = stmt.where(SensorReadingRow.timestamp >= from_ts)
        if to_ts:
            stmt = stmt.where(SensorReadingRow.timestamp <= to_ts)
        stmt = stmt.order_by(SensorReadingRow.timestamp.desc()).limit(limit)

        async with self._database.session_context() as session:
            result = await session.execute(stmt)
            return [self._to_entity(row) for row in result.scalars().all()]

    @staticmethod
    def _to_entity(row: SensorReadingRow) -> SensorReadingEntity:
        """Copy persisted fields into a domain value that does not depend on an open session."""
        return SensorReadingEntity(
            id=row.id,
            patient_id=row.patient_id,
            reading_type=row.reading_type,
            value=row.value,
            unit=row.unit,
            timestamp=row.timestamp,
            is_abnormal=row.is_abnormal,
            created_at=row.created_at,
        )
