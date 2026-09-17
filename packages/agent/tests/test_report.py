from threading import Event
from time import perf_counter
from unittest.mock import MagicMock, patch

import pytest

from rca_agent.adapters.secondary.report.s3_report_store import (
    S3ReportStore,
    _render_markdown,
    _step_mismatch,
)
from rca_agent.ports.dto.models import (
    AlarmPayload,
    AlarmTrigger,
    ExecutionStep,
    Hypothesis,
    HypothesisCategory,
    Playbook,
    PlaybookVerificationStatus,
    RcaReport,
    ScopingResult,
)
from rca_agent.services.report import (
    ReportOutput,
    run_report_generation,
)


def _make_playbook(*steps: ExecutionStep) -> Playbook:
    return Playbook(
        playbook_id="pb-1",
        failure_type="Memory leak",
        symptom_pattern="RSS grows monotonically",
        rca_id="rca-1",
        execution_steps=list(steps),
    )


def _make_step(step_id: str = "step-1") -> ExecutionStep:
    return ExecutionStep(
        step_id=step_id,
        intent="워커 풀 재시작",
        action="대상 서비스를 롤링 재시작한다",
        success_criteria="RSS가 임계치 미만으로 복귀",
    )


def _make_scoping() -> ScopingResult:
    alarm = AlarmPayload(
        alarm_name="HighCPU",
        trigger=AlarmTrigger(metric_name="CPUUtilization", namespace="AWS/ECS"),
    )
    return ScopingResult(alarm_summary="CPU spike on web-service", initial_severity="high", raw_alarm=alarm)


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
            severity="high",
            impact_summary="50% of requests returned 5xx for 15 minutes",
            detection_method="CloudWatch CPUUtilization alarm",
            root_cause="Model-authored conflicting root cause",
            temporary_mitigation="Restart tasks",
            permanent_remediation="Fix memory leak in v2.3.1",
            action_items=["[prevent] Add memory limit alerts", "[process] Update runbook"],
            lessons_learned="Detection was fast but escalation was delayed",
            timeline=["10:30 alarm fired", "10:35 scoping complete"],
        )
        agent = _make_mock_agent(output)

        report = run_report_generation(
            _make_scoping(), _make_hypothesis(), True, ["h-1"], ["cpu evidence"], ["rejected-1"], ["t1"], agent
        )

        assert isinstance(report, RcaReport)
        assert report.incident_summary == "ECS web-service CPU spike"
        assert report.severity == "high"
        assert report.impact_summary == "50% of requests returned 5xx for 15 minutes"
        assert report.detection_method == "CloudWatch CPUUtilization alarm"
        assert report.root_cause == _make_hypothesis().description
        assert report.root_cause_confirmed
        assert report.selected_hypothesis_id == "h-1"
        assert report.selected_hypothesis_title == _make_hypothesis().description
        assert report.temporary_mitigation == "Restart tasks"
        assert len(report.action_items) == 2
        assert report.lessons_learned == "Detection was fast but escalation was delayed"
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
        assert report.severity == "high"
        assert report.incident_summary.startswith("[보고서 생성 실패: RuntimeError]")
        assert _make_scoping().alarm_summary in report.incident_summary
        assert report.evidence_list == ["ev"] and report.timeline == ["t1"]
        assert report.hypothesis_path == ["h-1"] and report.rejected_hypotheses == ["rej"]

    @pytest.mark.parametrize("severity", ["critical", "high", "medium", "low"])
    def test_report_output_accepts_only_contract_severities(self, severity):
        """New generated output uses the existing four-value contract before SDK acceptance."""
        assert ReportOutput(incident_summary="incident", root_cause="cause", severity=severity).severity == severity

    def test_invalid_generated_severity_and_failed_report_are_not_normal_success(self):
        """Reject an unknown generated grade and retain an explicit nonsecret fallback marker."""
        with pytest.raises(ValueError, match="severity"):
            ReportOutput(incident_summary="incident", root_cause="cause", severity="catastrophic")
        scope, hypothesis = _make_scoping(), _make_hypothesis()
        with patch("rca_agent.services.report.invoke_agent", side_effect=RuntimeError("PRIVATE_PROVIDER_VALUE")):
            report = run_report_generation(scope, hypothesis, True, ["parent", "cause"], ["partial"], [], ["t1"], None)
        assert report.root_cause == hypothesis.description and report.root_cause_confirmed is True
        assert report.evidence_list == ["partial"]
        assert report.incident_observations == scope.incident_observations
        assert "[보고서 생성 실패: RuntimeError]" in _render_markdown(report, None)
        assert "PRIVATE_PROVIDER_VALUE" not in report.model_dump_json()
        scope.initial_severity = "catastrophic"
        with patch("rca_agent.services.report.invoke_agent", side_effect=RuntimeError("PRIVATE_PROVIDER_VALUE")):
            fallback = run_report_generation(scope, hypothesis, True, [], ["partial"], [], [], None)
        assert fallback.severity == "medium"
        assert "기본 등급 medium" in fallback.incident_summary
        assert fallback.evidence_list == ["partial"] and fallback.root_cause == hypothesis.description

    def test_timeout_returns_minimal_report_without_waiting_for_worker(self):
        def slow_agent(*args, **kwargs):  # noqa: ARG001
            Event().wait(0.35)

        started = perf_counter()
        report = run_report_generation(
            _make_scoping(),
            _make_hypothesis(),
            False,
            ["h-1"],
            ["ev"],
            ["rej"],
            ["t1"],
            MagicMock(side_effect=slow_agent),
            timeout_seconds=0,
        )
        elapsed = perf_counter() - started

        assert elapsed < 0.15
        assert report.root_cause == _make_hypothesis().description
        assert not report.root_cause_confirmed

    def test_prompt_includes_detection_info(self):
        output = ReportOutput(incident_summary="test", root_cause="test")
        agent = _make_mock_agent(output)

        run_report_generation(_make_scoping(), _make_hypothesis(), True, [], [], [], [], agent)

        prompt = agent.call_args[0][0]
        assert "HighCPU" in prompt
        assert "CPUUtilization" in prompt


class TestRenderMarkdown:
    def test_renders_sections(self):
        report = RcaReport(
            rca_id="rca-1",
            incident_summary="CPU spike",
            severity="high",
            impact_summary="Service degraded for 15 minutes",
            detection_method="CloudWatch alarm HighCPU",
            root_cause="Memory leak",
            root_cause_confirmed=True,
            confidence_score=0.9,
            selected_hypothesis_id="h-1",
            selected_hypothesis_title="Worker memory leak",
            hypothesis_path=["h-1"],
            evidence_list=["high CPU"],
            temporary_mitigation="Restart",
            permanent_remediation="Fix leak",
            action_items=["[prevent] Add memory alerts"],
            lessons_learned="Fast detection, slow escalation",
            timeline=["10:30 alarm"],
            rejected_hypotheses=["traffic spike"],
        )
        md = _render_markdown(report, _make_playbook())
        assert "# RCA Report: rca-1" in md
        assert "Memory leak" in md
        assert "`h-1`" in md
        assert "Worker memory leak" in md
        assert "Confirmed" in md
        assert "Restart" in md
        assert "**Severity**: high" in md
        assert "Impact Assessment" in md
        assert "Service degraded" in md
        assert "Detection" in md
        assert "Action Items" in md
        assert "memory alerts" in md
        assert "Lessons Learned" in md
        assert "Fast detection" in md

    def test_renders_minimal_report(self):
        report = RcaReport(
            rca_id="rca-2",
            incident_summary="Test",
            root_cause="Unknown",
            confidence_score=0.5,
        )
        md = _render_markdown(report, _make_playbook())
        assert "# RCA Report: rca-2" in md
        assert "**Severity**: medium" in md
        assert "Impact Assessment" not in md
        assert "Action Items" not in md
        assert "Lessons Learned" not in md


class TestReportCarriesItsPlaybook:
    """리포트는 플레이북을 포함한 하나의 산출물이다.

    사람은 리포트 본문에서 절차를 읽고 승인하는데 실행은 구조를 따라간다. 서술과 구조가
    어긋난 리포트를 저장하면 승인 게이트가 형식만 남으므로, 일치가 저장 조건이다.
    """

    def _report(self) -> RcaReport:
        return RcaReport(
            rca_id="rca-1",
            incident_summary="CPU spike",
            root_cause="Memory leak",
            root_cause_confirmed=True,
            confidence_score=0.9,
        )

    def test_renders_the_steps_a_person_approves(self):
        md = _render_markdown(
            self._report(),
            _make_playbook(_make_step("step-1"), _make_step("step-2")),
        )

        assert "## 대응 플레이북" in md
        assert "step-1" in md
        assert "step-2" in md
        assert "대상 서비스를 롤링 재시작한다" in md
        assert "RSS가 임계치 미만으로 복귀" in md

    def test_marks_the_procedure_as_a_draft(self):
        md = _render_markdown(self._report(), _make_playbook(_make_step()))

        # 실행되지 않은 절차가 검증된 절차로 읽히면 사람이 승인 판단을 잘못한다.
        assert "초안" in md

    def test_says_what_to_investigate_when_no_cause_was_confirmed(self):
        report = self._report()
        report.root_cause_confirmed = False

        md = _render_markdown(report, _make_playbook())

        assert "## 대응 플레이북" in md
        assert "실행 절차를 만들지 않았다" in md

    def test_saves_when_narrative_and_structure_agree(self):
        s3 = MagicMock()
        store = S3ReportStore(s3_client=s3)

        with patch(
            "rca_agent.adapters.secondary.report.s3_report_store.S3_REPORT_BUCKET",
            "reports-bucket",
        ):
            key = store.save(self._report(), playbook=_make_playbook(_make_step()))

        assert key
        s3.put_object.assert_called_once()
        assert "step-1" in s3.put_object.call_args.kwargs["Body"]

    def test_refuses_to_save_when_a_step_is_missing_from_the_narrative(self):
        s3 = MagicMock()
        store = S3ReportStore(s3_client=s3)
        playbook = _make_playbook(_make_step())

        with (
            patch(
                "rca_agent.adapters.secondary.report.s3_report_store.S3_REPORT_BUCKET",
                "reports-bucket",
            ),
            patch(
                "rca_agent.adapters.secondary.report.s3_report_store._render_playbook_section",
                return_value=["## 대응 플레이북", "", "절차를 서술하지 않음", ""],
            ),
        ):
            key = store.save(self._report(), playbook=playbook)

        assert key == ""
        s3.put_object.assert_not_called()

    def test_refuses_to_save_when_the_narrative_reorders_the_steps(self):
        s3 = MagicMock()
        store = S3ReportStore(s3_client=s3)
        playbook = _make_playbook(_make_step("step-1"), _make_step("step-2"))

        with (
            patch(
                "rca_agent.adapters.secondary.report.s3_report_store.S3_REPORT_BUCKET",
                "reports-bucket",
            ),
            patch(
                "rca_agent.adapters.secondary.report.s3_report_store._render_playbook_section",
                return_value=["## 대응 플레이북", "", "step-2 먼저", "step-1 나중", ""],
            ),
        ):
            key = store.save(self._report(), playbook=playbook)

        assert key == ""
        s3.put_object.assert_not_called()

    def test_saves_the_report_when_playbook_generation_failed(self):
        """플레이북 생성 실패가 완성된 리포트를 버리게 해서는 안 된다.

        플레이북은 미래를 위한 자산이고 이번 RCA의 결과물은 리포트다. 절차가 없는
        리포트는 승인 화면이 실행할 절차를 찾지 못해 실행을 제공하지 않으므로, 절차
        없이 저장하는 것이 승인 게이트를 약화시키지 않는다.
        """
        s3 = MagicMock()
        store = S3ReportStore(s3_client=s3)

        with patch(
            "rca_agent.adapters.secondary.report.s3_report_store.S3_REPORT_BUCKET",
            "reports-bucket",
        ):
            key = store.save(self._report(), playbook=None)

        assert key
        body = s3.put_object.call_args.kwargs["Body"]
        assert "## 대응 플레이북" in body
        # 없는 절차를 있는 것처럼 읽히게 하지 않는다.
        assert "플레이북 생성이 실패해" in body
        assert "초안" not in body


@pytest.mark.parametrize("with_steps", [True, False])
def test_report_preserves_all_knowledge_separate_from_runtime_steps(with_steps):
    from tests.test_runbook_contract import command_step, wait_step

    playbook = _make_playbook(
        *([ExecutionStep(**step) for step in [command_step(), wait_step()]] if with_steps else [])
    )
    playbook.severity_criteria = "Critical after 10 minutes"
    playbook.related_metrics = ["FailedWrites", "WriteAttempts"]
    playbook.verification_steps = ["1. Inspect owner\n### 2. wait", "2. Check stop evidence"]
    playbook.temporary_mitigation = "Temporary knowledge"
    playbook.permanent_remediation = "Permanent knowledge"
    playbook.escalation_criteria = "Escalate to service owner"
    playbook.prevention_measures = ["Alert on blocked writes"]
    playbook.tags = ["database"]
    report = TestReportCarriesItsPlaybook()._report()

    md = _render_markdown(report, playbook)
    knowledge, runtime = md.split("### 이번 사고의 런북", 1)

    for field in (
        "failure_type",
        "symptom_pattern",
        "severity_criteria",
        "related_metrics",
        "verification_steps",
        "temporary_mitigation",
        "permanent_remediation",
        "escalation_criteria",
        "prevention_measures",
        "tags",
    ):
        value = getattr(playbook, field)
        for item in value if isinstance(value, list) else [value]:
            assert item.replace("\n", "\n  ") in knowledge
    assert _step_mismatch(md, playbook) == ""
    if with_steps:
        assert "### 1. stop-owner" in runtime
        assert "### 2. observe" in runtime
    else:
        assert "실행 대상이 아니다" in runtime


def test_report_status_is_a_snapshot_of_final_analysis_value():
    playbook = _make_playbook(_make_step())
    report = TestReportCarriesItsPlaybook()._report()
    draft_report = _render_markdown(report, playbook)
    playbook.verification_status = PlaybookVerificationStatus.VERIFIED
    verified_report = _render_markdown(report, playbook)

    assert "**DRAFT**" in draft_report
    assert "**VERIFIED**" not in draft_report
    assert "**VERIFIED**" in verified_report
    assert "초안(DRAFT)" not in verified_report


@pytest.mark.parametrize("corruption", ["missing", "reordered", "extra", "operation"])
def test_step_mismatch_uses_runtime_headings_and_operations_in_their_own_step(corruption):
    from tests.test_runbook_contract import command_step, wait_step

    playbook = _make_playbook(*[ExecutionStep(**step) for step in [command_step(), wait_step()]])
    playbook.verification_steps = ["wait before stop", "### 1. stop\n### 2. wait"]
    md = _render_markdown(TestReportCarriesItsPlaybook()._report(), playbook)
    assert _step_mismatch(md, playbook) == ""
    if corruption == "missing":
        md = md.replace("\n### 1. stop-owner\n", "\n")
    elif corruption == "reordered":
        md = md.replace("\n### 1. stop-owner\n", "\n### 1. observe\n").replace(
            "\n### 2. observe\n", "\n### 2. stop-owner\n"
        )
    elif corruption == "extra":
        md += "\n### 3. extra\n"
    else:
        md = md.replace("incident-owner", "wrong-owner")
    assert _step_mismatch(md, playbook)


def test_claimed_reports_use_isolated_attempt_keys():
    report = RcaReport(
        rca_id="rca-1",
        incident_summary="test",
        root_cause="cause",
        confidence_score=0.9,
    )
    s3 = MagicMock()
    store = S3ReportStore(s3_client=s3)

    with patch(
        "rca_agent.adapters.secondary.report.s3_report_store.S3_REPORT_BUCKET",
        "reports-bucket",
    ):
        first_key = store.save(report, playbook=_make_playbook(), claim_token="claim-1", attempt=1)
        second_key = store.save(report, playbook=_make_playbook(), claim_token="claim-2", attempt=2)

    assert first_key == "reports/strands/rca-1/attempt-1-claim-1/report.md"
    assert second_key == "reports/strands/rca-1/attempt-2-claim-2/report.md"
    assert first_key != second_key
    assert [call.kwargs["Key"] for call in s3.put_object.call_args_list] == [
        first_key,
        second_key,
    ]


def test_claimed_report_without_bucket_preserves_disabled_store_contract():
    report = RcaReport(
        rca_id="rca-1",
        incident_summary="test",
        root_cause="cause",
        confidence_score=0.9,
    )
    store = S3ReportStore(s3_client=MagicMock())

    with patch(
        "rca_agent.adapters.secondary.report.s3_report_store.S3_REPORT_BUCKET",
        "",
    ):
        key = store.save(report, playbook=_make_playbook(), claim_token="claim-1", attempt=1)

    assert key == ""
