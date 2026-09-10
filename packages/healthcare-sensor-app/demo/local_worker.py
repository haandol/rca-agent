"""One proof phase, imported exclusively from a previously compiled source tree."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
from sqlalchemy import event, text
from sqlalchemy.engine import make_url

from test_service.adapters.secondary.database_adapter import SqlAlchemyDatabaseAdapter
from test_service.adapters.secondary.sensor_repository.models import Base
from test_service.adapters.secondary.sensor_repository.sqlalchemy_sensor_repository import (
    SqlAlchemySensorReadingRepository,
)
from test_service.config import get_settings
from test_service.maintenance import hold_lock, native_dsn
from test_service.revision.manifest import source_manifest
from test_service.services.db_observability import current_operation, operation_context
from test_service.services.sensor import SensorService


class EvidenceHandler(logging.Handler):
    """Collect only parameter-free database events; never traceback strings."""

    def __init__(self):
        """Keep structured evidence in memory until this worker publishes its phase JSON."""
        super().__init__()
        self.records = []

    def emit(self, record):
        """Capture event-time UTC and structured fields without serializing traceback text."""
        if not hasattr(record, "event") or not record.name.startswith("test_service."):
            return
        standard = logging.makeLogRecord({}).__dict__
        self.records.append(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                **{
                    key: value
                    for key, value in record.__dict__.items()
                    if key not in standard and key not in ("message", "asctime")
                },
            }
        )


def configure_schema(engine, schema):
    """Use a run-owned schema without changing production settings or DB roles."""

    @event.listens_for(engine.sync_engine, "connect")
    def on_connect(connection, _record):
        """Set the owned search path outside transactions so rollbacks cannot reset isolation."""
        autocommit = connection.autocommit
        connection.autocommit = True
        try:
            cursor = connection.cursor()
            cursor.execute(f'SET SESSION search_path TO "{schema}"')
            cursor.close()
        finally:
            connection.autocommit = autocommit


async def warm(db, count):
    """Initialize dialect and physical connections outside measured operations."""
    connections = await asyncio.gather(*(db.engine.connect() for _ in range(count)))
    try:
        await asyncio.gather(*(conn.execute(text("SELECT 1")) for conn in connections))
    finally:
        await asyncio.gather(*(conn.close() for conn in connections))


def reading(patient, index=0):
    """Build deterministic, valid synthetic inputs shared by all revision phases."""
    return {
        "patient_id": patient,
        "reading_type": "heart_rate",
        "value": 72.0,
        "unit": "bpm",
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
    }


async def measured(name, action):
    """Record successful or failed real service work without exposing SQL values."""
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    operation = None
    result = None
    error = None
    try:
        with operation_context(name):
            operation = current_operation()
            result = await action()
    except Exception as exc:
        error = type(exc).__name__
    completed_at = datetime.now(UTC).isoformat()
    return {
        "started_at": started_at,
        "completed_at": completed_at,
        "outcome": "error" if error else "ok",
        "error_type": error,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "operation": asdict(operation) if operation else None,
    }, result


async def query_case(service, patient):
    """Compare repeated real queries using identical inputs and full response-content hashes."""
    results = []
    for _ in range(5):
        record, rows = await measured("patient_vitals", lambda: service.get_patient_vitals(patient, limit=120))
        record["row_count"] = len(rows) if rows is not None else 0
        record["rows_sha256"] = hashlib.sha256(
            json.dumps([asdict(row) for row in rows or []], sort_keys=True, default=str).encode()
        ).hexdigest()
        results.append(record)
    return {
        "query_input": {
            "patient_id": patient,
            "limit": 120,
            "requests": 5,
            "reading_type": None,
            "from_ts": None,
            "to_ts": None,
        },
        "queries": results,
    }


async def pool_case(db, service, patient):
    """Contend using actual INSERT work, never pg_sleep or fake checked-out counts."""
    gate = asyncio.Event()

    async def write_batch(index):
        """Start at the shared gate and record actual INSERT completion or pool timeout."""
        await gate.wait()
        record, rows = await measured(
            "sensor_ingest",
            lambda: service.ingest([reading(patient, index * 1000 + j) for j in range(1000)]),
        )
        record["saved_rows"] = len(rows) if rows else 0
        return record

    tasks = [asyncio.create_task(write_batch(index)) for index in range(8)]
    try:
        gate.set()
        snapshot = await db.wait_snapshot()
        results = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {"concurrency": 8, "batch_rows": 1000, "writes": results, "snapshot_during_writes": snapshot}


async def exception_case(db, service, patient):
    """Compare real invalid-LIMIT errors and subsequent valid writes without resetting sessions."""
    baseline, _ = await measured("valid_write", lambda: service.ingest([reading(patient)]))
    probes = []
    for _ in range(3):
        record, _ = await measured(
            "patient_vitals_invalid_limit", lambda: service.get_patient_vitals(patient, limit=-1)
        )
        record["checked_out_after_request"] = db.checked_out_connections()
        probes.append(record)
    valid, _ = await measured("valid_write", lambda: service.ingest([reading(patient)]))
    snapshot = await db.wait_snapshot()
    return {"baseline_write": baseline, "probes": probes, "subsequent_write": valid, "snapshot": snapshot}


async def cancellation_case(db):
    """Cancel a real consumer after PostgreSQL checkout and successful SELECT.

    The Event marks a deterministic consumer boundary, not simulated DB work.
    Cancelling while asyncpg itself is executing may invalidate the connection;
    this probe specifically tests cleanup of the caller-owned transaction.
    """
    acquired = asyncio.Event()
    parked = asyncio.Event()
    pid = None

    async def consumer():
        """Signal a completed PostgreSQL checkout before allowing cancellation of its owner."""
        nonlocal pid
        with operation_context("cancel_after_checkout"):
            async with db.session_context() as session:
                pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                acquired.set()
                await parked.wait()

    task = asyncio.create_task(consumer())
    try:
        await asyncio.wait_for(acquired.wait(), 5)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return {
            "backend_pid": pid,
            "checked_out_after_cancel": db.checked_out_connections(),
            "snapshot": await db.wait_snapshot(),
        }
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def lock_case(db, service, patient, args):
    """Observe a separate lock owner's blocking effect and distinguish signal release from expiry."""
    normal, _ = await measured("valid_write", lambda: service.ingest([reading(patient)]))
    if args.phase != "fault":
        return {"write": normal}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "test_service.maintenance",
        "--run-id",
        args.run_id,
        "--hold-seconds",
        "30",
        "--schema",
        args.schema,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pending = None
    observer_stop = asyncio.Event()
    observer_task = None
    try:
        acquired = json.loads(await asyncio.wait_for(process.stdout.readline(), 15))
        if acquired.get("event") != "maintenance_lock_acquired":
            raise RuntimeError("Maintenance did not acquire its lock")
        observer_task = asyncio.create_task(db.observe(observer_stop, interval=0.05))
        pending = asyncio.create_task(measured("blocked_write", lambda: service.ingest([reading(patient)])))
        snapshot = None
        for _ in range(100):
            snapshot = await db.wait_snapshot()
            if any(acquired["backend_pid"] in item["blocking_pids"] for item in snapshot["activity"]):
                break
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("No PostgreSQL blocking relationship was observed")
        blocked, _ = await pending
        process.terminate()
        stdout, _ = await asyncio.wait_for(process.communicate(), 10)
        released = [json.loads(line) for line in stdout.splitlines()]
        restored, _ = await measured("restored_write", lambda: service.ingest([reading(patient)]))
        bounded_events = []
        await hold_lock(
            os.environ["DATABASE_URL"],
            run_id=args.run_id + "_bounded",
            hold_seconds=0.05,
            stop_event=asyncio.Event(),
            schema=args.schema,
            emit=bounded_events.append,
        )
        return {
            "baseline_write": normal,
            "maintenance": acquired,
            "snapshot": snapshot,
            "blocked_write": blocked,
            "release_events": released,
            "maintenance_exit_code": process.returncode,
            "restored_write": restored,
            "bounded_hold_events": bounded_events,
        }
    finally:
        observer_stop.set()
        if observer_task is not None:
            await observer_task
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), 12)
            except TimeoutError:
                process.kill()
                await process.wait()
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


async def run(args):
    """Bracket real phase execution, warmup exclusion, and owned cleanup with UTC timestamps."""
    started_at = datetime.now(UTC).isoformat()
    manifest = source_manifest()
    if not manifest["verified"]:
        raise RuntimeError("Proof must run a compiled source tree")
    pool_size = 1 if args.case == "pool" and args.phase == "fault" else 8 if args.case == "pool" else 3
    settings = replace(
        get_settings(),
        db_pool_size=pool_size,
        db_max_overflow=0,
        db_pool_timeout_seconds=0.02 if args.case == "pool" else 0.2,
        db_statement_timeout_ms=500 if args.case == "lock" else 0,
        db_observability_enabled=True,
        fault_db_leak=False,
        fault_slow_query_ms=0,
        fault_error_rate=0,
    )
    db = SqlAlchemyDatabaseAdapter(settings)
    configure_schema(db.engine, args.schema)
    service = SensorService(SqlAlchemySensorReadingRepository(db))
    proof = {
        "case": args.case,
        "phase": args.phase,
        "source": manifest,
        "pool_size": pool_size,
        "started_at": started_at,
    }
    try:
        if args.case == "setup":
            async with db.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await service.ingest([reading(args.run_id, index) for index in range(120)])
            return proof
        await warm(db, pool_size)
        proof["measurement_started_at"] = datetime.now(UTC).isoformat()
        try:
            if args.case == "query":
                proof.update(await query_case(service, args.run_id))
            elif args.case == "pool":
                proof.update(await pool_case(db, service, args.run_id + "_writes"))
            elif args.case == "exception":
                proof.update(await exception_case(db, service, args.run_id))
            elif args.case == "cancel":
                proof.update(await cancellation_case(db))
            elif args.case == "lock":
                proof.update(await lock_case(db, service, args.run_id, args))
        finally:
            proof["measurement_completed_at"] = datetime.now(UTC).isoformat()
        proof["checked_out_before_dispose"] = db.checked_out_connections()
    finally:
        try:
            await db.dispose()
            proof["owned_sessions_after_dispose"] = len(db._owned_sessions)
            # Independent PostgreSQL observation proves shutdown released backends.
            observer = await asyncpg.connect(native_dsn(settings.database_url))
            try:
                proof["remaining_owned_schema_locks"] = await observer.fetchval(
                    """
                SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid=l.relation
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1
            """,
                    args.schema,
                )
            finally:
                await observer.close()
        finally:
            proof["completed_at"] = datetime.now(UTC).isoformat()
    return proof


def main():
    """Reject unowned database targets and export only sanitized, execution-time evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("setup", "query", "pool", "exception", "cancel", "lock"), required=True)
    parser.add_argument("--phase", choices=("normal", "fault", "restore"), required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-port", type=int, default=32768)
    args = parser.parse_args()
    # The parent verifies host/port/database; the child also refuses missing DSNs.
    if (
        not os.environ.get("DATABASE_URL")
        or not re.fullmatch(r"proof_local_[0-9a-f]{16}", args.schema)
        or args.schema != "proof_" + args.run_id
    ):
        raise SystemExit("Explicit proof database and owned schema required")
    url = make_url(os.environ["DATABASE_URL"])
    if (
        url.host != "127.0.0.1"
        or url.port != args.expected_port
        or url.port == 5432
        or url.database != "rca_demo"
        or url.drivername != "postgresql+asyncpg"
    ):
        raise SystemExit("Proof worker refused a database outside the owned local fixture")
    handler = EvidenceHandler()
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    started_at = datetime.now(UTC).isoformat()
    try:
        proof = asyncio.run(run(args))
        proof["events"] = handler.records
        args.output.write_text(json.dumps(proof, indent=2, default=str))
    except Exception as exc:
        args.output.write_text(
            json.dumps(
                {
                    "error_type": type(exc).__name__,
                    "events": handler.records,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                },
                default=str,
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
