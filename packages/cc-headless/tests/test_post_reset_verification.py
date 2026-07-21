from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from cc_headless.services.post_reset_verification import verify_post_reset

STARTED_AT = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)
ALARM_DATA = {
    "AlarmName": "HighCPU",
    "Region": "us-east-1",
    "Trigger": {
        "Namespace": "AWS/ECS",
        "MetricName": "CPUUtilization",
        "Dimensions": [{"name": "ClusterName", "value": "prod"}],
        "Statistic": "Average",
        "Period": 60,
        "Threshold": 80,
        "ComparisonOperator": "GreaterThanThreshold",
        "EvaluationPeriods": 2,
        "DatapointsToAlarm": 2,
    },
}


def _alarm(**trigger_overrides):
    trigger = {**ALARM_DATA["Trigger"], **trigger_overrides}
    return {**ALARM_DATA, "Trigger": trigger}


def _now(minutes=3):
    return lambda: STARTED_AT + timedelta(minutes=minutes)


def _point(minutes, value):
    return {"Timestamp": STARTED_AT + timedelta(minutes=minutes), "Average": value}


def _verify(alarm_data, datapoints, *, now=None, attempts=1):
    cloudwatch = Mock()
    cloudwatch.get_metric_statistics.return_value = {"Datapoints": datapoints}
    result = verify_post_reset(
        alarm_data,
        cloudwatch,
        attempts=attempts,
        interval_seconds=0,
        started_at=STARTED_AT,
        sleep=Mock(),
        now=now or _now(),
    )
    return result, cloudwatch


def test_m_of_n_normalizes_only_after_full_window_has_fewer_than_m_breaches():
    result, cloudwatch = _verify(
        _alarm(EvaluationPeriods=3, DatapointsToAlarm=2),
        [_point(3, 20.0), _point(1, 90.0), _point(2, 30.0)],
        now=_now(4),
    )

    assert result["status"] == "NORMALIZED"
    assert result["observed_value"] == 20.0
    request = cloudwatch.get_metric_statistics.call_args.kwargs
    assert request["StartTime"] == STARTED_AT + timedelta(minutes=1)
    assert request["EndTime"] == STARTED_AT + timedelta(minutes=4)


def test_m_of_n_fails_when_m_periods_breach():
    result, _ = _verify(
        _alarm(EvaluationPeriods=3, DatapointsToAlarm=2),
        [_point(1, 90.0), _point(2, 20.0), _point(3, 81.0)],
        now=_now(4),
    )

    assert result["status"] == "FAILED"
    assert "2 of 3" in result["reason"]


def test_one_normal_datapoint_does_not_normalize_two_period_alarm():
    result, _ = _verify(ALARM_DATA, [_point(1, 20.0)])

    assert result["status"] == "PENDING"
    assert "1 of 2 required datapoints" in result["reason"]


def test_missing_datapoint_is_filled_as_breaching():
    result, _ = _verify(
        _alarm(TreatMissingData="breaching"),
        [_point(1, 90.0)],
    )

    assert result["status"] == "FAILED"
    assert "2 of 2" in result["reason"]


def test_missing_datapoint_is_filled_as_not_breaching():
    result, _ = _verify(
        _alarm(TreatMissingData="notBreaching"),
        [_point(1, 90.0)],
    )

    assert result["status"] == "NORMALIZED"
    assert "1 of 2" in result["reason"]


@pytest.mark.parametrize("missing_policy", [None, "ignore", "missing", "unsupported"])
def test_indeterminate_missing_data_policies_remain_pending(missing_policy):
    alarm = _alarm()
    if missing_policy is not None:
        alarm["Trigger"]["TreatMissingData"] = missing_policy

    result, _ = _verify(alarm, [_point(1, 20.0)])

    assert result["status"] == "PENDING"


def test_incomplete_future_period_is_not_filled_by_missing_policy():
    result, cloudwatch = _verify(
        _alarm(TreatMissingData="notBreaching"),
        [],
        now=_now(1),
    )

    assert result["status"] == "PENDING"
    assert "fewer than 2 complete post-reset periods" in result["reason"]
    cloudwatch.get_metric_statistics.assert_not_called()


def test_periods_are_sorted_deduplicated_and_bounded_to_latest_full_window():
    result, _ = _verify(
        ALARM_DATA,
        [
            _point(3, 95.0),  # partial period
            _point(1, 90.0),
            _point(-1, 95.0),  # pre-reset
            _point(1, 90.0),  # duplicate period
            _point(0, 95.0),  # complete but outside the latest N window
            _point(2, 20.0),
        ],
    )

    assert result["status"] == "NORMALIZED"
    assert "1 of 2" in result["reason"]


@pytest.mark.parametrize(
    ("operator", "expected_status"),
    [
        ("GreaterThanThreshold", "NORMALIZED"),
        ("GreaterThanOrEqualToThreshold", "FAILED"),
        ("LessThanThreshold", "NORMALIZED"),
        ("LessThanOrEqualToThreshold", "FAILED"),
    ],
)
def test_threshold_comparisons_remain_strict(operator, expected_status):
    result, _ = _verify(
        _alarm(
            ComparisonOperator=operator,
            EvaluationPeriods=1,
            DatapointsToAlarm=1,
        ),
        [_point(2, 80.0)],
    )

    assert result["status"] == expected_status


@pytest.mark.parametrize(
    ("evaluation_periods", "datapoints_to_alarm"),
    [
        (None, None),
        (0, 0),
        (-1, 1),
        (True, 1),
        (2, 0),
        (2, 3),
    ],
)
def test_invalid_m_of_n_configuration_is_pending(evaluation_periods, datapoints_to_alarm):
    result, cloudwatch = _verify(
        _alarm(
            EvaluationPeriods=evaluation_periods,
            DatapointsToAlarm=datapoints_to_alarm,
        ),
        [],
    )

    assert result["status"] == "PENDING"
    cloudwatch.get_metric_statistics.assert_not_called()


def test_omitted_datapoints_to_alarm_defaults_to_n():
    alarm = _alarm(EvaluationPeriods=2)
    alarm["Trigger"].pop("DatapointsToAlarm")

    result, _ = _verify(alarm, [_point(1, 90.0), _point(2, 20.0)])

    assert result["status"] == "NORMALIZED"


def test_cloudwatch_failure_remains_pending():
    cloudwatch = Mock()
    cloudwatch.get_metric_statistics.side_effect = RuntimeError("unavailable")

    result = verify_post_reset(
        ALARM_DATA,
        cloudwatch,
        attempts=2,
        interval_seconds=0,
        started_at=STARTED_AT,
        sleep=Mock(),
        now=_now(),
    )

    assert result["status"] == "PENDING"
    assert "RuntimeError" in result["reason"]


def test_invalid_datapoint_collection_is_not_filled_as_not_breaching():
    cloudwatch = Mock()
    cloudwatch.get_metric_statistics.return_value = {"Datapoints": None}

    result = verify_post_reset(
        _alarm(TreatMissingData="notBreaching"),
        cloudwatch,
        attempts=1,
        interval_seconds=0,
        started_at=STARTED_AT,
        sleep=Mock(),
        now=_now(),
    )

    assert result["status"] == "PENDING"
    assert "invalid datapoint collection" in result["reason"]
