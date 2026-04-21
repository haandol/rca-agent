from abc import ABC, abstractmethod
from datetime import datetime

from test_service.ports.dto.sensor import SensorReadingEntity


class SensorReadingRepositoryPort(ABC):
    @abstractmethod
    async def save_batch(self, readings: list[SensorReadingEntity]) -> list[SensorReadingEntity]: ...

    @abstractmethod
    async def find_by_patient(
        self,
        patient_id: str,
        *,
        reading_type: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 100,
    ) -> list[SensorReadingEntity]: ...

    @abstractmethod
    async def find_abnormal(
        self,
        *,
        patient_id: str | None = None,
        reading_type: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 50,
    ) -> list[SensorReadingEntity]: ...
