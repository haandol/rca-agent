import pytest

from cc_headless.services.remediation_policy import (
    SYMPTOM_METRIC_NAMES,
    SYMPTOM_NAMESPACE,
    SYMPTOM_SERVICE_NAME,
    HealthcareFaultType,
    validate_healthcare_alarm_target,
)

HEALTHCARE_CLUSTER = "healthcare-cluster"
HEALTHCARE_SERVICE = "healthcare-service"
HEALTHCARE_DATABASE = "healthcare-db"

CAUSE_TARGETS = {
    HealthcareFaultType.DB_LEAK: (
        "AWS/RDS",
        "DatabaseConnections",
        [{"name": "DBInstanceIdentifier", "value": HEALTHCARE_DATABASE}],
    ),
    HealthcareFaultType.SLOW_QUERY: (
        "AWS/RDS",
        "ReadLatency",
        [{"name": "DBInstanceIdentifier", "value": HEALTHCARE_DATABASE}],
    ),
    HealthcareFaultType.HIGH_CPU: (
        "AWS/ECS",
        "CPUUtilization",
        [
            {"name": "ClusterName", "value": HEALTHCARE_CLUSTER},
            {"name": "ServiceName", "value": HEALTHCARE_SERVICE},
        ],
    ),
    HealthcareFaultType.HIGH_MEMORY: (
        "AWS/ECS",
        "MemoryUtilization",
        [
            {"name": "ClusterName", "value": HEALTHCARE_CLUSTER},
            {"name": "ServiceName", "value": HEALTHCARE_SERVICE},
        ],
    ),
}


def _alarm(namespace: str, metric_name: str, dimensions: list[dict]) -> dict:
    return {
        "AlarmName": "healthcare-alarm",
        "Region": "us-east-1",
        "Trigger": {
            "Namespace": namespace,
            "MetricName": metric_name,
            "Dimensions": dimensions,
        },
    }


def _symptom_alarm(metric_name: str = "VitalIngestFailures") -> dict:
    return _alarm(
        SYMPTOM_NAMESPACE,
        metric_name,
        [{"name": "ServiceName", "value": SYMPTOM_SERVICE_NAME}],
    )


def _validate(alarm_data: object, fault_type: HealthcareFaultType, **overrides: str) -> str | None:
    kwargs = {
        "ecs_cluster_name": HEALTHCARE_CLUSTER,
        "ecs_service_name": HEALTHCARE_SERVICE,
        "rds_instance_identifier": HEALTHCARE_DATABASE,
    }
    kwargs.update(overrides)
    return validate_healthcare_alarm_target(alarm_data, fault_type, **kwargs)


@pytest.mark.parametrize("fault_type", list(CAUSE_TARGETS))
def test_cause_alarm_matching_confirmed_fault_type_is_accepted(fault_type: HealthcareFaultType) -> None:
    assert _validate(_alarm(*CAUSE_TARGETS[fault_type]), fault_type) is None


@pytest.mark.parametrize("metric_name", SYMPTOM_METRIC_NAMES)
@pytest.mark.parametrize("fault_type", list(CAUSE_TARGETS))
def test_symptom_alarm_is_accepted_for_every_supported_fault_type(
    fault_type: HealthcareFaultType,
    metric_name: str,
) -> None:
    assert _validate(_symptom_alarm(metric_name), fault_type) is None


@pytest.mark.parametrize("fault_type", list(CAUSE_TARGETS))
def test_symptom_alarm_is_blocked_when_derived_target_is_unconfigured(
    fault_type: HealthcareFaultType,
) -> None:
    # The symptom alarm carries no resource identity, so the target has to come
    # from the confirmed fault type. Missing identifiers must fail closed.
    unset = {
        "ecs_cluster_name": "",
        "ecs_service_name": "",
        "rds_instance_identifier": "",
    }
    error = _validate(_symptom_alarm(), fault_type, **unset)

    assert error == "expected Healthcare alarm target configuration is incomplete"


def test_symptom_alarm_for_unsupported_fault_type_is_blocked() -> None:
    error = _validate(_symptom_alarm(), HealthcareFaultType.UNSUPPORTED)

    assert error == "expected Healthcare alarm target configuration is incomplete"


def test_symptom_alarm_with_foreign_service_dimension_is_blocked() -> None:
    alarm = _alarm(
        SYMPTOM_NAMESPACE,
        "VitalIngestFailures",
        [{"name": "ServiceName", "value": "other-service"}],
    )

    error = _validate(alarm, HealthcareFaultType.DB_LEAK)

    assert error == "alarm dimensions do not exactly match the allowlisted Healthcare resource"


def test_unknown_metric_in_symptom_namespace_is_blocked() -> None:
    alarm = _alarm(
        SYMPTOM_NAMESPACE,
        "SomeOtherMetric",
        [{"name": "ServiceName", "value": SYMPTOM_SERVICE_NAME}],
    )

    error = _validate(alarm, HealthcareFaultType.DB_LEAK)

    assert error == "alarm metric matches neither the confirmed fault type nor an allowlisted Healthcare symptom"


def test_cause_alarm_for_a_different_fault_type_is_still_blocked() -> None:
    # Widening the allowlist to symptom metrics must not let one cause metric
    # authorize a different fault type's reset.
    error = _validate(
        _alarm(*CAUSE_TARGETS[HealthcareFaultType.HIGH_CPU]),
        HealthcareFaultType.DB_LEAK,
    )

    assert error == "alarm namespace does not match the allowlisted Healthcare target"


def test_cause_alarm_pointing_at_a_foreign_resource_is_blocked() -> None:
    error = _validate(
        _alarm(
            "AWS/RDS",
            "DatabaseConnections",
            [{"name": "DBInstanceIdentifier", "value": "other-database"}],
        ),
        HealthcareFaultType.DB_LEAK,
    )

    assert error == "alarm dimensions do not exactly match the allowlisted Healthcare resource"


def test_symptom_alarm_with_extra_dimension_is_blocked() -> None:
    alarm = _alarm(
        SYMPTOM_NAMESPACE,
        "VitalIngestFailures",
        [
            {"name": "ServiceName", "value": SYMPTOM_SERVICE_NAME},
            {"name": "ClusterName", "value": HEALTHCARE_CLUSTER},
        ],
    )

    error = _validate(alarm, HealthcareFaultType.DB_LEAK)

    assert error == "alarm dimensions do not exactly match the allowlisted Healthcare resource"


@pytest.mark.parametrize(
    "alarm_data",
    [
        None,
        "not-a-dict",
        {},
        {"Trigger": "not-a-dict"},
        {"Trigger": {"Namespace": SYMPTOM_NAMESPACE, "MetricName": "VitalIngestFailures"}},
    ],
)
def test_malformed_alarm_data_is_blocked(alarm_data: object) -> None:
    assert _validate(alarm_data, HealthcareFaultType.DB_LEAK) is not None
