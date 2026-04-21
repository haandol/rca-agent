from unittest.mock import MagicMock

from rca_agent.models import (
    Hypothesis,
    HypothesisCategory,
    RcaReport,
    ScopingResult,
)
from rca_agent.report import (
    ReportOutput,
    _render_markdown,
    run_report_generation,
    save_report_to_s3,
)


def _make_scoping() -> ScopingResult:
    return ScopingResult(alarm_summary="CPU spike on web-service", initial_severity="high")


def _make_hypothesis() -> Hypothesis:
    return Hypothesis(
        hypothesis_id="h-1",
        description="Memory leak in worker process",
        category=HypothesisCategory.INFRASTRUCTURE,
        confidence_score=0.9,
        tree_id="tree-1",
    )


def _make_mock_agent(output: ReportOutput) -> MagicMock:
    mock_result = MagicMock()
    mock_result.structured_output = output
    agent = MagicMock()
    agent.return_value = mock_result
    return agent


class TestRunReportGeneration:
    def test_generates_report(self):
        output = ReportOutput(
            incident_summary="ECS web-service CPU spike",
            root_cause="Memory leak in worker process",
            temporary_mitigation="Restart tasks",
            permanent_remediation="Fix memory leak in v2.3.1",
            timeline=["10:30 alarm fired", "10:35 scoping complete"],
        )
        agent = _make_mock_agent(output)

        report = run_report_generation(
            _make_scoping(), _make_hypothesis(), True, ["h-1"], ["cpu evidence"], ["rejected-1"], ["t1"], agent
        )

        assert isinstance(report, RcaReport)
        assert report.incident_summary == "ECS web-service CPU spike"
        assert report.root_cause_confirmed
        assert report.temporary_mitigation == "Restart tasks"
        assert report.rca_id

    def test_uses_structured_output(self):
        output = ReportOutput(incident_summary="test", root_cause="test")
        agent = _make_mock_agent(output)

        run_report_generation(_make_scoping(), _make_hypothesis(), True, [], [], [], [], agent)

        _, kwargs = agent.call_args
        assert kwargs["structured_output_model"] is ReportOutput

    def test_fallback_on_failure(self):
        agent = MagicMock(side_effect=RuntimeError("fail"))
        h = _make_hypothesis()

        report = run_report_generation(_make_scoping(), h, False, ["h-1"], ["ev"], ["rej"], ["t1"], agent)

        assert report.root_cause == h.description
        assert not report.root_cause_confirmed


class TestRenderMarkdown:
    def test_renders_sections(self):
        report = RcaReport(
            rca_id="rca-1",
            incident_summary="CPU spike",
            root_cause="Memory leak",
            root_cause_confirmed=True,
            confidence_score=0.9,
            hypothesis_path=["h-1"],
            evidence_list=["high CPU"],
            temporary_mitigation="Restart",
            permanent_remediation="Fix leak",
            timeline=["10:30 alarm"],
            rejected_hypotheses=["traffic spike"],
        )
        md = _render_markdown(report)
        assert "# RCA Report: rca-1" in md
        assert "Memory leak" in md
        assert "Confirmed" in md
        assert "Restart" in md


class TestSaveReportToS3:
    def test_skips_when_not_configured(self):
        report = RcaReport(rca_id="r-1", incident_summary="t", root_cause="t", confidence_score=0.5)
        assert save_report_to_s3(report) == ""

    def test_uploads_to_s3(self):
        from unittest.mock import patch

        report = RcaReport(rca_id="r-1", incident_summary="t", root_cause="t", confidence_score=0.5)
        mock_s3 = MagicMock()

        with patch("rca_agent.report.S3_REPORT_BUCKET", "my-bucket"):
            key = save_report_to_s3(report, s3_client=mock_s3)

        assert key == "reports/r-1.md"
        mock_s3.put_object.assert_called_once()
