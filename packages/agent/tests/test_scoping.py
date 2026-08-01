from threading import Event
from time import perf_counter
from unittest.mock import MagicMock

from rca_agent.ports.dto.models import (
    AlarmPayload,
    MetricTrend,
    ReportMatch,
    ScopingResult,
)
from rca_agent.services.scoping import (
    ConcurrentAlarmOutput,
    MetricObservationOutput,
    ScopingOutput,
    build_report_query,
    reconcile_trend,
    run_scoping,
)


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
            metric_observations=[
                MetricObservationOutput(
                    metric_name="CPUUtilization",
                    datapoints=[45.0, 60.0, 78.0, 92.5],
                    window_start="2026-04-22T10:00:00Z",
                    window_end="2026-04-22T10:30:00Z",
                    unit="Percent",
                    baseline=45.0,
                )
            ],
            concurrent_alarms=[ConcurrentAlarmOutput(alarm_name="VitalIngestFailures", state="ALARM")],
        )
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent, report_store=_report_store())

        assert isinstance(result, ScopingResult)
        assert result.alarm_summary == "CPU utilization on web-service exceeded 80% threshold"
        assert result.blast_radius == "single"
        assert result.initial_severity == "high"
        assert result.anomaly_start_time is not None
        observation = result.metric_observations[0]
        assert observation.datapoints == [45.0, 60.0, 78.0, 92.5]
        assert observation.window_start is not None
        assert result.concurrent_alarms[0].alarm_name == "VitalIngestFailures"
        assert result.raw_alarm == sample_alarm

    def test_the_model_keeps_its_reading_of_the_sequence(self, sample_alarm: AlarmPayload):
        """추세 해석은 모델이 한다.

        서버가 규칙으로 도출하면 규칙이 모르는 형태를 가장 가까운 항목으로 뭉개고 모델이
        반박할 수 없다. 추세는 되돌릴 수 없는 결정이 아니므로 서버가 권위를 가질 이유가
        없다 — 서버는 근거 없는 단정만 막는다.
        """
        output = ScopingOutput(
            alarm_summary="connections climbing in steps",
            metric_observations=[
                MetricObservationOutput(
                    metric_name="DatabaseConnections",
                    datapoints=[2.0, 12.0, 12.0, 27.0, 27.0],
                    trend=MetricTrend.RISING,
                    shape_note="계단식으로 두 번 올라 각각 유지됐다",
                )
            ],
        )
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent, report_store=_report_store())

        observation = result.metric_observations[0]
        assert observation.trend is MetricTrend.RISING
        # 어휘에 담기지 않는 형태를 뭉개지 않고 함께 전달한다.
        assert observation.shape_note == "계단식으로 두 번 올라 각각 유지됐다"

    def test_a_trend_claimed_from_one_datapoint_falls_back_to_unknown(self, sample_alarm: AlarmPayload):
        """근거 없는 단정은 서버가 막는다.

        한 점으로는 어떤 형태도 관측되지 않았다. 이 단정을 허용하면 시퀀스를 요구한
        이유가 사라진다.
        """
        output = ScopingOutput(
            alarm_summary="single sample",
            metric_observations=[
                MetricObservationOutput(
                    metric_name="DatabaseConnections",
                    datapoints=[30.0],
                    trend=MetricTrend.RISING,
                )
            ],
        )
        mock_agent = self._make_mock_agent(output)

        result = run_scoping(sample_alarm, mock_agent, report_store=_report_store())

        assert result.metric_observations[0].trend is MetricTrend.UNKNOWN

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


class TestReconcileTrend:
    """서버는 추세를 도출하지 않고 근거 없는 단정만 막는다."""

    def test_the_reported_reading_survives(self):
        # 규칙이 모르는 형태를 뭉개지 않는 것이 이 계약의 요점이다.
        assert reconcile_trend(MetricTrend.RISING, [2.0, 12.0, 12.0, 27.0]) is MetricTrend.RISING
        assert reconcile_trend(MetricTrend.SPIKE, [2.0, 30.0, 2.5]) is MetricTrend.SPIKE
        assert reconcile_trend(MetricTrend.FLAT, [10.0, 10.4, 9.8]) is MetricTrend.FLAT

    def test_too_few_points_cannot_claim_a_shape(self):
        # 한 점으로는 어떤 형태도 관측되지 않았다.
        assert reconcile_trend(MetricTrend.RISING, [15.0]) is MetricTrend.UNKNOWN
        assert reconcile_trend(MetricTrend.SPIKE, []) is MetricTrend.UNKNOWN

    def test_two_points_are_enough_to_report_a_shape(self):
        assert reconcile_trend(MetricTrend.RISING, [2.0, 30.0]) is MetricTrend.RISING
