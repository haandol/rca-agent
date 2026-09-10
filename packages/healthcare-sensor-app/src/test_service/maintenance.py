"""Hold a write-conflicting SHARE lock in one explicitly owned transaction.

Run as a separate ECS task using the service image and database secret:
python -m test_service.maintenance --run-id RUN --hold-seconds 120
SIGTERM, SIGINT, timeout, and exceptions all roll back this task's transaction.
"""

import argparse
import asyncio
import json
import re
import signal
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import asyncpg
from sqlalchemy.engine import make_url

from test_service.config import get_settings
from test_service.revision.manifest import source_manifest
from test_service.services.runtime_identity import runtime_identity

MAX_HOLD_SECONDS = 7200


def native_dsn(database_url: str) -> str:
    """Convert the SQLAlchemy driver URL without logging its credentials."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


async def hold_lock(
    database_url: str,
    *,
    run_id: str,
    hold_seconds: float,
    stop_event: asyncio.Event,
    schema: str = "public",
    emit: Callable[[dict], None] = lambda record: print(json.dumps(record), flush=True),
    stop_reason: Callable[[], str] = lambda: "stop_requested",
) -> None:
    """Own one lock-only transaction so its runtime log describes the code contract.

    Operation metadata declares behavior, not a completed rollback or RCA verdict.
    Schema/table names come from PostgreSQL's lock catalog; cleanup success is
    reported only by the existing release event after rollback and connection close.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", run_id):
        raise ValueError("run-id must contain 1..48 letters, digits, underscores or hyphens")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError("Invalid schema identifier")
    if not 0 < hold_seconds <= MAX_HOLD_SECONDS:
        raise ValueError(f"hold-seconds must be positive and at most {MAX_HOLD_SECONDS}")
    conn = await asyncpg.connect(
        native_dsn(database_url),
        timeout=5,
        server_settings={"application_name": f"healthcare-maint-{run_id}", "lock_timeout": "10000"},
    )
    transaction = conn.transaction()
    started = False
    lock_acquired = False
    pid = conn.get_server_pid()
    release_reason = "error"
    try:
        await transaction.start()
        started = True
        await conn.execute(f'LOCK TABLE "{schema}"."sensor_readings" IN SHARE MODE')
        lock_acquired = True
        acquired_at = datetime.now(UTC)
        deadline = asyncio.get_running_loop().time() + hold_seconds
        locks = await conn.fetch("""
            SELECT l.pid, l.mode, l.granted, l.relation::regclass::text AS relation,
                   n.nspname AS schema, c.relname AS table
            FROM pg_locks AS l
            LEFT JOIN pg_class AS c ON c.oid = l.relation
            LEFT JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation'
        """)
        emit(
            {
                "event": "maintenance_lock_acquired",
                "run_id": run_id,
                "backend_pid": pid,
                "hold_seconds": hold_seconds,
                "max_hold_seconds": MAX_HOLD_SECONDS,
                "acquired_at": acquired_at.isoformat(),
                "expires_at": (acquired_at + timedelta(seconds=hold_seconds)).isoformat(),
                "transaction_start": str(await conn.fetchval("SELECT transaction_timestamp()")),
                "locks": [dict(row) for row in locks],
                "operation_contract": {
                    "kind": "lock_only",
                    "row_changing_dml": False,
                    "cleanup_order": ["transaction_rollback", "connection_close"],
                    "close_on_rollback_error": True,
                },
            }
        )
        release_reason = "hold_expired"
        with suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=max(0, deadline - asyncio.get_running_loop().time()))
            release_reason = stop_reason()
    except asyncio.CancelledError:
        release_reason = "cancelled"
        raise
    except Exception:
        release_reason = "error"
        raise
    finally:

        async def release():
            """Rollback this task's transaction and close its backend even if rollback fails."""
            try:
                if started:
                    await transaction.rollback()
            finally:
                await conn.close(timeout=5)

        cleanup = asyncio.create_task(release())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
        emit(
            {
                "event": "maintenance_released",
                "run_id": run_id,
                "backend_pid": pid,
                "release_reason": release_reason,
                "released_at": datetime.now(UTC).isoformat(),
                "rollback_complete": started,
                "lock_acquired": lock_acquired,
            }
        )


async def _main(args) -> None:
    """Own signal handlers and describe them only for this CLI's acquired event.

    Registered SIGTERM/SIGINT handlers request graceful stop; they do not prove
    cleanup succeeded. Direct hold_lock callers do not inherit these handlers.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    reason = "stop_requested"

    def request_stop(signum):
        """Request graceful rollback and distinguish a signal from natural lock expiry."""
        nonlocal reason
        reason = "sigterm" if signum == signal.SIGTERM else "sigint"
        stop.set()

    def emit(record: dict) -> None:
        """Describe this CLI's installed handlers without claiming a signal occurred."""
        if record["event"] == "maintenance_lock_acquired":
            record = {
                **record,
                "signal_stop_contract": {
                    "SIGTERM": "sigterm",
                    "SIGINT": "sigint",
                    "action": "set_stop_event",
                },
            }
        print(json.dumps(record), flush=True)

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, request_stop, signum)
    try:
        print(json.dumps({"event": "source_manifest", **source_manifest()}), flush=True)
        print(json.dumps(await runtime_identity()), flush=True)
        await hold_lock(
            get_settings().database_url,
            run_id=args.run_id,
            hold_seconds=args.hold_seconds,
            stop_event=stop,
            schema=args.schema,
            emit=emit,
            stop_reason=lambda: reason,
        )
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def main() -> None:
    """CLI errors expose types only, since database exceptions may contain data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--hold-seconds", type=float, default=120)
    parser.add_argument("--schema", default="public")
    args = parser.parse_args()
    try:
        asyncio.run(_main(args))
    except Exception as exc:
        print(json.dumps({"event": "maintenance_error", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
