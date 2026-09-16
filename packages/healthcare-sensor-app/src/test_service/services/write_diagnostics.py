"""Allowlisted INSERT diagnostics without exception messages, SQL text, or patient values."""

import hashlib
import logging
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql.asyncpg import dialect

from test_service.revision.write import COLUMN_NAMES, write_statement
from test_service.services.db_observability import current_operation

logger = logging.getLogger(__name__)


def write_contract() -> dict:
    """Describe the bound driver SQL shape so schema observations can be compared safely.

    schema_name is absent until observed: the INSERT uses the connection's search
    path, so a constant public schema would incorrectly claim an observed relation.
    """
    operation = current_operation()
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "request_id": operation.request_id if operation else None,
        "operation": operation.name if operation else "ingest",
        "sql_hash": hashlib.sha256(str(write_statement().compile(dialect=dialect())).encode()).hexdigest(),
        "sql_hash_algorithm": "sha256",
        "schema_name": None,
        "table_name": "sensor_readings",
        "column_names": list(COLUMN_NAMES),
    }


def log_write_error(exc: Exception) -> None:
    """Unwrap actual driver attributes; never infer missing diagnostics from error strings."""
    driver = exc
    seen = set()
    while id(driver) not in seen:
        seen.add(id(driver))
        inner = getattr(driver, "orig", None) or getattr(driver, "__cause__", None)
        if inner is None:
            break
        driver = inner
    logger.error(
        "db_write_error",
        extra={
            "event": "db_write_error",
            **write_contract(),
            "sqlstate": getattr(driver, "sqlstate", None),
            "error_type": type(driver).__name__,
            "schema_name": getattr(driver, "schema_name", None),
            "driver_table_name": getattr(driver, "table_name", None),
            "driver_column_name": getattr(driver, "column_name", None),
        },
    )
