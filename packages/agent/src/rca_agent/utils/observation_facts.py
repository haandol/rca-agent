"""Pure source-field validation shared by initial reads and received tool observations."""

import json
import re
from datetime import UTC, datetime

from rca_agent.ports.dto.observations import CriticalFact

_HASH = re.compile(r"[a-f0-9]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,127}")
_ACCOUNTING_KEYS = {
    "cancellation_semantics",
    "success_count_field",
    "success_evidence_event",
    "attempt_semantics",
    "failure_semantics",
    "failure_metric",
    "success_semantics",
    "attempt_metric",
    "metric_namespace",
    "service_name",
}


def _utc(value) -> datetime:
    """Require an explicit timezone; never reinterpret evaluation or local wall-clock times."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation time requires a timezone")
    return parsed.astimezone(UTC)


def _identifier(value) -> str | None:
    """Accept bounded identifier fields without trying to parse sensitive exception messages."""
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _canonical(value) -> bytes:
    """Match the publisher's immutable byte contract before trusting its content fingerprint."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _message(event) -> dict:
    """Require structured logger data so free-form prose never becomes extracted facts."""
    raw = event.get("message")
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("structured observation required")
    return value


def _fact(event, scope, run_id: str, *, task=None) -> CriticalFact:
    """Extract only safe identifiers and hashes; never copy SQL text, parameters or exception prose."""
    message = _message(event)
    event_time = datetime.fromtimestamp(event["timestamp"] / 1000, UTC)
    observed_at = message.get("observed_at")
    if observed_at is None or abs((_utc(observed_at) - event_time).total_seconds()) > 60:
        raise ValueError("logger time does not match CloudWatch event time")
    group = event.get("log_group", "")
    stream = event.get("log_stream", event.get("logStreamName", ""))
    event_id = event.get("event_id", event.get("eventId", ""))
    if (
        group != scope["log_group"]
        or not stream
        or not event_id
        or len(stream.split("/")) < 3
        or stream.split("/")[-2] != scope["container_name"]
    ):
        raise ValueError("observation source identity missing or mismatched")
    columns = message.get("column_names", [])
    if not isinstance(columns, list) or len(columns) > 128 or any(_identifier(c) is None for c in columns):
        raise ValueError("invalid SQL identifiers")
    relation = _identifier(message.get("table_name"))
    schema = _identifier(message.get("schema_name"))
    actual_schema = {}
    if message.get("event") in {"schema_snapshot", "db_schema_snapshot"} and relation and schema and columns:
        actual_schema[f"{schema}.{relation}"] = columns
    sqlstate = message.get("sqlstate")
    sql_hash = message.get("sql_hash")
    fingerprint = message.get("fingerprint")
    containers = task.get("containers", []) if task else []
    container = next((c for c in containers if c.get("name") == scope["container_name"]), {})
    return CriticalFact(
        source="cloudwatch_logs",
        source_ref=f"cloudwatch-logs://{group}/{stream}#{event_id}",
        observed_at=observed_at,
        account_id=scope["account_id"],
        region=scope["region"],
        service=scope["service_arn"],
        run_id=run_id,
        sqlstate=sqlstate if isinstance(sqlstate, str) and re.fullmatch(r"[A-Z0-9]{5}", sqlstate) else None,
        relation=f"{schema}.{relation}" if schema and relation else relation,
        schema_name=schema,
        driver_relation=_identifier(message.get("driver_table_name")),
        driver_column=_identifier(message.get("driver_column_name")),
        columns=columns,
        actual_schema=actual_schema,
        sql_fingerprint=sql_hash if isinstance(sql_hash, str) and _HASH.fullmatch(sql_hash) else None,
        task_definition=task.get("taskDefinitionArn") if task else None,
        image_digest=container.get("imageDigest"),
        source_revision=fingerprint if isinstance(fingerprint, str) and _HASH.fullmatch(fingerprint) else None,
        write_accounting={
            key: value for key in _ACCOUNTING_KEYS if isinstance(value := message.get(key), str) and len(value) <= 256
        }
        if message.get("event") == "write_accounting"
        else {},
    )
