"""Executable guarantees for request-level symptoms and bounded periodic EMF."""

import asyncio
from types import SimpleNamespace

import pytest

from test_service.services import sensor as sensor_module
from test_service.services.db_observability import current_operation
from test_service.services.sensor import SensorService
from test_service.services.symptom_metrics import SymptomMetrics


class CapturingMetrics(SymptomMetrics):
    """Collect emitted documents without replacing buffer, gauge or timer behavior."""

    def __init__(self, *, interval=30):
        """Keep the real interval and bounded buffers while redirecting only the EMF sink."""
        super().__init__("healthcare-sensor-app", flush_interval=interval)
        self.emitted = []

    def _emit(self, payload):
        """Retain emitted documents for assertions without logging test patient data."""
        self.emitted.append(payload)


async def wait_until(predicate):
    """Wait for observable async progress with a deadline instead of assuming task ordering."""
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


class Repository:
    """A repository double that preserves operation context across asynchronous suspension."""

    def __init__(self, *, outcome="success", blocked=False):
        """Choose a terminal outcome or a gate so tests can inspect genuinely unfinished work."""
        self.outcome = outcome
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.operations = []

    async def _perform(self, result):
        """Capture the active DB identity before waiting, then produce the selected outcome."""
        operation = current_operation()
        self.operations.append((operation.name, operation.request_id))
        self.entered.set()
        await self.release.wait()
        if self.outcome == "error":
            raise RuntimeError("secret-bind-value: credential-do-not-log")
        if self.outcome == "cancelled":
            raise asyncio.CancelledError
        return result

    async def save_batch(self, readings):
        """Return saved entities only after the simulated repository operation finishes."""
        return await self._perform(readings)

    async def find_by_patient(self, *args, **kwargs):
        """Expose query completion/error/cancellation through the actual service boundary."""
        return await self._perform([])

    async def find_abnormal(self, **kwargs):
        """Expose alert calls under their own operation identity."""
        return await self._perform([])


def reading():
    """Create one synthetic reading whose private values must never enter diagnostic logs."""
    return {
        "patient_id": "secret-bind-value",
        "reading_type": "heart_rate",
        "value": 180,
        "unit": "bpm",
    }


@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_direct_query_records_milliseconds_for_every_exit(outcome, monkeypatch):
    """Internal service calls emit duration on all exits and restore the DB context."""
    metrics = CapturingMetrics()
    repository = Repository(outcome=outcome)
    service = SensorService(repository, metrics)
    ticks = iter([100.0, 100.125])
    monkeypatch.setattr(sensor_module, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
    if outcome == "success":
        assert await service.get_patient_vitals("secret-bind-value", limit=7) == []
    else:
        error = RuntimeError if outcome == "error" else asyncio.CancelledError
        with pytest.raises(error):
            await service.get_patient_vitals("secret-bind-value", limit=7)
    metrics.flush()
    payload = metrics.emitted[-1]
    assert payload["PatientVitalsQueryDuration"] == [125.0]
    assert payload["ServiceName"] == "healthcare-sensor-app"
    assert {"Name": "PatientVitalsQueryDuration", "Unit": "Milliseconds"} in (
        payload["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    )
    assert repository.operations[0][0] == "patient_vitals"
    assert current_operation() is None


async def test_repository_operations_have_independent_contexts_without_bind_logging(caplog):
    """Ingest, patient queries and alerts get distinct IDs and diagnostics omit private values."""
    caplog.set_level("INFO")
    repository = Repository()
    service = SensorService(repository, CapturingMetrics())
    await service.ingest([reading()])
    await service.get_patient_vitals("secret-bind-value")
    await service.get_alerts(patient_id="secret-bind-value")
    assert [operation[0] for operation in repository.operations] == ["ingest", "patient_vitals", "alerts"]
    assert len({operation[1] for operation in repository.operations}) == 3
    repository.outcome = "error"
    with pytest.raises(RuntimeError):
        await service.ingest([reading()])
    assert "secret-bind-value" not in str([record.__dict__ for record in caplog.records])
    assert "credential-do-not-log" not in caplog.text
    assert current_operation() is None


async def test_periodic_emission_preserves_inflight_during_stall_and_flushes_cancellation():
    """Stalled ingress stays visible across heartbeats without counting unfinished attempts."""
    metrics = CapturingMetrics()
    repository = Repository(blocked=True)
    service = SensorService(repository, metrics)
    stop = asyncio.Event()
    publisher = asyncio.create_task(metrics.run_periodic_flush(stop, interval=0.005))
    request = asyncio.create_task(service.ingest([reading(), reading()]))
    try:
        await repository.entered.wait()
        await wait_until(lambda: len(metrics.emitted) >= 2)
        first, second = metrics.emitted[:2]
        assert first["VitalIngestStarted"] == 2
        assert second["VitalIngestStarted"] == 0
        assert first["VitalIngestInFlight"] == second["VitalIngestInFlight"] == 2
        assert first["VitalIngestAttempts"] == second["VitalIngestAttempts"] == 0
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        stop.set()
        await publisher
    assert metrics.emitted[-1]["VitalIngestInFlight"] == 0
    assert sum(payload["VitalIngestAttempts"] for payload in metrics.emitted) == 0
    assert sum(payload["VitalIngestFailures"] for payload in metrics.emitted) == 0


@pytest.mark.parametrize("outcome", ["success", "error"])
async def test_ingest_gauge_closes_with_legacy_completed_reading_counts(outcome):
    """Success/error each complete every reading exactly once and release the live gauge."""
    metrics = CapturingMetrics()
    repository = Repository(outcome=outcome)
    service = SensorService(repository, metrics)
    if outcome == "success":
        await service.ingest([reading(), reading()])
    else:
        with pytest.raises(RuntimeError):
            await service.ingest([reading(), reading()])
    metrics.flush()
    assert metrics.emitted[-1]["VitalIngestStarted"] == 2
    assert metrics.emitted[-1]["VitalIngestInFlight"] == 0
    assert metrics.emitted[-1]["VitalIngestAttempts"] == 2
    assert metrics.emitted[-1]["VitalIngestFailures"] == (2 if outcome == "error" else 0)


def test_large_sample_stream_is_bounded_without_losing_average_inputs():
    """Emit full EMF batches at 100 and retain all samples, preserving alarm Average semantics."""
    metrics = CapturingMetrics()
    for value in range(1003):
        metrics.record_patient_vitals_duration(value)
        metrics.record_alert_delay(value / 1000)
        assert len(metrics._query_durations) <= 100
        assert len(metrics._delays) <= 100
    metrics.flush()
    durations = [sample for payload in metrics.emitted for sample in payload.get("PatientVitalsQueryDuration", [])]
    delays = [sample for payload in metrics.emitted for sample in payload.get("AbnormalAlertDelaySeconds", [])]
    assert durations == list(range(1003))
    assert delays == [value / 1000 for value in range(1003)]
    for payload in metrics.emitted:
        for name in ("PatientVitalsQueryDuration", "AbnormalAlertDelaySeconds"):
            if name in payload:
                assert 1 <= len(payload[name]) <= 100


@pytest.mark.parametrize("cancel", [False, True])
async def test_periodic_publisher_finally_flushes_pending_samples(cancel):
    """Both stop signals and task cancellation flush before the publisher returns."""
    metrics = CapturingMetrics()
    stop = asyncio.Event()
    publisher = asyncio.create_task(metrics.run_periodic_flush(stop, interval=60))
    await asyncio.sleep(0)
    metrics.record_patient_vitals_duration(7)
    if cancel:
        publisher.cancel()
    else:
        stop.set()
    await asyncio.gather(publisher, return_exceptions=True)
    assert metrics.emitted[-1]["PatientVitalsQueryDuration"] == [7]
