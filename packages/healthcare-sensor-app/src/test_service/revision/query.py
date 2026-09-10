"""Stable patient query implementation."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from test_service.adapters.secondary.sensor_repository.models import SensorReadingRow


async def fetch_patient_rows(session: AsyncSession, stmt: Select) -> list[SensorReadingRow]:
    """Read the requested rows in one database round trip."""
    return list((await session.execute(stmt)).scalars().all())
