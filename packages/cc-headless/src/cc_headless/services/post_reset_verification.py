from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cc_headless.ports.dto.models import parse_alarm

_STATISTICS = {"Average", "Sum", "Minimum", "Maximum", "SampleCount"}
_COMPARISONS: dict[str, Callable[[float, float], bool]] = {
    "GreaterThanThreshold": lambda value, threshold: value > threshold,
    "GreaterThanOrEqualToThreshold": lambda value, threshold: value >= threshold,
    "LessThanThreshold": lambda value, threshold: value < threshold,
    "LessThanOrEqualToThreshold": lambda value, threshold: value <= threshold,
}


def _pending(reason: str, *, attempts: int = 0, alarm=None) -> dict:
    return {
        "status": "PENDING",
        "namespace": alarm.namespace if alarm else None,
        "metric_name": alarm.metric_name if alarm else None,
        "comparison_operator": alarm.comparison_operator if alarm else None,
        "threshold": alarm.threshold if alarm else None,
        "observed_value": None,
        "observed_at": None,
        "attempts": attempts,
        "reason": reason,
    }


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _complete_period_window(
    started_at: datetime,
    current_time: datetime,
    period: int,
    evaluation_periods: int,
) -> list[int]:
    first_period_start = math.ceil(started_at.timestamp() / period) * period
    last_complete_period_end = math.floor(current_time.timestamp() / period) * period
    available_periods = list(range(first_period_start, last_complete_period_end, period))
    if len(available_periods) < evaluation_periods:
        return []
    return available_periods[-evaluation_periods:]


def _datapoint_timestamp(point: Any) -> float:
    if not isinstance(point, dict):
        return float("-inf")
    timestamp = point.get("Timestamp")
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        return float("-inf")
    return timestamp.timestamp()


def _window_values(
    datapoints: Any,
    *,
    statistic: str,
    expected_timestamps: list[int],
    comparison: Callable[[float, float], bool],
    threshold: float,
) -> dict[int, float] | None:
    if not isinstance(datapoints, list):
        return None

    values_by_timestamp: dict[int, list[float]] = {}
    expected = set(expected_timestamps)
    for point in sorted(datapoints, key=_datapoint_timestamp):
        if not isinstance(point, dict):
            continue
        timestamp = point.get("Timestamp")
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            continue
        timestamp_value = timestamp.timestamp()
        if not timestamp_value.is_integer():
            continue
        timestamp_epoch = int(timestamp_value)
        raw_value = point.get(statistic)
        if timestamp_epoch not in expected or isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            continue
        value = float(raw_value)
        if math.isfinite(value):
            values_by_timestamp.setdefault(timestamp_epoch, []).append(value)

    # Duplicate timestamps represent one period. Prefer a breaching value if
    # conflicting duplicates are returned so verification cannot normalize early.
    return {
        timestamp: next(
            (value for value in values if comparison(value, threshold)),
            values[0],
        )
        for timestamp, values in values_by_timestamp.items()
    }


def _result(
    status: str,
    reason: str,
    *,
    alarm,
    values_by_timestamp: dict[int, float],
    attempt: int,
) -> dict:
    latest_timestamp = max(values_by_timestamp, default=None)
    return {
        "status": status,
        "namespace": alarm.namespace,
        "metric_name": alarm.metric_name,
        "comparison_operator": alarm.comparison_operator,
        "threshold": alarm.threshold,
        "observed_value": values_by_timestamp.get(latest_timestamp),
        "observed_at": (
            datetime.fromtimestamp(latest_timestamp, tz=UTC).isoformat() if latest_timestamp is not None else None
        ),
        "attempts": attempt,
        "reason": reason,
    }


def verify_post_reset(
    alarm_data: dict,
    cloudwatch_client,
    *,
    attempts: int,
    interval_seconds: int,
    started_at: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict:
    alarm = parse_alarm(alarm_data)
    statistic = alarm.statistic or "Average"
    comparison = _COMPARISONS.get(alarm.comparison_operator or "")
    if (
        not cloudwatch_client
        or not alarm.namespace
        or not alarm.metric_name
        or isinstance(alarm.threshold, bool)
        or not isinstance(alarm.threshold, (int, float))
        or not math.isfinite(float(alarm.threshold))
        or comparison is None
        or statistic not in _STATISTICS
        or not _positive_int(alarm.period)
        or not _positive_int(alarm.evaluation_periods)
        or not _positive_int(alarm.datapoints_to_alarm)
        or alarm.datapoints_to_alarm > alarm.evaluation_periods
    ):
        return _pending("alarm metric configuration is insufficient for server verification", alarm=alarm)

    started_at = started_at or now()
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    started_at = started_at.astimezone(UTC)
    period = alarm.period
    evaluation_periods = alarm.evaluation_periods
    datapoints_to_alarm = alarm.datapoints_to_alarm
    attempts = max(attempts, 1)
    last_failure: dict[str, Any] | None = None
    last_pending_reason = "no complete post-reset CloudWatch evaluation window was available"
    errors: list[str] = []

    for attempt in range(1, attempts + 1):
        current_time = now()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        current_time = current_time.astimezone(UTC)
        expected_timestamps = _complete_period_window(
            started_at,
            current_time,
            period,
            evaluation_periods,
        )
        if not expected_timestamps:
            last_pending_reason = f"fewer than {evaluation_periods} complete post-reset periods were available"
            if attempt < attempts:
                sleep(max(interval_seconds, 0))
            continue

        try:
            response = cloudwatch_client.get_metric_statistics(
                Namespace=alarm.namespace,
                MetricName=alarm.metric_name,
                Dimensions=[{"Name": name, "Value": value} for name, value in alarm.dimensions.items()],
                StartTime=datetime.fromtimestamp(expected_timestamps[0], tz=UTC),
                EndTime=datetime.fromtimestamp(expected_timestamps[-1] + period, tz=UTC),
                Period=period,
                Statistics=[statistic],
            )
            values_by_timestamp = _window_values(
                response.get("Datapoints"),
                statistic=statistic,
                expected_timestamps=expected_timestamps,
                comparison=comparison,
                threshold=float(alarm.threshold),
            )
            if values_by_timestamp is None:
                last_failure = None
                last_pending_reason = "CloudWatch returned an invalid datapoint collection"
                if attempt < attempts:
                    sleep(max(interval_seconds, 0))
                continue
            missing_count = evaluation_periods - len(values_by_timestamp)
            if missing_count:
                if alarm.treat_missing_data not in {"breaching", "notBreaching"}:
                    last_failure = None
                    last_pending_reason = (
                        f"received {len(values_by_timestamp)} of {evaluation_periods} required datapoints "
                        "without a supported missing-data policy"
                    )
                    if attempt < attempts:
                        sleep(max(interval_seconds, 0))
                    continue
                missing_breaches = missing_count if alarm.treat_missing_data == "breaching" else 0
            else:
                missing_breaches = 0

            observed_breaches = sum(comparison(value, float(alarm.threshold)) for value in values_by_timestamp.values())
            breach_count = observed_breaches + missing_breaches
            if breach_count >= datapoints_to_alarm:
                last_failure = _result(
                    "FAILED",
                    (
                        f"{breach_count} of {evaluation_periods} post-reset periods breach the threshold; "
                        f"{datapoints_to_alarm} breaches trigger the alarm"
                    ),
                    alarm=alarm,
                    values_by_timestamp=values_by_timestamp,
                    attempt=attempt,
                )
            else:
                return _result(
                    "NORMALIZED",
                    (
                        f"{breach_count} of {evaluation_periods} post-reset periods breach the threshold; "
                        f"fewer than {datapoints_to_alarm} breaches trigger the alarm"
                    ),
                    alarm=alarm,
                    values_by_timestamp=values_by_timestamp,
                    attempt=attempt,
                )
        except Exception as exc:
            last_failure = None
            errors.append(type(exc).__name__)

        if attempt < attempts:
            sleep(max(interval_seconds, 0))

    if last_failure:
        last_failure["attempts"] = attempts
        return last_failure

    reason = last_pending_reason
    if errors:
        reason = f"CloudWatch verification was unavailable: {','.join(errors)}"
    return _pending(reason, attempts=attempts, alarm=alarm)
