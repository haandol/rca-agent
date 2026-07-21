from datetime import UTC, datetime, timedelta
from threading import Event
from time import perf_counter
from unittest.mock import Mock

import pytest

from rca_agent.ports.dto.models import (
    RemediationResult,
    VerificationStatus,
)
from rca_agent.services import verification
from rca_agent.services.verification import VerificationOutput, run_verification

_REMEDIATION_TIME = 1_800_000_000
_DEFAULT_COMPLETE_PERIOD_SECONDS = 180


def _alarm_definition(**overrides):
    alarm = {
        "AlarmName": "RdsHighConnections",
        "Namespace": "AWS/RDS",
        "MetricName": "DatabaseConnections",
        "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "healthcare-db"}],
        "Statistic": "Maximum",
        "Period": 60,
        "Threshold": 30.0,
        "ComparisonOperator": "GreaterThanThreshold",
        "EvaluationPeriods": 3,
        "DatapointsToAlarm": 2,
    }
    alarm.update(overrides)
    return alarm


def _client(*, alarm=None, datapoints=None):
    cloudwatch = Mock()
    cloudwatch.describe_alarms.return_value = {
        "MetricAlarms": [alarm or _alarm_definition()],
        "CompositeAlarms": [],
    }
    cloudwatch.get_metric_statistics.return_value = {
        "Datapoints": datapoints or [],
    }
    return cloudwatch


def _period_timestamps(
    period=60,
    count=3,
    *,
    remediation_time=_REMEDIATION_TIME,
    complete_period_seconds=_DEFAULT_COMPLETE_PERIOD_SECONDS,
):
    end_epoch = int((remediation_time + complete_period_seconds) // period) * period
    start = datetime.fromtimestamp(end_epoch - period * count, tz=UTC)
    return [start + timedelta(seconds=period * index) for index in range(count)]


def _run(
    monkeypatch,
    cloudwatch,
    *,
    agent=None,
    alarm_name="RdsHighConnections",
    remediation_time=_REMEDIATION_TIME,
):
    clock = {"now": float(remediation_time)}

    def advance_clock(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(verification.time, "time", lambda: clock["now"])
    sleep = Mock(side_effect=advance_clock)
    monkeypatch.setattr(verification.time, "sleep", sleep)
    result = run_verification(
        cloudwatch_client=cloudwatch,
        alarm_name=alarm_name,
        remediation=RemediationResult(rca_id="rca-1"),
        remediation_time=remediation_time,
        agent=agent,
    )
    return result, sleep


def test_all_n_normal_periods_are_server_normalized(monkeypatch):
    timestamps = _period_timestamps()
    cloudwatch = _client(
        datapoints=[
            {"Timestamp": timestamps[2], "Maximum": 14.0},
            {"Timestamp": timestamps[0], "Maximum": 12.0},
            {"Timestamp": timestamps[1], "Maximum": 13.0},
        ]
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.NORMALIZED
    assert result.metrics_normalized is True
    cloudwatch.describe_alarms.assert_called_once_with(AlarmNames=["RdsHighConnections"])
    request = cloudwatch.get_metric_statistics.call_args.kwargs
    assert request["Namespace"] == "AWS/RDS"
    assert request["MetricName"] == "DatabaseConnections"
    assert request["Dimensions"] == [{"Name": "DBInstanceIdentifier", "Value": "healthcare-db"}]
    assert request["Period"] == 60
    assert request["Statistics"] == ["Maximum"]
    assert request["EndTime"] - request["StartTime"] == timedelta(minutes=3)


def test_m_of_n_breaches_are_server_failed(monkeypatch):
    timestamps = _period_timestamps()
    cloudwatch = _client(
        datapoints=[
            {"Timestamp": timestamps[0], "Maximum": 31.0},
            {"Timestamp": timestamps[1], "Maximum": 10.0},
            {"Timestamp": timestamps[2], "Maximum": 35.0},
        ]
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.FAILED
    assert result.metrics_normalized is False


def test_insufficient_datapoints_remain_pending(monkeypatch):
    timestamps = _period_timestamps()
    cloudwatch = _client(
        datapoints=[
            {"Timestamp": timestamps[0], "Maximum": 40.0},
            {"Timestamp": timestamps[1], "Maximum": 10.0},
        ]
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.PENDING
    assert "2 of 3" in result.verification_summary


def test_observed_m_breaches_fail_even_when_one_period_is_missing(monkeypatch):
    timestamps = _period_timestamps()
    cloudwatch = _client(
        datapoints=[
            {"Timestamp": timestamps[0], "Maximum": 40.0},
            {"Timestamp": timestamps[1], "Maximum": 41.0},
        ]
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.FAILED
    assert result.metrics_normalized is False
    assert "2 available periods" in result.verification_summary


def test_observed_m_breaches_fail_when_other_period_data_is_missing(monkeypatch):
    timestamps = _period_timestamps(count=2, complete_period_seconds=120)
    cloudwatch = _client(
        datapoints=[
            {"Timestamp": timestamps[0], "Maximum": 40.0},
            {"Timestamp": timestamps[1], "Maximum": 41.0},
        ]
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.FAILED
    assert "2 available periods" in result.verification_summary


def test_fewer_than_n_datapoints_without_m_breaches_remains_pending(
    monkeypatch,
):
    timestamp = _period_timestamps(count=1, complete_period_seconds=60)[0]
    cloudwatch = _client(
        datapoints=[{"Timestamp": timestamp, "Maximum": 10.0}],
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.PENDING
    assert "1 of 3" in result.verification_summary


def test_excessive_required_wait_is_pending_without_metric_query(monkeypatch):
    remediation_time = _REMEDIATION_TIME + 30
    cloudwatch = _client(alarm=_alarm_definition(Period=3600))

    result, sleep = _run(
        monkeypatch,
        cloudwatch,
        remediation_time=remediation_time,
    )

    assert result.status is VerificationStatus.PENDING
    assert "exceeds the bounded limit" in result.verification_summary
    sleep.assert_not_called()
    cloudwatch.get_metric_statistics.assert_not_called()


def test_pre_remediation_and_overlapping_periods_are_excluded(monkeypatch):
    remediation_time = _REMEDIATION_TIME + 30
    first_post_remediation = datetime.fromtimestamp(
        _REMEDIATION_TIME + 60,
        tz=UTC,
    )
    cloudwatch = _client(
        datapoints=[
            {
                "Timestamp": datetime.fromtimestamp(_REMEDIATION_TIME - 60, tz=UTC),
                "Maximum": 50.0,
            },
            {
                "Timestamp": datetime.fromtimestamp(_REMEDIATION_TIME, tz=UTC),
                "Maximum": 50.0,
            },
            {"Timestamp": first_post_remediation, "Maximum": 10.0},
            {"Timestamp": first_post_remediation + timedelta(seconds=60), "Maximum": 11.0},
            {"Timestamp": first_post_remediation + timedelta(seconds=120), "Maximum": 12.0},
        ]
    )

    result, _ = _run(monkeypatch, cloudwatch, remediation_time=remediation_time)

    assert result.status is VerificationStatus.NORMALIZED
    request = cloudwatch.get_metric_statistics.call_args.kwargs
    assert request["StartTime"] == first_post_remediation
    assert request["EndTime"] == first_post_remediation + timedelta(seconds=180)


def test_pre_remediation_normal_periods_cannot_hide_post_remediation_breaches(
    monkeypatch,
):
    post_remediation = _period_timestamps(count=2, complete_period_seconds=120)
    cloudwatch = _client(
        datapoints=[
            {
                "Timestamp": datetime.fromtimestamp(_REMEDIATION_TIME - 120, tz=UTC),
                "Maximum": 10.0,
            },
            {
                "Timestamp": datetime.fromtimestamp(_REMEDIATION_TIME - 60, tz=UTC),
                "Maximum": 11.0,
            },
            {"Timestamp": post_remediation[0], "Maximum": 40.0},
            {"Timestamp": post_remediation[1], "Maximum": 41.0},
        ]
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.FAILED
    assert "2 available periods" in result.verification_summary


def test_cloudwatch_api_error_remains_pending(monkeypatch):
    cloudwatch = _client()
    cloudwatch.describe_alarms.side_effect = RuntimeError("unavailable")

    result, sleep = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.PENDING
    sleep.assert_not_called()
    cloudwatch.get_metric_statistics.assert_not_called()


def test_missing_authoritative_alarm_name_remains_pending(monkeypatch):
    cloudwatch = _client()

    result, sleep = _run(monkeypatch, cloudwatch, alarm_name="")

    assert result.status is VerificationStatus.PENDING
    sleep.assert_not_called()
    cloudwatch.describe_alarms.assert_not_called()
    cloudwatch.get_metric_statistics.assert_not_called()


def test_optional_llm_success_cannot_change_pending_server_status(monkeypatch):
    agent = Mock(
        return_value={
            "output": VerificationOutput(
                verification_summary="Everything is normalized.",
                remaining_issues=[],
            )
        }
    )

    result, _ = _run(monkeypatch, _client(datapoints=[]), agent=agent)

    assert result.status is VerificationStatus.PENDING
    assert result.metrics_normalized is False
    assert "Insufficient CloudWatch datapoints" in result.verification_summary
    assert "Everything is normalized" in result.verification_summary
    agent.assert_called_once()


def test_optional_llm_failure_cannot_change_normalized_server_status(monkeypatch):
    timestamps = _period_timestamps()
    agent = Mock(side_effect=RuntimeError("summary unavailable"))
    cloudwatch = _client(datapoints=[{"Timestamp": timestamp, "Maximum": 10.0} for timestamp in timestamps])

    result, _ = _run(monkeypatch, cloudwatch, agent=agent)

    assert result.status is VerificationStatus.NORMALIZED
    assert result.metrics_normalized is True
    assert "Additional assessment" not in result.verification_summary


def test_optional_llm_timeout_returns_without_waiting_for_worker(monkeypatch):
    timestamps = _period_timestamps()
    cloudwatch = _client(
        datapoints=[{"Timestamp": timestamp, "Maximum": 10.0} for timestamp in timestamps],
    )

    def slow_agent(*args, **kwargs):  # noqa: ARG001
        Event().wait(0.35)
        return {
            "output": VerificationOutput(
                verification_summary="too late",
            )
        }

    monkeypatch.setattr(
        verification,
        "_verification_wait_seconds",
        lambda *args: 0,
    )
    monkeypatch.setattr(
        verification.time,
        "time",
        lambda: float(_REMEDIATION_TIME + _DEFAULT_COMPLETE_PERIOD_SECONDS),
    )
    sleep = Mock()
    monkeypatch.setattr(verification.time, "sleep", sleep)

    started = perf_counter()
    result = run_verification(
        cloudwatch_client=cloudwatch,
        alarm_name="RdsHighConnections",
        remediation=RemediationResult(rca_id="rca-1"),
        remediation_time=_REMEDIATION_TIME,
        agent=Mock(side_effect=slow_agent),
        timeout=0,
    )
    elapsed = perf_counter() - started

    assert elapsed < 0.15
    assert result.status is VerificationStatus.NORMALIZED
    assert "too late" not in result.verification_summary
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "describe_response",
    [
        {"MetricAlarms": [], "CompositeAlarms": []},
        {
            "MetricAlarms": [_alarm_definition(), _alarm_definition()],
            "CompositeAlarms": [],
        },
        {
            "MetricAlarms": [],
            "CompositeAlarms": [{"AlarmName": "RdsHighConnections"}],
        },
        {
            "MetricAlarms": [
                _alarm_definition(
                    Metrics=[
                        {
                            "Id": "m1",
                            "Expression": "AVG(METRICS())",
                        }
                    ]
                )
            ],
            "CompositeAlarms": [],
        },
    ],
)
def test_no_multiple_composite_or_metric_math_alarm_is_pending(
    monkeypatch,
    describe_response,
):
    cloudwatch = _client()
    cloudwatch.describe_alarms.return_value = describe_response

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.PENDING
    cloudwatch.get_metric_statistics.assert_not_called()


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("GreaterThanThreshold", 30.1, VerificationStatus.FAILED),
        ("GreaterThanOrEqualToThreshold", 30.0, VerificationStatus.FAILED),
        ("LessThanThreshold", 29.9, VerificationStatus.FAILED),
        ("LessThanOrEqualToThreshold", 30.0, VerificationStatus.FAILED),
    ],
)
def test_supported_comparison_operators(monkeypatch, operator, value, expected):
    alarm = _alarm_definition(
        ComparisonOperator=operator,
        EvaluationPeriods=1,
        DatapointsToAlarm=1,
    )
    timestamp = _period_timestamps(count=1, complete_period_seconds=60)[0]

    result, _ = _run(
        monkeypatch,
        _client(
            alarm=alarm,
            datapoints=[{"Timestamp": timestamp, "Maximum": value}],
        ),
    )

    assert result.status is expected


@pytest.mark.parametrize(
    "operator",
    ["GreaterThanThreshold", "LessThanThreshold"],
)
def test_strict_comparison_threshold_equality_is_not_a_breach(
    monkeypatch,
    operator,
):
    alarm = _alarm_definition(
        ComparisonOperator=operator,
        EvaluationPeriods=1,
        DatapointsToAlarm=1,
    )
    timestamp = _period_timestamps(count=1, complete_period_seconds=60)[0]

    result, _ = _run(
        monkeypatch,
        _client(
            alarm=alarm,
            datapoints=[{"Timestamp": timestamp, "Maximum": 30.0}],
        ),
    )

    assert result.status is VerificationStatus.NORMALIZED


@pytest.mark.parametrize(
    "statistic",
    ["Average", "Sum", "Minimum", "Maximum", "SampleCount"],
)
def test_supported_standard_statistics_are_queried(monkeypatch, statistic):
    alarm = _alarm_definition(
        Statistic=statistic,
        EvaluationPeriods=1,
        DatapointsToAlarm=1,
    )
    timestamp = _period_timestamps(count=1, complete_period_seconds=60)[0]
    cloudwatch = _client(
        alarm=alarm,
        datapoints=[{"Timestamp": timestamp, statistic: 1.0}],
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.NORMALIZED
    assert cloudwatch.get_metric_statistics.call_args.kwargs["Statistics"] == [statistic]


@pytest.mark.parametrize("low_sample_policy", [None, "evaluate"])
def test_defensive_extended_statistic_uses_extended_statistics(
    monkeypatch,
    low_sample_policy,
):
    alarm = _alarm_definition(
        Statistic=None,
        ExtendedStatistic="p99.9",
        EvaluateLowSampleCountPercentile=low_sample_policy,
        EvaluationPeriods=1,
        DatapointsToAlarm=1,
    )
    timestamp = _period_timestamps(count=1, complete_period_seconds=60)[0]
    cloudwatch = _client(
        alarm=alarm,
        datapoints=[
            {
                "Timestamp": timestamp,
                "ExtendedStatistics": {"p99.9": 10.0},
            }
        ],
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.NORMALIZED
    request = cloudwatch.get_metric_statistics.call_args.kwargs
    assert request["ExtendedStatistics"] == ["p99.9"]
    assert "Statistics" not in request


def test_alarm_unit_is_forwarded_to_metric_query(monkeypatch):
    alarm = _alarm_definition(
        Unit="Count",
        EvaluationPeriods=1,
        DatapointsToAlarm=1,
    )
    timestamp = _period_timestamps(count=1, complete_period_seconds=60)[0]
    cloudwatch = _client(
        alarm=alarm,
        datapoints=[{"Timestamp": timestamp, "Maximum": 10.0}],
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.NORMALIZED
    assert cloudwatch.get_metric_statistics.call_args.kwargs["Unit"] == "Count"


def test_omitted_datapoints_to_alarm_defaults_to_n_of_n(monkeypatch):
    alarm = _alarm_definition(EvaluationPeriods=2)
    alarm.pop("DatapointsToAlarm")
    timestamps = _period_timestamps(count=2, complete_period_seconds=120)
    cloudwatch = _client(
        alarm=alarm,
        datapoints=[
            {"Timestamp": timestamps[0], "Maximum": 31.0},
            {"Timestamp": timestamps[1], "Maximum": 10.0},
        ],
    )

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.NORMALIZED
    assert "fewer than 2 breaches" in result.verification_summary


@pytest.mark.parametrize(
    "alarm",
    [
        _alarm_definition(ComparisonOperator="LessThanLowerOrGreaterThanUpperThreshold"),
        _alarm_definition(Statistic="Percentile"),
        _alarm_definition(Statistic=None, ExtendedStatistic="not-a-percentile"),
        _alarm_definition(DatapointsToAlarm=4),
    ],
)
def test_unsupported_alarm_configuration_is_pending(monkeypatch, alarm):
    cloudwatch = _client(alarm=alarm)

    result, _ = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.PENDING
    cloudwatch.get_metric_statistics.assert_not_called()


def test_wait_uses_authoritative_period_and_evaluation_count(monkeypatch):
    alarm = _alarm_definition(EvaluationPeriods=2, DatapointsToAlarm=2)
    timestamps = _period_timestamps(count=2, complete_period_seconds=120)
    cloudwatch = _client(
        alarm=alarm,
        datapoints=[{"Timestamp": timestamp, "Maximum": 10.0} for timestamp in timestamps],
    )
    events = []
    cloudwatch.describe_alarms.side_effect = lambda **_kwargs: (
        events.append("describe")
        or {
            "MetricAlarms": [alarm],
            "CompositeAlarms": [],
        }
    )

    clock = {"now": float(_REMEDIATION_TIME)}

    def sleep(seconds):
        events.append(("sleep", seconds))
        clock["now"] += seconds

    monkeypatch.setattr(verification.time, "time", lambda: clock["now"])
    monkeypatch.setattr(verification.time, "sleep", sleep)

    result = run_verification(
        cloudwatch_client=cloudwatch,
        alarm_name="RdsHighConnections",
        remediation=RemediationResult(rca_id="rca-1"),
        remediation_time=_REMEDIATION_TIME,
    )

    assert result.status is VerificationStatus.NORMALIZED
    assert events[0] == "describe"
    assert events[1][0] == "sleep"
    assert events[1][1] >= 2 * 60


def test_low_sample_ignore_percentile_alarm_is_pending(monkeypatch):
    alarm = _alarm_definition(
        Statistic=None,
        ExtendedStatistic="p99",
        EvaluateLowSampleCountPercentile="ignore",
        EvaluationPeriods=1,
        DatapointsToAlarm=1,
    )
    cloudwatch = _client(alarm=alarm)

    result, sleep = _run(monkeypatch, cloudwatch)

    assert result.status is VerificationStatus.PENDING
    assert "EvaluateLowSampleCountPercentile=ignore" in result.verification_summary
    sleep.assert_not_called()
    cloudwatch.get_metric_statistics.assert_not_called()
