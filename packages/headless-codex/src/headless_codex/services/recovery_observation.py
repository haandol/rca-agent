"""Headless bridge for the same server-owned recovery observation contract.

Observer/fact/context bodies mirror Strands. No Agent runtime dependency is shipped.
Injected clients must use bounded SDK calls (connect <=5s, read <=60s, one attempt).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from pydantic import BaseModel, Field

from headless_codex.ports.dto.models import parse_alarm
from headless_codex.services.runbook_contract import SERVICE_SETTING_DEFAULTS
from headless_codex.services.source_locations import declared_source_locations

AWS_SDK_CALL_WORST_CASE_SECONDS = 65


class CriticalFact(BaseModel):
    """Keep independently sourced facts outside prose truncation and never infer absent fields."""

    source: str
    source_ref: str
    observed_at: str
    account_id: str = ""
    region: str = ""
    service: str = ""
    run_id: str = ""
    sqlstate: str | None = None
    relation: str | None = None
    schema_name: str | None = None
    driver_relation: str | None = None
    driver_column: str | None = None
    columns: list[str] = Field(default_factory=list)
    sql_fingerprint: str | None = None
    actual_schema: dict[str, list[str]] = Field(default_factory=dict)
    task_definition: str | None = None
    image_digest: str | None = None
    source_revision: str | None = None
    write_accounting: dict[str, str] = Field(default_factory=dict)


class IncidentObservations(BaseModel):
    """Verified refers to source/scope checks, never a confirmed diagnosis."""

    critical_facts: list[CriticalFact] = Field(default_factory=list)
    baseline_verified: bool = False
    baseline: dict = Field(default_factory=dict)
    current: dict = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)


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


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_HASH = re.compile(r"[a-f0-9]{64}")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_MAX_BYTES = 512 * 1024
_MAX_EVENTS = 100
_ACCOUNTING_KEYS = {
    "metric_namespace",
    "service_name",
    "attempt_metric",
    "failure_metric",
    "attempt_semantics",
    "failure_semantics",
    "cancellation_semantics",
    "success_evidence_event",
    "success_count_field",
    "success_semantics",
}
_SETTING_KEYS = (
    "desiredCount",
    "networkConfiguration",
    "capacityProviderStrategy",
    "launchType",
    "deploymentController",
    "schedulingStrategy",
    "enableExecuteCommand",
    "deploymentConfiguration",
    "platformVersion",
)
_MESSAGE_KEYS = {
    "event",
    "observed_at",
    "operation",
    "sql_hash",
    "sql_hash_algorithm",
    "schema_name",
    "table_name",
    "column_names",
    "sqlstate",
    "driver_table_name",
    "driver_column_name",
    "fingerprint",
    "revision",
    "verified",
    "count",
    "completion_semantics",
    "request_id",
    "files",
    "base_fingerprint",
    "input_contract",
    "input_contract_sha256",
    "event_schema_version",
} | _ACCOUNTING_KEYS


def _safe_message(message: dict) -> dict:
    """Preserve validated optional Git locators as declarations alongside existing safe facts."""
    result = {key: value for key, value in message.items() if key in _MESSAGE_KEYS}
    if locations := declared_source_locations(message):
        result["source_locations"] = locations
    return result


class AwsIncidentObservation:
    def __init__(self, *, s3_client, logs_client_for_region, ecs_client_for_region, evidence_bucket: str):
        """Use injected application clients and a configured bucket instead of alarm-selected credentials."""
        self.s3 = s3_client
        self.logs_for_region = logs_client_for_region
        self.ecs_for_region = ecs_client_for_region
        self.bucket = evidence_bucket

    def observe(self, alarm, *, timeout_seconds: float) -> IncidentObservations:
        """Validate coordinates before AWS reads and keep failed normal proof out of approval context."""
        result = IncidentObservations()
        # Source-only evaluations must not replace historical evidence with live AWS data.
        if alarm.eval_source_metadata is not None or not alarm.alarm_description:
            return result
        deadline = time.monotonic() + max(0, timeout_seconds)
        try:
            metadata = json.loads(alarm.alarm_description)
            if not isinstance(metadata, dict) or "baseline_ref" not in metadata:
                return result
            baseline, scope = self._load_baseline(alarm, metadata, deadline)
        except Exception as exc:
            result.diagnostics.append(f"baseline reference unavailable: {type(exc).__name__}")
            return result
        try:
            self._verify_normal(baseline, scope, alarm, deadline)
            result.baseline = self._safe_baseline(baseline, metadata["baseline_ref"])
            result.baseline_verified = True
        except Exception as exc:
            result.diagnostics.append(f"normal baseline unverified: {type(exc).__name__}: {exc}")
        try:
            current, facts = self._current(scope, metadata["run_id"], alarm, deadline)
            result.current = current
            result.critical_facts = facts
        except Exception as exc:
            result.diagnostics.append(f"current observations incomplete: {type(exc).__name__}: {exc}")
        from headless_codex.services.recovery_evidence import seal_observations

        seal_observations(result, alarm)
        return result

    def refresh_current(self, alarm, observations, *, timeout_seconds):
        """Refresh only deployment control metadata using the original baseline and alarm cutoff."""
        refreshed = observations.model_copy(deep=True)
        refreshed.current = {}
        if not observations.baseline_verified or alarm.eval_source_metadata is not None:
            return refreshed
        try:
            current, _ = self._current(
                observations.baseline["scope"],
                observations.baseline["run_id"],
                alarm,
                time.monotonic() + max(0, timeout_seconds),
                collect_logs=False,
            )
            previous = observations.current
            if any(current.get(k) != previous.get(k) for k in ("deployment_id", "task_definition_arn")):
                raise ValueError("deployment identity changed since incident observation")
            if previous.get("image_digest") and current["image_digest"] != previous["image_digest"]:
                raise ValueError("deployment image changed since incident observation")
            for fact in observations.critical_facts:
                if (
                    fact.task_definition == current["task_definition_arn"]
                    and fact.image_digest != current["image_digest"]
                ):
                    raise ValueError("observed image changed since causal evidence collection")
            current["observations"] = previous.get("observations", [])
            current["log_window"] = previous.get("log_window", {})
            refreshed.current = current
            from headless_codex.services.recovery_evidence import has_reader_receipt, seal_observations

            if has_reader_receipt(observations, alarm):
                seal_observations(refreshed, alarm)
        except Exception as exc:
            refreshed.diagnostics.append(f"deployment refresh unavailable: {type(exc).__name__}: {exc}")
        return refreshed

    def _check_budget(self, deadline):
        """Do not start a new bounded SDK request after the observation deadline."""
        if deadline - time.monotonic() < AWS_SDK_CALL_WORST_CASE_SECONDS:
            raise TimeoutError("observation budget exhausted")

    def _safe_baseline(self, baseline, reference):
        """Transport approved observation fields, not arbitrary S3 JSON or logger extras."""
        return {
            "schema_version": 1,
            "run_id": baseline["run_id"],
            "observed_at": baseline["observed_at"],
            "scope": {
                key: baseline["scope"][key]
                for key in (
                    "account_id",
                    "region",
                    "cluster_arn",
                    "service_arn",
                    "service_name",
                    "container_name",
                    "log_group",
                    "desired_count",
                )
            },
            "normal": {key: baseline["normal"][key] for key in ("task_definition_arn", "image_digest")},
            "baseline_ref": reference,
            "service_settings": {
                key: baseline["service_settings"][key]
                for key in _SETTING_KEYS
                if key in baseline.get("service_settings", {})
            },
            "metrics": baseline.get("metrics", {}),
            "metric_observations": baseline["metric_observations"],
            "observations": [
                {
                    "message": _safe_message(_message(event)),
                    **{key: event[key] for key in ("timestamp", "event_id", "log_group", "log_stream")},
                    **baseline["normal"],
                }
                for event in baseline["observations"]
            ],
        }

    def _load_baseline(self, alarm, metadata, deadline):
        """Only the pinned object in the configured bucket is eligible for trust checks."""
        run_id = metadata.get("run_id")
        ref = metadata.get("baseline_ref")
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id) or not isinstance(ref, dict):
            raise ValueError("invalid baseline reference")
        if (
            not self.bucket
            or ref.get("bucket") != self.bucket
            or ref.get("key") != f"baselines/{run_id}/normal.json"
            or not isinstance(ref.get("sha256"), str)
            or not _HASH.fullmatch(ref["sha256"])
        ):
            raise ValueError("baseline reference outside allowlisted scope")
        arn = (alarm.alarm_arn or "").split(":")
        if len(arn) < 6 or arn[2] != "cloudwatch" or not re.fullmatch(r"\d{12}", arn[4]):
            raise ValueError("alarm account is unknown")
        self._check_budget(deadline)
        response = self.s3.get_object(Bucket=self.bucket, Key=ref["key"], ExpectedBucketOwner=arn[4])
        body = response["Body"]
        try:
            raw = body.read(_MAX_BYTES + 1)
        finally:
            body.close()
        if len(raw) > _MAX_BYTES or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("baseline size or content fingerprint mismatch")
        baseline = json.loads(raw)
        if (
            _canonical(baseline) != raw
            or type(baseline.get("schema_version")) is not int
            or baseline["schema_version"] != 1
            or baseline.get("run_id") != run_id
        ):
            raise ValueError("invalid canonical baseline")
        scope = baseline.get("scope", {})
        if scope.get("account_id") != arn[4] or scope.get("region") != arn[3] or alarm.region != arn[3]:
            raise ValueError("baseline account/region mismatch")
        if metadata.get("service") not in {scope.get("service_name"), scope.get("service_arn")}:
            raise ValueError("baseline service mismatch")
        for key in ("cluster_arn", "service_arn"):
            value = scope.get(key, "")
            if not isinstance(value, str) or not value.startswith(f"arn:{arn[1]}:ecs:{arn[3]}:{arn[4]}:"):
                raise ValueError("invalid ECS scope")
        if not scope.get("container_name") or not scope.get("log_group") or not scope.get("service_name"):
            raise ValueError("missing service/log scope")
        ecs_prefix = f"arn:{arn[1]}:ecs:{arn[3]}:{arn[4]}:"
        cluster_prefix = ecs_prefix + "cluster/"
        if not scope["cluster_arn"].startswith(cluster_prefix):
            raise ValueError("invalid cluster ARN")
        cluster_name = scope["cluster_arn"][len(cluster_prefix) :]
        if scope["service_arn"] != f"{ecs_prefix}service/{cluster_name}/{scope['service_name']}":
            raise ValueError("service ARN does not match the named service/cluster")
        if type(scope.get("desired_count")) is not int or scope["desired_count"] < 1:
            raise ValueError("normal desired task count missing")
        if baseline.get("service_settings", {}).get("desiredCount") != scope["desired_count"]:
            raise ValueError("normal service configuration is inconsistent")
        if _utc(baseline["observed_at"]) >= _utc(alarm.state_change_time):
            raise ValueError("baseline was not observed before the alarm")
        return baseline, scope

    def _tasks(self, scope, task_ids, deadline):
        """Bind log streams to actual ECS service tasks before associating source or image facts."""
        if not task_ids or len(task_ids) > 100:
            raise ValueError("missing or excessive task identities")
        self._check_budget(deadline)
        response = self.ecs_for_region(scope["region"]).describe_tasks(cluster=scope["cluster_arn"], tasks=task_ids)
        if response.get("failures") or len(response.get("tasks", [])) != len(task_ids):
            raise ValueError("task ownership unavailable")
        tasks = response["tasks"]
        for task in tasks:
            if (
                task.get("clusterArn") != scope["cluster_arn"]
                or task.get("group") != f"service:{scope['service_name']}"
            ):
                raise ValueError("task outside service scope")
        return {task["taskArn"].rsplit("/", 1)[-1]: task for task in tasks}

    def _verify_normal(self, baseline, scope, alarm, deadline):
        """Require actual committed writes, zero failures, schema and verified installed source."""
        normal = baseline["normal"]
        digest = normal.get("image_digest", "")
        if not _DIGEST.fullmatch(digest):
            raise ValueError("normal image digest missing")
        coordinates = baseline.get("metrics", {})
        if not {"attempts", "failures"} <= coordinates.keys():
            raise ValueError("normal metric coordinates missing")
        for metric in coordinates.values():
            if (
                not isinstance(metric, dict)
                or set(metric) != {"namespace", "metric_name", "dimensions"}
                or not isinstance(metric["dimensions"], dict)
                or not metric["dimensions"]
                or not all(isinstance(v, str) and v for v in metric["dimensions"].values())
            ):
                raise ValueError("invalid normal metric coordinates")
        if alarm.trigger is None or coordinates["failures"] != {
            "namespace": alarm.trigger.namespace,
            "metric_name": alarm.trigger.metric_name,
            "dimensions": alarm.trigger.dimensions,
        }:
            raise ValueError("baseline failure metric does not match the alarm")
        if any(
            metric["namespace"] != coordinates["failures"]["namespace"]
            or metric["dimensions"] != coordinates["failures"]["dimensions"]
            for metric in coordinates.values()
        ):
            raise ValueError("baseline metrics have different scopes")
        window = baseline["metric_observations"]
        start, end = _utc(window["start"]), _utc(window["end"])
        if not start < end <= _utc(baseline["observed_at"]) < _utc(alarm.state_change_time):
            raise ValueError("normal metric window is not before the fault")
        metric_times = []
        for key in ("attempts", "failures"):
            rows = window[key]
            if not rows or len(rows) > 60:
                raise ValueError("normal metric samples missing")
            timestamps = []
            for row in rows:
                stamp = _utc(row["Timestamp"])
                count = row.get("Sum")
                if (
                    not start <= stamp < end
                    or row.get("Unit") != "Count"
                    or type(count) not in (float, int)
                    or (not count > 0 if key == "attempts" else count != 0)
                ):
                    raise ValueError("normal write metrics not proven")
                timestamps.append(stamp)
            metric_times.append(set(timestamps))
        if metric_times[0] != metric_times[1]:
            raise ValueError("normal metric windows do not match")
        events = baseline["observations"]
        if not events or len(events) > _MAX_EVENTS:
            raise ValueError("normal observations missing or excessive")
        ids = {event["log_stream"].rsplit("/", 1)[-1] for event in events}
        identities = {(event["event_id"], event["log_group"], event["log_stream"]) for event in events}
        if len(identities) != len(events):
            raise ValueError("normal observations repeat a source identity")
        tasks = self._tasks(scope, sorted(ids), deadline)
        proven: dict[str, set[str]] = {}
        for event in events:
            task_id = event["log_stream"].rsplit("/", 1)[-1]
            task = tasks[task_id]
            fact = _fact(event, scope, baseline["run_id"], task=task)
            if fact.task_definition != normal["task_definition_arn"] or fact.image_digest != digest:
                raise ValueError("normal log source deployment mismatch")
            stamp = datetime.fromtimestamp(event["timestamp"] / 1000, UTC)
            if stamp > _utc(baseline["observed_at"]):
                raise ValueError("normal event is from a later deployment window")
            message = _message(event)
            event_type = message.get("event")
            checks = proven.setdefault(task_id, set())
            if event_type == "write_completed":
                if (
                    start <= stamp < end
                    and type(message.get("count")) is int
                    and message["count"] > 0
                    and message.get("completion_semantics") == "committed_rows"
                    and fact.sql_fingerprint
                ):
                    checks.add("write")
            elif event_type == "source_manifest" and message.get("verified") is True and fact.source_revision:
                checks.add("source")
            elif event_type in {"schema_snapshot", "db_schema_snapshot"} and fact.actual_schema:
                checks.add("schema")
        if not proven or any(checks != {"write", "source", "schema"} for checks in proven.values()):
            raise ValueError("normal committed write/source/schema proof missing")

    def _current(self, scope, run_id, alarm, deadline, *, collect_logs=True):
        """Read actual ECS state and one bounded log window without choosing a cause or rollback."""
        self._check_budget(deadline)
        ecs = self.ecs_for_region(scope["region"])
        response = ecs.describe_services(cluster=scope["cluster_arn"], services=[scope["service_arn"]])
        if response.get("failures") or len(response.get("services", [])) != 1:
            raise ValueError("current service unavailable")
        service = response["services"][0]
        if service["serviceArn"] != scope["service_arn"] or service["clusterArn"] != scope["cluster_arn"]:
            raise ValueError("current service scope mismatch")
        self._check_budget(deadline)
        listed = ecs.list_tasks(
            cluster=scope["cluster_arn"], serviceName=scope["service_name"], desiredStatus="RUNNING"
        )
        if listed.get("nextToken"):
            raise ValueError("current task set exceeds bounded read")
        tasks = self._tasks(scope, listed.get("taskArns", []), deadline)
        self._check_budget(deadline)
        definition = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])["taskDefinition"]
        container = next(
            (c for c in definition.get("containerDefinitions", []) if c.get("name") == scope["container_name"]),
            None,
        )
        if container is None:
            raise ValueError("current container missing")
        log_options = container.get("logConfiguration", {}).get("options", {})
        if (
            log_options.get("awslogs-group") != scope["log_group"]
            or log_options.get("awslogs-region") != scope["region"]
        ):
            raise ValueError("current log scope differs from pinned service")
        now = datetime.now(UTC)
        alarm_time = _utc(alarm.state_change_time)
        start = alarm_time - timedelta(minutes=5)
        end = min(now, alarm_time + timedelta(minutes=5))
        if end <= start:
            raise ValueError("current observation window invalid")
        facts, observations, coverage, errors = [], [], {}, {}
        primary = [d for d in service.get("deployments", []) if d.get("status") == "PRIMARY"]
        if len(primary) != 1 or primary[0].get("taskDefinition") != service["taskDefinition"]:
            raise ValueError("current primary task definition unavailable")
        prefix = log_options.get("awslogs-stream-prefix")
        if not isinstance(prefix, str) or not prefix:
            raise ValueError("current container log stream prefix unavailable")
        streams = {
            f"{prefix}/{scope['container_name']}/{task_id}": task
            for task_id, task in tasks.items()
            if task.get("taskDefinitionArn") == primary[0]["taskDefinition"]
            and any(c.get("name") == scope["container_name"] for c in task.get("containers", []))
        }
        # Separate event-kind queues prevent frequent schema events from exhausting
        # error/source capacity. Round-robin pages give every kind its first read.
        kinds = (
            "db_write_error",
            "source_manifest",
            "write_contract",
            "write_accounting",
            "schema_snapshot",
            "db_schema_snapshot",
            "input_contract_observed",
        )
        pending = [(kind, None) for kind in kinds] if collect_logs and streams else []
        seen_tokens, seen_events, compact = set(), set(), {}
        pages = dict.fromkeys(kinds, 0)
        while pending:
            kind, token = pending.pop(0)
            if deadline - time.monotonic() < AWS_SDK_CALL_WORST_CASE_SECONDS:
                coverage[kind] = "budget_exhausted"
                for remaining, _ in pending:
                    coverage[remaining] = "budget_exhausted"
                break
            kwargs = dict(
                logGroupName=scope["log_group"],
                logStreamNames=sorted(streams),
                startTime=int(start.timestamp() * 1000),
                endTime=int(end.timestamp() * 1000),
                limit=_MAX_EVENTS,
                filterPattern='{ $.event = "' + kind + '" }',
            )
            if token:
                kwargs["nextToken"] = token
            try:
                logs = self.logs_for_region(scope["region"]).filter_log_events(**kwargs)
            except Exception as exc:
                # A later read cannot erase earlier verified source observations.
                # Do not retry this page implicitly or expose provider error prose.
                coverage[kind] = "failed"
                errors[kind] = type(exc).__name__
                continue
            pages[kind] += 1
            for event in logs.get("events", []):
                event = {**event, "log_group": scope["log_group"]}
                task = streams.get(event.get("logStreamName"))
                try:
                    if task is None or _message(event).get("event") != kind:
                        continue
                    if not start.timestamp() * 1000 <= event["timestamp"] < end.timestamp() * 1000:
                        continue
                    fact = _fact(event, scope, run_id, task=task)
                    if fact.source_ref in seen_events:
                        continue
                    seen_events.add(fact.source_ref)
                    safe = _safe_message(_message(event))
                    observations.append(
                        {
                            "message": safe,
                            "source_ref": fact.source_ref,
                            "timestamp": event["timestamp"],
                            "event_id": event.get("eventId", event.get("event_id")),
                            "log_group": scope["log_group"],
                            "log_stream": event.get("logStreamName", event.get("log_stream")),
                            "task_definition_arn": fact.task_definition,
                            "image_digest": fact.image_digest,
                        }
                    )
                    identity = fact.model_dump(exclude={"observed_at", "source_ref"})
                    key = _canonical({"event": kind, "fact": identity})
                    if key not in compact:
                        compact[key] = fact
                        facts.append(fact)
                except (ValueError, TypeError, KeyError):
                    continue
            next_token = logs.get("nextToken")
            coverage[kind] = "complete"
            if next_token:
                if (kind, next_token) in seen_tokens or pages[kind] >= 3:
                    coverage[kind] = "pagination_incomplete"
                else:
                    seen_tokens.add((kind, next_token))
                    pending.append((kind, next_token))
                    coverage[kind] = "pagination_incomplete"
        current = {
            "observed_at": now.isoformat(),
            "scope": scope,
            "task_definition_arn": service["taskDefinition"],
            "service_settings": {key: service[key] for key in _SETTING_KEYS if key in service},
            "deployments": [
                {
                    key: d[key].isoformat() if isinstance(d[key], datetime) else d[key]
                    for key in ("id", "status", "taskDefinition", "rolloutState", "createdAt")
                    if key in d
                }
                for d in service.get("deployments", [])
            ],
            "tasks": [
                {
                    "task_arn": task["taskArn"],
                    "task_definition_arn": task["taskDefinitionArn"],
                    "last_status": task.get("lastStatus"),
                    "containers": [
                        {key: c[key] for key in ("name", "image", "imageDigest", "lastStatus") if key in c}
                        for c in task.get("containers", [])
                    ],
                }
                for task in tasks.values()
            ],
            "observations": observations,
            "log_stream_prefix": prefix,
            "log_window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": _MAX_EVENTS,
                "coverage": coverage,
                "errors": errors,
                "pages": pages,
            },
        }
        primary = [d for d in current["deployments"] if d.get("status") == "PRIMARY"]
        current["deployment_id"] = primary[0].get("id") if len(primary) == 1 else None
        digests = {
            c.get("imageDigest")
            for task in current["tasks"]
            for c in task["containers"]
            if c.get("name") == scope["container_name"]
        }
        current["image_digest"] = next(iter(digests)) if len(digests) == 1 else None
        return current, facts


def build_rollback_context(scoping: object | None) -> dict | None:
    """Require the same service/settings and a single observed fault deployment before offering rollback."""
    if scoping is None or not scoping.incident_observations.baseline_verified:
        return None
    observations = scoping.incident_observations
    baseline, current = observations.baseline, observations.current
    try:
        scope = baseline["scope"]
        if current["scope"] != scope:
            return None
        normal_settings = {
            key: baseline["service_settings"].get(key, default) for key, default in SERVICE_SETTING_DEFAULTS.items()
        }
        fault_settings = {
            key: current["service_settings"].get(key, default) for key, default in SERVICE_SETTING_DEFAULTS.items()
        }
        # AWS may omit this optional collection while CLI snapshots encode null.
        # Normalize only that representational absence; retain every supplied
        # strategy entry and every nested mutable deployment setting verbatim.
        for settings in (normal_settings, fault_settings):
            if settings["capacityProviderStrategy"] is None:
                settings["capacityProviderStrategy"] = []
        if (
            normal_settings != fault_settings
            or scope["desired_count"] != current["service_settings"]["desiredCount"]
            or scope["desired_count"] != len(current["tasks"])
        ):
            return None
        deployments = current["deployments"]
        if len(deployments) != 1:
            return None
        deployment = deployments[0]
        if (
            deployment["taskDefinition"] != current["task_definition_arn"]
            or deployment["status"] != "PRIMARY"
            or deployment["rolloutState"] != "COMPLETED"
            or not deployment["id"]
        ):
            return None
        observed_at = baseline["observed_at"]
        if not datetime.fromisoformat(observed_at) < datetime.fromisoformat(current["observed_at"]):
            return None
        if not datetime.fromisoformat(observed_at) < datetime.fromisoformat(deployment["createdAt"]):
            return None
        if (
            scoping.raw_alarm is None
            or scoping.raw_alarm.state_change_time is None
            or datetime.fromisoformat(deployment["createdAt"]) > scoping.raw_alarm.state_change_time
        ):
            return None
        if current["deployment_id"] != deployment["id"]:
            return None
        digests = set()
        for task in current["tasks"]:
            if task["task_definition_arn"] != current["task_definition_arn"] or task["last_status"] != "RUNNING":
                return None
            containers = [c for c in task["containers"] if c["name"] == scope["container_name"]]
            if len(containers) != 1:
                return None
            digests.add(containers[0]["imageDigest"])
        if len(digests) != 1:
            return None
        fault_digest = next(iter(digests))
        if fault_digest != current["image_digest"]:
            return None
        if (
            baseline["normal"]["task_definition_arn"] == current["task_definition_arn"]
            or baseline["normal"]["image_digest"] == fault_digest
        ):
            return None
        context = deepcopy(
            {
                "baseline_ref": baseline["baseline_ref"],
                "scope": scope,
                "normal": baseline["normal"],
                "current": {
                    "task_definition_arn": current["task_definition_arn"],
                    "image_digest": fault_digest,
                    "deployment_id": deployment["id"],
                },
                "service_settings": normal_settings,
            }
        )
        if descriptor := normal_write_accounting(baseline):
            context["write_accounting"] = descriptor
        return context
    except (KeyError, ValueError, TypeError):
        return None


def normal_write_accounting(baseline: dict) -> dict | None:
    """Normalize only the known producer contract from reader-verified normal image logs."""
    metrics = baseline["metrics"]
    attempts, failures = metrics["attempts"], metrics["failures"]
    dimensions = attempts["dimensions"]
    if (
        not isinstance(dimensions, dict)
        or set(dimensions) != {"ServiceName"}
        or not isinstance(dimensions["ServiceName"], str)
        or not dimensions["ServiceName"].strip()
        or failures["dimensions"] != dimensions
        or failures["namespace"] != attempts["namespace"]
    ):
        return None
    expected = {
        "metric_namespace": attempts["namespace"],
        "service_name": dimensions["ServiceName"],
        "attempt_metric": attempts["metric_name"],
        "failure_metric": failures["metric_name"],
        "attempt_semantics": "completed_successful_rows_plus_failed_rows",
        "failure_semantics": "failed_rows",
        "cancellation_semantics": "excluded_from_completed_counters",
        "success_evidence_event": "write_completed",
        "success_count_field": "count",
        "success_semantics": "committed_rows",
    }
    events = [e for e in baseline["observations"] if e["message"].get("event") == "write_accounting"]
    if not events or any(any(e["message"].get(k) != v for k, v in expected.items()) for e in events):
        return None
    event = events[0]
    return {
        "namespace": attempts["namespace"],
        "dimensions": dimensions,
        "attempts_metric": attempts["metric_name"],
        "failures_metric": failures["metric_name"],
        "operation_kind": "write",
        "accounting": "completed",
        "source_ref": f"cloudwatch-logs://{event['log_group']}/{event['log_stream']}#{event['event_id']}",
        **baseline["normal"],
    }


def observe_recovery_evidence(
    alarm_data: dict,
    *,
    s3_client,
    logs_client_for_region,
    ecs_client_for_region,
    evidence_bucket: str,
    timeout_seconds: float,
) -> dict:
    """Read the pinned raw alarm with injected clients; return context/frozen observations or unavailable.

    Supply the original SNS alarm dictionary, not model scoping or control ARNs.
    The caller owns the remaining overall budget and freezes these observations
    with its incident identity before publishing any recovery part.
    """
    from headless_codex.services.recovery_evidence import prepare_recovery_evidence

    parsed = parse_alarm(alarm_data)
    alarm = SimpleNamespace(
        alarm_name=parsed.alarm_name,
        alarm_arn=alarm_data.get("AlarmArn"),
        alarm_description=parsed.alarm_description,
        region=parsed.region,
        state_change_time=_utc(parsed.state_change_time),
        eval_source_metadata=alarm_data.get("EvalSourceMetadata"),
        trigger=SimpleNamespace(
            metric_name=parsed.metric_name, namespace=parsed.namespace, dimensions=parsed.dimensions
        ),
    )
    reader = AwsIncidentObservation(
        s3_client=s3_client,
        logs_client_for_region=logs_client_for_region,
        ecs_client_for_region=ecs_client_for_region,
        evidence_bucket=evidence_bucket,
    )
    observations = reader.observe(alarm, timeout_seconds=timeout_seconds)
    return prepare_recovery_evidence(SimpleNamespace(raw_alarm=alarm, incident_observations=observations))
