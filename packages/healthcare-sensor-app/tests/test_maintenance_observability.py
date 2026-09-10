"""Check descriptive maintenance metadata against cleanup and real lock behavior."""

import asyncio
import json
import os
import signal
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from test_service import maintenance

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.fixture
def backend(monkeypatch):
    """Model only the owned connection; retain call order for cleanup assertions."""
    transaction = MagicMock(start=AsyncMock(), rollback=AsyncMock())
    locks = [
        {
            "pid": 314,
            "mode": "ShareLock",
            "granted": True,
            "relation": "owned_schema.sensor_readings",
            "schema": "owned_schema",
            "table": "sensor_readings",
        }
    ]
    conn = MagicMock(
        transaction=MagicMock(return_value=transaction),
        get_server_pid=MagicMock(return_value=314),
        execute=AsyncMock(),
        fetch=AsyncMock(return_value=locks),
        fetchval=AsyncMock(return_value="2026-09-10T00:00:00+00:00"),
        close=AsyncMock(),
    )
    cleanup = MagicMock()
    cleanup.attach_mock(transaction.rollback, "rollback")
    cleanup.attach_mock(conn.close, "close")
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(maintenance.asyncpg, "connect", connect)
    return SimpleNamespace(conn=conn, transaction=transaction, cleanup=cleanup, connect=connect, locks=locks)


def assert_operation_contract(acquired):
    """Keep the fixed declaration separate from observed release completion."""
    assert acquired["operation_contract"] == {
        "kind": "lock_only",
        "row_changing_dml": False,
        "cleanup_order": ["transaction_rollback", "connection_close"],
        "close_on_rollback_error": True,
    }
    assert "rollback_complete" not in acquired
    assert "release_reason" not in acquired


@pytest.mark.parametrize("exit_path", ["stop", "expiry", "cancel", "error"])
async def test_contract_matches_sql_and_cleanup_on_each_exit(backend, exit_path):
    records = []
    ready = asyncio.Event()
    stop = asyncio.Event()

    def emit(record):
        records.append(record)
        if record["event"] == "maintenance_lock_acquired":
            assert_operation_contract(record)
            backend.transaction.rollback.assert_not_awaited()
            backend.conn.close.assert_not_awaited()
            ready.set()
            if exit_path == "error":
                raise RuntimeError("event sink failed")

    task = asyncio.create_task(
        maintenance.hold_lock(
            "postgresql://unused@localhost/unused",
            run_id="owned_test",
            hold_seconds=0.01 if exit_path == "expiry" else 30,
            stop_event=stop,
            schema="owned_schema",
            emit=emit,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), 2)
        if exit_path == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif exit_path == "error":
            with pytest.raises(RuntimeError, match="event sink"):
                await task
        else:
            if exit_path == "stop":
                stop.set()
            await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    acquired, released = records
    assert acquired["locks"] == backend.locks
    assert "signal_stop_contract" not in acquired  # Direct callers install no handlers.
    assert acquired["max_hold_seconds"] == 7200
    assert acquired["run_id"] == released["run_id"] == "owned_test"
    assert acquired["backend_pid"] == released["backend_pid"] == 314
    backend.connect.assert_awaited_once_with(
        "postgresql://unused@localhost/unused",
        timeout=5,
        server_settings={"application_name": "healthcare-maint-owned_test", "lock_timeout": "10000"},
    )
    backend.transaction.start.assert_awaited_once()
    backend.conn.execute.assert_awaited_once_with('LOCK TABLE "owned_schema"."sensor_readings" IN SHARE MODE')
    for query in (backend.conn.fetch.call_args.args[0], backend.conn.fetchval.call_args.args[0]):
        assert query.lstrip().startswith("SELECT")
    assert backend.cleanup.mock_calls == [call.rollback(), call.close(timeout=5)]
    assert released["rollback_complete"] is True
    assert released["lock_acquired"] is True
    assert (
        released["release_reason"]
        == {
            "stop": "stop_requested",
            "expiry": "hold_expired",
            "cancel": "cancelled",
            "error": "error",
        }[exit_path]
    )


@pytest.mark.parametrize("failure", ["start", "lock", "observation", "rollback", "close"])
async def test_failures_do_not_publish_unobserved_acquisition_or_cleanup(backend, failure):
    records = []
    stop = asyncio.Event()
    stop.set()
    failing_call = {
        "start": backend.transaction.start,
        "lock": backend.conn.execute,
        "observation": backend.conn.fetch,
        "rollback": backend.transaction.rollback,
        "close": backend.conn.close,
    }[failure]
    failing_call.side_effect = RuntimeError("database failure")
    with pytest.raises(RuntimeError, match="database failure"):
        await maintenance.hold_lock(
            "postgresql://unused@localhost/unused",
            run_id="owned_test",
            hold_seconds=30,
            stop_event=stop,
            emit=records.append,
        )
    backend.conn.close.assert_awaited_once_with(timeout=5)
    if failure == "start":
        backend.transaction.rollback.assert_not_awaited()
    else:
        assert backend.cleanup.mock_calls == [call.rollback(), call.close(timeout=5)]
    if failure in ("rollback", "close"):
        assert [r["event"] for r in records] == ["maintenance_lock_acquired"]
        assert_operation_contract(records[0])
    else:
        assert [r["event"] for r in records] == ["maintenance_released"]
        assert records[0]["release_reason"] == "error"
        assert records[0]["rollback_complete"] is (failure != "start")
        assert records[0]["lock_acquired"] is (failure == "observation")


async def test_cancellation_during_rollback_waits_for_connection_close(backend):
    """A stop declaration must not let cancellation abandon the cleanup task."""
    rolling_back = asyncio.Event()
    finish_rollback = asyncio.Event()

    async def rollback():
        rolling_back.set()
        await finish_rollback.wait()

    backend.transaction.rollback.side_effect = rollback
    stop = asyncio.Event()
    stop.set()
    records = []
    task = asyncio.create_task(
        maintenance.hold_lock(
            "postgresql://unused@localhost/unused",
            run_id="owned_test",
            hold_seconds=30,
            stop_event=stop,
            emit=records.append,
        )
    )
    try:
        await asyncio.wait_for(rolling_back.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        backend.conn.close.assert_not_awaited()
    finally:
        finish_rollback.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert backend.cleanup.mock_calls == [call.rollback(), call.close(timeout=5)]
    assert [r["event"] for r in records] == ["maintenance_lock_acquired"]


@pytest.fixture
async def owned_postgres():
    """Use only an explicitly supplied, nondefault local test DB and a new schema."""
    dsn = os.environ.get("HEALTHCARE_MAINTENANCE_TEST_DSN")
    if not dsn:
        pytest.skip("Requires explicitly owned HEALTHCARE_MAINTENANCE_TEST_DSN")
    url = make_url(dsn)
    assert url.host == "127.0.0.1" and url.port and url.port != 5432
    assert url.database == "healthcare_maintenance_test"
    conn = await asyncpg.connect(dsn)
    schema = "maint_" + uuid.uuid4().hex
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'CREATE TABLE "{schema}".sensor_readings (id integer PRIMARY KEY)')
        await conn.execute(f'INSERT INTO "{schema}".sensor_readings VALUES (1)')
        yield SimpleNamespace(dsn=dsn, conn=conn, schema=schema)
    finally:
        try:
            await conn.execute("SET lock_timeout = '5s'")
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await conn.close()


@pytest.mark.parametrize("exit_path", ["expiry", "cancel", "error"])
async def test_real_postgres_non_signal_cleanup_preserves_rows(owned_postgres, exit_path):
    """Check that timeout, cancellation and post-acquisition error close the real owner."""
    db = owned_postgres
    records = []
    ready = asyncio.Event()

    def emit(record):
        records.append(record)
        if record["event"] == "maintenance_lock_acquired":
            ready.set()
            if exit_path == "error":
                raise RuntimeError("event sink failed")

    task = asyncio.create_task(
        maintenance.hold_lock(
            db.dsn,
            run_id=db.schema,
            schema=db.schema,
            hold_seconds=0.05 if exit_path == "expiry" else 30,
            stop_event=asyncio.Event(),
            emit=emit,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), 5)
        if exit_path == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif exit_path == "error":
            with pytest.raises(RuntimeError, match="event sink"):
                await task
        else:
            await asyncio.wait_for(task, 5)
        acquired, released = records
        assert_operation_contract(acquired)
        assert released["rollback_complete"] is True
        expected_reason = {"expiry": "hold_expired", "cancel": "cancelled", "error": "error"}[exit_path]
        assert released["release_reason"] == expected_reason
        assert (
            await db.conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid = $1", acquired["backend_pid"]) == 0
        )
        assert await db.conn.fetchval(f'SELECT array_agg(id) FROM "{db.schema}".sensor_readings') == [1]
        await db.conn.execute("SET lock_timeout = '1s'")
        assert await db.conn.execute(f'INSERT INTO "{db.schema}".sensor_readings VALUES (2)') == "INSERT 0 1"
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
async def test_real_postgres_cli_signal_contract_and_write_recovery(owned_postgres, signum):
    """Observe a real owner, blocked writer, unchanged rows, and CLI signal cleanup."""
    db = owned_postgres
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ECS_", "AWS_"))}
    env.update(DATABASE_URL=db.dsn, PYTHONPATH=str(PACKAGE / "src"))
    run_id = db.schema
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "test_service.maintenance",
        "--run-id",
        run_id,
        "--schema",
        db.schema,
        "--hold-seconds",
        "30",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    writer = None
    pending = None
    try:
        startup = []
        for _ in range(3):
            line = await asyncio.wait_for(process.stdout.readline(), 10)
            assert line, "Maintenance exited before its acquired event"
            startup.append(json.loads(line))
        assert [r["event"] for r in startup] == [
            "source_manifest",
            "ecs_runtime_identity",
            "maintenance_lock_acquired",
        ]
        acquired = startup[-1]
        assert_operation_contract(acquired)
        assert acquired["signal_stop_contract"] == {
            "SIGTERM": "sigterm",
            "SIGINT": "sigint",
            "action": "set_stop_event",
        }
        owner = acquired["backend_pid"]
        assert acquired["run_id"] == run_id
        assert (
            await db.conn.fetchval("SELECT application_name FROM pg_stat_activity WHERE pid = $1", owner)
            == f"healthcare-maint-{run_id}"
        )
        assert any(
            lock["pid"] == owner
            and lock["schema"] == db.schema
            and lock["table"] == "sensor_readings"
            and lock["mode"] == "ShareLock"
            and lock["granted"]
            for lock in acquired["locks"]
        )
        # SHARE permits reads; the maintenance transaction has not changed rows.
        assert await db.conn.fetchval(f'SELECT array_agg(id) FROM "{db.schema}".sensor_readings') == [1]
        writer = await asyncpg.connect(db.dsn)
        await writer.execute("SET statement_timeout = '10s'")
        pending = asyncio.create_task(writer.execute(f'INSERT INTO "{db.schema}".sensor_readings VALUES (2)'))
        async with asyncio.timeout(5):
            while owner not in await db.conn.fetchval("SELECT pg_blocking_pids($1)", writer.get_server_pid()):
                await asyncio.sleep(0.02)
        assert not pending.done()
        process.send_signal(signum)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        (released,) = [json.loads(line) for line in stdout.splitlines()]
        assert released["event"] == "maintenance_released"
        assert released["run_id"] == run_id and released["backend_pid"] == owner
        assert released["release_reason"] == ("sigterm" if signum == signal.SIGTERM else "sigint")
        assert released["rollback_complete"] is True
        assert await asyncio.wait_for(pending, 5) == "INSERT 0 1"
        assert await db.conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid = $1", owner) == 0
        assert await db.conn.fetchval(f'SELECT array_agg(id ORDER BY id) FROM "{db.schema}".sensor_readings') == [1, 2]
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        if writer is not None:
            await writer.close()
