from datetime import datetime

from pydantic import BaseModel

from test_service.ports.dto.sensor import ReadingType


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


class HealthResponse(BaseModel):
    status: str
    db_connected: bool
    active_db_connections: int
    uptime_seconds: float


class FaultRequest(BaseModel):
    # Default has to exceed the connection pool ceiling, or a single injection
    # leaks without exhausting the pool and never reaches the ingest failures the
    # entry-point alarm watches.
    count: int = 20


class FaultMemoryRequest(BaseModel):
    megabytes: int = 256


class FaultSlowQueryRequest(BaseModel):
    seconds: int = 5
