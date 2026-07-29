from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HealthcareFaultType(StrEnum):
    DB_LEAK = "db-leak"
    HIGH_CPU = "high-cpu"
    HIGH_MEMORY = "high-memory"
    SLOW_QUERY = "slow-query"
    UNSUPPORTED = "unsupported"


RESET_PATHS: dict[HealthcareFaultType, str] = {
    HealthcareFaultType.DB_LEAK: "/fault/db-leak/reset",
    HealthcareFaultType.HIGH_CPU: "/fault/high-cpu/reset",
    HealthcareFaultType.HIGH_MEMORY: "/fault/high-memory/reset",
    HealthcareFaultType.SLOW_QUERY: "/fault/slow-query/reset",
}


SYMPTOM_NAMESPACE = "Healthcare/Sensor"
SYMPTOM_METRIC_NAMES = ("VitalIngestFailures", "AbnormalAlertDelaySeconds")
SYMPTOM_SERVICE_NAME = "healthcare-sensor-app"


@dataclass(frozen=True)
class AlarmTarget:
    namespace: str
    metric_name: str
    dimensions: tuple[tuple[str, str], ...]


def _cause_alarm_target(
    fault_type: HealthcareFaultType,
    *,
    ecs_cluster_name: str,
    ecs_service_name: str,
    rds_instance_identifier: str,
) -> AlarmTarget | None:
    """The cause-level alarm coordinate whose dimensions identify the target resource."""
    if fault_type in {HealthcareFaultType.DB_LEAK, HealthcareFaultType.SLOW_QUERY}:
        if not rds_instance_identifier:
            return None
        metric_name = "DatabaseConnections" if fault_type is HealthcareFaultType.DB_LEAK else "ReadLatency"
        return AlarmTarget(
            namespace="AWS/RDS",
            metric_name=metric_name,
            dimensions=(("DBInstanceIdentifier", rds_instance_identifier),),
        )

    if fault_type in {HealthcareFaultType.HIGH_CPU, HealthcareFaultType.HIGH_MEMORY}:
        if not ecs_cluster_name or not ecs_service_name:
            return None
        metric_name = "CPUUtilization" if fault_type is HealthcareFaultType.HIGH_CPU else "MemoryUtilization"
        return AlarmTarget(
            namespace="AWS/ECS",
            metric_name=metric_name,
            dimensions=(
                ("ClusterName", ecs_cluster_name),
                ("ServiceName", ecs_service_name),
            ),
        )

    return None


def _symptom_alarm_targets() -> tuple[AlarmTarget, ...]:
    """Symptom alarm coordinates that any supported fault type may surface through.

    These carry no resource identity, so a symptom-entry recovery still has to
    derive its target from the confirmed fault type's cause-level coordinate.
    """
    return tuple(
        AlarmTarget(
            namespace=SYMPTOM_NAMESPACE,
            metric_name=metric_name,
            dimensions=(("ServiceName", SYMPTOM_SERVICE_NAME),),
        )
        for metric_name in SYMPTOM_METRIC_NAMES
    )


def validate_healthcare_alarm_target(
    alarm_data: object,
    fault_type: HealthcareFaultType,
    *,
    ecs_cluster_name: str,
    ecs_service_name: str,
    rds_instance_identifier: str,
) -> str | None:
    # The confirmed fault type must always resolve to a fully configured
    # cause-level target. That resolution is what identifies the resource to act
    # on, whether the alarm itself is the cause metric or a symptom metric.
    cause_target = _cause_alarm_target(
        fault_type,
        ecs_cluster_name=ecs_cluster_name,
        ecs_service_name=ecs_service_name,
        rds_instance_identifier=rds_instance_identifier,
    )
    if cause_target is None:
        return "expected Healthcare alarm target configuration is incomplete"
    if not isinstance(alarm_data, dict):
        return "server-owned alarm data is missing"

    trigger = alarm_data.get("Trigger")
    if not isinstance(trigger, dict):
        return "server-owned alarm trigger is missing"

    raw_dimensions = trigger.get("Dimensions")
    if not isinstance(raw_dimensions, list):
        return "alarm dimensions are missing"
    dimensions: list[tuple[str, str]] = []
    for dimension in raw_dimensions:
        if not isinstance(dimension, dict):
            return "alarm dimensions are malformed"
        name = dimension.get("name")
        value = dimension.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or not name or not value:
            return "alarm dimensions are malformed"
        dimensions.append((name, value))

    namespace = trigger.get("Namespace")
    metric_name = trigger.get("MetricName")

    def matches(target: AlarmTarget) -> bool:
        return (
            namespace == target.namespace
            and metric_name == target.metric_name
            and len(dimensions) == len(target.dimensions)
            and dict(dimensions) == dict(target.dimensions)
        )

    if matches(cause_target):
        return None
    if any(matches(target) for target in _symptom_alarm_targets()):
        return None

    if namespace not in {cause_target.namespace, SYMPTOM_NAMESPACE}:
        return "alarm namespace does not match the allowlisted Healthcare target"
    if metric_name != cause_target.metric_name and metric_name not in SYMPTOM_METRIC_NAMES:
        return "alarm metric matches neither the confirmed fault type nor an allowlisted Healthcare symptom"
    return "alarm dimensions do not exactly match the allowlisted Healthcare resource"


def parse_fault_type(value: object) -> HealthcareFaultType | None:
    if not isinstance(value, str):
        return None
    try:
        return HealthcareFaultType(value)
    except ValueError:
        return None
