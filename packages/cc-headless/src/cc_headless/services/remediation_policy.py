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


@dataclass(frozen=True)
class AlarmTarget:
    namespace: str
    metric_name: str
    dimensions: tuple[tuple[str, str], ...]


def _expected_alarm_target(
    fault_type: HealthcareFaultType,
    *,
    ecs_cluster_name: str,
    ecs_service_name: str,
    rds_instance_identifier: str,
) -> AlarmTarget | None:
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


def validate_healthcare_alarm_target(
    alarm_data: object,
    fault_type: HealthcareFaultType,
    *,
    ecs_cluster_name: str,
    ecs_service_name: str,
    rds_instance_identifier: str,
) -> str | None:
    expected = _expected_alarm_target(
        fault_type,
        ecs_cluster_name=ecs_cluster_name,
        ecs_service_name=ecs_service_name,
        rds_instance_identifier=rds_instance_identifier,
    )
    if expected is None:
        return "expected Healthcare alarm target configuration is incomplete"
    if not isinstance(alarm_data, dict):
        return "server-owned alarm data is missing"

    trigger = alarm_data.get("Trigger")
    if not isinstance(trigger, dict):
        return "server-owned alarm trigger is missing"
    if trigger.get("Namespace") != expected.namespace:
        return "alarm namespace does not match the allowlisted Healthcare target"
    if trigger.get("MetricName") != expected.metric_name:
        return "alarm metric does not match the confirmed Healthcare fault type"

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

    if len(dimensions) != len(expected.dimensions) or dict(dimensions) != dict(expected.dimensions):
        return "alarm dimensions do not exactly match the allowlisted Healthcare resource"
    return None


def parse_fault_type(value: object) -> HealthcareFaultType | None:
    if not isinstance(value, str):
        return None
    try:
        return HealthcareFaultType(value)
    except ValueError:
        return None
