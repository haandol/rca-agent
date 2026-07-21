from __future__ import annotations

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
        or alarm.threshold is None
        or comparison is None
        or statistic not in _STATISTICS
    ):
        return _pending("alarm metric configuration is insufficient for server verification", alarm=alarm)

    started_at = started_at or now()
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    period = max(alarm.period or 60, 60)
    attempts = max(attempts, 1)
    last_observation: dict[str, Any] | None = None
    errors: list[str] = []

    for attempt in range(1, attempts + 1):
        try:
            response = cloudwatch_client.get_metric_statistics(
                Namespace=alarm.namespace,
                MetricName=alarm.metric_name,
                Dimensions=[{"Name": name, "Value": value} for name, value in alarm.dimensions.items()],
                StartTime=started_at,
                EndTime=now(),
                Period=period,
                Statistics=[statistic],
            )
            datapoints = [
                point
                for point in response.get("Datapoints", [])
                if isinstance(point.get("Timestamp"), datetime)
                and point["Timestamp"].astimezone(UTC) >= started_at.astimezone(UTC)
                and isinstance(point.get(statistic), (int, float))
            ]
            if datapoints:
                latest = max(datapoints, key=lambda point: point["Timestamp"])
                value = float(latest[statistic])
                last_observation = {
                    "value": value,
                    "timestamp": latest["Timestamp"].astimezone(UTC),
                    "attempt": attempt,
                }
                if not comparison(value, float(alarm.threshold)):
                    return {
                        "status": "NORMALIZED",
                        "namespace": alarm.namespace,
                        "metric_name": alarm.metric_name,
                        "comparison_operator": alarm.comparison_operator,
                        "threshold": alarm.threshold,
                        "observed_value": value,
                        "observed_at": last_observation["timestamp"].isoformat(),
                        "attempts": attempt,
                        "reason": "post-reset metric no longer breaches the alarm threshold",
                    }
        except Exception as exc:
            errors.append(type(exc).__name__)

        if attempt < attempts:
            sleep(max(interval_seconds, 0))

    if last_observation:
        return {
            "status": "FAILED",
            "namespace": alarm.namespace,
            "metric_name": alarm.metric_name,
            "comparison_operator": alarm.comparison_operator,
            "threshold": alarm.threshold,
            "observed_value": last_observation["value"],
            "observed_at": last_observation["timestamp"].isoformat(),
            "attempts": attempts,
            "reason": "post-reset metric still breaches the alarm threshold after bounded verification",
        }

    reason = "no post-reset CloudWatch datapoint was available within the bounded verification window"
    if errors:
        reason = f"CloudWatch verification was unavailable: {','.join(errors)}"
    return _pending(reason, attempts=attempts, alarm=alarm)
