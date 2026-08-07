import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from rca_agent.adapters.secondary.session.dynamodb_session_store import SessionCancelledError
from rca_agent.ports.dto.models import (
    BranchingResult,
    CompletionHandoff,
    FaultType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationResult,
    HypothesisStatus,
    NotificationMessage,
    Playbook,
    PrioritizationResult,
    RcaReport,
    RcaSession,
    RcaSessionState,
    ScopingResult,
    TerminationDecision,
    TerminationReason,
    ValidationJudgment,
    ValidationResult,
)
from rca_agent.ports.interfaces.session_store import (
    ClaimDisposition,
    SessionClaim,
    SideEffectLeaseUnavailableError,
)
from rca_agent.services.evidence import EvidenceCollectionSummary
from rca_agent.services.pipeline import (
    PipelineOrchestrator,
    RunContext,
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
    container.session_store.claim_session.return_value = SessionClaim(
        ClaimDisposition.CLAIMED,
        "claim-1",
        1,
    )
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


def _run_context(*, rca_id="rca-1", claim_token="claim-1", attempt=1, trace=None):
    return RunContext(
        rca_id=rca_id,
        claim_token=claim_token,
        attempt=attempt,
        trace=trace or MagicMock(),
        start_time=time.monotonic(),
    )


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

    def _run(
        self,
        *,
        hypo_result=None,
        validation_result=None,
        termination=None,
        notification_success=True,
        report_s3_key="reports/rca-1.md",
    ):
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
        container.report_store.save.return_value = report_s3_key
        container.session_store.mark_completed.return_value = True
        container.notification.send.return_value = notification_success
        container.session_store.mark_completion_notified.return_value = True

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
            result = orchestrator.process_alarm(_make_body())
            active["_container"] = container
            active["_result"] = result
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
        assert mocks["_result"] is True

    def test_completion_is_persisted_before_failed_notification_delivery(self):
        mocks = self._run(notification_success=False)
        container = mocks["_container"]

        assert mocks["_result"] is False
        completed = container.session_store.mark_completed.call_args
        assert completed.kwargs["selected_hypothesis_id"] == "h-1"
        assert completed.kwargs["fault_type"] == FaultType.UNSUPPORTED
        assert isinstance(completed.kwargs["completion_notification"], NotificationMessage)
        assert completed.kwargs["report_s3_key"] == "reports/rca-1.md"
        assert completed.kwargs["playbook_span_id"] == "s-1"
        assert completed.kwargs["playbook_id"] == "pb-1"
        assert completed.kwargs["claim_token"] == "claim-1"
        saved = container.report_store.save.call_args.kwargs
        assert saved["claim_token"] == "claim-1"
        assert saved["attempt"] == 1
        # 리포트는 플레이북을 포함한 하나의 산출물이므로 절차 없이 저장되지 않는다.
        assert saved["playbook"] is not None
        container.session_store.mark_completion_notified.assert_not_called()

    def test_claim_receives_complete_raw_alarm_context(self):
        mocks = self._run()

        claim = mocks["_container"].session_store.claim_session.call_args
        assert claim.kwargs["alarm_data"] == _make_body()

    def test_configured_report_failure_leaves_session_retryable(self):
        with patch(f"{_P}.settings.S3_REPORT_BUCKET", "reports-bucket"):
            mocks = self._run(report_s3_key="")

        container = mocks["_container"]
        assert mocks["_result"] is False
        container.session_store.mark_completed.assert_not_called()
        container.notification.send.assert_not_called()
        container.report_store.save_vectors.assert_not_called()

    def test_unconfigured_report_store_keeps_local_completion_behavior(self):
        with patch(f"{_P}.settings.S3_REPORT_BUCKET", ""):
            mocks = self._run(report_s3_key="")

        container = mocks["_container"]
        assert mocks["_result"] is True
        container.session_store.mark_completed.assert_called_once()
        container.notification.send.assert_called_once()

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

    def test_review_gate_grace_exit_is_confirmed_end_to_end(self):
        accepted = _make_hypothesis("accepted", 0.4)
        accepted.description = "Validated CPU saturation"
        investigate = _make_hypothesis("investigate", 0.4)
        child = _make_hypothesis("child", 0.4)
        child.parent_id = investigate.hypothesis_id
        child.depth = 1
        hr = _hypo_result([accepted, investigate])
        first_validation = ValidationResult(
            tree_id="tree-1",
            judgments=[
                ValidationJudgment(
                    hypothesis_id=accepted.hypothesis_id,
                    status=HypothesisStatus.CONFIRMED,
                    confidence_score=0.85,
                    reasoning="CPU evidence confirms the accepted cause",
                ),
                ValidationJudgment(
                    hypothesis_id=investigate.hypothesis_id,
                    status=HypothesisStatus.NEEDS_INVESTIGATION,
                    confidence_score=0.6,
                ),
            ],
        )
        blocked_validation = ValidationResult(
            tree_id="tree-1",
            judgments=[
                ValidationJudgment(
                    hypothesis_id=investigate.hypothesis_id,
                    status=HypothesisStatus.REJECTED,
                    confidence_score=0.1,
                ),
                ValidationJudgment(
                    hypothesis_id=child.hypothesis_id,
                    status=HypothesisStatus.REJECTED,
                    confidence_score=0.1,
                ),
            ],
            all_rejected=True,
        )
        empty_blocked_validation = ValidationResult(
            tree_id="tree-1",
            judgments=[],
            all_rejected=True,
        )
        branching = BranchingResult(
            tree_id="tree-1",
            parent_id=investigate.hypothesis_id,
            children=[child],
        )
        prioritization = PrioritizationResult(tree_id="tree-1", prioritized=[])
        playbook = Playbook(
            playbook_id="pb-review-gate",
            failure_type="cpu-spike",
            symptom_pattern="CPU > 90%",
        )
        container = _make_container()
        container.report_store.save.return_value = "reports/review-gate.md"
        container.session_store.mark_completed.return_value = True
        container.notification.send.return_value = True
        container.session_store.mark_completion_notified.return_value = True

        def build_report(_scoping, best, confirmed, *_args):
            return RcaReport(
                rca_id="rca-review-gate",
                incident_summary="CPU spike",
                root_cause=best.description,
                root_cause_confirmed=confirmed,
                confidence_score=best.confidence_score,
            )

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
            patch(f"{_P}.run_prioritization", return_value=prioritization),
            patch(f"{_P}.run_evidence_collection", return_value=EvidenceCollectionSummary()),
            patch(
                f"{_P}.run_validation",
                side_effect=[
                    first_validation,
                    blocked_validation,
                    empty_blocked_validation,
                ],
            ) as validation,
            patch(f"{_P}.run_branching", return_value=branching) as branch,
            patch(f"{_P}.run_report_generation", side_effect=build_report) as report,
            patch(f"{_P}.run_playbook_generation", return_value=playbook),
            patch(f"{_P}.TraceStore", return_value=mock_trace),
        ):
            result = PipelineOrchestrator(container).process_alarm(_make_body())

        assert result is True
        assert validation.call_count == 3
        branch.assert_called_once()
        assert report.call_args.args[1] is accepted
        assert report.call_args.args[2] is True
        completed = container.session_store.mark_completed.call_args.kwargs
        assert completed["confirmed"] is True
        assert completed["selected_hypothesis_id"] == accepted.hypothesis_id
        notification = completed["completion_notification"]
        assert notification.confirmed is True
        assert notification.selected_hypothesis_id == accepted.hypothesis_id

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

    def test_contended_claim_does_not_run_or_ack(self):
        container = _make_container()
        container.session_store.claim_session.return_value = SessionClaim(ClaimDisposition.CONTENDED)

        with patch(f"{_P}.run_scoping") as mock_scoping:
            orchestrator = PipelineOrchestrator(container)
            result = orchestrator.process_alarm(_make_body(), receive_count=2)

        assert result is False
        mock_scoping.assert_not_called()

    def test_initial_stale_alarm_is_marked_outdated(self):
        container = _make_container()
        body = {
            **_make_body(),
            "StateChangeTime": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        }
        orchestrator = PipelineOrchestrator(container)
        orchestrator._run_pipeline = MagicMock(return_value=True)

        result = orchestrator.process_alarm(body, receive_count=1)

        assert result is True
        container.session_store.mark_outdated.assert_called_once()
        assert container.session_store.mark_outdated.call_args.kwargs["claim_token"] == "claim-1"
        orchestrator._run_pipeline.assert_not_called()

    def test_redelivery_bypasses_initial_staleness_check(self):
        container = _make_container()
        container.session_store.claim_session.return_value = SessionClaim(
            ClaimDisposition.CLAIMED,
            "claim-2",
            2,
        )
        body = {
            **_make_body(),
            "StateChangeTime": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        }
        orchestrator = PipelineOrchestrator(container)
        orchestrator._run_pipeline = MagicMock(return_value=True)

        result = orchestrator.process_alarm(body, receive_count=2)

        assert result is True
        container.session_store.mark_outdated.assert_not_called()
        orchestrator._run_pipeline.assert_called_once()

    def test_terminal_duplicate_flushes_pending_completion_handoff(self):
        container = _make_container()
        container.session_store.claim_session.return_value = SessionClaim(
            ClaimDisposition.TERMINAL_DUPLICATE,
            "claim-complete",
            1,
        )
        notification = NotificationMessage(
            rca_id="rca-1",
            root_cause_summary="complete",
            severity="high",
        )
        container.session_store.get_completion_handoff.return_value = CompletionHandoff(
            rca_id="rca-1",
            state=RcaSessionState.COMPLETED,
            notification_status="PENDING",
            notification=notification,
        )
        container.notification.send.return_value = True
        container.session_store.mark_completion_notified.return_value = True

        result = PipelineOrchestrator(container).process_alarm(_make_body())

        assert result is True
        container.notification.send.assert_called_once_with(notification)
        container.session_store.mark_completion_notified.assert_called_once_with(
            container.session_store.get_completion_handoff.call_args.args[0],
            claim_token="claim-complete",
        )

    def test_failed_first_attempt_is_reprocessed_and_second_attempt_completes(self):
        container = _make_container()
        container.session_store.claim_session.side_effect = [
            SessionClaim(ClaimDisposition.CLAIMED, "claim-1", 1),
            SessionClaim(ClaimDisposition.CLAIMED, "claim-2", 2),
        ]
        orchestrator = PipelineOrchestrator(container)
        orchestrator._run_pipeline = MagicMock(side_effect=[RuntimeError("transient failure"), True])

        first = orchestrator.process_alarm(
            _make_body(),
            receive_count=1,
            message_id="message-a",
        )
        second = orchestrator.process_alarm(
            _make_body(),
            receive_count=2,
            message_id="message-a",
        )

        assert first is False
        assert second is True
        assert orchestrator._run_pipeline.call_count == 2
        # 각 시도는 자기 claim 으로만 기록해야 한다 — 재시도가 이전 시도의 토큰을
        # 물려받으면 fencing 이 무력화된다.
        assert [call.args[1].claim_token for call in orchestrator._run_pipeline.call_args_list] == [
            "claim-1",
            "claim-2",
        ]
        assert [call.args[1].attempt for call in orchestrator._run_pipeline.call_args_list] == [1, 2]
        assert [call.kwargs["receive_count"] for call in container.session_store.claim_session.call_args_list] == [1, 2]
        assert [call.kwargs["message_id"] for call in container.session_store.claim_session.call_args_list] == [
            "message-a",
            "message-a",
        ]

    def test_playbook_store_is_not_called_when_claim_is_lost_before_save(self):
        container = _make_container()
        container.session_store.acquire_side_effect_lease.side_effect = SideEffectLeaseUnavailableError("claim lost")
        orchestrator = PipelineOrchestrator(container)
        report = RcaReport(
            rca_id="rca-1",
            incident_summary="CPU spike",
            root_cause="Bad deploy",
            confidence_score=0.95,
        )
        playbook = Playbook(
            playbook_id="pb-1",
            failure_type="cpu-spike",
            symptom_pattern="CPU > 90%",
        )

        with (
            patch(f"{_P}.run_playbook_generation", return_value=playbook),
            pytest.raises(SideEffectLeaseUnavailableError),
        ):
            orchestrator._run_playbook(
                report,
                _scoping(),
                _run_context(claim_token="claim-1"),
            )

        container.playbook_store.save.assert_not_called()

    def test_lease_release_failure_is_not_treated_as_success(self):
        container = _make_container()
        container.session_store.acquire_side_effect_lease.return_value = "lease-1"
        container.session_store.release_side_effect_lease.return_value = False
        orchestrator = PipelineOrchestrator(container)

        with (
            pytest.raises(SideEffectLeaseUnavailableError),
            orchestrator._side_effect_lease("rca-1", "claim-1", "playbook"),
        ):
            pass

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
        assert container.session_store.mark_failed.call_args.kwargs["claim_token"] == "claim-1"

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
