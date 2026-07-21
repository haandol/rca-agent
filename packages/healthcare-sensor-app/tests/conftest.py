import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from test_service.adapters.secondary.sensor_repository.models import Base
from test_service.adapters.secondary.sensor_repository.sqlalchemy_sensor_repository import (
    SqlAlchemySensorReadingRepository,
)
from test_service.ports.interfaces.database import DatabasePort
from test_service.services import fault
from test_service.services.fault_state import reset_environment_database_leak_state_for_testing

TEST_DB_URL = "sqlite+aiosqlite://"


def _clear_fault_state() -> None:
    fault._cpu_stop_event.set()
    for thread in fault._cpu_threads:
        if thread.is_alive():
            thread.join(timeout=0.1)
    fault._cpu_threads.clear()

    fault._slow_query_stop_event.set()
    if fault._slow_query_thread and fault._slow_query_thread.is_alive():
        fault._slow_query_thread.join(timeout=0.1)
    fault._slow_query_thread = None

    fault._leaked_sessions.clear()
    fault._memory_ballast.clear()
    reset_environment_database_leak_state_for_testing()
    fault._cpu_stop_event.clear()
    fault._slow_query_stop_event.clear()


@pytest.fixture(autouse=True)
def clean_fault_state():
    _clear_fault_state()
    yield
    _clear_fault_state()


class InMemoryDatabaseAdapter(DatabasePort):
    def __init__(self, engine, session_factory):
        self._engine = engine
        self._session_factory = session_factory

    async def session(self):
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def leaky_session(self):
        session = self._session_factory()
        yield session

    def checked_out_connections(self) -> int:
        return 0

    def pool_size(self) -> int:
        return 5

    async def dispose(self) -> None:
        await self._engine.dispose()


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def app(db_engine):
    from test_service.di.app_container import AppContainer

    test_session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    test_db = InMemoryDatabaseAdapter(db_engine, test_session_factory)

    app_container = AppContainer()
    app_container._database = test_db
    app_container._sensor_repository = SqlAlchemySensorReadingRepository(test_db)

    from fastapi import FastAPI

    test_app = FastAPI()
    test_app.include_router(app_container.create_router())

    yield test_app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
