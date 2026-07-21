from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from rca_agent.config.settings import (
    LLM_DEFAULT_TIMEOUT_SECONDS,
    REMEDIATION_VERIFICATION_MAX_WAIT_SECONDS,
)
from rca_agent.ports.dto.models import (
    RemediationResult,
    VerificationResult,
    VerificationStatus,
)
from rca_agent.prompts.verification import VERIFICATION_USER_PROMPT_TEMPLATE
from rca_agent.utils.timeout import call_with_timeout

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)

VERIFICATION_INGESTION_GRACE_SECONDS = 10
MAX_VERIFICATION_WAIT_SECONDS = REMEDIATION_VERIFICATION_MAX_WAIT_SECONDS
_STATISTICS = {"Average", "Sum", "Minimum", "Maximum", "SampleCount"}
_COMPARISON_OPERATORS = {
    "GreaterThanThreshold",
    "GreaterThanOrEqualToThreshold",
    "LessThanThreshold",
    "LessThanOrEqualToThreshold",
}
_EXTENDED_STATISTIC_PATTERN = re.compile(r"p(?:100(?:\.0+)?|\d{1,2}(?:\.\d+)?)", re.IGNORECASE)


class VerificationOutput(BaseModel):
    verification_summary: str = ""
    remaining_issues: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _AlarmMetricDefinition:
    namespace: str
    metric_name: str
    dimensions: list[dict[str, str]]
    statistic: str
    extended: bool
    period: int
    threshold: float
    comparison_operator: str
    evaluation_periods: int
    datapoints_to_alarm: int
    unit: str | None

    @property
    def dimensions_for_prompt(self) -> dict[str, str]:
        return {dimension["Name"]: dimension["Value"] for dimension in self.dimensions}


@dataclass(frozen=True)
class _ServerEvaluation:
    status: VerificationStatus
    summary: str
    definition: _AlarmMetricDefinition | None = None


class _UnsupportedAlarmError(ValueError):
    pass


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _UnsupportedAlarmError(f"{field_name} is missing")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _UnsupportedAlarmError(f"{field_name} must be a positive integer")
    return value


def _valid_period(period: int) -> bool:
    return period in {1, 5, 10, 20, 30} or period % 60 == 0


def _parse_alarm_definition(
    response: dict[str, Any],
    expected_alarm_name: str,
) -> _AlarmMetricDefinition:
    metric_alarms = response.get("MetricAlarms")
    composite_alarms = response.get("CompositeAlarms", [])
    if not isinstance(metric_alarms, list) or len(metric_alarms) != 1:
        raise _UnsupportedAlarmError("describe_alarms did not return exactly one metric alarm")
    if not isinstance(composite_alarms, list) or composite_alarms:
        raise _UnsupportedAlarmError("composite alarms are not supported")

    alarm = metric_alarms[0]
    if not isinstance(alarm, dict):
        raise _UnsupportedAlarmError("metric alarm definition is invalid")
    if alarm.get("AlarmName") != expected_alarm_name:
        raise _UnsupportedAlarmError("describe_alarms returned a different alarm")
    if alarm.get("Metrics"):
        raise _UnsupportedAlarmError("metric math alarms are not supported")

    namespace = _require_non_empty_string(alarm.get("Namespace"), "Namespace")
    metric_name = _require_non_empty_string(alarm.get("MetricName"), "MetricName")
    dimensions_value = alarm.get("Dimensions")
    if not isinstance(dimensions_value, list):
        raise _UnsupportedAlarmError("Dimensions must be a list")
    dimensions: list[dict[str, str]] = []
    for dimension in dimensions_value:
        if not isinstance(dimension, dict):
            raise _UnsupportedAlarmError("Dimensions contains an invalid entry")
        name = _require_non_empty_string(dimension.get("Name"), "Dimension.Name")
        value = dimension.get("Value")
        if not isinstance(value, str):
            raise _UnsupportedAlarmError("Dimension.Value must be a string")
        dimensions.append({"Name": name, "Value": value})

    statistic_value = alarm.get("Statistic")
    extended_statistic_value = alarm.get("ExtendedStatistic")
    if statistic_value and extended_statistic_value:
        raise _UnsupportedAlarmError("alarm defines both Statistic and ExtendedStatistic")
    if statistic_value:
        statistic = _require_non_empty_string(statistic_value, "Statistic")
        if statistic not in _STATISTICS:
            raise _UnsupportedAlarmError(f"unsupported statistic: {statistic}")
        extended = False
    else:
        statistic = _require_non_empty_string(extended_statistic_value, "ExtendedStatistic")
        if _EXTENDED_STATISTIC_PATTERN.fullmatch(statistic) is None:
            raise _UnsupportedAlarmError(f"unsupported extended statistic: {statistic}")
        low_sample_policy = alarm.get("EvaluateLowSampleCountPercentile")
        if low_sample_policy == "ignore":
            raise _UnsupportedAlarmError("EvaluateLowSampleCountPercentile=ignore is not supported")
        if low_sample_policy not in {None, "evaluate"}:
            raise _UnsupportedAlarmError("EvaluateLowSampleCountPercentile has an unsupported value")
        extended = True

    period = _require_positive_int(alarm.get("Period"), "Period")
    if not _valid_period(period):
        raise _UnsupportedAlarmError(f"unsupported period: {period}")
    evaluation_periods = _require_positive_int(alarm.get("EvaluationPeriods"), "EvaluationPeriods")
    datapoints_to_alarm = _require_positive_int(
        alarm.get("DatapointsToAlarm", evaluation_periods),
        "DatapointsToAlarm",
    )
    if datapoints_to_alarm > evaluation_periods:
        raise _UnsupportedAlarmError("DatapointsToAlarm exceeds EvaluationPeriods")

    threshold_value = alarm.get("Threshold")
    if isinstance(threshold_value, bool) or not isinstance(threshold_value, (int, float)):
        raise _UnsupportedAlarmError("Threshold must be numeric")
    threshold = float(threshold_value)
    if not math.isfinite(threshold):
        raise _UnsupportedAlarmError("Threshold must be finite")

    comparison_operator = _require_non_empty_string(
        alarm.get("ComparisonOperator"),
        "ComparisonOperator",
    )
    if comparison_operator not in _COMPARISON_OPERATORS:
        raise _UnsupportedAlarmError(f"unsupported comparison operator: {comparison_operator}")
    unit_value = alarm.get("Unit")
    if unit_value is not None and (not isinstance(unit_value, str) or not unit_value.strip()):
        raise _UnsupportedAlarmError("Unit must be a non-empty string")

    return _AlarmMetricDefinition(
        namespace=namespace,
        metric_name=metric_name,
        dimensions=dimensions,
        statistic=statistic,
        extended=extended,
        period=period,
        threshold=threshold,
        comparison_operator=comparison_operator,
        evaluation_periods=evaluation_periods,
        datapoints_to_alarm=datapoints_to_alarm,
        unit=unit_value,
    )


def _breaches(value: float, threshold: float, operator: str) -> bool:
    if operator == "GreaterThanThreshold":
        return value > threshold
    if operator == "GreaterThanOrEqualToThreshold":
        return value >= threshold
    if operator == "LessThanThreshold":
        return value < threshold
    if operator == "LessThanOrEqualToThreshold":
        return value <= threshold
    raise _UnsupportedAlarmError(f"unsupported comparison operator: {operator}")


def _metric_window(
    period: int,
    evaluation_periods: int,
    remediation_time: float,
    now: float,
) -> tuple[datetime | None, datetime | None, list[int]]:
    first_post_remediation_start = int(math.ceil(remediation_time / period) * period)
    last_complete_period_end = int(now // period) * period
    available_periods = list(
        range(
            first_post_remediation_start,
            last_complete_period_end,
            period,
        )
    )
    expected_timestamps = available_periods[-evaluation_periods:]
    if not expected_timestamps:
        return None, None, []
    return (
        datetime.fromtimestamp(expected_timestamps[0], tz=UTC),
        datetime.fromtimestamp(expected_timestamps[-1] + period, tz=UTC),
        expected_timestamps,
    )


def _evaluate_datapoints(
    datapoints: Any,
    definition: _AlarmMetricDefinition,
    expected_timestamps: list[int],
) -> _ServerEvaluation:
    if not isinstance(datapoints, list):
        return _ServerEvaluation(
            VerificationStatus.PENDING,
            "CloudWatch returned an invalid datapoint collection.",
            definition,
        )

    def timestamp_sort_key(item: Any) -> float:
        if not isinstance(item, dict):
            return float("-inf")
        timestamp = item.get("Timestamp")
        if not isinstance(timestamp, datetime):
            return float("-inf")
        return timestamp.timestamp()

    value_by_timestamp: dict[int, float] = {}
    for datapoint in sorted(
        datapoints,
        key=timestamp_sort_key,
    ):
        if not isinstance(datapoint, dict):
            continue
        timestamp = datapoint.get("Timestamp")
        if not isinstance(timestamp, datetime):
            continue
        timestamp_epoch = int(timestamp.timestamp())
        if timestamp_epoch not in expected_timestamps or timestamp_epoch in value_by_timestamp:
            continue
        if definition.extended:
            extended_statistics = datapoint.get("ExtendedStatistics")
            raw_value = extended_statistics.get(definition.statistic) if isinstance(extended_statistics, dict) else None
        else:
            raw_value = datapoint.get(definition.statistic)
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            continue
        value = float(raw_value)
        if math.isfinite(value):
            value_by_timestamp[timestamp_epoch] = value

    ordered_values = [
        value_by_timestamp[timestamp] for timestamp in expected_timestamps if timestamp in value_by_timestamp
    ]
    breach_count = sum(
        _breaches(value, definition.threshold, definition.comparison_operator) for value in ordered_values
    )
    if breach_count >= definition.datapoints_to_alarm:
        return _ServerEvaluation(
            VerificationStatus.FAILED,
            (
                f"CloudWatch observed {breach_count} breaching datapoints across "
                f"{len(value_by_timestamp)} available periods; "
                f"{definition.datapoints_to_alarm} breaches trigger the alarm."
            ),
            definition,
        )
    if len(value_by_timestamp) != definition.evaluation_periods:
        return _ServerEvaluation(
            VerificationStatus.PENDING,
            (
                "Insufficient CloudWatch datapoints: "
                f"received {len(value_by_timestamp)} of {definition.evaluation_periods} required periods."
            ),
            definition,
        )

    return _ServerEvaluation(
        VerificationStatus.NORMALIZED,
        (
            f"CloudWatch observed {breach_count} breaching datapoints across "
            f"all {definition.evaluation_periods} periods; "
            f"fewer than {definition.datapoints_to_alarm} breaches trigger the alarm."
        ),
        definition,
    )


def _load_alarm_definition(
    cloudwatch_client: Any,
    alarm_name: str,
) -> _AlarmMetricDefinition | _ServerEvaluation:
    if cloudwatch_client is None:
        return _ServerEvaluation(
            VerificationStatus.PENDING,
            "CloudWatch client is unavailable.",
        )
    if not alarm_name or alarm_name == "unknown":
        return _ServerEvaluation(
            VerificationStatus.PENDING,
            "Original alarm name is unavailable.",
        )

    try:
        return _parse_alarm_definition(
            cloudwatch_client.describe_alarms(AlarmNames=[alarm_name]),
            alarm_name,
        )
    except _UnsupportedAlarmError as exc:
        logger.warning("Verification is pending for alarm %s: %s", alarm_name, exc)
        return _ServerEvaluation(
            VerificationStatus.PENDING,
            f"CloudWatch alarm cannot be evaluated: {exc}.",
        )
    except Exception as exc:
        logger.error("CloudWatch alarm lookup failed for alarm %s: %s", alarm_name, exc)
        return _ServerEvaluation(
            VerificationStatus.PENDING,
            f"CloudWatch observation failed: {exc}.",
        )


def _verification_wait_seconds(
    definition: _AlarmMetricDefinition,
    remediation_time: float,
    now: float,
) -> float:
    if not math.isfinite(remediation_time) or not math.isfinite(now):
        raise _UnsupportedAlarmError("verification timestamps must be finite")

    first_post_remediation_start = math.ceil(remediation_time / definition.period) * definition.period
    observation_ready_at = (
        first_post_remediation_start
        + definition.evaluation_periods * definition.period
        + VERIFICATION_INGESTION_GRACE_SECONDS
    )
    wait = max(0.0, observation_ready_at - now)
    if wait > MAX_VERIFICATION_WAIT_SECONDS:
        raise _UnsupportedAlarmError(f"required verification wait of {wait:.0f}s exceeds the bounded limit")
    return wait


def _evaluate_cloudwatch(
    cloudwatch_client: Any,
    definition: _AlarmMetricDefinition,
    remediation_time: float,
    now: float,
) -> _ServerEvaluation:
    try:
        start_time, end_time, expected_timestamps = _metric_window(
            definition.period,
            definition.evaluation_periods,
            remediation_time,
            now,
        )
        if start_time is None or end_time is None:
            return _ServerEvaluation(
                VerificationStatus.PENDING,
                "No complete post-remediation CloudWatch period is available yet.",
                definition,
            )
        request: dict[str, Any] = {
            "Namespace": definition.namespace,
            "MetricName": definition.metric_name,
            "Dimensions": definition.dimensions,
            "StartTime": start_time,
            "EndTime": end_time,
            "Period": definition.period,
        }
        if definition.unit is not None:
            request["Unit"] = definition.unit
        if definition.extended:
            request["ExtendedStatistics"] = [definition.statistic]
        else:
            request["Statistics"] = [definition.statistic]
        response = cloudwatch_client.get_metric_statistics(**request)
        return _evaluate_datapoints(
            response.get("Datapoints"),
            definition,
            expected_timestamps,
        )
    except Exception as exc:
        logger.error(
            "CloudWatch metric verification failed for %s/%s: %s",
            definition.namespace,
            definition.metric_name,
            exc,
        )
        return _ServerEvaluation(
            VerificationStatus.PENDING,
            f"CloudWatch observation failed: {exc}.",
            definition,
        )


def _build_user_prompt(
    *,
    alarm_name: str,
    remediation: RemediationResult,
    seconds_since: int,
    evaluation: _ServerEvaluation,
) -> str:
    definition = evaluation.definition
    action_lines = []
    for action in remediation.actions_taken:
        status = "SUCCESS" if action.success else "FAILED"
        action_lines.append(f"- [{status}] {action.description}")

    return VERIFICATION_USER_PROMPT_TEMPLATE.format(
        alarm_name=alarm_name,
        namespace=definition.namespace if definition else "Unavailable",
        metric_name=definition.metric_name if definition else "Unavailable",
        dimensions=json.dumps(
            definition.dimensions_for_prompt if definition else {},
            sort_keys=True,
        ),
        statistic=definition.statistic if definition else "Unavailable",
        period=definition.period if definition else "Unavailable",
        threshold=definition.threshold if definition else "Unavailable",
        comparison_operator=definition.comparison_operator if definition else "Unavailable",
        evaluation_periods=definition.evaluation_periods if definition else "Unavailable",
        datapoints_to_alarm=definition.datapoints_to_alarm if definition else "Unavailable",
        server_status=evaluation.status.value,
        server_evaluation=evaluation.summary,
        remediation_summary="\n".join(action_lines) or "No actions taken",
        seconds_since_remediation=seconds_since,
    )


def _llm_summary(
    *,
    agent: Agent | None,
    prompt: str,
    timeout: int,
) -> VerificationOutput | None:
    if agent is None:
        return None

    try:
        result = call_with_timeout(
            lambda: agent(
                prompt,
                output_model=VerificationOutput,
            ),
            timeout,
        )
        return VerificationOutput.model_validate(result["output"])
    except Exception as exc:
        logger.warning("Optional verification summary failed: %s", exc)
        return None


def run_verification(
    *,
    cloudwatch_client: Any,
    alarm_name: str,
    remediation: RemediationResult,
    remediation_time: float,
    agent: Agent | None = None,
    timeout: int = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> VerificationResult:
    definition_or_error = _load_alarm_definition(
        cloudwatch_client,
        alarm_name,
    )
    if isinstance(definition_or_error, _ServerEvaluation):
        evaluation = definition_or_error
    else:
        definition = definition_or_error
        try:
            wait = _verification_wait_seconds(
                definition,
                remediation_time,
                time.time(),
            )
        except _UnsupportedAlarmError as exc:
            logger.warning("Verification is pending for alarm %s: %s", alarm_name, exc)
            evaluation = _ServerEvaluation(
                VerificationStatus.PENDING,
                f"CloudWatch alarm cannot be evaluated: {exc}.",
                definition,
            )
        else:
            if wait > 0:
                logger.info(
                    "Waiting %.0fs for %d complete CloudWatch periods",
                    wait,
                    definition.evaluation_periods,
                )
                time.sleep(wait)
            evaluation = _evaluate_cloudwatch(
                cloudwatch_client,
                definition,
                remediation_time,
                time.time(),
            )

    elapsed = max(0, int(time.time() - remediation_time))
    output = _llm_summary(
        agent=agent,
        prompt=_build_user_prompt(
            alarm_name=alarm_name,
            remediation=remediation,
            seconds_since=elapsed,
            evaluation=evaluation,
        ),
        timeout=timeout,
    )
    summary = evaluation.summary
    remaining_issues: list[str] = []
    if output is not None:
        if output.verification_summary.strip():
            summary = f"{summary} Additional assessment: {output.verification_summary.strip()}"
        remaining_issues = output.remaining_issues

    logger.info(
        "Verification completed",
        extra={
            "status": evaluation.status.value,
            "issues": len(remaining_issues),
        },
    )
    return VerificationResult(
        rca_id=remediation.rca_id,
        status=evaluation.status,
        verification_summary=summary,
        remaining_issues=remaining_issues,
    )
