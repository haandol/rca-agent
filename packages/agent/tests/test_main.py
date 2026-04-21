import json
from unittest.mock import MagicMock, patch

from rca_agent.main import _parse_sns_envelope, _process_alarm
from rca_agent.models import ScopingResult


class TestParseSnsEnvelope:
    def test_extracts_message_from_sns_wrapper(self):
        alarm_data = {"AlarmName": "HighCPU", "NewStateValue": "ALARM"}
        body = {"Message": json.dumps(alarm_data), "Type": "Notification"}
        result = _parse_sns_envelope(body)
        assert result == alarm_data

    def test_returns_raw_body_when_no_envelope(self):
        body = {"AlarmName": "HighCPU", "NewStateValue": "ALARM"}
        result = _parse_sns_envelope(body)
        assert result == body

    def test_returns_raw_body_when_message_is_not_string(self):
        body = {"Message": {"nested": True}}
        result = _parse_sns_envelope(body)
        assert result == body


class TestProcessAlarm:
    def test_invokes_scoping(self):
        body = {
            "AlarmName": "HighCPU",
            "NewStateValue": "ALARM",
            "NewStateReason": "Threshold crossed",
            "Trigger": {
                "MetricName": "CPUUtilization",
                "Namespace": "AWS/ECS",
                "Dimensions": [],
            },
        }
        mock_agent = MagicMock()
        mock_scoping_result = ScopingResult(alarm_summary="test")

        with patch("rca_agent.main.run_scoping", return_value=mock_scoping_result) as mock_run:
            _process_alarm(body, mock_agent, s3_vectors_client=None)

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs[0][0].alarm_name == "HighCPU"
        assert call_kwargs[0][1] is mock_agent

    def test_handles_sns_wrapped_body(self):
        alarm_data = {
            "AlarmName": "HighLatency",
            "NewStateValue": "ALARM",
            "NewStateReason": "p99 > 500ms",
        }
        body = {"Message": json.dumps(alarm_data), "Type": "Notification"}
        mock_agent = MagicMock()
        mock_scoping_result = ScopingResult(alarm_summary="test")

        with patch("rca_agent.main.run_scoping", return_value=mock_scoping_result) as mock_run:
            _process_alarm(body, mock_agent, s3_vectors_client=None)

        assert mock_run.call_args[0][0].alarm_name == "HighLatency"
