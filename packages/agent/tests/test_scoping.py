from unittest.mock import MagicMock, patch

from rca_agent.ports.dto.models import AlarmPayload, ScopingResult
from rca_agent.services.scoping import ScopingOutput, run_scoping, search_similar_reports


class TestSearchSimilarReports:
    def test_returns_empty_when_no_client(self, sample_alarm: AlarmPayload, fake_embedding):
        result = search_similar_reports(sample_alarm, embedding=fake_embedding, s3_vectors_client=None)
        assert result == []

    @patch("rca_agent.services.scoping.S3_VECTOR_BUCKET_NAME", "my-vector-bucket")
    def test_returns_matches_above_threshold(self, sample_alarm: AlarmPayload, fake_embedding):
        mock_client = MagicMock()
        mock_client.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "rca-001",
                    "distance": 0.85,
                    "metadata": {
                        "root_cause": "Task count too low",
                        "incident_summary": "CPU spike",
                        "confirmed": "true",
                    },
                },
                {
                    "key": "rca-002",
                    "distance": 0.5,
                    "metadata": {"root_cause": "Irrelevant"},
                },
            ]
        }
        result = search_similar_reports(sample_alarm, embedding=fake_embedding, s3_vectors_client=mock_client)
        assert len(result) == 1
        assert result[0].rca_id == "rca-001"
        assert result[0].similarity == 0.85
        assert result[0].confirmed is True

    @patch("rca_agent.services.scoping.S3_VECTOR_BUCKET_NAME", "my-vector-bucket")
    def test_handles_api_error_gracefully(self, sample_alarm: AlarmPayload, fake_embedding):
        mock_client = MagicMock()
        mock_client.query_vectors.side_effect = RuntimeError("S3 Vectors unavailable")
        result = search_similar_reports(
            sample_alarm, embedding=fake_embedding, s3_vectors_client=mock_client, base_delay=0.01
        )
        assert result == []

    @patch("rca_agent.services.scoping.S3_VECTOR_BUCKET_NAME", "my-vector-bucket")
    def test_retries_with_exponential_backoff(self, sample_alarm: AlarmPayload, fake_embedding):
        mock_client = MagicMock()
        mock_client.query_vectors.side_effect = [
            RuntimeError("transient error"),
            {"vectors": [{"key": "rca-1", "distance": 0.9, "metadata": {"root_cause": "Found"}}]},
        ]
        result = search_similar_reports(
            sample_alarm, embedding=fake_embedding, s3_vectors_client=mock_client, base_delay=0.01
        )
        assert len(result) == 1
        assert result[0].rca_id == "rca-1"
        assert mock_client.query_vectors.call_count == 2

    @patch("rca_agent.services.scoping.S3_VECTOR_BUCKET_NAME", "my-vector-bucket")
    def test_exhausts_all_retries(self, sample_alarm: AlarmPayload, fake_embedding):
        mock_client = MagicMock()
        mock_client.query_vectors.side_effect = RuntimeError("persistent failure")
        result = search_similar_reports(
            sample_alarm, embedding=fake_embedding, s3_vectors_client=mock_client, max_retries=2, base_delay=0.01
        )
        assert result == []
        assert mock_client.query_vectors.call_count == 2


class TestRunScoping:
    def _make_mock_agent(self, output: ScopingOutput) -> MagicMock:
        mock_result = MagicMock()
        mock_result.structured_output = output
        mock_agent = MagicMock()
        mock_agent.return_value = mock_result
        return mock_agent

    def test_returns_scoping_result(self, sample_alarm: AlarmPayload, fake_embedding):
        output = ScopingOutput(
            alarm_summary="CPU utilization on web-service exceeded 80% threshold",
            anomaly_start_time="2026-04-22T10:25:00Z",
            blast_radius="single",
            initial_severity="high",
            metric_snapshot={"CPUUtilization": {"current": 92.5, "baseline": 45.0, "unit": "Percent"}},
        )
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent, embedding=fake_embedding)

        assert isinstance(result, ScopingResult)
        assert result.alarm_summary == "CPU utilization on web-service exceeded 80% threshold"
        assert result.blast_radius == "single"
        assert result.initial_severity == "high"
        assert result.anomaly_start_time is not None
        assert result.metric_snapshot["CPUUtilization"]["current"] == 92.5
        assert result.raw_alarm == sample_alarm

    def test_passes_structured_output_model(self, sample_alarm: AlarmPayload, fake_embedding):
        output = ScopingOutput(alarm_summary="test")
        mock_agent = self._make_mock_agent(output)

        run_scoping(sample_alarm, mock_agent, embedding=fake_embedding)

        mock_agent.assert_called_once()
        _, kwargs = mock_agent.call_args
        assert kwargs["structured_output_model"] is ScopingOutput

    def test_handles_null_anomaly_time(self, sample_alarm: AlarmPayload, fake_embedding):
        output = ScopingOutput(alarm_summary="test", anomaly_start_time=None)
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent, embedding=fake_embedding)
        assert result.anomaly_start_time is None

    def test_handles_invalid_anomaly_time(self, sample_alarm: AlarmPayload, fake_embedding):
        output = ScopingOutput(alarm_summary="test", anomaly_start_time="not-a-date")
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent, embedding=fake_embedding)
        assert result.anomaly_start_time is None

    @patch("rca_agent.services.scoping.S3_VECTOR_BUCKET_NAME", "my-vector-bucket")
    def test_includes_reports_in_result(self, sample_alarm: AlarmPayload, fake_embedding):
        output = ScopingOutput(alarm_summary="test")
        mock_agent = self._make_mock_agent(output)

        mock_s3v = MagicMock()
        mock_s3v.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "rca-1",
                    "distance": 0.9,
                    "metadata": {
                        "root_cause": "Past CPU incident",
                        "incident_summary": "Memory leak",
                        "confirmed": "true",
                    },
                }
            ]
        }

        result = run_scoping(sample_alarm, mock_agent, embedding=fake_embedding, s3_vectors_client=mock_s3v)
        assert len(result.similar_reports) == 1
        assert result.similar_reports[0].root_cause == "Past CPU incident"

    def test_prompt_contains_alarm_details(self, sample_alarm: AlarmPayload, fake_embedding):
        output = ScopingOutput(alarm_summary="test")
        mock_agent = self._make_mock_agent(output)

        run_scoping(sample_alarm, mock_agent, embedding=fake_embedding)

        call_args = mock_agent.call_args
        prompt = call_args[0][0]
        assert "HighCPU-web-service" in prompt
        assert "CPUUtilization" in prompt
        assert "AWS/ECS" in prompt

    def test_timeout_returns_fallback_result(self, sample_alarm: AlarmPayload, fake_embedding):
        import time as _time

        def slow_agent(prompt, **kwargs):
            _time.sleep(5)
            return MagicMock(structured_output=ScopingOutput(alarm_summary="too late"))

        mock_agent = MagicMock(side_effect=slow_agent)

        result = run_scoping(sample_alarm, mock_agent, embedding=fake_embedding, timeout_seconds=1)

        assert isinstance(result, ScopingResult)
        assert result.alarm_summary.startswith("[Timeout]")
        assert "HighCPU-web-service" in result.alarm_summary
        assert result.raw_alarm == sample_alarm

    def test_agent_exception_returns_fallback_result(self, sample_alarm: AlarmPayload, fake_embedding):
        mock_agent = MagicMock(side_effect=RuntimeError("LLM error"))

        result = run_scoping(sample_alarm, mock_agent, embedding=fake_embedding)

        assert isinstance(result, ScopingResult)
        assert result.alarm_summary.startswith("[Timeout]")
        assert result.raw_alarm == sample_alarm
