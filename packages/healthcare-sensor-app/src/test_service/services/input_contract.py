"""Describe the existing service input path without changing accepted input."""

import hashlib
import json
import logging
import platform
from datetime import UTC, datetime
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

from test_service.services.db_observability import current_operation

logger = logging.getLogger(__name__)

# Include both entry points and their common normalization, not the varying SQL.
INPUT_SOURCE_PATHS = (
    "adapters/primary/schemas.py",
    "adapters/primary/sensors/sensor_controller.py",
    "ports/dto/sensor.py",
    "services/sensor.py",
    "services/traffic_generator.py",
    "services/input_contract.py",
)


@lru_cache(maxsize=1)
def input_contract() -> dict:
    """Fingerprint installed input code and its interpreting runtime, not a v2 label."""
    root = Path(__file__).resolve().parents[1]
    descriptor = {
        "format": "sensor-service-batch-v1",
        "timestamp_mapping": "timestamp-or-server-utc",
        "source_files": {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in INPUT_SOURCE_PATHS},
        "runtime": {"python": platform.python_version(), "pydantic": version("pydantic")},
    }
    digest = hashlib.sha256(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return {"input_contract": descriptor, "input_contract_sha256": digest}


def observe_input(readings: list[dict]) -> None:
    """Emit after actual normalization and before SQL, sharing the commit request ID.

    Observe only the supported current shape. Unknown input produces no witness;
    it still follows the existing service behavior. Patient values never enter logs.
    """
    operation = current_operation()
    if operation is None or not readings:
        return
    required = {"patient_id", "reading_type", "value", "unit"}
    if any(
        not required <= row.keys()
        or row.keys() - (required | {"timestamp"})
        or not all(isinstance(row[key], str) for key in ("patient_id", "reading_type", "unit"))
        or type(row["value"]) not in (int, float)
        or (row.get("timestamp") is not None and not isinstance(row["timestamp"], datetime))
        for row in readings
    ):
        return
    try:
        contract = input_contract()
    except (OSError, ValueError):
        # Observational metadata must not turn a valid ingest into a failed write.
        return
    logger.info(
        "input_contract_observed",
        extra={
            "event": "input_contract_observed",
            "observed_at": datetime.now(UTC).isoformat(),
            "operation": operation.name,
            "request_id": operation.request_id,
            "count": len(readings),
            **contract,
        },
    )


@lru_cache(maxsize=1)
def vital_input_contract() -> dict:
    """Describe the actual versioned durable entry path; do not reuse the legacy batch witness."""
    root = Path(__file__).resolve().parents[1]
    paths = (
        "adapters/primary/vital_controller.py",
        "ports/dto/vital.py",
        "services/vital.py",
        "services/input_contract.py",
        "adapters/secondary/vital_repository/postgresql.py",
        "adapters/secondary/vital_repository/models.py",
        "ports/dto/sensor.py",
        "services/sensor.py",
        "adapters/secondary/sensor_repository/models.py",
    )
    descriptor = {
        "format": "vital-event-v1-v2",
        "timestamp_mapping": "v1.timestamp-or-v2.sampled_at-to-timestamp",
        "delivery_stage": "measurement_attempt",
        "event_versions": [1, 2],
        "source_files": {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths},
        "runtime": {"python": platform.python_version(), "pydantic": version("pydantic")},
    }
    digest = hashlib.sha256(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return {"input_contract": descriptor, "input_contract_sha256": digest}


def observe_vital_input(event) -> None:
    """Join a real normalized versioned attempt to its commit/error without exposing input values."""
    operation = current_operation()
    if operation is None:
        return
    logger.info(
        "input_contract_observed",
        extra={
            "event": "input_contract_observed",
            "observed_at": datetime.now(UTC).isoformat(),
            "operation": operation.name,
            "request_id": operation.request_id,
            "count": 1,
            "event_schema_version": event.schema_version,
            **vital_input_contract(),
        },
    )
