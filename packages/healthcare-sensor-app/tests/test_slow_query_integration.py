import asyncio
import os
import time
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from test_service.ports.interfaces.database import DatabasePort
from test_service.services import fault
from test_service.services.fault import FaultInjectionService

TEST_POSTGRES_DSN = os.getenv("TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="TEST_POSTGRES_DSN is not set; real PostgreSQL slow-query integration test skipped",
)


class IntegrationDatabase(DatabasePort):
    def __init__(self, dsn: str) -> None:
        self.engine = SimpleNamespace(url=make_url(dsn))

    async def session(self) -> AsyncGenerator[Any]:
        raise AssertionError("slow-query worker must not use DatabasePort.session()")
        yield

    async def leaky_session(self) -> AsyncGenerator[Any]:
        if False:
            yield

    def checked_out_connections(self) -> int:
        return 0

    def pool_size(self) -> int:
        return 0

    async def dispose(self) -> None:
        return None


async def _wait_for_active_slow_query(
    observer: asyncpg.Connection,
    application_name: str,
    *,
    timeout: float = 5,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = await observer.fetchval(
            """
            SELECT pid
            FROM pg_stat_activity
            WHERE application_name = $1
              AND state = 'active'
              AND query LIKE 'SELECT pg_sleep%'
            """,
            application_name,
        )
        if pid is not None:
            return int(pid)
        await asyncio.sleep(0.05)
    raise AssertionError("slow-query worker did not start pg_sleep before timeout")


@pytest.mark.asyncio
async def test_slow_query_worker_runs_and_reset_closes_real_postgres_connection() -> None:
    assert TEST_POSTGRES_DSN is not None
    application_name = f"rca_slow_query_test_{uuid.uuid4().hex}"
    base_url = make_url(TEST_POSTGRES_DSN)
    worker_url = base_url.set(query={**base_url.query, "application_name": application_name})
    service = FaultInjectionService(IntegrationDatabase(worker_url.render_as_string(hide_password=False)))
    observer_dsn = base_url.set(drivername="postgresql").render_as_string(hide_password=False)
    observer = await asyncpg.connect(observer_dsn)

    try:
        assert service.start_slow_query(1) == {
            "status": "started",
            "interval_seconds": 1,
        }
        worker_pid = await _wait_for_active_slow_query(observer, application_name)

        reset_result = await service.stop_slow_query()

        assert reset_result == {"status": "stopped"}
        assert fault._slow_query_thread is None
        assert (
            await observer.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = $1",
                worker_pid,
            )
            == 0
        )
    finally:
        if fault._slow_query_thread is not None:
            await service.stop_slow_query()
        await observer.close()
