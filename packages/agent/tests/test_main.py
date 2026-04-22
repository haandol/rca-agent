import json
from unittest.mock import MagicMock, patch

from rca_agent.main import _Agents, _parse_sns_envelope, _process_alarm
from rca_agent.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationResult,
    HypothesisStatus,
    Playbook,
    RcaReport,
    ScopingResult,
    TerminationDecision,
    TerminationReason,
    ValidationJudgment,
    ValidationResult,
)


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


def _make_body(alarm_name="HighCPU"):
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


def _make_agents():
    agents = MagicMock(spec=_Agents)
    agents.scoping = MagicMock()
    agents.hypothesis = MagicMock()
    agents.prioritization = MagicMock()
    agents.validation = MagicMock()
    agents.branching = MagicMock()
    agents.report = MagicMock()
    agents.playbook = MagicMock()
    return agents


def _make_hypothesis(hid="h-1", confidence=0.5):
    return Hypothesis(
        hypothesis_id=hid,
        description=f"Hypothesis {hid}",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=confidence,
        tree_id="tree-1",
    )


def _scoping():
    return ScopingResult(alarm_summary="CPU spike on web-service")


def _hypo_result(hypotheses=None):
    sr = _scoping()
    hyps = hypotheses or [_make_hypothesis("h-1"), _make_hypothesis("h-2")]
    return HypothesisGenerationResult(
        tree_id="tree-1",
        hypotheses=hyps,
        scoping_result=sr,
    )


class TestProcessAlarmFullPipeline:
    """Test the full F1-F9 pipeline orchestration."""

    def _run(self, *, hypo_result=None, validation_result=None, termination=None):
        """Helper that patches all pipeline functions and runs _process_alarm."""
        sr = _scoping()
        hr = hypo_result or _hypo_result()
        vr = validation_result or ValidationResult(
            tree_id="tree-1",
            judgments=[
                ValidationJudgment(
                    hypothesis_id="h-1",
                    status=HypothesisStatus.CONFIRMED,
                    confidence_score=0.95,
                ),
            ],
        )
        td = termination or TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.CONFIRMED,
            best_hypothesis=_make_hypothesis("h-1", 0.95),
        )
        rca = RcaReport(
            rca_id="rca-1",
            incident_summary="CPU spike",
            root_cause="Bad deploy",
            confidence_score=0.95,
        )
        pb = Playbook(
            playbook_id="pb-1",
            failure_type="cpu-spike",
            symptom_pattern="CPU > 90%",
        )

        names = [
            "run_scoping",
            "run_hypothesis_generation",
            "run_prioritization",
            "run_validation",
            "check_termination",
            "run_report_generation",
            "save_report_to_s3",
            "run_playbook_generation",
            "save_playbook_to_s3_vectors",
            "build_notification",
            "send_notification",
        ]
        returns = [
            sr,
            hr,
            MagicMock(),
            vr,
            td,
            rca,
            "reports/rca-1.md",
            pb,
            True,
            MagicMock(),
            True,
        ]

        active = {}
        stack = []
        for name, rv in zip(names, returns, strict=True):
            p = patch(f"rca_agent.main.{name}", return_value=rv)
            active[name] = p.start()
            stack.append(p)

        try:
            _process_alarm(_make_body(), _make_agents())
            return active
        finally:
            for p in stack:
                p.stop()

    def test_full_pipeline_calls_all_stages(self):
        mocks = self._run()
        assert mocks["run_scoping"].called
        assert mocks["run_hypothesis_generation"].called
        assert mocks["run_prioritization"].called
        assert mocks["run_validation"].called
        assert mocks["check_termination"].called
        assert mocks["run_report_generation"].called
        assert mocks["save_report_to_s3"].called
        assert mocks["run_playbook_generation"].called
        assert mocks["send_notification"].called

    def test_early_exit_on_no_hypotheses(self):
        empty_hr = HypothesisGenerationResult(
            tree_id="tree-1",
            hypotheses=[],
            scoping_result=_scoping(),
        )
        with (
            patch("rca_agent.main.run_scoping", return_value=_scoping()),
            patch("rca_agent.main.run_hypothesis_generation", return_value=empty_hr),
            patch("rca_agent.main.run_prioritization") as mock_prio,
        ):
            _process_alarm(_make_body(), _make_agents())

        mock_prio.assert_not_called()

    def test_all_rejected_triggers_regeneration(self):
        hr1 = _hypo_result([_make_hypothesis("h-1")])
        hr2 = _hypo_result([_make_hypothesis("h-2")])
        vr_rejected = ValidationResult(
            tree_id="tree-1",
            judgments=[
                ValidationJudgment(
                    hypothesis_id="h-1",
                    status=HypothesisStatus.REJECTED,
                    confidence_score=0.1,
                ),
            ],
            all_rejected=True,
        )
        vr_confirmed = ValidationResult(
            tree_id="tree-1",
            judgments=[
                ValidationJudgment(
                    hypothesis_id="h-2",
                    status=HypothesisStatus.CONFIRMED,
                    confidence_score=0.95,
                ),
            ],
        )
        td_continue = TerminationDecision(should_terminate=False)
        td_stop = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.CONFIRMED,
            best_hypothesis=_make_hypothesis("h-2", 0.95),
        )
        rca = RcaReport(
            rca_id="rca-1",
            incident_summary="test",
            root_cause="test",
            confidence_score=0.95,
        )

        with (
            patch("rca_agent.main.run_scoping", return_value=_scoping()),
            patch(
                "rca_agent.main.run_hypothesis_generation",
                side_effect=[hr1, hr2],
            ) as mock_hypo,
            patch("rca_agent.main.run_prioritization"),
            patch(
                "rca_agent.main.run_validation",
                side_effect=[vr_rejected, vr_confirmed],
            ),
            patch(
                "rca_agent.main.check_termination",
                side_effect=[td_continue, td_stop],
            ),
            patch("rca_agent.main.run_report_generation", return_value=rca),
            patch("rca_agent.main.save_report_to_s3", return_value=""),
            patch("rca_agent.main.run_playbook_generation", return_value=MagicMock()),
            patch("rca_agent.main.save_playbook_to_s3_vectors"),
            patch("rca_agent.main.build_notification", return_value=MagicMock()),
            patch("rca_agent.main.send_notification"),
        ):
            _process_alarm(_make_body(), _make_agents())

        assert mock_hypo.call_count == 2

    def test_branching_on_needs_investigation(self):
        from rca_agent.models import BranchingResult

        h1 = _make_hypothesis("h-1", 0.5)
        hr = _hypo_result([h1])
        vr_needs = ValidationResult(
            tree_id="tree-1",
            judgments=[
                ValidationJudgment(
                    hypothesis_id="h-1",
                    status=HypothesisStatus.NEEDS_INVESTIGATION,
                    confidence_score=0.5,
                ),
            ],
        )
        child = _make_hypothesis("h-child", 0.9)
        child.parent_id = "h-1"
        child.depth = 1
        br = BranchingResult(tree_id="tree-1", parent_id="h-1", children=[child])

        vr_confirmed = ValidationResult(
            tree_id="tree-1",
            judgments=[
                ValidationJudgment(
                    hypothesis_id="h-child",
                    status=HypothesisStatus.CONFIRMED,
                    confidence_score=0.95,
                ),
            ],
        )
        td_continue = TerminationDecision(should_terminate=False)
        td_stop = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.CONFIRMED,
            best_hypothesis=child,
        )
        rca = RcaReport(
            rca_id="rca-1",
            incident_summary="test",
            root_cause="test",
            confidence_score=0.95,
        )

        with (
            patch("rca_agent.main.run_scoping", return_value=_scoping()),
            patch("rca_agent.main.run_hypothesis_generation", return_value=hr),
            patch("rca_agent.main.run_prioritization"),
            patch(
                "rca_agent.main.run_validation",
                side_effect=[vr_needs, vr_confirmed],
            ),
            patch(
                "rca_agent.main.check_termination",
                side_effect=[td_continue, td_stop],
            ),
            patch("rca_agent.main.run_branching", return_value=br) as mock_branch,
            patch("rca_agent.main.run_report_generation", return_value=rca),
            patch("rca_agent.main.save_report_to_s3", return_value=""),
            patch("rca_agent.main.run_playbook_generation", return_value=MagicMock()),
            patch("rca_agent.main.save_playbook_to_s3_vectors"),
            patch("rca_agent.main.build_notification", return_value=MagicMock()),
            patch("rca_agent.main.send_notification"),
        ):
            _process_alarm(_make_body(), _make_agents())

        mock_branch.assert_called_once()

    def test_handles_sns_wrapped_body(self):
        alarm_data = {
            "AlarmName": "HighLatency",
            "NewStateValue": "ALARM",
            "NewStateReason": "p99 > 500ms",
        }
        body = {"Message": json.dumps(alarm_data), "Type": "Notification"}
        hr = _hypo_result([_make_hypothesis("h-1")])
        vr = ValidationResult(
            tree_id="tree-1",
            judgments=[
                ValidationJudgment(
                    hypothesis_id="h-1",
                    status=HypothesisStatus.CONFIRMED,
                    confidence_score=0.95,
                ),
            ],
        )
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.CONFIRMED,
            best_hypothesis=_make_hypothesis("h-1", 0.95),
        )
        rca = RcaReport(
            rca_id="rca-1",
            incident_summary="test",
            root_cause="test",
            confidence_score=0.95,
        )

        with (
            patch("rca_agent.main.run_scoping", return_value=_scoping()) as mock_scoping,
            patch("rca_agent.main.run_hypothesis_generation", return_value=hr),
            patch("rca_agent.main.run_prioritization"),
            patch("rca_agent.main.run_validation", return_value=vr),
            patch("rca_agent.main.check_termination", return_value=td),
            patch("rca_agent.main.run_report_generation", return_value=rca),
            patch("rca_agent.main.save_report_to_s3", return_value=""),
            patch("rca_agent.main.run_playbook_generation", return_value=MagicMock()),
            patch("rca_agent.main.save_playbook_to_s3_vectors"),
            patch("rca_agent.main.build_notification", return_value=MagicMock()),
            patch("rca_agent.main.send_notification"),
        ):
            _process_alarm(body, _make_agents())

        assert mock_scoping.call_args[0][0].alarm_name == "HighLatency"
