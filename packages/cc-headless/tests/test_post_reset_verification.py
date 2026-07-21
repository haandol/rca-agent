from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

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
    },
}


def _now():
    return STARTED_AT + timedelta(minutes=5)


def test_post_reset_verification_records_normalized_from_fresh_datapoint():
    cloudwatch = Mock()
    cloudwatch.get_metric_statistics.return_value = {
        "Datapoints": [{"Timestamp": STARTED_AT + timedelta(minutes=1), "Average": 25.0}]
    }
    sleep = Mock()

    result = verify_post_reset(
        ALARM_DATA,
        cloudwatch,
        attempts=3,
        interval_seconds=10,
        started_at=STARTED_AT,
        sleep=sleep,
        now=_now,
    )

    assert result["status"] == "NORMALIZED"
    assert result["observed_value"] == 25.0
    assert result["attempts"] == 1
    sleep.assert_not_called()


def test_post_reset_verification_records_failed_after_bounded_breaches():
    cloudwatch = Mock()
    cloudwatch.get_metric_statistics.side_effect = [
        {"Datapoints": [{"Timestamp": STARTED_AT + timedelta(minutes=1), "Average": 95.0}]},
        {"Datapoints": [{"Timestamp": STARTED_AT + timedelta(minutes=2), "Average": 90.0}]},
        {"Datapoints": [{"Timestamp": STARTED_AT + timedelta(minutes=3), "Average": 85.0}]},
    ]
    sleep = Mock()

    result = verify_post_reset(
        ALARM_DATA,
        cloudwatch,
        attempts=3,
        interval_seconds=10,
        started_at=STARTED_AT,
        sleep=sleep,
        now=_now,
    )

    assert result["status"] == "FAILED"
    assert result["observed_value"] == 85.0
    assert result["attempts"] == 3
    assert sleep.call_count == 2


def test_post_reset_verification_records_pending_without_fresh_observation():
    cloudwatch = Mock()
    cloudwatch.get_metric_statistics.return_value = {
        "Datapoints": [{"Timestamp": STARTED_AT - timedelta(minutes=1), "Average": 20.0}]
    }

    result = verify_post_reset(
        ALARM_DATA,
        cloudwatch,
        attempts=2,
        interval_seconds=0,
        started_at=STARTED_AT,
        sleep=Mock(),
        now=_now,
    )

    assert result["status"] == "PENDING"
    assert result["observed_value"] is None
    assert "no post-reset CloudWatch datapoint" in result["reason"]


def test_post_reset_verification_records_pending_when_cloudwatch_fails():
    cloudwatch = Mock()
    cloudwatch.get_metric_statistics.side_effect = RuntimeError("unavailable")

    result = verify_post_reset(
        ALARM_DATA,
        cloudwatch,
        attempts=2,
        interval_seconds=0,
        started_at=STARTED_AT,
        sleep=Mock(),
        now=_now,
    )

    assert result["status"] == "PENDING"
    assert "RuntimeError" in result["reason"]
