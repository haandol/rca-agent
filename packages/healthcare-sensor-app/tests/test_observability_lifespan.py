"""Lifespan integration for observation, bounded traffic and complete resource cleanup."""

import asyncio
import importlib
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import APIRouter

from test_service.adapters.secondary.database_adapter import SqlAlchemyDatabaseAdapter
from test_service.config import get_settings
from test_service.di.app_container import AppContainer
from test_service.services.sensor import SensorService

from .test_bounded_observability import CapturingMetrics, Repository, wait_until
from .test_bounded_workload import total


class ObservedDatabase(SqlAlchemyDatabaseAdapter):
    """Exercise the actual adapter type gate without opening a PostgreSQL connection."""

    def __init__(self, *, observer_failure=False, schema_failure=False):
        """Provide controllable startup/observer failures and record disposal ordering."""
        self._engine = SimpleNamespace(begin=self.begin)
        self.entered = asyncio.Event()
        self.drained = False
        self.disposed = False
        self.observer_failure = observer_failure
        self.schema_failure = schema_failure
        self.stop_event = None
        self.interval = None

    @asynccontextmanager
    async def begin(self):
        """Keep the existing schema initialization path observable without a real database."""
        yield SimpleNamespace(run_sync=self.create_tables)

    async def create_tables(self, callback):
        """Simulate startup failure before any background work is admitted."""
        if self.schema_failure:
            raise RuntimeError("schema initialization failed")

    async def observe(self, stop_event, interval=5):
        """Retain the worker's async interface and expose cleanup after cancellation."""
        self.stop_event = stop_event
        self.interval = interval
        self.entered.set()
        try:
            if self.observer_failure:
                raise RuntimeError("observer failed")
            await asyncio.Future()
        finally:
            self.drained = True

    async def dispose(self):
        """Require active observation to drain before the adapter releases its resources."""
        assert not self.entered.is_set() or self.drained
        self.disposed = True


class LifecycleContainer:
    """A narrow container double retaining the production service and metrics implementations."""

    def __init__(self, database, **overrides):
        """Use short test intervals with real blocked operations to expose lifecycle ordering."""
        self.database = database
        self.settings = replace(
            get_settings(),
            **{
                "traffic_enabled": True,
                "traffic_interval_seconds": 0.005,
                "traffic_max_concurrency": 2,
                "metric_flush_interval_seconds": 0.005,
                "db_observability_enabled": True,
                "db_observability_interval_seconds": 0.007,
                **overrides,
            },
        )
        self.symptom_metrics = CapturingMetrics()
        self.repository = Repository(blocked=True)
        self.sensor_service = SensorService(self.repository, self.symptom_metrics)
        self.cleaned = False

    async def cleanup(self):
        """Dispose the adapter only after its owned workload has released in-flight readings."""
        self.symptom_metrics.flush()
        await self.database.dispose()
        self.cleaned = True


@pytest.fixture
def lifecycle_module(monkeypatch):
    """Import lifespan without process-wide telemetry setup or production adapter construction."""
    from test_service import telemetry

    monkeypatch.setattr(telemetry, "setup_logging", lambda settings: None)
    monkeypatch.setattr(telemetry, "setup_telemetry", lambda app, settings: None)
    monkeypatch.setattr(AppContainer, "create_router", lambda self: APIRouter())
    return importlib.import_module("test_service.main")


def install_container(monkeypatch, module, container):
    """Replace only the lifespan's collaborators, leaving its scheduling and cleanup intact."""
    monkeypatch.setattr(module, "container", container)
    monkeypatch.setattr(module, "containers", [container])


def assert_background_drained():
    """Check that shutdown leaves no owned observer, publisher, scheduler or request task alive."""
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("healthcare-") and not task.done()]


async def test_lifespan_flushes_stalled_requests_and_drops_before_clean_shutdown(lifecycle_module, monkeypatch):
    """Real service stalls expose ingress/drops; shutdown drains tasks before the final zero gauge."""
    database = ObservedDatabase()
    container = LifecycleContainer(database)
    install_container(monkeypatch, lifecycle_module, container)
    metrics = container.symptom_metrics
    async with lifecycle_module.lifespan(None):
        await database.entered.wait()
        await wait_until(lambda: total(metrics, "TrafficSkipped") >= 2)
        assert database.interval == 0.007
        assert any(payload["VitalIngestInFlight"] > 0 for payload in metrics.emitted)
        assert total(metrics, "VitalIngestAttempts") == 0
        assert len(container.repository.operations) == 2
    assert container.cleaned and database.disposed and database.drained
    assert database.stop_event.is_set()
    assert metrics.emitted[-1]["VitalIngestInFlight"] == 0
    assert total(metrics, "TrafficStarted") == total(metrics, "TrafficCompleted") == 2
    assert total(metrics, "TrafficCancelled") == 2
    assert any(payload.get("PatientVitalsQueryDuration") for payload in metrics.emitted)
    assert_background_drained()


async def test_disabled_workload_and_observer_still_publish_heartbeat(lifecycle_module, monkeypatch):
    """Disabled optional work starts no requests or DB observer while metric publication stays live."""
    database = ObservedDatabase()
    container = LifecycleContainer(database, traffic_enabled=False, db_observability_enabled=False)
    install_container(monkeypatch, lifecycle_module, container)
    async with lifecycle_module.lifespan(None):
        await wait_until(lambda: bool(container.symptom_metrics.emitted))
        assert container.repository.operations == []
        assert not database.entered.is_set()
    assert container.cleaned
    assert_background_drained()


async def test_non_sqlalchemy_database_is_not_given_observer_work(lifecycle_module, monkeypatch):
    """Test adapters keep their existing interface even when the optional observation flag is enabled."""
    calls = []

    async def dispose():
        """Mark cleanup of an adapter that intentionally exposes no observation API."""
        calls.append("disposed")

    container = LifecycleContainer(SimpleNamespace(dispose=dispose), traffic_enabled=False)
    install_container(monkeypatch, lifecycle_module, container)
    async with lifecycle_module.lifespan(None):
        await asyncio.sleep(0)
    assert calls == ["disposed"]
    assert_background_drained()


@pytest.mark.parametrize("cancel", [False, True])
async def test_body_failure_or_cancellation_still_drains_requests(lifecycle_module, monkeypatch, cancel):
    """Application errors and cancellation both pass through the same complete shutdown path."""
    container = LifecycleContainer(ObservedDatabase())
    install_container(monkeypatch, lifecycle_module, container)
    entered = asyncio.Event()

    async def run_lifespan():
        """Raise from the serving phase only after the background request is really in flight."""
        async with lifecycle_module.lifespan(None):
            await container.repository.entered.wait()
            entered.set()
            if cancel:
                await asyncio.Future()
            raise RuntimeError("serving failed")

    task = asyncio.create_task(run_lifespan())
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await task
    assert container.cleaned
    assert container.symptom_metrics.emitted[-1]["VitalIngestInFlight"] == 0
    assert_background_drained()


async def test_observer_and_cleanup_failures_do_not_skip_other_cleanup(lifecycle_module, monkeypatch):
    """Collect shutdown failures only after every container and background task has been handled."""
    database = ObservedDatabase(observer_failure=True)
    container = LifecycleContainer(database, traffic_enabled=False)
    install_container(monkeypatch, lifecycle_module, container)
    cleanup_calls = []

    async def broken_cleanup():
        """Simulate one failed container without giving it ownership of the others."""
        cleanup_calls.append("broken")
        raise RuntimeError("cleanup failed")

    async def final_cleanup():
        """Prove a prior observer or container error cannot skip remaining cleanup."""
        cleanup_calls.append("final")

    monkeypatch.setattr(
        lifecycle_module,
        "containers",
        [SimpleNamespace(cleanup=broken_cleanup), container, SimpleNamespace(cleanup=final_cleanup)],
    )
    with pytest.raises(BaseExceptionGroup) as caught:
        async with lifecycle_module.lifespan(None):
            await database.entered.wait()
    assert {str(error) for error in caught.value.exceptions} == {"observer failed", "cleanup failed"}
    assert cleanup_calls == ["broken", "final"]
    assert container.cleaned and database.disposed
    assert_background_drained()


async def test_schema_startup_failure_still_disposes_database(lifecycle_module, monkeypatch):
    """A failed pre-yield setup releases the adapter even though no worker was started."""
    container = LifecycleContainer(ObservedDatabase(schema_failure=True))
    install_container(monkeypatch, lifecycle_module, container)
    with pytest.raises(RuntimeError, match="schema initialization failed"):
        async with lifecycle_module.lifespan(None):
            pytest.fail("startup must fail before serving")
    assert container.cleaned
    assert_background_drained()


async def test_container_metric_flush_failure_cannot_skip_database_disposal():
    """Production container cleanup closes the database even if stdout emission is unavailable."""
    container = AppContainer()
    database = ObservedDatabase()

    def fail_flush():
        """Represent a failed metrics sink after the application has stopped serving."""
        raise OSError("metrics sink unavailable")

    container._database = database
    container._symptom_metrics = SimpleNamespace(flush=fail_flush)
    with pytest.raises(OSError, match="metrics sink unavailable"):
        await container.cleanup()
    assert database.disposed
