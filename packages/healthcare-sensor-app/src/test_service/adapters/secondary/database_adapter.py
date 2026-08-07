import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from test_service.config import AppSettings
from test_service.ports.interfaces.database import DatabasePort
from test_service.services.fault_state import (
    begin_environment_database_leak,
    close_environment_leaked_connections,
    environment_database_leaks_enabled,
    environment_leaked_connections,
    finish_environment_database_leak,
    register_environment_database_leak,
    retain_environment_leaked_connection,
)

logger = logging.getLogger(__name__)

_leaked_connections = environment_leaked_connections


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
        register_environment_database_leak(self)

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

    async def leaky_session(self) -> AsyncGenerator[AsyncSession]:
        session = self._session_factory()
        if not environment_database_leaks_enabled() or not self._fault_db_leak:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()
            return

        # The session is never closed, so its connection stays checked out for
        # the lifetime of the process. Reset tracks it to make recovery possible.
        if begin_environment_database_leak(self):
            try:
                retain_environment_leaked_connection(self, session)
            finally:
                finish_environment_database_leak()
        logger.warning(
            "DB session not returned to the pool",
            extra={"pool_checked_out": self._engine.pool.checkedout()},
        )
        yield session

    def checked_out_connections(self) -> int:
        return self._engine.pool.checkedout()

    def pool_size(self) -> int:
        return self._engine.pool.size()

    async def dispose(self) -> None:
        await close_environment_leaked_connections()
        await self._engine.dispose()
