"""Patient query source installed in the r2 image."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from test_service.adapters.secondary.sensor_repository.models import SensorReadingRow


async def fetch_patient_rows(session: AsyncSession, stmt: Select) -> list[SensorReadingRow]:
    """Resolve ordered IDs first, then load each row through a separate SELECT."""
    ids = (await session.execute(stmt.with_only_columns(SensorReadingRow.id))).scalars().all()
    rows = []
    for reading_id in ids:
        # execute rather than Session.get: an identity-map hit must not hide SQL.
        row = (
            await session.execute(select(SensorReadingRow).where(SensorReadingRow.id == reading_id))
        ).scalar_one_or_none()
        if row is not None:
            rows.append(row)
    return rows
