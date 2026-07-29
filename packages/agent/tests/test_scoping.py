from threading import Event
from time import perf_counter
from unittest.mock import MagicMock

from rca_agent.ports.dto.models import AlarmPayload, ReportMatch, ScopingResult
from rca_agent.services.scoping import ScopingOutput, build_report_query, run_scoping


def _report_store(matches: list[ReportMatch] | None = None) -> MagicMock:
    store = MagicMock()
    store.search_similar.return_value = matches or []
    return store


class TestBuildReportQuery:
    def test_uses_shared_embedding_template(self, sample_alarm: AlarmPayload):
        query = build_report_query(sample_alarm)

        assert query.startswith("장애유형: HighCPU-web-service")
        assert "증상: Threshold Crossed" in query
        assert "메트릭: CPUUtilization" in query

    def test_truncates_each_field_to_80_chars(self):
        alarm = AlarmPayload(
            alarm_name="A" * 200,
            new_state_reason="B" * 200,
        )

        query = build_report_query(alarm)

        assert "A" * 80 in query
        assert "A" * 81 not in query
        assert "B" * 80 in query
        assert "B" * 81 not in query


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

        result = run_scoping(sample_alarm, mock_agent, report_store=_report_store())

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

        run_scoping(sample_alarm, mock_agent, report_store=_report_store())

        mock_agent.assert_called_once()
        _, kwargs = mock_agent.call_args
        assert kwargs["structured_output_model"] is ScopingOutput

    def test_handles_null_anomaly_time(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(alarm_summary="test", anomaly_start_time=None)
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent, report_store=_report_store())
        assert result.anomaly_start_time is None

    def test_handles_invalid_anomaly_time(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(alarm_summary="test", anomaly_start_time="not-a-date")
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent, report_store=_report_store())
        assert result.anomaly_start_time is None

    def test_includes_reports_from_store_in_result(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(alarm_summary="test")
        mock_agent = self._make_mock_agent(output)
        store = _report_store(
            [
                ReportMatch(
                    rca_id="rca-1",
                    similarity=0.9,
                    root_cause="Past CPU incident",
                    incident_summary="Memory leak",
                    confirmed=True,
                )
            ]
        )

        result = run_scoping(sample_alarm, mock_agent, report_store=store)

        store.search_similar.assert_called_once_with(build_report_query(sample_alarm))
        assert len(result.similar_reports) == 1
        assert result.similar_reports[0].root_cause == "Past CPU incident"

    def test_injects_reports_into_prompt(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(alarm_summary="test")
        mock_agent = self._make_mock_agent(output)
        store = _report_store(
            [
                ReportMatch(
                    rca_id="rca-1",
                    similarity=0.91,
                    root_cause="Past CPU incident",
                    incident_summary="Memory leak",
                    confirmed=True,
                )
            ]
        )

        run_scoping(sample_alarm, mock_agent, report_store=store)

        prompt = mock_agent.call_args[0][0]
        assert "## Similar Past RCA Reports" in prompt
        assert "Past CPU incident" in prompt
        assert "similarity: 0.91" in prompt
        assert "confirmed" in prompt

    def test_prompt_contains_alarm_details(self, sample_alarm: AlarmPayload):
        output = ScopingOutput(alarm_summary="test")
        mock_agent = self._make_mock_agent(output)

        run_scoping(sample_alarm, mock_agent, report_store=_report_store())

        call_args = mock_agent.call_args
        prompt = call_args[0][0]
        assert "HighCPU-web-service" in prompt
        assert "CPUUtilization" in prompt
        assert "AWS/ECS" in prompt

    def test_timeout_returns_fallback_result(self, sample_alarm: AlarmPayload):
        def slow_agent(prompt, **kwargs):
            Event().wait(0.35)
            return MagicMock(structured_output=ScopingOutput(alarm_summary="too late"))

        mock_agent = MagicMock(side_effect=slow_agent)

        started = perf_counter()
        result = run_scoping(sample_alarm, mock_agent, report_store=_report_store(), timeout_seconds=0)
        elapsed = perf_counter() - started

        assert elapsed < 0.15
        assert isinstance(result, ScopingResult)
        assert result.alarm_summary.startswith("[Timeout]")
        assert "HighCPU-web-service" in result.alarm_summary
        assert result.raw_alarm == sample_alarm

    def test_agent_exception_returns_fallback_result(self, sample_alarm: AlarmPayload):
        mock_agent = MagicMock(side_effect=RuntimeError("LLM error"))

        result = run_scoping(sample_alarm, mock_agent, report_store=_report_store())

        assert isinstance(result, ScopingResult)
        assert result.alarm_summary.startswith("[Timeout]")
        assert result.raw_alarm == sample_alarm
