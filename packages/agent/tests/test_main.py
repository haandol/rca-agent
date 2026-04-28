import json
from unittest.mock import MagicMock, patch

from rca_agent.adapters.secondary.session.dynamodb_session_store import SessionCancelledError
from rca_agent.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationResult,
    HypothesisStatus,
    Playbook,
    RcaReport,
    RcaSession,
    RcaSessionState,
    ScopingResult,
    TerminationDecision,
    TerminationReason,
    ValidationJudgment,
    ValidationResult,
)
from rca_agent.services.evidence import EvidenceCollectionSummary
from rca_agent.services.pipeline import (
    PipelineOrchestrator,
    parse_sns_envelope,
    prune_subtree,
)


class TestParseSnsEnvelope:
    def test_extracts_message_from_sns_wrapper(self):
        alarm_data = {"AlarmName": "HighCPU", "NewStateValue": "ALARM"}
        body = {"Message": json.dumps(alarm_data), "Type": "Notification"}
        result = parse_sns_envelope(body)
        assert result == alarm_data

    def test_returns_raw_body_when_no_envelope(self):
        body = {"AlarmName": "HighCPU", "NewStateValue": "ALARM"}
        result = parse_sns_envelope(body)
        assert result == body

    def test_returns_raw_body_when_message_is_not_string(self):
        body = {"Message": {"nested": True}}
        result = parse_sns_envelope(body)
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


def _make_container():
    container = MagicMock()
    container.session_store = MagicMock()
    container.session_store.check_duplicate.return_value = False
    container.report_store = MagicMock()
    container.notification = MagicMock()
    container.playbook_store = MagicMock()
    container.s3_vectors_client = MagicMock()
    container.s3_client = MagicMock()
    container.dynamodb_client = MagicMock()
    container.scoping_agent = MagicMock()
    container.hypothesis_agent = MagicMock()
    container.prioritization_agent = MagicMock()
    container.evidence_mcp_clients = [MagicMock()]
    container.validation_agent = MagicMock()
    container.branching_agent = MagicMock()
    container.report_agent = MagicMock()
    container.playbook_agent = MagicMock()
    return container


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


_P = "rca_agent.services.pipeline"


class TestProcessAlarmFullPipeline:
    """Test the full F1-F9 pipeline orchestration."""

    def _run(self, *, hypo_result=None, validation_result=None, termination=None):
        """Helper that patches all pipeline functions and runs process_alarm."""
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
        session = RcaSession(
            rca_id="rca-1",
            idempotency_key="HighCPU#unknown",
            state=RcaSessionState.ALARM_RECEIVED,
        )

        container = _make_container()
        container.session_store.create_session.return_value = session

        names = [
            "run_scoping",
            "run_hypothesis_generation",
            "run_prioritization",
            "run_evidence_collection",
            "run_validation",
            "check_termination",
            "run_report_generation",
            "run_playbook_generation",
        ]
        returns = [
            sr,
            hr,
            MagicMock(),
            EvidenceCollectionSummary(
                evidence_map={"h-1": "metrics evidence", "h-2": "logs evidence"},
                failed_ids=set(),
            ),
            vr,
            td,
            rca,
            pb,
        ]

        active = {}
        stack = []
        for name, rv in zip(names, returns, strict=True):
            p = patch(f"{_P}.{name}", return_value=rv)
            active[name] = p.start()
            stack.append(p)

        stack.append(
            patch(
                f"{_P}.TraceStore",
                return_value=MagicMock(
                    span=MagicMock(
                        return_value=MagicMock(
                            __enter__=MagicMock(return_value=MagicMock()),
                            __exit__=MagicMock(return_value=False),
                        )
                    ),
                    start_span=MagicMock(return_value=MagicMock(span_id="s-1")),
                    end_span=MagicMock(),
                    put_hypotheses=MagicMock(),
                    update_hypothesis_status=MagicMock(),
                    update_hypothesis_evidence=MagicMock(),
                    check_cancelled=MagicMock(),
                ),
            )
        )
        stack[-1].start()

        notification_mock = patch(
            "rca_agent.services.notification.build_notification",
            return_value=MagicMock(),
        )
        active["build_notification"] = notification_mock.start()
        stack.append(notification_mock)

        try:
            orchestrator = PipelineOrchestrator(container)
            orchestrator.process_alarm(_make_body())
            active["_container"] = container
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
        assert mocks["run_playbook_generation"].called

    def test_early_exit_on_no_hypotheses(self):
        empty_hr = HypothesisGenerationResult(
            tree_id="tree-1",
            hypotheses=[],
            scoping_result=_scoping(),
        )
        container = _make_container()
        session = RcaSession(rca_id="rca-1", idempotency_key="k", state=RcaSessionState.ALARM_RECEIVED)
        container.session_store.create_session.return_value = session

        with (
            patch(f"{_P}.run_scoping", return_value=_scoping()),
            patch(f"{_P}.run_hypothesis_generation", return_value=empty_hr),
            patch(f"{_P}.run_evidence_collection", return_value=EvidenceCollectionSummary()),
            patch(f"{_P}.run_prioritization") as mock_prio,
            patch(
                f"{_P}.TraceStore",
                return_value=MagicMock(
                    span=MagicMock(
                        return_value=MagicMock(
                            __enter__=MagicMock(return_value=MagicMock()),
                            __exit__=MagicMock(return_value=False),
                        )
                    ),
                    start_span=MagicMock(return_value=MagicMock(span_id="s-1")),
                    end_span=MagicMock(),
                    put_hypotheses=MagicMock(),
                    update_hypothesis_status=MagicMock(),
                    check_cancelled=MagicMock(),
                ),
            ),
        ):
            orchestrator = PipelineOrchestrator(container)
            orchestrator.process_alarm(_make_body())

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
        session = RcaSession(rca_id="rca-1", idempotency_key="k", state=RcaSessionState.ALARM_RECEIVED)

        container = _make_container()
        container.session_store.create_session.return_value = session

        mock_trace = MagicMock(
            span=MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=MagicMock()),
                    __exit__=MagicMock(return_value=False),
                )
            ),
            start_span=MagicMock(return_value=MagicMock(span_id="s-1")),
            end_span=MagicMock(),
            put_hypotheses=MagicMock(),
            update_hypothesis_status=MagicMock(),
            update_hypothesis_evidence=MagicMock(),
            check_cancelled=MagicMock(),
        )

        with (
            patch(f"{_P}.run_scoping", return_value=_scoping()),
            patch(f"{_P}.run_hypothesis_generation", side_effect=[hr1, hr2]) as mock_hypo,
            patch(f"{_P}.run_prioritization"),
            patch(f"{_P}.run_evidence_collection", return_value=EvidenceCollectionSummary()),
            patch(f"{_P}.run_validation", side_effect=[vr_rejected, vr_confirmed]),
            patch(f"{_P}.check_termination", side_effect=[td_continue, td_stop]),
            patch(f"{_P}.run_report_generation", return_value=rca),
            patch(f"{_P}.run_playbook_generation", return_value=MagicMock()),
            patch(f"{_P}.TraceStore", return_value=mock_trace),
            patch("rca_agent.services.notification.build_notification", return_value=MagicMock()),
        ):
            orchestrator = PipelineOrchestrator(container)
            orchestrator.process_alarm(_make_body())

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
        session = RcaSession(rca_id="rca-1", idempotency_key="k", state=RcaSessionState.ALARM_RECEIVED)

        container = _make_container()
        container.session_store.create_session.return_value = session

        mock_trace = MagicMock(
            span=MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=MagicMock()),
                    __exit__=MagicMock(return_value=False),
                )
            ),
            start_span=MagicMock(return_value=MagicMock(span_id="s-1")),
            end_span=MagicMock(),
            put_hypotheses=MagicMock(),
            update_hypothesis_status=MagicMock(),
            update_hypothesis_evidence=MagicMock(),
            check_cancelled=MagicMock(),
        )

        with (
            patch(f"{_P}.run_scoping", return_value=_scoping()),
            patch(f"{_P}.run_hypothesis_generation", return_value=hr),
            patch(f"{_P}.run_prioritization"),
            patch(f"{_P}.run_evidence_collection", return_value=EvidenceCollectionSummary()),
            patch(f"{_P}.run_validation", side_effect=[vr_needs, vr_confirmed]),
            patch(f"{_P}.check_termination", side_effect=[td_continue, td_stop]),
            patch(f"{_P}.run_branching", return_value=br) as mock_branch,
            patch(f"{_P}.run_report_generation", return_value=rca),
            patch(f"{_P}.run_playbook_generation", return_value=MagicMock()),
            patch(f"{_P}.TraceStore", return_value=mock_trace),
            patch("rca_agent.services.notification.build_notification", return_value=MagicMock()),
        ):
            orchestrator = PipelineOrchestrator(container)
            orchestrator.process_alarm(_make_body())

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
        session = RcaSession(rca_id="rca-1", idempotency_key="k", state=RcaSessionState.ALARM_RECEIVED)

        container = _make_container()
        container.session_store.create_session.return_value = session

        mock_trace = MagicMock(
            span=MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=MagicMock()),
                    __exit__=MagicMock(return_value=False),
                )
            ),
            start_span=MagicMock(return_value=MagicMock(span_id="s-1")),
            end_span=MagicMock(),
            put_hypotheses=MagicMock(),
            update_hypothesis_status=MagicMock(),
            update_hypothesis_evidence=MagicMock(),
            check_cancelled=MagicMock(),
        )

        with (
            patch(f"{_P}.run_scoping", return_value=_scoping()) as mock_scoping,
            patch(f"{_P}.run_hypothesis_generation", return_value=hr),
            patch(f"{_P}.run_prioritization"),
            patch(f"{_P}.run_evidence_collection", return_value=EvidenceCollectionSummary()),
            patch(f"{_P}.run_validation", return_value=vr),
            patch(f"{_P}.check_termination", return_value=td),
            patch(f"{_P}.run_report_generation", return_value=rca),
            patch(f"{_P}.run_playbook_generation", return_value=MagicMock()),
            patch(f"{_P}.TraceStore", return_value=mock_trace),
            patch("rca_agent.services.notification.build_notification", return_value=MagicMock()),
        ):
            orchestrator = PipelineOrchestrator(container)
            orchestrator.process_alarm(body)

        assert mock_scoping.call_args[0][0].alarm_name == "HighLatency"

    def test_skips_duplicate_alarm(self):
        container = _make_container()
        container.session_store.check_duplicate.return_value = True

        with (
            patch(f"{_P}.run_scoping") as mock_scoping,
        ):
            orchestrator = PipelineOrchestrator(container)
            orchestrator.process_alarm(_make_body())

        container.session_store.create_session.assert_not_called()
        mock_scoping.assert_not_called()

    def test_marks_failed_on_pipeline_exception(self):
        session = RcaSession(rca_id="rca-1", idempotency_key="k", state=RcaSessionState.ALARM_RECEIVED)

        container = _make_container()
        container.session_store.create_session.return_value = session

        mock_trace = MagicMock(
            span=MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=MagicMock()),
                    __exit__=MagicMock(return_value=False),
                )
            ),
            start_span=MagicMock(return_value=MagicMock(span_id="s-1")),
            end_span=MagicMock(),
            put_hypotheses=MagicMock(),
            update_hypothesis_status=MagicMock(),
            check_cancelled=MagicMock(),
        )

        with (
            patch(f"{_P}.run_scoping", side_effect=RuntimeError("boom")),
            patch(f"{_P}.TraceStore", return_value=mock_trace),
        ):
            orchestrator = PipelineOrchestrator(container)
            orchestrator.process_alarm(_make_body())

        container.session_store.mark_failed.assert_called_once()
        assert container.session_store.mark_failed.call_args[0][0] == "rca-1"

    def test_state_transitions_in_full_pipeline(self):
        mocks = self._run()
        container = mocks["_container"]
        calls = [c[0][1] for c in container.session_store.update_state.call_args_list]
        assert RcaSessionState.SCOPING in calls
        assert RcaSessionState.HYPOTHESIS_GENERATION in calls
        assert RcaSessionState.HYPOTHESIS_PRIORITIZATION in calls
        assert RcaSessionState.EVIDENCE_COLLECTION in calls
        assert RcaSessionState.HYPOTHESIS_VALIDATION in calls
        assert RcaSessionState.REPORT_GENERATION in calls

    def test_evidence_collection_called_in_pipeline(self):
        mocks = self._run()
        mocks["run_evidence_collection"].assert_called_once()

    def test_cancelled_session_stops_pipeline(self):
        session = RcaSession(rca_id="rca-1", idempotency_key="k", state=RcaSessionState.ALARM_RECEIVED)

        container = _make_container()
        container.session_store.create_session.return_value = session
        container.session_store.update_state.side_effect = SessionCancelledError("rca-1")

        mock_trace = MagicMock(
            span=MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=MagicMock()),
                    __exit__=MagicMock(return_value=False),
                )
            ),
            start_span=MagicMock(return_value=MagicMock(span_id="s-1")),
            end_span=MagicMock(),
            put_hypotheses=MagicMock(),
            update_hypothesis_status=MagicMock(),
            check_cancelled=MagicMock(),
        )

        with (
            patch(f"{_P}.run_scoping") as mock_scoping,
            patch(f"{_P}.TraceStore", return_value=mock_trace),
        ):
            orchestrator = PipelineOrchestrator(container)
            orchestrator.process_alarm(_make_body())

        mock_scoping.assert_not_called()
        container.session_store.mark_failed.assert_not_called()


class TestPruneSubtree:
    def test_prunes_direct_children(self):
        parent = _make_hypothesis("h-1")
        parent.status = HypothesisStatus.REJECTED
        child1 = _make_hypothesis("h-1a")
        child1.parent_id = "h-1"
        child1.depth = 1
        child2 = _make_hypothesis("h-1b")
        child2.parent_id = "h-1"
        child2.depth = 1
        unrelated = _make_hypothesis("h-2")

        hypotheses = [parent, child1, child2, unrelated]
        pruned = prune_subtree("h-1", hypotheses)

        assert set(pruned) == {"h-1a", "h-1b"}
        assert child1.status == HypothesisStatus.REJECTED
        assert child2.status == HypothesisStatus.REJECTED
        assert unrelated.status == HypothesisStatus.PENDING

    def test_prunes_deep_descendants(self):
        parent = _make_hypothesis("h-1")
        parent.status = HypothesisStatus.REJECTED
        child = _make_hypothesis("h-1a")
        child.parent_id = "h-1"
        child.depth = 1
        grandchild = _make_hypothesis("h-1a1")
        grandchild.parent_id = "h-1a"
        grandchild.depth = 2

        hypotheses = [parent, child, grandchild]
        pruned = prune_subtree("h-1", hypotheses)

        assert set(pruned) == {"h-1a", "h-1a1"}
        assert grandchild.status == HypothesisStatus.REJECTED

    def test_skips_already_rejected_descendants(self):
        parent = _make_hypothesis("h-1")
        parent.status = HypothesisStatus.REJECTED
        child = _make_hypothesis("h-1a")
        child.parent_id = "h-1"
        child.status = HypothesisStatus.REJECTED

        hypotheses = [parent, child]
        pruned = prune_subtree("h-1", hypotheses)

        assert pruned == []

    def test_no_children_returns_empty(self):
        parent = _make_hypothesis("h-1")
        parent.status = HypothesisStatus.REJECTED

        pruned = prune_subtree("h-1", [parent])
        assert pruned == []
