from unittest.mock import MagicMock, patch

from rca_agent.models import AlarmPayload, ScopingResult
from rca_agent.scoping import ScopingOutput, run_scoping, search_similar_playbooks


class TestSearchSimilarPlaybooks:
    def test_returns_empty_when_no_client(self, sample_alarm: AlarmPayload):
        result = search_similar_playbooks(sample_alarm, s3_vectors_client=None)
        assert result == []

    @patch("rca_agent.scoping.S3_VECTOR_BUCKET_NAME", "my-vector-bucket")
    def test_returns_matches_above_threshold(self, sample_alarm: AlarmPayload):
        mock_client = MagicMock()
        mock_client.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "playbook-001",
                    "distance": 0.85,
                    "metadata": {"title": "ECS CPU spike", "root_cause_summary": "Task count too low"},
                },
                {
                    "key": "playbook-002",
                    "distance": 0.5,
                    "metadata": {"title": "Irrelevant playbook"},
                },
            ]
        }
        result = search_similar_playbooks(sample_alarm, s3_vectors_client=mock_client)
        assert len(result) == 1
        assert result[0].playbook_id == "playbook-001"
        assert result[0].similarity == 0.85

    @patch("rca_agent.scoping.S3_VECTOR_BUCKET_NAME", "my-vector-bucket")
    def test_handles_api_error_gracefully(self, sample_alarm: AlarmPayload):
        mock_client = MagicMock()
        mock_client.query_vectors.side_effect = RuntimeError("S3 Vectors unavailable")
        result = search_similar_playbooks(sample_alarm, s3_vectors_client=mock_client)
        assert result == []


class TestRunScoping:
    def _make_mock_agent(self, output: ScopingOutput) -> MagicMock:
        mock_result = MagicMock()
        mock_result.structured_output = output
        mock_agent = MagicMock()
        mock_agent.return_value = mock_result
        return mock_agent

    def test_returns_scoping_result(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(
            alarm_summary="CPU utilization on web-service exceeded 80% threshold",
            anomaly_start_time="2026-04-22T10:25:00Z",
            blast_radius="single",
            initial_severity="high",
            metric_snapshot={"CPUUtilization": {"current": 92.5, "baseline": 45.0, "unit": "Percent"}},
        )
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent)

        assert isinstance(result, ScopingResult)
        assert result.alarm_summary == "CPU utilization on web-service exceeded 80% threshold"
        assert result.blast_radius == "single"
        assert result.initial_severity == "high"
        assert result.anomaly_start_time is not None
        assert result.metric_snapshot["CPUUtilization"]["current"] == 92.5
        assert result.raw_alarm == sample_alarm

    def test_passes_structured_output_model(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(alarm_summary="test")
        mock_agent = self._make_mock_agent(output)

        run_scoping(sample_alarm, mock_agent)

        mock_agent.assert_called_once()
        _, kwargs = mock_agent.call_args
        assert kwargs["structured_output_model"] is ScopingOutput

    def test_handles_null_anomaly_time(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(alarm_summary="test", anomaly_start_time=None)
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent)
        assert result.anomaly_start_time is None

    def test_handles_invalid_anomaly_time(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(alarm_summary="test", anomaly_start_time="not-a-date")
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent)
        assert result.anomaly_start_time is None

    @patch("rca_agent.scoping.S3_VECTOR_BUCKET_NAME", "my-vector-bucket")
    def test_includes_playbooks_in_result(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(alarm_summary="test")
        mock_agent = self._make_mock_agent(output)

        mock_s3v = MagicMock()
        mock_s3v.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "pb-1",
                    "distance": 0.9,
                    "metadata": {"title": "Past CPU incident", "root_cause_summary": "Memory leak"},
                }
            ]
        }

        result = run_scoping(sample_alarm, mock_agent, s3_vectors_client=mock_s3v)
        assert len(result.similar_playbooks) == 1
        assert result.similar_playbooks[0].title == "Past CPU incident"

    def test_prompt_contains_alarm_details(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(alarm_summary="test")
        mock_agent = self._make_mock_agent(output)

        run_scoping(sample_alarm, mock_agent)

        call_args = mock_agent.call_args
        prompt = call_args[0][0]
        assert "HighCPU-web-service" in prompt
        assert "CPUUtilization" in prompt
        assert "AWS/ECS" in prompt
