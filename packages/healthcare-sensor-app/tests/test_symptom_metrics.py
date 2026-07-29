import json

import pytest

from test_service.ports.dto.sensor import SensorReadingEntity
from test_service.services.sensor import SensorService
from test_service.services.symptom_metrics import (
    METRIC_ALERT_DELAY_SECONDS,
    METRIC_INGEST_ATTEMPTS,
    METRIC_INGEST_FAILURES,
    NAMESPACE,
    SymptomMetrics,
)


class RecordingMetrics(SymptomMetrics):
    def __init__(self) -> None:
        super().__init__("test-service", flush_interval=0.0)
        self.emitted: list[dict] = []

    def _emit(self, payload: dict) -> None:  # type: ignore[override]
        self.emitted.append(payload)


class FakeRepository:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.saved: list[SensorReadingEntity] = []

    async def save_batch(self, readings: list[SensorReadingEntity]) -> list[SensorReadingEntity]:
        if self.fails:
            raise RuntimeError("database unavailable")
        self.saved.extend(readings)
        return readings

    async def find_by_patient(self, *args: object, **kwargs: object) -> list:
        return []

    async def find_abnormal(self, *args: object, **kwargs: object) -> list:
        return []


def _reading(value: float = 80.0) -> dict:
    return {
        "patient_id": "P-001",
        "reading_type": "heart_rate",
        "value": value,
        "unit": "bpm",
    }


def _metric_names(payload: dict) -> set[str]:
    return {m["Name"] for m in payload["_aws"]["CloudWatchMetrics"][0]["Metrics"]}


def test_emitted_payload_is_valid_emf_on_the_expected_namespace() -> None:
    metrics = RecordingMetrics()

    metrics.record_ingest(attempted=3, failed=0)

    payload = metrics.emitted[-1]
    directive = payload["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == NAMESPACE
    assert directive["Dimensions"] == [["ServiceName"]]
    assert payload["ServiceName"] == "test-service"
    assert isinstance(payload["_aws"]["Timestamp"], int)
    # The whole line must be parseable as one JSON document for EMF extraction.
    assert json.loads(json.dumps(payload)) == payload


def test_ingest_counters_reset_between_flushes() -> None:
    metrics = RecordingMetrics()

    metrics.record_ingest(attempted=2, failed=1)
    metrics.record_ingest(attempted=3, failed=0)

    assert [p[METRIC_INGEST_ATTEMPTS] for p in metrics.emitted] == [2, 3]
    assert [p[METRIC_INGEST_FAILURES] for p in metrics.emitted] == [1, 0]


def test_alert_delay_is_emitted_as_a_value_array() -> None:
    metrics = RecordingMetrics()

    metrics.record_alert_delay(1.5)

    payload = metrics.emitted[-1]
    assert payload[METRIC_ALERT_DELAY_SECONDS] == [1.5]
    assert METRIC_ALERT_DELAY_SECONDS in _metric_names(payload)


def test_delay_metric_is_omitted_when_no_abnormal_reading_was_seen() -> None:
    metrics = RecordingMetrics()

    metrics.record_ingest(attempted=1, failed=0)

    assert METRIC_ALERT_DELAY_SECONDS not in _metric_names(metrics.emitted[-1])


def test_flush_emits_nothing_when_no_activity_was_recorded() -> None:
    metrics = RecordingMetrics()

    metrics.flush()

    assert metrics.emitted == []


@pytest.mark.asyncio
async def test_successful_ingest_reports_attempts_without_failures() -> None:
    metrics = RecordingMetrics()
    service = SensorService(FakeRepository(), metrics)

    await service.ingest([_reading(), _reading()])

    totals = [(p[METRIC_INGEST_ATTEMPTS], p[METRIC_INGEST_FAILURES]) for p in metrics.emitted]
    assert (2, 0) in totals


@pytest.mark.asyncio
async def test_failed_ingest_counts_every_reading_as_a_failure_and_propagates() -> None:
    metrics = RecordingMetrics()
    service = SensorService(FakeRepository(fails=True), metrics)

    with pytest.raises(RuntimeError):
        await service.ingest([_reading(), _reading(), _reading()])

    payload = metrics.emitted[-1]
    assert payload[METRIC_INGEST_ATTEMPTS] == 3
    assert payload[METRIC_INGEST_FAILURES] == 3


@pytest.mark.asyncio
async def test_abnormal_reading_records_an_alert_delay() -> None:
    metrics = RecordingMetrics()
    service = SensorService(FakeRepository(), metrics)

    await service.ingest([_reading(value=180.0)])

    delays = [p[METRIC_ALERT_DELAY_SECONDS] for p in metrics.emitted if METRIC_ALERT_DELAY_SECONDS in p]
    assert delays
    assert all(delay >= 0 for group in delays for delay in group)


@pytest.mark.asyncio
async def test_sensor_service_works_without_a_metrics_collaborator() -> None:
    service = SensorService(FakeRepository())

    saved = await service.ingest([_reading()])

    assert len(saved) == 1
