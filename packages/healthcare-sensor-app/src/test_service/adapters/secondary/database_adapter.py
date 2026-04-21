import contextlib
import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine

from test_service.config import AppSettings
from test_service.ports.interfaces.database import DatabasePort

logger = logging.getLogger(__name__)

_leaked_connections: list[AsyncConnection] = []


class SqlAlchemyDatabaseAdapter(DatabasePort):
    def __init__(self, settings: AppSettings) -> None:
        self._engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)
        self._fault_db_leak = settings.fault_db_leak

    @property
    def engine(self):
        return self._engine

    @property
    def session_factory(self):
        return self._session_factory

    async def session(self) -> AsyncGenerator[AsyncSession]:
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
            if self._fault_db_leak:
                conn = await self._engine.connect()
                _leaked_connections.append(conn)
                logger.warning(
                    "DB connection not returned (FAULT_DB_LEAK enabled)",
                    extra={"pool_checked_out": self._engine.pool.checkedout()},
                )

    async def leaky_session(self) -> AsyncGenerator[AsyncSession]:
        session = self._session_factory()
        yield session

    def checked_out_connections(self) -> int:
        return self._engine.pool.checkedout()

    def pool_size(self) -> int:
        return self._engine.pool.size()

    async def dispose(self) -> None:
        for c in _leaked_connections:
            with contextlib.suppress(Exception):
                await c.close()
        _leaked_connections.clear()
        await self._engine.dispose()
