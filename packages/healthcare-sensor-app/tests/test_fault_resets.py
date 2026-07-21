import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from test_service.adapters.primary.fault.fault_controller import FaultController
from test_service.adapters.secondary import database_adapter
from test_service.config import AppSettings
from test_service.middleware import FaultFlagMiddleware
from test_service.ports.interfaces.database import DatabasePort
from test_service.services import fault
from test_service.services.fault import FaultInjectionService


class FakeDatabase(DatabasePort):
    def __init__(self) -> None:
        self.checked_out = 0

    async def session(self) -> AsyncGenerator[Any]:
        if False:
            yield

    async def leaky_session(self) -> AsyncGenerator[Any]:
        if False:
            yield

    def checked_out_connections(self) -> int:
        return self.checked_out

    def pool_size(self) -> int:
        return 5

    async def dispose(self) -> None:
        return None


class FakeConnection:
    def __init__(self, *, close_fails: bool = False) -> None:
        self.closed = False
        self.close_attempts = 0
        self.close_fails = close_fails

    async def close(self) -> None:
        self.close_attempts += 1
        if self.close_fails:
            raise RuntimeError("close failed")
        self.closed = True


class FakePool:
    def checkedout(self) -> int:
        return 0

    def size(self) -> int:
        return 5


class FakeEngine:
    def __init__(self) -> None:
        self.pool = FakePool()
        self.connections: list[FakeConnection] = []

    async def connect(self) -> FakeConnection:
        connection = FakeConnection()
        self.connections.append(connection)
        return connection

    async def dispose(self) -> None:
        return None


class BlockingFakeEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.connect_started = asyncio.Event()
        self.release_connect = asyncio.Event()

    async def connect(self) -> FakeConnection:
        self.connect_started.set()
        await self.release_connect.wait()
        return await super().connect()


class ExistingLeakBlockingFakeEngine(FakeEngine):
    def __init__(self, existing_leak: FakeConnection) -> None:
        super().__init__()
        self.existing_leak = existing_leak
        self.connect_started = asyncio.Event()

    async def connect(self) -> FakeConnection:
        self.connect_started.set()
        while not self.existing_leak.closed:
            await asyncio.sleep(0)
        return await super().connect()


class FakeSession:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeThread:
    def __init__(self, *, stops_when_joined: bool = True) -> None:
        self.alive = True
        self.join_timeouts: list[float | None] = []
        self._stops_when_joined = stops_when_joined

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)
        if self._stops_when_joined:
            self.alive = False


def make_settings(**overrides: object) -> AppSettings:
    settings = AppSettings(
        database_url="postgresql+asyncpg://unused:unused@localhost/unused",
        otel_exporter_otlp_endpoint="http://localhost:4317",
        otel_service_name="test",
        log_level="INFO",
        fault_injection_enabled=True,
        db_pool_size=5,
        db_max_overflow=10,
        fault_db_leak=False,
        fault_slow_query_ms=0,
        fault_error_rate=0.0,
    )
    return replace(settings, **overrides)


def make_app(
    service: FaultInjectionService,
    settings: AppSettings | None = None,
    *,
    with_fault_middleware: bool = False,
) -> FastAPI:
    app = FastAPI()
    active_settings = settings or make_settings()
    if with_fault_middleware:
        app.add_middleware(FaultFlagMiddleware, settings=active_settings)
    app.include_router(FaultController(service, active_settings).router)

    @app.get("/probe")
    async def probe() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.fixture
def fake_database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def fault_service(fake_database: FakeDatabase) -> FaultInjectionService:
    return FaultInjectionService(fake_database)


@pytest.fixture
async def fault_client(fault_service: FaultInjectionService):
    transport = ASGITransport(app=make_app(fault_service))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_db_leak_reset_closes_connections_then_is_idempotent_noop(
    fault_client: AsyncClient,
    fake_database: FakeDatabase,
) -> None:
    connections = [FakeConnection(), FakeConnection()]
    fault._leaked_sessions.extend(connections)
    fake_database.checked_out = 3

    response = await fault_client.post("/fault/db-leak/reset")

    assert response.status_code == 200
    assert response.json() == {"closed": 2, "pool_checked_out": 3}
    assert all(connection.closed for connection in connections)
    assert fault._leaked_sessions == []

    second_response = await fault_client.post("/fault/db-leak/reset")

    assert second_response.status_code == 200
    assert second_response.json() == {"closed": 0, "pool_checked_out": 3}


@pytest.mark.asyncio
async def test_high_cpu_reset_stops_threads_then_is_idempotent_noop(fault_client: AsyncClient) -> None:
    threads = [FakeThread(), FakeThread()]
    fault._cpu_threads.extend(threads)

    response = await fault_client.post("/fault/high-cpu/reset")

    assert response.status_code == 200
    assert response.json() == {"status": "stopped", "threads_stopped": 2}
    assert fault._cpu_stop_event.is_set()
    assert [thread.join_timeouts for thread in threads] == [[5], [5]]
    assert fault._cpu_threads == []

    second_response = await fault_client.post("/fault/high-cpu/reset")

    assert second_response.status_code == 200
    assert second_response.json() == {"status": "not_running"}


@pytest.mark.asyncio
async def test_high_memory_reset_releases_ballasts_then_is_idempotent_noop(fault_client: AsyncClient) -> None:
    fault._memory_ballast.extend([b"first", b"second"])

    response = await fault_client.post("/fault/high-memory/reset")

    assert response.status_code == 200
    assert response.json() == {"released_ballasts": 2}
    assert fault._memory_ballast == []

    second_response = await fault_client.post("/fault/high-memory/reset")

    assert second_response.status_code == 200
    assert second_response.json() == {"released_ballasts": 0}


@pytest.mark.asyncio
async def test_slow_query_reset_signals_and_joins_then_is_idempotent_noop(fault_client: AsyncClient) -> None:
    thread = FakeThread()
    fault._slow_query_thread = thread

    response = await fault_client.post("/fault/slow-query/reset")

    assert response.status_code == 200
    assert response.json() == {"status": "stopped"}
    assert fault._slow_query_stop_event.is_set()
    assert thread.join_timeouts == [35]
    assert fault._slow_query_thread is None

    second_response = await fault_client.post("/fault/slow-query/reset")

    assert second_response.status_code == 200
    assert second_response.json() == {"status": "not_running"}


@pytest.mark.asyncio
async def test_slow_query_reset_does_not_report_stopped_while_query_thread_is_alive(
    fault_client: AsyncClient,
) -> None:
    thread = FakeThread(stops_when_joined=False)
    fault._slow_query_thread = thread

    response = await fault_client.post("/fault/slow-query/reset")

    assert thread.join_timeouts == [35]
    assert response.json() == {"status": "stop_timeout"}
    assert thread.is_alive() is True
    assert fault._slow_query_thread is thread


@pytest.mark.asyncio
async def test_db_leak_reset_clears_environment_driven_leaks(fault_client: AsyncClient) -> None:
    environment_leak = FakeConnection()
    database_adapter._leaked_connections.append(environment_leak)

    response = await fault_client.post("/fault/db-leak/reset")

    assert response.status_code == 200
    assert environment_leak.closed is True
    assert database_adapter._leaked_connections == []


@pytest.mark.asyncio
async def test_db_leak_reset_waits_for_environment_leak_acquisition_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = BlockingFakeEngine()
    monkeypatch.setattr(database_adapter, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(database_adapter, "async_sessionmaker", lambda *args, **kwargs: FakeSession)
    settings = make_settings(fault_db_leak=True)
    adapter = database_adapter.SqlAlchemyDatabaseAdapter(settings)

    async def use_session() -> None:
        async for _ in adapter.session():
            pass

    session_task = asyncio.create_task(use_session())
    await engine.connect_started.wait()

    service = FaultInjectionService(adapter)
    transport = ASGITransport(app=make_app(service, settings))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reset_task = asyncio.create_task(client.post("/fault/db-leak/reset"))
        await asyncio.sleep(0)
        assert reset_task.done() is False
        assert adapter._fault_db_leak is False

        engine.release_connect.set()
        await session_task
        reset_response = await reset_task

    assert reset_response.status_code == 200
    assert reset_response.json() == {"closed": 0, "pool_checked_out": 0}
    assert engine.connections[0].closed is True
    assert database_adapter._leaked_connections == []


@pytest.mark.asyncio
async def test_db_leak_reset_closes_existing_leak_before_waiting_for_blocked_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_leak = FakeConnection()
    engine = ExistingLeakBlockingFakeEngine(existing_leak)
    monkeypatch.setattr(database_adapter, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(database_adapter, "async_sessionmaker", lambda *args, **kwargs: FakeSession)
    settings = make_settings(fault_db_leak=True)
    adapter = database_adapter.SqlAlchemyDatabaseAdapter(settings)
    database_adapter._leaked_connections.append(existing_leak)

    async def use_session() -> None:
        async for _ in adapter.session():
            pass

    session_task = asyncio.create_task(use_session())
    await engine.connect_started.wait()

    service = FaultInjectionService(adapter)
    transport = ASGITransport(app=make_app(service, settings))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reset_response = await asyncio.wait_for(client.post("/fault/db-leak/reset"), timeout=1)
    await session_task

    assert reset_response.status_code == 200
    assert reset_response.json() == {"closed": 1, "pool_checked_out": 0}
    assert existing_leak.closed is True
    assert engine.connections[0].closed is True
    assert database_adapter._leaked_connections == []


@pytest.mark.asyncio
async def test_db_leak_reset_retains_close_failures_and_returns_non_2xx(
    fault_client: AsyncClient,
) -> None:
    explicit_leak = FakeConnection(close_fails=True)
    environment_leak = FakeConnection(close_fails=True)
    fault._leaked_sessions.append(explicit_leak)
    database_adapter._leaked_connections.append(environment_leak)

    response = await fault_client.post("/fault/db-leak/reset")

    assert response.status_code == 500
    assert response.json() == {
        "closed": 0,
        "pool_checked_out": 0,
        "status": "failed",
        "failed": 2,
    }
    assert fault._leaked_sessions == [explicit_leak]
    assert database_adapter._leaked_connections == [environment_leak]

    explicit_leak.close_fails = False
    environment_leak.close_fails = False
    retry_response = await fault_client.post("/fault/db-leak/reset")

    assert retry_response.status_code == 200
    assert retry_response.json() == {"closed": 2, "pool_checked_out": 0}
    assert fault._leaked_sessions == []
    assert database_adapter._leaked_connections == []

    noop_response = await fault_client.post("/fault/db-leak/reset")

    assert noop_response.status_code == 200
    assert noop_response.json() == {"closed": 0, "pool_checked_out": 0}


@pytest.mark.asyncio
async def test_db_leak_reset_disables_future_environment_driven_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    monkeypatch.setattr(database_adapter, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(database_adapter, "async_sessionmaker", lambda *args, **kwargs: FakeSession)
    settings = make_settings(fault_db_leak=True)
    adapter = database_adapter.SqlAlchemyDatabaseAdapter(settings)

    async for _ in adapter.session():
        pass

    assert len(engine.connections) == 1
    assert database_adapter._leaked_connections == engine.connections

    service = FaultInjectionService(adapter)
    transport = ASGITransport(app=make_app(service, settings))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reset_response = await client.post("/fault/db-leak/reset")

    assert reset_response.status_code == 200
    assert reset_response.json() == {"closed": 1, "pool_checked_out": 0}
    assert engine.connections[0].closed is True
    assert database_adapter._leaked_connections == []

    async for _ in adapter.session():
        pass

    assert len(engine.connections) == 1
    assert database_adapter._leaked_connections == []

    new_engine = FakeEngine()
    monkeypatch.setattr(database_adapter, "create_async_engine", lambda *args, **kwargs: new_engine)
    new_adapter = database_adapter.SqlAlchemyDatabaseAdapter(settings)

    async for _ in new_adapter.session():
        pass

    assert new_adapter._fault_db_leak is False
    assert new_engine.connections == []


@pytest.mark.asyncio
async def test_slow_query_reset_clears_environment_driven_latency(
    fault_service: FaultInjectionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("test_service.middleware.fault_flag.asyncio.sleep", record_sleep)
    settings = make_settings(fault_slow_query_ms=250)
    transport = ASGITransport(app=make_app(fault_service, settings, with_fault_middleware=True))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reset_response = await client.post("/fault/slow-query/reset")
        probe_response = await client.get("/probe")

    assert reset_response.status_code == 200
    assert probe_response.status_code == 200
    assert delays == []


@pytest.mark.xfail(
    strict=True,
    reason="no reset endpoint clears FAULT_ERROR_RATE captured by FaultFlagMiddleware",
)
@pytest.mark.asyncio
async def test_fault_reset_clears_environment_driven_error_rate(fault_service: FaultInjectionService) -> None:
    settings = make_settings(fault_error_rate=1.0)
    transport = ASGITransport(app=make_app(fault_service, settings, with_fault_middleware=True))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reset_response = await client.post("/fault/db-leak/reset")
        probe_response = await client.get("/probe")

    assert reset_response.status_code == 200
    assert probe_response.status_code == 200
