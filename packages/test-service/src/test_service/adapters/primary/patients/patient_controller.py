from datetime import datetime

from fastapi import APIRouter, Query

from test_service.adapters.primary.schemas import SensorReadingResponse
from test_service.ports.dto.sensor import SensorReadingEntity
from test_service.services.sensor import SensorService


class PatientController:
    def __init__(self, service: SensorService) -> None:
        self._service = service
        self.router = APIRouter(prefix="/patients", tags=["patients"])
        self.router.add_api_route(
            "/{patient_id}/vitals", self.get_vitals, methods=["GET"], response_model=list[SensorReadingResponse]
        )

    async def get_vitals(
        self,
        patient_id: str,
        reading_type: str | None = Query(None),
        from_ts: datetime | None = Query(None),
        to_ts: datetime | None = Query(None),
        limit: int = Query(100, le=500),
    ) -> list[SensorReadingResponse]:
        entities = await self._service.get_patient_vitals(
            patient_id, reading_type=reading_type, from_ts=from_ts, to_ts=to_ts, limit=limit
        )
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
