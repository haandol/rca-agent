from datetime import datetime

from pydantic import BaseModel

from test_service.ports.dto.sensor import ReadingType, SensorReadingEntity


class SensorReadingCreate(BaseModel):
    patient_id: str
    reading_type: ReadingType
    value: float
    unit: str
    timestamp: datetime | None = None


class SensorReadingBatch(BaseModel):
    readings: list[SensorReadingCreate]


class SensorReadingResponse(BaseModel):
    id: str
    patient_id: str
    reading_type: str
    value: float
    unit: str
    timestamp: datetime
    is_abnormal: bool
    created_at: datetime


def sensor_reading_response(e: SensorReadingEntity) -> SensorReadingResponse:
    """Keep the three HTTP endpoints' explicit field mapping and validation order identical."""
    return SensorReadingResponse(
        id=e.id,
        patient_id=e.patient_id,
        reading_type=e.reading_type,
        value=e.value,
        unit=e.unit,
        timestamp=e.timestamp,
        is_abnormal=e.is_abnormal,
        created_at=e.created_at,
    )


class HealthResponse(BaseModel):
    status: str
    db_connected: bool
    active_db_connections: int
    uptime_seconds: float
