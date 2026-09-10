from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_REGION_PATTERNS = {
    "aws": re.compile(
        r"(?:af|ap|ca|eu|il|me|mx|sa|us)-(?:central|north|south|(?:north|south)?(?:east|west))-[1-9][0-9]*"
    ),
    "aws-us-gov": re.compile(r"us-gov-(?:east|west)-[1-9][0-9]*"),
    "aws-cn": re.compile(r"cn-(?:north|northwest)-[1-9][0-9]*"),
}


def _region_partition(value: object) -> str | None:
    """Recognize supported region syntax so display labels cannot become AWS scope.

    Compact partition patterns avoid a static list of individual regions. This
    validates naming and partition compatibility, not live regional availability.
    """
    if isinstance(value, str):
        for partition, pattern in _REGION_PATTERNS.items():
            if pattern.fullmatch(value):
                return partition
    return None


def _alarm_arn_region(value: object) -> str | None:
    """Use a region only from a structurally valid, supported CloudWatch alarm ARN.

    Other services/resources, invalid accounts and partition/region mismatches
    cannot supply alarm scope. Alarm names may contain colons; preserve that tail.
    This is format validation, not proof of the sender's identity or ownership.
    """
    if not isinstance(value, str):
        return None
    parts = value.split(":", 5)
    if len(parts) != 6:
        return None
    prefix, partition, service, region, account, resource = parts
    if (
        prefix != "arn"
        or service != "cloudwatch"
        or _region_partition(region) != partition
        or re.fullmatch(r"[0-9]{12}", account) is None
        or not resource.startswith("alarm:")
    ):
        return None
    alarm_name = resource.removeprefix("alarm:")
    if not alarm_name.strip() or any(ord(char) < 32 or ord(char) == 127 for char in alarm_name):
        return None
    return region


def _alarm_region(data: dict) -> str:
    """Resolve production scope without redirecting explicit, unresolved coordinates.

    Preserve supported canonical Region values and reject conflicts with valid
    alarm ARNs. A valid ARN resolves missing regions or SNS display labels.
    Non-string, non-null regions always raise ValueError, as do nonblank strings
    that neither source resolves. Only absent/null/blank regions without a valid
    ARN use canonical AWS_REGION or historical us-east-1. Never mutate the source.
    """
    supplied_region = data.get("Region")
    if supplied_region is not None and not isinstance(supplied_region, str):
        raise ValueError(f"Region must be a string or null, got {supplied_region!r}")
    arn_region = _alarm_arn_region(data.get("AlarmArn"))
    if _region_partition(supplied_region) is not None:
        if arn_region is not None and supplied_region != arn_region:
            raise ValueError(f"Region {supplied_region!r} conflicts with AlarmArn region {arn_region!r}")
        return supplied_region
    if arn_region is not None:
        return arn_region
    if supplied_region is not None and supplied_region.strip():
        raise ValueError(f"Region {supplied_region!r} cannot be resolved without a valid AlarmArn")
    configured_region = os.environ.get("AWS_REGION")
    return configured_region if _region_partition(configured_region) is not None else "us-east-1"


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
    evaluation_periods: int | None = None
    datapoints_to_alarm: int | None = None
    treat_missing_data: str | None = None
    # None preserves production defaults; a dict renders only supplied eval metadata.
    eval_source_metadata: dict | None = None
    alarm_description: str | None = None


def parse_alarm(data: dict) -> AlarmContext:
    """Parse production metadata without mutating the source or using region labels as scope.

    Conflicting or unresolved explicit regions and malformed region types raise
    ValueError to prevent routing an incident elsewhere. Missing/null/blank regions
    may use the validated ARN, canonical environment region, or us-east-1 fallback.
    Descriptions remain uninterpreted; source-only eval metadata is handled
    separately by the evaluation adapter.
    """
    trigger = data.get("Trigger", {}) or {}
    dims_raw = trigger.get("Dimensions") or []
    dimensions = {d["name"]: d["value"] for d in dims_raw if "name" in d and "value" in d}
    evaluation_periods = trigger.get("EvaluationPeriods")

    return AlarmContext(
        alarm_name=data.get("AlarmName", "UnknownAlarm"),
        state_reason=data.get("NewStateReason", ""),
        state_change_time=data.get("StateChangeTime"),
        region=_alarm_region(data),
        metric_name=trigger.get("MetricName"),
        namespace=trigger.get("Namespace"),
        dimensions=dimensions,
        statistic=trigger.get("Statistic"),
        period=trigger.get("Period"),
        threshold=trigger.get("Threshold"),
        comparison_operator=trigger.get("ComparisonOperator"),
        evaluation_periods=evaluation_periods,
        datapoints_to_alarm=trigger.get("DatapointsToAlarm", evaluation_periods),
        treat_missing_data=trigger.get("TreatMissingData"),
        alarm_description=data.get("AlarmDescription") if isinstance(data.get("AlarmDescription"), str) else None,
    )


@dataclass
class CodexResult:
    success: bool
    result: str
    raw_output: str
    cancelled: bool = False
