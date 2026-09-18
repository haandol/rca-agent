import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from sqlalchemy.exc import TimeoutError as SATimeoutError

from test_service.ports.dto.sensor import SensorReadingEntity
from test_service.services import symptom_metrics as metrics_module
from test_service.services.sensor import SensorService
from test_service.services.symptom_metrics import (
    METRIC_ALERT_DELAY_SECONDS,
    METRIC_INGEST_ATTEMPTS,
    METRIC_INGEST_FAILURES,
    NAMESPACE,
    SymptomMetrics,
)


class RecordingMetrics(SymptomMetrics):
    def __init__(self, *, flush_interval: float = 0.0) -> None:
        super().__init__("test-service", flush_interval=flush_interval)
        self.emitted: list[dict] = []

    def _emit(self, payload: dict) -> None:  # type: ignore[override]
        self.emitted.append(payload)


_INCIDENT_MINUTE = int(datetime(2026, 9, 10, 13, 29, tzinfo=UTC).timestamp())


class MetricClock:
    """Control source and interval clocks without changing asyncio's own clock."""

    elapsed = 0.0

    def time(self) -> float:
        return _INCIDENT_MINUTE + 35 + self.elapsed

    def monotonic(self) -> float:
        return self.elapsed


@pytest.fixture
def metric_clock(monkeypatch):
    clock = MetricClock()
    monkeypatch.setattr(metrics_module, "time", clock)
    return clock


@pytest.mark.parametrize("heartbeat", [False, True])
def test_failures_recorded_before_minute_boundary_keep_source_minute_on_late_flush(metric_clock, heartbeat):
    metrics = RecordingMetrics(flush_interval=30)
    for elapsed, failures in ((3, 3), (13, 3), (23, 4)):  # 13:29:38/48/58, total 10.
        metric_clock.elapsed = elapsed
        metrics.record_ingest(attempted=failures, failed=failures)
    assert metrics.emitted == []

    metric_clock.elapsed = 30  # The normal 30-second flush is at 13:30:05.
    metrics.flush(heartbeat=heartbeat)

    failures = [payload for payload in metrics.emitted if payload[METRIC_INGEST_FAILURES]]
    assert sum(payload[METRIC_INGEST_FAILURES] for payload in failures) == 10
    assert sum(payload[METRIC_INGEST_ATTEMPTS] for payload in metrics.emitted) == 10
    assert all(_INCIDENT_MINUTE * 1000 <= p["_aws"]["Timestamp"] < (_INCIDENT_MINUTE + 60) * 1000 for p in failures)


def _minute(payload: dict) -> int:
    return payload["_aws"]["Timestamp"] // 60_000 * 60


@pytest.mark.parametrize(
    ("method", "arguments", "expected"),
    [
        ("record_ingest", {"attempted": 4, "failed": 0}, {"VitalIngestAttempts": 4}),
        ("record_ingest_started", {"readings": 4}, {"VitalIngestStarted": 4, "VitalIngestInFlight": 11}),
        ("record_ingest_finished", {"readings": 4, "cancelled": True}, {"VitalIngestInFlight": 3}),
        (
            "record_traffic",
            {"offered": 2, "started": 1, "skipped": 1},
            {"TrafficOffered": 2, "TrafficStarted": 1, "TrafficSkipped": 1},
        ),
        ("record_alert_delay", {"seconds": 0.25}, {"AbnormalAlertDelaySeconds": [0.25]}),
        ("record_patient_vitals_duration", {"milliseconds": 12.0}, {"PatientVitalsQueryDuration": [12.0]}),
        ("record_measurement_sql_started", {}, {"VitalMeasurementSQLStarted": 1}),
    ],
)
def test_every_record_path_rotates_all_counters_and_samples_before_accepting_next_minute(
    metric_clock, method, arguments, expected
):
    metrics = RecordingMetrics(flush_interval=30)
    metric_clock.elapsed = 3
    metrics.record_ingest_started(10)
    metrics.record_ingest_finished(3, failed=True)
    metrics.record_ingest(attempted=7, failed=7)
    metrics.record_traffic(offered=6, started=4, completed=3, skipped=2, failed=2, cancelled=1)
    metrics.record_alert_delay(1.5)
    metrics.record_patient_vitals_duration(2000)
    metrics.record_measurement_sql_started()

    metric_clock.elapsed = 25  # Exactly 13:30:00 belongs to the next minute.
    getattr(metrics, method)(**arguments)
    assert len(metrics.emitted) == 1
    previous = metrics.emitted[0]
    assert _minute(previous) == _INCIDENT_MINUTE
    old_values = {
        "VitalIngestAttempts": 10,
        "VitalIngestFailures": 10,
        "VitalIngestStarted": 10,
        "VitalIngestInFlight": 7,
        "VitalMeasurementSQLStarted": 1,
        "TrafficOffered": 6,
        "TrafficStarted": 4,
        "TrafficCompleted": 3,
        "TrafficSkipped": 2,
        "TrafficFailed": 2,
        "TrafficCancelled": 1,
        "AbnormalAlertDelaySeconds": [1.5],
        "PatientVitalsQueryDuration": [2000],
    }
    assert {name: previous[name] for name in old_values} == old_values

    metrics.record_ingest(attempted=2, failed=0)
    metrics.flush()
    current = metrics.emitted[-1]
    assert _minute(current) == _INCIDENT_MINUTE + 60
    new_values = {name: 0 for name, value in old_values.items() if not isinstance(value, list)}
    new_values.update({"VitalIngestInFlight": 7, **expected})
    new_values["VitalIngestAttempts"] += 2
    assert {name: current[name] for name in new_values} == new_values
    for name in ("AbnormalAlertDelaySeconds", "PatientVitalsQueryDuration"):
        assert current.get(name) == expected.get(name)
    units = {m["Name"]: m["Unit"] for m in previous["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert units == {
        **dict.fromkeys(new_values.keys() - {"AbnormalAlertDelaySeconds", "PatientVitalsQueryDuration"}, "Count"),
        "AbnormalAlertDelaySeconds": "Seconds",
        "PatientVitalsQueryDuration": "Milliseconds",
    }


def test_capacity_and_boundary_flushes_preserve_samples_and_the_thirty_second_deadline(metric_clock):
    metrics = RecordingMetrics(flush_interval=30)
    metric_clock.elapsed = 3
    for value in range(100):
        metrics.record_alert_delay(value)
    assert len(metrics.emitted) == 1

    metric_clock.elapsed = 24
    metrics.record_patient_vitals_duration(2000)
    metric_clock.elapsed = 25
    metrics.record_patient_vitals_duration(12)
    assert len(metrics.emitted) == 2
    metric_clock.elapsed = 29
    metrics.record_ingest(attempted=2, failed=0)
    assert len(metrics.emitted) == 2
    metric_clock.elapsed = 30
    metrics.record_traffic(completed=1)
    assert len(metrics.emitted) == 3
    metrics.flush()
    assert len(metrics.emitted) == 3
    assert [_minute(p) for p in metrics.emitted] == [_INCIDENT_MINUTE, _INCIDENT_MINUTE, _INCIDENT_MINUTE + 60]
    assert metrics.emitted[0]["AbnormalAlertDelaySeconds"] == list(range(100))
    assert metrics.emitted[1]["PatientVitalsQueryDuration"] == [2000]
    assert metrics.emitted[2]["PatientVitalsQueryDuration"] == [12]
    assert metrics.emitted[2]["VitalIngestAttempts"] == 2
    assert metrics.emitted[2]["VitalIngestFailures"] == 0
    assert metrics.emitted[2]["TrafficCompleted"] == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_periodic_shutdown_after_boundary_preserves_pending_minute_and_current_gauge(metric_clock, cancel):
    metrics = RecordingMetrics(flush_interval=30)
    metric_clock.elapsed = 3
    metrics.record_ingest_started(2)
    metrics.record_patient_vitals_duration(2000)
    stop = asyncio.Event()
    publisher = asyncio.create_task(metrics.run_periodic_flush(stop, interval=60))
    await asyncio.sleep(0)
    metric_clock.elapsed = 25
    if cancel:
        publisher.cancel()
    else:
        stop.set()
    await asyncio.gather(publisher, return_exceptions=True)

    previous, current = metrics.emitted
    assert _minute(previous) == _INCIDENT_MINUTE
    assert previous["VitalIngestStarted"] == 2
    assert previous["PatientVitalsQueryDuration"] == [2000]
    assert _minute(current) == _INCIDENT_MINUTE + 60
    assert current["VitalIngestStarted"] == 0
    assert current["VitalIngestInFlight"] == previous["VitalIngestInFlight"] == 2
    assert "PatientVitalsQueryDuration" not in current
    metrics.record_ingest_finished(2, cancelled=True)
    metrics.flush()
    assert metrics.emitted[-1]["VitalIngestInFlight"] == 0
    assert sum(p["VitalIngestAttempts"] for p in metrics.emitted) == 0
    assert sum(p["VitalIngestFailures"] for p in metrics.emitted) == 0


async def test_periodic_timeout_emits_old_counts_then_current_heartbeat_without_backfilling_idle_minutes(metric_clock):
    metrics = RecordingMetrics(flush_interval=30)
    metric_clock.elapsed = 3
    metrics.record_ingest(attempted=10, failed=10)
    metric_clock.elapsed = 145  # 13:32:00: missed minutes must not inherit failures.
    stop = asyncio.Event()
    publisher = asyncio.create_task(metrics.run_periodic_flush(stop, interval=0.001))
    try:
        async with asyncio.timeout(2):
            while len(metrics.emitted) < 2:
                await asyncio.sleep(0.001)
    finally:
        stop.set()
        await publisher
    assert {_minute(p) for p in metrics.emitted} == {_INCIDENT_MINUTE, _INCIDENT_MINUTE + 180}
    assert metrics.emitted[0]["VitalIngestFailures"] == 10
    assert all(p["VitalIngestAttempts"] == p["VitalIngestFailures"] == 0 for p in metrics.emitted[1:])


def test_concurrent_record_capacity_and_manual_flush_preserve_each_minutes_totals(metric_clock):
    metrics = RecordingMetrics(flush_interval=30)
    barrier = Barrier(6, timeout=10)  # Four producers, one flusher and the clock owner.
    repetitions = 150

    def produce(worker):
        for period in range(2):
            barrier.wait()
            for index in range(repetitions):
                cancelled = index % 5 == 0
                metrics.record_ingest_started(2)
                metrics.record_ingest_finished(2, failed=period == 0, cancelled=cancelled)
                metrics.record_traffic(
                    offered=2,
                    started=1,
                    completed=1,
                    skipped=1,
                    failed=int(period == 0 and not cancelled),
                    cancelled=int(cancelled),
                )
                sample = period * 10000 + worker * 1000 + index
                metrics.record_patient_vitals_duration(sample)
                metrics.record_alert_delay(sample / 1000)
            barrier.wait()

    def flush():
        for _ in range(2):
            barrier.wait()
            for index in range(repetitions):
                metrics.flush(heartbeat=index % 2 == 0)
            barrier.wait()

    with ThreadPoolExecutor(max_workers=5) as executor:
        tasks = [executor.submit(produce, worker) for worker in range(4)]
        tasks.append(executor.submit(flush))
        for elapsed in (3, 25):
            metric_clock.elapsed = elapsed
            barrier.wait()
            barrier.wait()
        for task in tasks:
            task.result()
    metrics.flush()

    assert {_minute(p) for p in metrics.emitted} == {_INCIDENT_MINUTE, _INCIDENT_MINUTE + 60}
    for period in range(2):
        payloads = [p for p in metrics.emitted if _minute(p) == _INCIDENT_MINUTE + period * 60]
        totals = {
            "VitalIngestStarted": 1200,
            "VitalIngestAttempts": 960,
            "VitalIngestFailures": 960 if period == 0 else 0,
            "TrafficOffered": 1200,
            "TrafficStarted": 600,
            "TrafficCompleted": 600,
            "TrafficSkipped": 600,
            "TrafficFailed": 480 if period == 0 else 0,
            "TrafficCancelled": 120,
        }
        assert {name: sum(p[name] for p in payloads) for name in totals} == totals
        expected = [period * 10000 + worker * 1000 + index for worker in range(4) for index in range(repetitions)]
        for name, divisor in (("PatientVitalsQueryDuration", 1), ("AbnormalAlertDelaySeconds", 1000)):
            assert sorted(value for p in payloads for value in p.get(name, [])) == [v / divisor for v in expected]
            assert all(len(p.get(name, [])) <= 100 for p in payloads)
        assert all(0 <= p["VitalIngestInFlight"] <= 8 for p in payloads)
    assert metrics.emitted[-1]["VitalIngestInFlight"] == 0


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


class PoolExhaustedRepository(FakeRepository):
    """저장이 커넥션 풀 고갈로 실패하는 저장소.

    누수가 쌓여 풀이 비면 요청은 커넥션을 얻지 못하고 SQLAlchemy 가 대기 후
    TimeoutError 를 던진다. 데모의 진입점이 증상 알람이므로, 이 실패가 증상 지표에
    나타나는지가 사슬의 마지막 구간이다.
    """

    async def save_batch(self, readings: list[SensorReadingEntity]) -> list[SensorReadingEntity]:
        raise SATimeoutError("QueuePool limit of size 5 overflow 10 reached, connection timed out")


def test_pool_exhaustion_surfaces_as_the_symptom_the_entry_alarm_watches() -> None:
    """풀 고갈이 증상 지표를 움직여야 RCA 가 시작된다.

    커넥션 누수는 원인 지표(커넥션 수)를 먼저 올리고, 풀이 고갈되면 바이탈 수집이
    실패해 증상 지표에 나타난다. 이 마지막 연결이 끊기면 장애를 주입해도 진입 알람이
    뜨지 않아 분석이 시작되지 않는다.
    """
    metrics = RecordingMetrics()
    service = SensorService(PoolExhaustedRepository(), metrics)

    with pytest.raises(SATimeoutError):
        asyncio.run(service.ingest([_reading(), _reading(90.0)]))

    payload = metrics.emitted[-1]
    # 진입 알람은 이 지표가 1 이상인 것을 본다. 시도 수와 같아야 부분 실패가 아니라
    # 배치 전체가 유실됐음을 나타낸다.
    assert payload[METRIC_INGEST_FAILURES] == 2
    assert payload[METRIC_INGEST_ATTEMPTS] == 2
    assert METRIC_INGEST_FAILURES in _metric_names(payload)


def test_a_successful_ingest_leaves_the_entry_alarm_metric_at_zero() -> None:
    """정상 상태에서 진입 알람이 뜨지 않아야 한다 — 그렇지 않으면 상시 발화한다."""
    metrics = RecordingMetrics()
    service = SensorService(FakeRepository(), metrics)

    asyncio.run(service.ingest([_reading(), _reading()]))

    assert metrics.emitted[-1][METRIC_INGEST_FAILURES] == 0
