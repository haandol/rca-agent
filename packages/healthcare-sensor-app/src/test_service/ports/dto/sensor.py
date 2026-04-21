import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime


class ReadingType(enum.StrEnum):
    HEART_RATE = "heart_rate"
    BLOOD_PRESSURE_SYSTOLIC = "blood_pressure_systolic"
    BLOOD_PRESSURE_DIASTOLIC = "blood_pressure_diastolic"
    TEMPERATURE = "temperature"
    SPO2 = "spo2"


@dataclass
class SensorReadingEntity:
    id: str
    patient_id: str
    reading_type: str
    value: float
    unit: str
    timestamp: datetime
    is_abnormal: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
