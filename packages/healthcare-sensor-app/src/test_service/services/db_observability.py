"""Parameter-free SQL and connection accounting, isolated by operation context."""

import hashlib
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy import event

logger = logging.getLogger(__name__)


@dataclass
class Operation:
    """Keep one request's counters available even when its connection returns late."""

    name: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sql_count: int = 0
    sql_time_ms: float = 0
    checkouts: int = 0
    checkins: int = 0
    acquire_time_ms: float = 0
    patterns: dict[str, dict] = field(default_factory=dict)


_operation: ContextVar[Operation | None] = ContextVar("db_operation", default=None)


def current_operation() -> Operation | None:
    """Expose counters for proof collection without SQL arguments or results."""
    return _operation.get()


@contextmanager
def operation_context(name: str) -> Iterator[None]:
    """Accumulate SQL under one operation; nested callers share its request ID."""
    if _operation.get() is not None:
        yield
        return
    operation = Operation(name=name)
    token = _operation.set(operation)
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        logger.info(
            "db_operation",
            extra={
                "event": "db_operation",
                "operation": name,
                "request_id": operation.request_id,
                "outcome": outcome,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "sql_count": operation.sql_count,
                "sql_time_ms": operation.sql_time_ms,
                "checkouts": operation.checkouts,
                "checkins": operation.checkins,
                "acquire_time_ms": operation.acquire_time_ms,
                "sql_patterns": list(operation.patterns.values()),
            },
        )
        _operation.reset(token)


def install_hooks(engine) -> None:
    """Attach synchronous SQLAlchemy events to an async engine's sync facade.

    Store only a hash of SQL text: even text() SQL can contain inline literals.
    No driver error string, bound value, connection URL, or row is logged.
    """
    target = engine.sync_engine

    @event.listens_for(target, "before_cursor_execute")
    def before(_conn, _cursor, statement, _parameters, context, _many):
        """Start timing and capture request/backend identity without retaining SQL values."""
        context._observation = (
            time.perf_counter(),
            hashlib.sha256(statement.encode()).hexdigest(),
            _operation.get(),
            _conn.info.get("observation_backend_pid"),
        )

    def finish(context, outcome):
        """Count one completed SQL attempt exactly once, including failed attempts.

        Clearing the context prevents duplicate accounting if multiple hooks
        observe the same execution. Only hashes and timings leave this hook.
        """
        observation = getattr(context, "_observation", None)
        if observation is None:
            return
        context._observation = None
        started, sql_hash, operation, backend_pid = observation
        elapsed = (time.perf_counter() - started) * 1000
        if operation is not None:
            operation.sql_count += 1
            operation.sql_time_ms += elapsed
            pattern = operation.patterns.setdefault(sql_hash, {"sql_hash": sql_hash, "count": 0, "time_ms": 0.0})
            pattern["count"] += 1
            pattern["time_ms"] += elapsed
        logger.info(
            "db_sql",
            extra={
                "event": "db_sql",
                "request_id": operation.request_id if operation else None,
                "sql_hash": sql_hash,
                "sql_hash_algorithm": "sha256",
                "backend_pid": backend_pid,
                "count": 1,
                "elapsed_ms": elapsed,
                "outcome": outcome,
            },
        )

    @event.listens_for(target, "after_cursor_execute")
    def after(_conn, _cursor, _statement, _parameters, context, _many):
        """Finalize successful execution through the same accounting path as errors."""
        finish(context, "ok")

    @event.listens_for(target, "handle_error")
    def error(context):
        """Record failed SQL timing without logging driver errors that may contain parameters."""
        finish(context.execution_context, "error")

    @event.listens_for(target, "checkout")
    def checkout(_connection, record, _proxy):
        """Bind a borrowed connection to its request so later returns retain attribution."""
        operation = _operation.get()
        record.info["observation_owner"] = operation
        driver = getattr(_connection, "driver_connection", None)
        backend_pid = driver.get_server_pid() if hasattr(driver, "get_server_pid") else None
        record.info["observation_backend_pid"] = backend_pid
        if operation:
            operation.checkouts += 1
        logger.info(
            "db_connection",
            extra={
                "event": "db_checkout",
                "request_id": operation.request_id if operation else None,
                "backend_pid": backend_pid,
            },
        )

    @event.listens_for(target, "checkin")
    def checkin(_connection, record):
        """Attribute the return to the original borrower, even after its context has ended."""
        operation = record.info.pop("observation_owner", None)
        if operation:
            operation.checkins += 1
        logger.info(
            "db_connection",
            extra={
                "event": "db_checkin",
                "request_id": operation.request_id if operation else None,
                "backend_pid": record.info.get("observation_backend_pid"),
            },
        )
