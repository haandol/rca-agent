"""Request cadence, admission bounds and cancellation behavior under actual asyncio scheduling."""

import asyncio
from types import SimpleNamespace

import pytest

from test_service.services import traffic_generator

from .test_bounded_observability import CapturingMetrics, wait_until


def total(metrics, name):
    """Sum emitted interval deltas rather than depending on private accumulator layout."""
    return sum(payload[name] for payload in metrics.emitted)


class WorkloadService:
    """Exercise real coroutine suspension with controllable failures and bounded concurrent calls."""

    def __init__(self, *, blocked=False, fail_ingest=False, cancel_after=None):
        """Keep deterministic gates and counters for observation before/after request completion."""
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.fail_ingest = fail_ingest
        self.cancel_after = cancel_after
        self.operations = []
        self.active = 0
        self.peak = 0
        self.cancelled = 0
        self.batches = []
        self.patients = []
        self.alerts = []

    async def _request(self, name):
        """Hold active requests at a real await and always release counts when cancelled."""
        self.operations.append(name)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await self.release.wait()
            if self.cancel_after is not None and len(self.operations) >= self.cancel_after:
                raise asyncio.CancelledError
            if name == "ingest" and self.fail_ingest:
                raise RuntimeError("secret-bind-value credential-do-not-log")
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1

    async def ingest(self, batch):
        """Retain synthetic inputs so configured reproducibility can be tested."""
        self.batches.append([{key: value for key, value in item.items() if key != "timestamp"} for item in batch])
        await self._request("ingest")

    async def get_patient_vitals(self, patient, *, limit):
        """Record query tuning before suspending like a repository wait."""
        self.patients.append((patient, limit))
        await self._request("patient_vitals")

    async def get_alerts(self, *, limit, patient_id=None):
        """Record occasional alert requests in the same bounded execution path."""
        self.alerts.append((patient_id, limit))
        await self._request("alerts")


async def test_stalled_requests_do_not_stop_offers_or_exceed_concurrency():
    """Two stalled requests leave all later slots counted as skipped with no pending backlog."""
    service = WorkloadService(blocked=True)
    metrics = CapturingMetrics(interval=0)
    scheduler = asyncio.create_task(
        traffic_generator.run_traffic_generator(service, interval=0.005, max_concurrency=2, symptom_metrics=metrics)
    )
    try:
        await wait_until(lambda: total(metrics, "TrafficOffered") >= 6)
        assert service.operations == ["ingest", "patient_vitals"]
        assert service.active == service.peak == 2
        assert total(metrics, "TrafficStarted") == 2
        assert total(metrics, "TrafficCompleted") == 0
        assert total(metrics, "TrafficSkipped") >= 4
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
    assert service.active == 0
    assert service.cancelled == 2
    assert total(metrics, "TrafficCompleted") == total(metrics, "TrafficCancelled") == 2
    assert total(metrics, "TrafficOffered") == total(metrics, "TrafficStarted") + total(metrics, "TrafficSkipped")


async def test_failed_request_does_not_stop_later_operations_or_log_bind_values(caplog):
    """A failed ingest releases its slot and future queries/alerts continue without sensitive logs."""
    metrics = CapturingMetrics(interval=0)
    service = WorkloadService(fail_ingest=True)
    scheduler = asyncio.create_task(
        traffic_generator.run_traffic_generator(service, interval=0.005, symptom_metrics=metrics)
    )
    try:
        await wait_until(lambda: len(service.operations) >= 3)
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
    assert service.operations[:3] == ["ingest", "patient_vitals", "alerts"]
    assert total(metrics, "TrafficFailed") >= 1
    assert total(metrics, "TrafficStarted") == total(metrics, "TrafficCompleted")
    assert service.active == 0
    assert "secret-bind-value" not in caplog.text
    assert "credential-do-not-log" not in caplog.text


async def test_event_loop_delay_skips_missed_deadlines_without_catchup_tasks(monkeypatch):
    """A simulated 90-second stall produces skipped slots and resumes one request per interval."""
    clock = SimpleNamespace(now=0.0, sleeps=0)
    scheduled = []

    async def advance_clock(delay):
        """Introduce exactly one scheduler stall while letting real child tasks execute."""
        clock.now += delay + (90 if clock.sleeps == 1 else 0)
        clock.sleeps += 1
        await asyncio.sleep(0)

    def create_task(coroutine, *, name):
        """Observe admissions at scheduler time while retaining real task cancellation behavior."""
        scheduled.append(clock.now)
        return asyncio.create_task(coroutine, name=name)

    monkeypatch.setattr(
        traffic_generator,
        "asyncio",
        SimpleNamespace(
            sleep=advance_clock,
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now),
            create_task=create_task,
            gather=asyncio.gather,
            CancelledError=asyncio.CancelledError,
        ),
    )
    service = WorkloadService(cancel_after=3)
    metrics = CapturingMetrics(interval=0)
    with pytest.raises(asyncio.CancelledError):
        await traffic_generator.run_traffic_generator(service, interval=10, symptom_metrics=metrics)
    assert scheduled == [0, 100, 110]
    assert total(metrics, "TrafficOffered") == 12
    assert total(metrics, "TrafficSkipped") == 9
    assert total(metrics, "TrafficStarted") == total(metrics, "TrafficCompleted") == 3


async def test_fixed_seed_and_patient_reproduce_inputs_without_changing_global_random_state():
    """Identical tuning repeats values and query limits across independent workload runs."""
    random_state = traffic_generator.random.getstate()
    services = [WorkloadService(cancel_after=15), WorkloadService(cancel_after=15)]
    for service in services:
        with pytest.raises(asyncio.CancelledError):
            await traffic_generator.run_traffic_generator(
                service, interval=0, patient_id="synthetic-fixed", query_limit=37, seed=0
            )
    assert services[0].batches == services[1].batches
    assert all(reading["patient_id"] == "synthetic-fixed" for batch in services[0].batches for reading in batch)
    assert set(services[0].patients) == {("synthetic-fixed", 37)}
    assert set(services[0].alerts) == {("synthetic-fixed", 10)}
    assert traffic_generator.random.getstate() == random_state


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval": -1},
        {"interval": float("inf")},
        {"max_concurrency": 0},
        {"max_concurrency": float("inf")},
        {"query_limit": 0},
        {"query_limit": 1.5},
    ],
)
async def test_invalid_direct_scheduler_tuning_is_rejected_before_work(kwargs):
    """Direct coroutine callers cannot bypass the settings bounds to create invalid workloads."""
    service = WorkloadService()
    with pytest.raises(ValueError):
        await traffic_generator.run_traffic_generator(service, **kwargs)
    assert service.operations == []
