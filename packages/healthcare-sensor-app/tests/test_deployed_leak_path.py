"""Covers the leak path that a deployed FAULT_DB_LEAK revision activates.

The read path deliberately borrows a session it never returns when the flag is
on. These tests pin the two properties that keep the demo honest: the flag off
means no leak at all, and the flag on means reset can still recover.
"""

from dataclasses import replace

import pytest

from test_service.adapters.secondary import database_adapter
from test_service.adapters.secondary.sensor_repository.sqlalchemy_sensor_repository import (
    SqlAlchemySensorReadingRepository,
)
from test_service.config import AppSettings
from test_service.services.fault import FaultInjectionService


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
        deployed_revision="test",
    )
    return replace(settings, **overrides)


class FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.committed = False
        self.rolled_back = False

    async def execute(self, _stmt: object) -> object:
        class Result:
            @staticmethod
            def scalars():
                class Scalars:
                    @staticmethod
                    def all() -> list:
                        return []

                return Scalars()

        return Result()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def close(self) -> None:
        self.closed = True


class FakePool:
    def checkedout(self) -> int:
        return 0

    def size(self) -> int:
        return 5


class FakeEngine:
    def __init__(self) -> None:
        self.pool = FakePool()
        self.connections: list[object] = []

    async def connect(self) -> object:
        return object()

    async def dispose(self) -> None:
        return None


@pytest.fixture
def adapter_factory(monkeypatch: pytest.MonkeyPatch):
    def build(*, fault_db_leak: bool) -> tuple[database_adapter.SqlAlchemyDatabaseAdapter, list[FakeSession]]:
        sessions: list[FakeSession] = []

        def session_factory() -> FakeSession:
            session = FakeSession()
            sessions.append(session)
            return session

        monkeypatch.setattr(database_adapter, "create_async_engine", lambda *a, **k: FakeEngine())
        monkeypatch.setattr(database_adapter, "async_sessionmaker", lambda *a, **k: session_factory)
        adapter = database_adapter.SqlAlchemyDatabaseAdapter(make_settings(fault_db_leak=fault_db_leak))
        return adapter, sessions

    return build


@pytest.mark.asyncio
async def test_read_path_does_not_retain_its_session_when_the_leak_flag_is_off(adapter_factory) -> None:
    adapter, sessions = adapter_factory(fault_db_leak=False)
    repository = SqlAlchemySensorReadingRepository(adapter)

    await repository.find_by_patient("P-001")

    assert len(sessions) == 1
    assert database_adapter._leaked_connections == []


@pytest.mark.asyncio
async def test_read_path_closes_its_session_on_generator_exit_when_the_flag_is_off(adapter_factory) -> None:
    adapter, sessions = adapter_factory(fault_db_leak=False)

    generator = adapter.leaky_session()
    async for _ in generator:
        break
    await generator.aclose()

    assert sessions[0].closed is True
    assert database_adapter._leaked_connections == []


@pytest.mark.asyncio
async def test_leaked_session_survives_generator_exit_when_the_flag_is_on(adapter_factory) -> None:
    # Closing the generator must not release the connection — that is exactly
    # what makes the leak outlive the request that created it.
    adapter, sessions = adapter_factory(fault_db_leak=True)

    generator = adapter.leaky_session()
    async for _ in generator:
        break
    await generator.aclose()

    assert sessions[0].closed is False
    assert database_adapter._leaked_connections == sessions


@pytest.mark.asyncio
async def test_read_path_leaks_its_session_when_the_deployed_flag_is_on(adapter_factory) -> None:
    adapter, sessions = adapter_factory(fault_db_leak=True)
    repository = SqlAlchemySensorReadingRepository(adapter)

    await repository.find_by_patient("P-001")
    await repository.find_by_patient("P-002")

    assert [session.closed for session in sessions] == [False, False]
    assert database_adapter._leaked_connections == sessions


@pytest.mark.asyncio
async def test_leaks_accumulate_per_request_rather_than_all_at_once(adapter_factory) -> None:
    # Gradual accumulation is what makes the metric show a trend tied to the
    # deployment instead of a single step change.
    adapter, _ = adapter_factory(fault_db_leak=True)
    repository = SqlAlchemySensorReadingRepository(adapter)

    counts = []
    for _ in range(3):
        await repository.find_by_patient("P-001")
        counts.append(len(database_adapter._leaked_connections))

    assert counts == [1, 2, 3]


@pytest.mark.asyncio
async def test_reset_closes_sessions_leaked_by_the_deployed_read_path(adapter_factory) -> None:
    adapter, sessions = adapter_factory(fault_db_leak=True)
    repository = SqlAlchemySensorReadingRepository(adapter)
    await repository.find_by_patient("P-001")

    result = await FaultInjectionService(adapter).reset_leaked_connections()

    assert result["closed"] == 1
    assert sessions[0].closed is True
    assert database_adapter._leaked_connections == []


@pytest.mark.asyncio
async def test_read_path_stops_leaking_after_reset(adapter_factory) -> None:
    adapter, _ = adapter_factory(fault_db_leak=True)
    repository = SqlAlchemySensorReadingRepository(adapter)
    await repository.find_by_patient("P-001")
    await FaultInjectionService(adapter).reset_leaked_connections()

    await repository.find_by_patient("P-002")
    await repository.find_by_patient("P-003")

    assert database_adapter._leaked_connections == []


@pytest.mark.asyncio
async def test_write_path_never_leaks_regardless_of_the_flag(adapter_factory) -> None:
    adapter, sessions = adapter_factory(fault_db_leak=True)

    async for session in adapter.session():
        assert isinstance(session, FakeSession)

    assert sessions[0].closed is True
