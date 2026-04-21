from datetime import datetime

from fastapi import APIRouter, Query

from test_service.adapters.primary.schemas import SensorReadingResponse
from test_service.ports.dto.sensor import SensorReadingEntity
from test_service.services.sensor import SensorService


class AlertController:
    def __init__(self, service: SensorService) -> None:
        self._service = service
        self.router = APIRouter(prefix="/alerts", tags=["alerts"])
        self.router.add_api_route("", self.get_alerts, methods=["GET"], response_model=list[SensorReadingResponse])

    async def get_alerts(
        self,
        patient_id: str | None = Query(None),
        reading_type: str | None = Query(None),
        from_ts: datetime | None = Query(None),
        to_ts: datetime | None = Query(None),
        limit: int = Query(50, le=200),
    ) -> list[SensorReadingResponse]:
        entities = await self._service.get_alerts(
            patient_id=patient_id, reading_type=reading_type, from_ts=from_ts, to_ts=to_ts, limit=limit
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
