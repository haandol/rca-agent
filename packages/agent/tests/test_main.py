import json
from unittest.mock import MagicMock, patch

from rca_agent.main import _parse_sns_envelope, _process_alarm
from rca_agent.models import HypothesisGenerationResult, ScopingResult


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
    def _make_body(self, alarm_name="HighCPU"):
        return {
            "AlarmName": alarm_name,
            "NewStateValue": "ALARM",
            "NewStateReason": "Threshold crossed",
            "Trigger": {
                "MetricName": "CPUUtilization",
                "Namespace": "AWS/ECS",
                "Dimensions": [],
            },
        }

    def test_invokes_scoping_and_hypothesis(self):
        body = self._make_body()
        mock_scoping_agent = MagicMock()
        mock_hypothesis_agent = MagicMock()
        mock_scoping_result = ScopingResult(alarm_summary="test")
        mock_hypothesis_result = HypothesisGenerationResult(
            tree_id="tree-1", hypotheses=[], scoping_result=mock_scoping_result
        )

        with (
            patch("rca_agent.main.run_scoping", return_value=mock_scoping_result) as mock_run_scoping,
            patch("rca_agent.main.run_hypothesis_generation", return_value=mock_hypothesis_result) as mock_run_hypo,
        ):
            _process_alarm(body, mock_scoping_agent, mock_hypothesis_agent, s3_vectors_client=None)

        mock_run_scoping.assert_called_once()
        assert mock_run_scoping.call_args[0][0].alarm_name == "HighCPU"
        assert mock_run_scoping.call_args[0][1] is mock_scoping_agent

        mock_run_hypo.assert_called_once()
        assert mock_run_hypo.call_args[0][0] is mock_scoping_result
        assert mock_run_hypo.call_args[0][1] is mock_hypothesis_agent

    def test_handles_sns_wrapped_body(self):
        alarm_data = {
            "AlarmName": "HighLatency",
            "NewStateValue": "ALARM",
            "NewStateReason": "p99 > 500ms",
        }
        body = {"Message": json.dumps(alarm_data), "Type": "Notification"}
        mock_scoping_result = ScopingResult(alarm_summary="test")
        mock_hypothesis_result = HypothesisGenerationResult(
            tree_id="tree-1", hypotheses=[], scoping_result=mock_scoping_result
        )

        with (
            patch("rca_agent.main.run_scoping", return_value=mock_scoping_result) as mock_run_scoping,
            patch("rca_agent.main.run_hypothesis_generation", return_value=mock_hypothesis_result),
        ):
            _process_alarm(body, MagicMock(), MagicMock(), s3_vectors_client=None)

        assert mock_run_scoping.call_args[0][0].alarm_name == "HighLatency"
