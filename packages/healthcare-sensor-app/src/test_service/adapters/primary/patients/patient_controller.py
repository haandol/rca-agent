from datetime import datetime

from fastapi import APIRouter, Query

from test_service.adapters.primary.schemas import SensorReadingResponse, sensor_reading_response
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
        return [sensor_reading_response(e) for e in entities]
