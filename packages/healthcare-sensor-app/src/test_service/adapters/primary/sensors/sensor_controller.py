from fastapi import APIRouter

from test_service.adapters.primary.schemas import SensorReadingBatch, SensorReadingResponse
from test_service.ports.dto.sensor import SensorReadingEntity
from test_service.services.sensor import SensorService


class SensorController:
    def __init__(self, service: SensorService) -> None:
        self._service = service
        self.router = APIRouter(prefix="/sensors", tags=["sensors"])
        self.router.add_api_route("/data", self.ingest, methods=["POST"], response_model=list[SensorReadingResponse])

    async def ingest(self, batch: SensorReadingBatch) -> list[SensorReadingResponse]:
        readings = [
            {
                "patient_id": r.patient_id,
                "reading_type": r.reading_type.value,
                "value": r.value,
                "unit": r.unit,
                "timestamp": r.timestamp,
            }
            for r in batch.readings
        ]
        entities = await self._service.ingest(readings)
        return [self._to_response(e) for e in entities]

    @staticmethod
    def _to_response(e: SensorReadingEntity) -> SensorReadingResponse:
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
