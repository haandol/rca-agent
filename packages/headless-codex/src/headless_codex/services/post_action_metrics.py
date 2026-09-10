"""Fixed post-action observations; clocks, waiting, commands and persistence are injectable.

This module never invokes AWS or changes an execution outcome. The execution MCP supplies
its gated, audited CLI executor. All journal entries are append-only execution evidence.
"""

from __future__ import annotations

import json
import math
import shlex
import time
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event


class ObservationStoppedError(Exception):
    """The existing execution budget, cancellation or claim no longer permits observation."""


def utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an aware ISO time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.timestamp()


def number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("nonfinite or nonnumeric metric value")
    if value < 0:
        raise ValueError("negative metric value")
    return float(value)


class ObservationBudget:
    def __init__(
        self,
        deadline: float,
        check_control: Callable[[], float],
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], object] | None = None,
    ) -> None:
        self.deadline = deadline
        self.clock = clock
        self.monotonic = monotonic
        self.monotonic_deadline = monotonic() + max(0, deadline - clock())
        self.check_control = check_control
        self.wait_event = Event()
        self.wait = wait or self.wait_event.wait

    def remaining(self) -> float:
        execution_deadline = self.check_control()
        remaining = min(
            self.deadline - self.clock(),
            self.monotonic_deadline - self.monotonic(),
            execution_deadline - self.clock(),
        )
        if remaining <= 0:
            raise ObservationStoppedError("availability or execution deadline exhausted")
        return remaining

    def pause(self, seconds: float = 5) -> None:
        # Short Event.wait intervals also check the runner's claim/cancel heartbeat.
        end = self.monotonic() + min(seconds, self.remaining())
        while self.monotonic() < end:
            self.wait(min(0.5, end - self.monotonic(), self.remaining()))
        self.remaining()


def normalize_request(
    step_id: str,
    action_step_id: str,
    metrics: dict,
    failure_alarm_name: str,
    latency_alarm_name: str,
    region: str,
    max_wait_seconds: int,
    completed_work_evidence: dict | None = None,
) -> dict:
    def text(value: object) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise ValueError("nonblank bounded metric coordinate required")
        if any(c in value for c in ("\n", "\r", "\x00")):
            raise ValueError("invalid metric coordinate")
        return value

    if type(max_wait_seconds) is not int or not 1 <= max_wait_seconds <= 300:
        raise ValueError("max_wait_seconds must be between 1 and 300")
    if not isinstance(metrics, dict) or set(metrics) not in (
        {"attempts", "failures"},
        {"attempts", "failures", "latency"},
    ):
        raise ValueError("metrics requires attempts/failures and optional approved latency")
    if bool(latency_alarm_name) != ("latency" in metrics):
        raise ValueError("latency descriptor and optional alarm must be supplied together")
    normalized = {}
    for role, metric in metrics.items():
        if not isinstance(metric, dict) or set(metric) != {"namespace", "metric_name", "dimensions"}:
            raise ValueError("metric requires namespace, metric_name and dimensions only")
        dimensions = metric["dimensions"]
        if not isinstance(dimensions, dict) or not 1 <= len(dimensions) <= 30:
            raise ValueError("explicit nonempty observed dimensions required")
        normalized[role] = {
            "namespace": text(metric["namespace"]),
            "metric_name": text(metric["metric_name"]),
            "dimensions": {text(k): text(v) for k, v in sorted(dimensions.items())},
        }
    if len({m["metric_name"] for m in normalized.values()}) != len(normalized):
        raise ValueError("attempt, failure and query metrics must be distinct")
    base = normalized["failures"]
    if any(m["namespace"] != base["namespace"] or m["dimensions"] != base["dimensions"] for m in normalized.values()):
        raise ValueError("all metrics must share the approved namespace and dimensions")
    return {
        "step_id": text(step_id),
        "action_step_id": text(action_step_id),
        "metrics": normalized,
        "failure_alarm_name": text(failure_alarm_name),
        "latency_alarm_name": text(latency_alarm_name) if latency_alarm_name else "",
        "region": text(region),
        "max_wait_seconds": max_wait_seconds,
        "completed_work_evidence": completed_work_evidence,
    }


def _option(argv: list | tuple, name: str) -> str | None:
    # Observed commands with ambiguous duplicate options are not metadata authority.
    if argv.count(name) != 1:
        return None
    index = argv.index(name)
    return argv[index + 1] if index + 1 < len(argv) else None


def _payload(record: dict) -> dict | None:
    if record.get("stdout_truncated") or record.get("output_incomplete"):
        return None
    try:
        data = json.loads(record.get("stdout", ""))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _unaltered_aws_observation(argv: tuple[str, ...]) -> bool:
    """Synthetic output, alternate credentials/endpoints and input overrides are not evidence."""
    forbidden = {
        "--query",
        "--generate-cli-skeleton",
        "--endpoint-url",
        "--profile",
        "--cli-input-json",
        "--cli-input-yaml",
        "--dry-run",
    }
    return not any(arg.split("=", 1)[0] in forbidden for arg in argv[3:])


def _dimensions(value: object) -> dict:
    if not isinstance(value, list):
        raise ValueError("missing observed dimensions")
    result = {}
    for pair in value:
        if not isinstance(pair, dict) or set(pair) != {"Name", "Value"} or pair["Name"] in result:
            raise ValueError("invalid or duplicate observed dimensions")
        result[pair["Name"]] = pair["Value"]
    return result


def metric_matches(metadata: dict, metric: dict) -> bool:
    try:
        return (
            metadata.get("Namespace") == metric["namespace"]
            and metadata.get("MetricName") == metric["metric_name"]
            and _dimensions(metadata.get("Dimensions")) == metric["dimensions"]
        )
    except (ValueError, TypeError):
        return False


def bind_request(request: dict, records: list[dict], context: dict, execution_id: str, gate: Callable) -> dict:
    """Bind to approved order and intact server observations from this execution only."""
    steps = context.get("playbook", {}).get("execution_steps", [])
    ids = [s.get("step_id") for s in steps if isinstance(s, dict)]
    step, action = request["step_id"], request["action_step_id"]
    if step not in ids or action not in ids or ids.index(action) >= ids.index(step):
        raise ValueError("approved prior action and current verification step required")
    criterion = steps[ids.index(step)].get("success_criteria", "")
    failure = request["metrics"]["failures"]
    if failure["metric_name"] not in criterion or request["failure_alarm_name"] not in criterion:
        raise ValueError("failure metric and exact alarm must belong to the approved verification criterion")
    if context.get("alarm_name") != request["failure_alarm_name"]:
        raise ValueError("failure alarm differs from the current approved alarm context")
    if "latency" in request["metrics"] and (
        request["metrics"]["latency"]["metric_name"] not in criterion or request["latency_alarm_name"] not in criterion
    ):
        raise ValueError("optional latency must be explicitly included in the approved verification criterion")
    anchor = None
    metadata = []
    for index, record in enumerate(records):
        if (
            record.get("type") != "attempt"
            or record.get("execution_id") != execution_id
            or record.get("step_id") not in ids[: ids.index(step) + 1]
            or record.get("succeeded") is not True
            or str(record.get("exit_status")) != "0"
            or record.get("blocked")
        ):
            continue
        verdict = gate(record.get("command", ""))
        if not verdict.allowed:
            continue
        if not _unaltered_aws_observation(verdict.argv):
            if record.get("step_id") == action and (verdict.service, verdict.operation) == ("ecs", "stop-task"):
                raise ValueError("synthetic or overridden StopTask output cannot anchor an observation")
            continue
        region = _option(verdict.argv, "--region")
        payload = _payload(record)
        if record.get("step_id") == action and (verdict.service, verdict.operation) == ("ecs", "stop-task"):
            # The first successful action fixes the anchor; never pick a later retry for better bins.
            if anchor is not None:
                continue
            if region != request["region"] or payload is None:
                raise ValueError("action scope or intact StopTask response unavailable")
            task = payload.get("task", {})
            task_arn = task.get("taskArn", "")
            cluster_arn = task.get("clusterArn", "")
            if (
                _option(verdict.argv, "--task") != task_arn
                or not task_arn.startswith(f"arn:aws:ecs:{region}:")
                or _option(verdict.argv, "--cluster") not in (cluster_arn, cluster_arn.rsplit("/", 1)[-1])
                or not cluster_arn.startswith(f"arn:aws:ecs:{region}:")
                or task_arn.split(":")[4] != cluster_arn.split(":")[4]
            ):
                raise ValueError("StopTask response does not match the actual approved command scope")
            ended = timestamp(record.get("ended_at"))
            anchor = {
                "record_index": index,
                "action_step_id": action,
                "ended_at": utc(ended),
                "command": record["command"],
                "task_arn": task_arn,
                "cluster_arn": cluster_arn,
                "account_id": task_arn.split(":")[4],
            }
        if verdict.service == "cloudwatch" and region == request["region"] and payload is not None:
            if verdict.operation == "list-metrics":
                metadata.extend(payload.get("Metrics", []))
            elif verdict.operation == "describe-alarms":
                metadata.extend(payload.get("MetricAlarms", []))
    if anchor is None:
        raise ValueError("successful server-recorded current execution StopTask action required")
    alarm_context = context.get("alarm_data", {})
    if alarm_context.get("AWSAccountId") and alarm_context["AWSAccountId"] != anchor["account_id"]:
        raise ValueError("action account differs from approved incident context")
    expected_incident_arn = (
        f"arn:aws:cloudwatch:{request['region']}:{anchor['account_id']}:alarm:{request['failure_alarm_name']}"
    )
    if alarm_context.get("AlarmArn") and alarm_context["AlarmArn"] != expected_incident_arn:
        raise ValueError("action region/account differs from approved incident alarm")
    for role, metric in request["metrics"].items():
        if not any(isinstance(m, dict) and metric_matches(m, metric) for m in metadata):
            raise ValueError(f"{role} coordinates require intact server-recorded metric/alarm discovery")
    alarms = {}
    for role in ("failures", "latency") if "latency" in request["metrics"] else ("failures",):
        name = request["failure_alarm_name" if role == "failures" else "latency_alarm_name"]
        matches = [
            m
            for m in metadata
            if isinstance(m, dict) and m.get("AlarmName") == name and metric_matches(m, request["metrics"][role])
        ]
        if not matches:
            raise ValueError(f"observed {role} alarm metadata missing")
        alarm = matches[-1]
        expected_arn = f"arn:aws:cloudwatch:{request['region']}:{anchor['account_id']}:alarm:{name}"
        if alarm.get("AlarmArn") != expected_arn or alarm.get("Period") != 60 or "Metrics" in alarm:
            raise ValueError("simple same-scope 60-second alarm metadata required")
        if alarm.get("ComparisonOperator") not in ("GreaterThanOrEqualToThreshold", "GreaterThanThreshold"):
            raise ValueError("unsupported alarm comparison")
        number(alarm.get("Threshold"))
        if alarm.get("Statistic") not in ("Average", "Maximum", "Sum"):
            raise ValueError("unsupported alarm statistic")
        if role == "failures" and alarm["Statistic"] != "Sum":
            raise ValueError("failure alarm must use Sum")
        alarms[role] = alarm
    accounting = _completed_accounting(request, records, context, execution_id, ids[: ids.index(step) + 1], gate)
    start = math.floor(timestamp(anchor["ended_at"]) / 60) * 60 + 60
    return {
        "anchor": anchor,
        "start": utc(start),
        "end": utc(start + 120),
        "alarms": alarms,
        "completed_write_accounting": accounting,
    }


def _completed_accounting(
    request: dict, records: list[dict], context: dict, execution_id: str, ids: list, gate: Callable
):
    """Optional producer descriptor reference, never caller-supplied counts/semantic assertions.

    Without an actual descriptor the arithmetic remains useful availability evidence, but
    the operator must verify successful writes independently. The current approved plan
    does not gain a dependency on a newly invented metric/producer descriptor.
    """
    reference = request.get("completed_work_evidence")
    if reference is None:
        return None
    if not isinstance(reference, dict) or set(reference) != {"record_index", "json_pointer"}:
        raise ValueError("completed_work_evidence requires record_index and json_pointer")
    index, pointer = reference["record_index"], reference["json_pointer"]
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("source descriptor requires JSON pointer")
    if index == "approved_context":
        document = context
    elif type(index) is int and 0 <= index < len(records):
        source = records[index]
        if (
            source.get("type") != "attempt"
            or source.get("execution_id") != execution_id
            or source.get("step_id") not in ids
            or source.get("succeeded") is not True
            or str(source.get("exit_status")) != "0"
        ):
            raise ValueError("completed-work evidence must be a successful current execution observation")
        verdict = gate(source.get("command", ""))
        if (
            not verdict.allowed
            or not _unaltered_aws_observation(verdict.argv)
            or _option(verdict.argv, "--region") != request["region"]
        ):
            raise ValueError("completed-work evidence is outside the observed execution scope")
        if (verdict.service, verdict.operation) not in (
            ("logs", "get-log-events"),
            ("logs", "filter-log-events"),
        ):
            raise ValueError("completed-work descriptor requires an actual producer log observation")
        document = _payload(source)
    else:
        raise ValueError("completed-work source reference unavailable")
    try:
        for part in pointer[1:].split("/"):
            if isinstance(document, str):
                document = json.loads(document)
            key = part.replace("~1", "/").replace("~0", "~")
            document = document[int(key)] if isinstance(document, list) else document[key]
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise ValueError("completed-work source descriptor unavailable") from exc
    expected = {
        "namespace": request["metrics"]["attempts"]["namespace"],
        "dimensions": request["metrics"]["attempts"]["dimensions"],
        "attempts_metric": request["metrics"]["attempts"]["metric_name"],
        "failures_metric": request["metrics"]["failures"]["metric_name"],
        "operation_kind": "write",
        "accounting": "completed",
    }
    if not isinstance(document, dict) or any(document.get(k) != v for k, v in expected.items()):
        raise ValueError("source descriptor does not prove completed-write accounting for these metrics")
    return {"reference": reference, "descriptor": document}


def metric_command(metric: dict, role: str, bound: dict, region: str) -> str:
    statistic = bound["alarms"]["latency"]["Statistic"] if role == "latency" else "Sum"
    dimensions = [{"Name": k, "Value": v} for k, v in metric["dimensions"].items()]
    return shlex.join(
        [
            "aws",
            "cloudwatch",
            "get-metric-statistics",
            "--namespace",
            metric["namespace"],
            "--metric-name",
            metric["metric_name"],
            "--dimensions",
            json.dumps(dimensions),
            "--start-time",
            bound["start"],
            "--end-time",
            bound["end"],
            "--period",
            "60",
            "--statistics",
            statistic,
            "SampleCount",
            "--region",
            region,
            "--output",
            "json",
        ]
    )


def assess_metric(role: str, response: dict, bound: dict, observed_at: float) -> tuple[dict, list[str]]:
    """Evaluate unhealthy complete datapoints even if sibling datapoints/queries are missing."""
    if response.get("blocked"):
        return {}, ["CLI gate blocked the metric query"]
    if response.get("stdout_truncated") or response.get("output_incomplete"):
        return {}, ["incomplete metric CLI output"]
    try:
        payload = json.loads(response.get("stdout", ""))
        points = payload["Datapoints"]
        if not isinstance(points, list):
            raise ValueError("Datapoints must be a list")
        if "Label" in payload and payload["Label"] != bound["request"]["metrics"][role]["metric_name"]:
            raise ValueError("metric label mismatch")
    except (ValueError, TypeError, KeyError):
        return {}, ["invalid metric CLI JSON"] if response.get("ok") else []
    start = timestamp(bound["start"])
    statistic = bound["alarms"]["latency"]["Statistic"] if role == "latency" else "Sum"
    seen, complete, failures = set(), {}, []
    for point in points:
        try:
            t = timestamp(point.get("Timestamp"))
            if t % 60:
                raise ValueError("misaligned datapoint")
            if t in seen:
                raise ValueError("duplicate datapoint")
            seen.add(t)
            # Reject malformed numbers wherever observed, including current/outside points.
            for name in ("Sum", "Average", "Maximum", "Minimum", "SampleCount"):
                if name in point:
                    number(point[name])
            if t not in (start, start + 60) or t + 60 > observed_at:
                continue
            value = number(point[statistic]) if statistic in point else None
            sample = number(point["SampleCount"]) if "SampleCount" in point else None
            expected_unit = "Count" if role != "latency" else bound["alarms"]["latency"].get("Unit")
            if role == "latency" and point.get("Unit") not in ("Seconds", "Milliseconds", "Microseconds"):
                raise ValueError("query latency unit unavailable or invalid")
            if expected_unit and point.get("Unit") != expected_unit:
                raise ValueError("metric unit disagrees with observed alarm/count unit")
            if sample is not None and sample <= 0:
                failures.append(f"{role} {utc(t)} SampleCount is not positive")
            if value is not None:
                if role == "attempts" and (value <= 0 or not value.is_integer()):
                    failures.append(f"attempts {utc(t)} is not a positive integer")
                if role == "failures" and value != 0:
                    failures.append(f"failures {utc(t)} is nonzero")
                if role == "latency":
                    alarm = bound["alarms"]["latency"]
                    if value > alarm["Threshold"] or (
                        value == alarm["Threshold"] and alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
                    ):
                        failures.append(f"latency {utc(t)} breaches observed alarm threshold")
            if value is not None and sample is not None and sample > 0 and response.get("ok"):
                complete[utc(t)] = {"value": value, "sample_count": sample, "unit": point["Unit"]}
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            failures.append(str(exc))
    return complete, failures


def poll_fixed_metrics(
    bound: dict,
    budget: ObservationBudget,
    execute: Callable[[str, ObservationBudget], dict],
    append: Callable[[dict], None],
) -> dict:
    request = bound["request"]
    step_id = request["step_id"]

    def record(phase: str, **fields: object) -> dict:
        receipt = {
            "type": "metric_wait",
            "phase": phase,
            "step_id": step_id,
            "observed_at": utc(budget.clock()),
            **fields,
        }
        append(receipt)
        return receipt

    try:
        while True:
            budget.remaining()
            # No fixed bin can be assessed before the first one ends. Keep checking
            # cancellation/claim while waiting, without issuing unusable metric reads.
            first_complete = timestamp(bound["start"]) + 60
            if budget.clock() < first_complete:
                budget.pause(min(10, first_complete - budget.clock()))
                continue
            values, failures = {}, []
            for role, metric in request["metrics"].items():
                command = metric_command(metric, role, bound, request["region"])
                response = execute(command, budget)
                observed = budget.clock()
                # Persist before parsing so malformed/partial/error output cannot be lost.
                record("poll", role=role, command=command, response=response)
                values[role], errors = assess_metric(role, response, bound, observed)
                failures.extend(errors)
                if failures:
                    return record("terminal", status="UNHEALTHY", ok=False, failures=failures, binding=bound)
                budget.remaining()
            command = shlex.join(
                [
                    "aws",
                    "cloudwatch",
                    "describe-alarms",
                    "--alarm-names",
                    request["failure_alarm_name"],
                    *([request["latency_alarm_name"]] if request["latency_alarm_name"] else []),
                    "--region",
                    request["region"],
                    "--output",
                    "json",
                ]
            )
            response = execute(command, budget)
            record("poll", role="alarms", command=command, response=response)
            alarm_ok = False
            if response.get("ok") and not response.get("stdout_truncated"):
                try:
                    alarms = json.loads(response.get("stdout", ""))["MetricAlarms"]
                    # A configuration change invalidates this request; never silently adopt a new threshold.
                    for role, original in bound["alarms"].items():
                        found = [a for a in alarms if a.get("AlarmName") == original["AlarmName"]]
                        if len(found) != 1:
                            raise ValueError("missing/duplicate current alarm")
                        current = found[0]
                        for key in (
                            "AlarmArn",
                            "Namespace",
                            "MetricName",
                            "Dimensions",
                            "Period",
                            "Statistic",
                            "Threshold",
                            "ComparisonOperator",
                            "Unit",
                        ):
                            if current.get(key) != original.get(key):
                                raise ValueError("observed alarm metadata changed")
                        if role == "failures":
                            alarm_ok = current.get("StateValue") == "OK"
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    return record("terminal", status="INVALID_DATA", ok=False, error=str(exc), binding=bound)
            starts = (bound["start"], utc(timestamp(bound["start"]) + 60))
            if alarm_ok and all(set(values.get(role, {})) == set(starts) for role in request["metrics"]):
                budget.remaining()
                bins = [
                    {
                        "start": t,
                        "end": utc(timestamp(t) + 60),
                        "attempts": values["attempts"][t]["value"],
                        "failures": values["failures"][t]["value"],
                        "attempts_minus_failures": values["attempts"][t]["value"] - values["failures"][t]["value"],
                        **(
                            {"successful_writes": values["attempts"][t]["value"] - values["failures"][t]["value"]}
                            if bound.get("completed_write_accounting")
                            else {}
                        ),
                        **(
                            {
                                "query_sample_count": values["latency"][t]["sample_count"],
                                "query_latency": values["latency"][t],
                            }
                            if "latency" in values
                            else {}
                        ),
                    }
                    for t in starts
                ]
                return record(
                    "terminal",
                    status="HEALTHY",
                    ok=True,
                    bins=bins,
                    binding=bound,
                    write_semantics_verified=bool(bound.get("completed_write_accounting")),
                    guidance=(
                        "Metric receipt only; separately verify exact owned release and all approved criteria. "
                        "Without completed-write accounting evidence, attempts-minus-failures is arithmetic, "
                        "not successful-write proof: verify real write successes independently."
                    ),
                )
            budget.pause(10)
    except ObservationStoppedError as exc:
        return record("terminal", status="UNOBSERVABLE", ok=False, error=str(exc), binding=bound)
