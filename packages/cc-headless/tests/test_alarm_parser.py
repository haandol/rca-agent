from cc_headless.alarm_parser import AlarmContext, parse_alarm


def test_parse_full_alarm():
    data = {
        "AlarmName": "HighCPU",
        "NewStateReason": "Threshold crossed",
        "StateChangeTime": "2024-01-01T00:00:00Z",
        "Region": "ap-northeast-2",
        "Trigger": {
            "MetricName": "CPUUtilization",
            "Namespace": "AWS/ECS",
            "Dimensions": [
                {"name": "ClusterName", "value": "prod"},
                {"name": "ServiceName", "value": "api"},
            ],
            "Statistic": "Average",
            "Period": 60,
            "Threshold": 80.0,
            "ComparisonOperator": "GreaterThanThreshold",
        },
    }
    alarm = parse_alarm(data)

    assert alarm.alarm_name == "HighCPU"
    assert alarm.state_reason == "Threshold crossed"
    assert alarm.state_change_time == "2024-01-01T00:00:00Z"
    assert alarm.region == "ap-northeast-2"
    assert alarm.metric_name == "CPUUtilization"
    assert alarm.namespace == "AWS/ECS"
    assert alarm.dimensions == {"ClusterName": "prod", "ServiceName": "api"}
    assert alarm.statistic == "Average"
    assert alarm.period == 60
    assert alarm.threshold == 80.0
    assert alarm.comparison_operator == "GreaterThanThreshold"


def test_parse_minimal_alarm():
    data = {"AlarmName": "TestAlarm"}
    alarm = parse_alarm(data)

    assert alarm.alarm_name == "TestAlarm"
    assert alarm.state_reason == ""
    assert alarm.state_change_time is None
    assert alarm.metric_name is None
    assert alarm.dimensions == {}


def test_parse_empty_alarm():
    alarm = parse_alarm({})

    assert alarm.alarm_name == "UnknownAlarm"
    assert isinstance(alarm, AlarmContext)
