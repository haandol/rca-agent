from datetime import UTC, datetime

from rca_agent.models import AlarmPayload, RcaSessionState, ScopingResult


class TestRcaSessionState:
    def test_states_are_strings(self):
        assert RcaSessionState.ALARM_RECEIVED == "ALARM_RECEIVED"
        assert RcaSessionState.SCOPING == "SCOPING"
        assert RcaSessionState.COMPLETED == "COMPLETED"
        assert RcaSessionState.FAILED == "FAILED"

    def test_all_states_exist(self):
        expected = {
            "ALARM_RECEIVED",
            "SCOPING",
            "HYPOTHESIS_GENERATION",
            "HYPOTHESIS_PRIORITIZATION",
            "EVIDENCE_COLLECTION",
            "HYPOTHESIS_VALIDATION",
            "REPORT_GENERATION",
            "COMPLETED",
            "FAILED",
            "OUTDATED",
            "CANCELLED",
        }
        assert {s.value for s in RcaSessionState} == expected


class TestAlarmPayload:
    def test_resource_id_from_dimensions(self, sample_alarm: AlarmPayload):
        assert sample_alarm.resource_id == "web-service"

    def test_resource_id_fallback_to_alarm_name(self):
        alarm = AlarmPayload(alarm_name="my-alarm")
        assert alarm.resource_id == "my-alarm"

    def test_service_name_from_namespace(self, sample_alarm: AlarmPayload):
        assert sample_alarm.service_name == "AWS/ECS"

    def test_service_name_unknown_without_trigger(self):
        alarm = AlarmPayload(alarm_name="my-alarm")
        assert alarm.service_name == "Unknown"

    def test_from_dict(self):
        data = {
            "alarm_name": "test-alarm",
            "new_state_reason": "threshold crossed",
            "trigger": {
                "metric_name": "Latency",
                "namespace": "AWS/ELB",
            },
        }
        alarm = AlarmPayload.model_validate(data)
        assert alarm.alarm_name == "test-alarm"
        assert alarm.trigger.metric_name == "Latency"


class TestAlarmPayloadFromCloudwatchSns:
    def test_parses_full_payload(self):
        raw = {
            "AlarmName": "HighCPU",
            "AlarmArn": "arn:aws:cloudwatch:ap-northeast-2:123456789012:alarm:HighCPU",
            "NewStateValue": "ALARM",
            "NewStateReason": "Threshold crossed",
            "StateChangeTime": "2026-04-22T10:30:00.000+0000",
            "Trigger": {
                "MetricName": "CPUUtilization",
                "Namespace": "AWS/ECS",
                "Dimensions": [
                    {"name": "ServiceName", "value": "web-service"},
                    {"name": "ClusterName", "value": "prod"},
                ],
                "Statistic": "Average",
                "Period": 300,
                "Threshold": 80.0,
                "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            },
        }
        alarm = AlarmPayload.from_cloudwatch_sns(raw)
        assert alarm.alarm_name == "HighCPU"
        assert alarm.region == "ap-northeast-2"
        assert alarm.trigger is not None
        assert alarm.trigger.metric_name == "CPUUtilization"
        assert alarm.trigger.dimensions == {"ServiceName": "web-service", "ClusterName": "prod"}
        assert alarm.trigger.threshold == 80.0

    def test_parses_minimal_payload(self):
        raw = {"AlarmName": "SimpleAlarm", "NewStateReason": "something broke"}
        alarm = AlarmPayload.from_cloudwatch_sns(raw)
        assert alarm.alarm_name == "SimpleAlarm"
        assert alarm.trigger is None
        assert alarm.region == "us-east-1"

    def test_extracts_region_from_arn(self):
        raw = {
            "AlarmName": "test",
            "AlarmArn": "arn:aws:cloudwatch:eu-west-1:111111111111:alarm:test",
        }
        alarm = AlarmPayload.from_cloudwatch_sns(raw)
        assert alarm.region == "eu-west-1"

    def test_handles_empty_trigger(self):
        raw = {"AlarmName": "test", "Trigger": {}}
        alarm = AlarmPayload.from_cloudwatch_sns(raw)
        assert alarm.trigger is None


class TestScopingResult:
    def test_minimal(self):
        result = ScopingResult(alarm_summary="CPU spike on web service")
        assert result.alarm_summary == "CPU spike on web service"
        assert result.blast_radius == "single"
        assert result.initial_severity == "medium"
        assert result.similar_reports == []
        assert result.metric_snapshot == {}

    def test_full(self, sample_alarm: AlarmPayload):
        result = ScopingResult(
            alarm_summary="CPU spike",
            anomaly_start_time=datetime(2026, 4, 22, 10, 25, 0, tzinfo=UTC),
            blast_radius="service_wide",
            initial_severity="high",
            metric_snapshot={"CPUUtilization": {"current": 92.5, "baseline": 45.0, "unit": "Percent"}},
            raw_alarm=sample_alarm,
        )
        assert result.blast_radius == "service_wide"
        assert result.metric_snapshot["CPUUtilization"]["current"] == 92.5
        assert result.raw_alarm.alarm_name == "HighCPU-web-service"
