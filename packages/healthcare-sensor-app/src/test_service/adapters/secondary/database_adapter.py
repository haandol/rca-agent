import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from test_service.config import AppSettings
from test_service.ports.interfaces.database import DatabasePort
from test_service.revision.manifest import source_manifest
from test_service.revision.session import close_session, session_scope
from test_service.services.db_observability import current_operation, install_hooks
from test_service.services.write_diagnostics import write_contract

logger = logging.getLogger(__name__)


class SqlAlchemyDatabaseAdapter(DatabasePort):
    def __init__(self, settings: AppSettings) -> None:
        """Own bounded pools and session registries, verifying source identity before use."""
        self._settings = settings
        self._owned_sessions: set[AsyncSession] = set()
        self._observer_engine = None
        self._observer_lock = asyncio.Lock()
        self._pool_timeout = getattr(settings, "db_pool_timeout_seconds", 30)
        self._statement_timeout = getattr(settings, "db_statement_timeout_ms", 0)
        self._engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            pool_timeout=self._pool_timeout,
            hide_parameters=True,
            connect_args={
                "server_settings": {
                    "statement_timeout": str(self._statement_timeout),
                    "application_name": "healthcare-service",
                }
            },
        )
        if getattr(settings, "db_observability_enabled", False):
            install_hooks(self._engine)
        self._session_factory = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)
        logger.info(
            "source_manifest",
            extra={"event": "source_manifest", "observed_at": datetime.now(UTC).isoformat(), **source_manifest()},
        )
        logger.info("write_contract", extra={"event": "write_contract", **write_contract()})
        logger.info(
            "db_pool_config",
            extra={
                "event": "db_pool_config",
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout_seconds": self._pool_timeout,
                "statement_timeout_ms": self._statement_timeout,
            },
        )

    @property
    def engine(self):
        """Expose the owned engine for schema setup without transferring disposal ownership."""
        return self._engine

    @property
    def session_factory(self):
        """Expose session creation for legacy callers that manage their own lifetime."""
        return self._session_factory

    async def session(self) -> AsyncGenerator[AsyncSession]:
        """Compatibility iterator; new consumers must use session_context."""
        async with self.session_context() as session:
            yield session

    @asynccontextmanager
    async def session_context(self):
        """Deliver consumer exceptions to the compiled session implementation."""
        async with AsyncExitStack() as stack:
            # Acquire inside this context, so all query/body exceptions have a
            # deterministic owner. Includes pool queue and physical connect time.
            started = time.perf_counter()
            try:
                session = await stack.enter_async_context(session_scope(self._session_factory, self._owned_sessions))
                if isinstance(session, AsyncSession):
                    await session.connection()
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                operation = current_operation()
                if operation is not None:
                    operation.acquire_time_ms += elapsed
                if getattr(self._settings, "db_observability_enabled", False):
                    logger.info(
                        "db_acquire",
                        extra={
                            "event": "db_acquire",
                            "request_id": operation.request_id if operation else None,
                            "elapsed_ms": elapsed,
                            "pool_checked_out": self.checked_out_connections(),
                        },
                    )
            yield session

    def checked_out_connections(self) -> int:
        """Report actual application-pool occupancy, excluding the independent observer pool."""
        return self._engine.pool.checkedout()

    def pool_size(self) -> int:
        """Expose configured persistent pool capacity without conflating overflow or server limits."""
        return self._engine.pool.size()

    async def dispose(self) -> None:
        """Close only this adapter's sessions, then dispose both owned pools."""
        errors = []
        for session in list(self._owned_sessions):
            try:
                await close_session(session)
                self._owned_sessions.discard(session)
            except Exception as exc:
                errors.append(exc)
        for engine in (self._engine, self._observer_engine):
            if engine is not None:
                try:
                    await engine.dispose()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("Owned database resource cleanup failed", errors)

    def _observation_engine(self):
        """Share one bounded observer pool so catalog reads never borrow writer capacity."""
        if self._observer_engine is None:
            self._observer_engine = create_async_engine(
                self._settings.database_url,
                pool_size=1,
                max_overflow=0,
                pool_timeout=2,
                hide_parameters=True,
                connect_args={
                    "server_settings": {"application_name": "healthcare-observer", "statement_timeout": "2000"}
                },
            )
        return self._observer_engine

    async def schema_snapshot(self) -> dict:
        """Observe the resolved INSERT relation on a separate read-only transaction.

        An independent connection remains usable after a writer transaction fails.
        Catalog rows describe the existing schema and never create missing columns.
        """
        async with self._observer_lock, self._observation_engine().connect() as conn, conn.begin():
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            result = await conn.execute(
                text("""
                    SELECT n.nspname AS schema_name, c.relname AS table_name,
                           a.attname AS column_name
                    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_attribute a ON a.attrelid = c.oid
                    WHERE c.oid = to_regclass('sensor_readings')
                      AND a.attnum > 0 AND NOT a.attisdropped
                    ORDER BY a.attnum
                """)
            )
            rows = list(result.mappings())
        snapshot = {
            "event": "db_schema_snapshot",
            "observed_at": datetime.now(UTC).isoformat(),
            "schema_name": rows[0]["schema_name"] if rows else None,
            "table_name": rows[0]["table_name"] if rows else None,
            "column_names": [row["column_name"] for row in rows],
        }
        logger.info("db_schema_snapshot", extra=snapshot)
        return snapshot

    async def wait_snapshot(self) -> dict:
        """Read wait/lock metadata with at most one independent observer backend.

        Never emit pg_stat_activity.query: SQL text may contain user parameters.
        A hash links repeated queries while blocker IDs establish the lock chain.
        """
        async with self._observer_lock:
            async with self._observation_engine().connect() as conn:
                result = await conn.execute(
                    text("""
                    SELECT pid, application_name, state, wait_event_type, wait_event,
                           xact_start::text, query_start::text,
                           pg_blocking_pids(pid) AS blocking_pids, md5(query) AS sql_hash
                    FROM pg_stat_activity
                    WHERE datname = current_database() AND pid <> pg_backend_pid()
                    ORDER BY pid LIMIT 100
                """)
                )
                activity = [dict(row) for row in result.mappings()]
                result = await conn.execute(
                    text("""
                    SELECT l.pid, l.locktype, l.mode, l.granted,
                           c.relname AS relation
                    FROM pg_locks l LEFT JOIN pg_class c ON c.oid = l.relation
                    WHERE l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
                    AND l.pid <> pg_backend_pid()
                    ORDER BY l.pid LIMIT 200
                """)
                )
                locks = [dict(row) for row in result.mappings()]
                result = await conn.execute(
                    text("""
                    SELECT current_setting('max_connections')::int AS server_max_connections,
                           (SELECT count(*) FROM pg_stat_activity
                            WHERE datname = current_database()) AS database_connections
                """)
                )
                capacity = dict(result.mappings().one())
            return {
                "event": "db_wait_snapshot",
                "sql_hash_algorithm": "md5",
                "activity": activity,
                "locks": locks,
                "pool_checked_out": self.checked_out_connections(),
                "pool_size": self.pool_size(),
                **capacity,
            }

    async def observe(self, stop_event: asyncio.Event, interval: float = 5) -> None:
        """Emit periodic bounded snapshots independently of request completion."""
        if not getattr(self._settings, "db_observability_enabled", False):
            return
        if interval <= 0:
            raise ValueError("Observer interval must be positive")
        while not stop_event.is_set():
            try:
                async with asyncio.timeout(5):
                    await self.schema_snapshot()
                    snapshot = await self.wait_snapshot()
                    logger.info("db_wait_snapshot", extra=snapshot)
            except Exception as exc:
                logger.warning("db_observer_error", extra={"error_type": type(exc).__name__})
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
