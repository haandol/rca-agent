from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class AlarmContext:
    alarm_name: str = "UnknownAlarm"
    state_reason: str = ""
    state_change_time: str | None = None
    region: str = "us-east-1"
    metric_name: str | None = None
    namespace: str | None = None
    dimensions: dict[str, str] = field(default_factory=dict)
    statistic: str | None = None
    period: int | None = None
    threshold: float | None = None
    comparison_operator: str | None = None


def parse_alarm(data: dict) -> AlarmContext:
    trigger = data.get("Trigger", {}) or {}
    dims_raw = trigger.get("Dimensions") or []
    dimensions = {d["name"]: d["value"] for d in dims_raw if "name" in d and "value" in d}

    return AlarmContext(
        alarm_name=data.get("AlarmName", "UnknownAlarm"),
        state_reason=data.get("NewStateReason", ""),
        state_change_time=data.get("StateChangeTime"),
        region=data.get("Region", os.environ.get("AWS_REGION", "us-east-1")),
        metric_name=trigger.get("MetricName"),
        namespace=trigger.get("Namespace"),
        dimensions=dimensions,
        statistic=trigger.get("Statistic"),
        period=trigger.get("Period"),
        threshold=trigger.get("Threshold"),
        comparison_operator=trigger.get("ComparisonOperator"),
    )
