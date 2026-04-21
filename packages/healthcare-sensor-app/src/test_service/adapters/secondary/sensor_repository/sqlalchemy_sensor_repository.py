from datetime import datetime

from sqlalchemy import select

from test_service.adapters.secondary.sensor_repository.models import SensorReadingRow
from test_service.ports.dto.sensor import SensorReadingEntity
from test_service.ports.interfaces.database import DatabasePort
from test_service.ports.interfaces.sensor_reading_repository import SensorReadingRepositoryPort


class SqlAlchemySensorReadingRepository(SensorReadingRepositoryPort):
    def __init__(self, database: DatabasePort) -> None:
        self._database = database

    async def save_batch(self, readings: list[SensorReadingEntity]) -> list[SensorReadingEntity]:
        async for session in self._database.session():
            rows = []
            for r in readings:
                row = SensorReadingRow(
                    id=r.id,
                    patient_id=r.patient_id,
                    reading_type=r.reading_type,
                    value=r.value,
                    unit=r.unit,
                    timestamp=r.timestamp,
                    is_abnormal=r.is_abnormal,
                    created_at=r.created_at,
                )
                session.add(row)
                rows.append(row)
            await session.flush()
            await session.commit()
            return [self._to_entity(row) for row in rows]
        return []

    async def find_by_patient(
        self,
        patient_id: str,
        *,
        reading_type: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 100,
    ) -> list[SensorReadingEntity]:
        stmt = select(SensorReadingRow).where(SensorReadingRow.patient_id == patient_id)
        if reading_type:
            stmt = stmt.where(SensorReadingRow.reading_type == reading_type)
        if from_ts:
            stmt = stmt.where(SensorReadingRow.timestamp >= from_ts)
        if to_ts:
            stmt = stmt.where(SensorReadingRow.timestamp <= to_ts)
        stmt = stmt.order_by(SensorReadingRow.timestamp.desc()).limit(limit)

        async for session in self._database.session():
            result = await session.execute(stmt)
            return [self._to_entity(row) for row in result.scalars().all()]
        return []

    async def find_abnormal(
        self,
        *,
        patient_id: str | None = None,
        reading_type: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 50,
    ) -> list[SensorReadingEntity]:
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

        async for session in self._database.session():
            result = await session.execute(stmt)
            return [self._to_entity(row) for row in result.scalars().all()]
        return []

    @staticmethod
    def _to_entity(row: SensorReadingRow) -> SensorReadingEntity:
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
