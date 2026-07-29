import logging
import uuid
from datetime import UTC, datetime

from test_service.ports.dto.sensor import SensorReadingEntity
from test_service.ports.interfaces.sensor_reading_repository import SensorReadingRepositoryPort
from test_service.services.symptom_metrics import SymptomMetrics

logger = logging.getLogger(__name__)

ABNORMAL_THRESHOLDS: dict[str, tuple[float, float]] = {
    "heart_rate": (60.0, 100.0),
    "blood_pressure_systolic": (90.0, 140.0),
    "blood_pressure_diastolic": (60.0, 90.0),
    "temperature": (36.0, 38.0),
    "spo2": (95.0, 100.0),
}


class SensorService:
    def __init__(
        self,
        repository: SensorReadingRepositoryPort,
        symptom_metrics: SymptomMetrics | None = None,
    ) -> None:
        self._repository = repository
        self._symptom_metrics = symptom_metrics

    async def ingest(self, readings: list[dict]) -> list[SensorReadingEntity]:
        entities = []
        abnormal_entities: list[SensorReadingEntity] = []
        for r in readings:
            reading_type = r["reading_type"]
            value = r["value"]
            abnormal = self._is_abnormal(reading_type, value)

            entity = SensorReadingEntity(
                id=str(uuid.uuid4()),
                patient_id=r["patient_id"],
                reading_type=reading_type,
                value=value,
                unit=r["unit"],
                timestamp=r.get("timestamp") or datetime.now(UTC),
                is_abnormal=abnormal,
            )
            entities.append(entity)

            if abnormal:
                abnormal_entities.append(entity)
                logger.warning(
                    "Abnormal reading detected",
                    extra={"patient_id": entity.patient_id, "reading_type": reading_type, "value": value},
                )

        try:
            saved = await self._repository.save_batch(entities)
        except Exception:
            self._record_ingest(attempted=len(entities), failed=len(entities))
            logger.exception(
                "Vital ingest failed — readings were not persisted",
                extra={"batch_size": len(entities)},
            )
            raise

        self._record_ingest(attempted=len(entities), failed=0)
        self._record_alert_delays(abnormal_entities)
        return saved

    def _record_ingest(self, *, attempted: int, failed: int) -> None:
        if self._symptom_metrics is None:
            return
        self._symptom_metrics.record_ingest(attempted=attempted, failed=failed)

    def _record_alert_delays(self, abnormal: list[SensorReadingEntity]) -> None:
        if self._symptom_metrics is None or not abnormal:
            return
        now = datetime.now(UTC)
        for entity in abnormal:
            observed = entity.timestamp
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=UTC)
            self._symptom_metrics.record_alert_delay(max((now - observed).total_seconds(), 0.0))

    async def get_patient_vitals(
        self,
        patient_id: str,
        *,
        reading_type: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 100,
    ) -> list[SensorReadingEntity]:
        return await self._repository.find_by_patient(
            patient_id, reading_type=reading_type, from_ts=from_ts, to_ts=to_ts, limit=limit
        )

    async def get_alerts(
        self,
        *,
        patient_id: str | None = None,
        reading_type: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 50,
    ) -> list[SensorReadingEntity]:
        return await self._repository.find_abnormal(
            patient_id=patient_id, reading_type=reading_type, from_ts=from_ts, to_ts=to_ts, limit=limit
        )

    @staticmethod
    def _is_abnormal(reading_type: str, value: float) -> bool:
        bounds = ABNORMAL_THRESHOLDS.get(reading_type)
        if not bounds:
            return False
        low, high = bounds
        return value < low or value > high
