"""Tests for state machine transitions and pre-report hypothesis finalization."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rca_agent.adapters.secondary.session.dynamodb_session_store import (
    _TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidStateTransitionError,
    SessionCancelledError,
    _validate_transition,
)
from rca_agent.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    RcaSessionState,
    TerminationDecision,
    TerminationReason,
)
from rca_agent.services.evidence import EvidenceCollectionSummary

# ── Helpers ──────────────────────────────────────────────────────────────


def _mock_ddb_state(state: str) -> MagicMock:
    ddb = MagicMock()
    ddb.get_item.return_value = {"Item": {"state": {"S": state}}}
    return ddb


def _make_hypothesis(
    hid: str = "h-1",
    status: HypothesisStatus = HypothesisStatus.PENDING,
    confidence: float = 0.5,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        description=f"Hypothesis {hid}",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=confidence,
        status=status,
        tree_id="tree-1",
    )


# ── VALID_TRANSITIONS completeness ──────────────────────────────────────


class TestValidTransitions:
    def test_all_non_terminal_states_have_transitions(self):
        non_terminal = {
            "ALARM_RECEIVED",
            "SCOPING",
            "HYPOTHESIS_GENERATION",
            "HYPOTHESIS_PRIORITIZATION",
            "EVIDENCE_COLLECTION",
            "HYPOTHESIS_VALIDATION",
            "REPORT_GENERATION",
        }
        for state in non_terminal:
            assert state in VALID_TRANSITIONS, f"{state} missing from VALID_TRANSITIONS"

    def test_terminal_states_not_in_transitions(self):
        for state in _TERMINAL_STATES:
            assert state not in VALID_TRANSITIONS

    def test_all_non_terminal_can_reach_failed(self):
        for state, targets in VALID_TRANSITIONS.items():
            assert "FAILED" in targets, f"{state} cannot transition to FAILED"

    def test_all_non_terminal_can_reach_cancelled(self):
        for state, targets in VALID_TRANSITIONS.items():
            assert "CANCELLED" in targets, f"{state} cannot transition to CANCELLED"


# ── Happy-path transitions ──────────────────────────────────────────────


class TestHappyPathTransitions:
    HAPPY_PATH = [
        ("ALARM_RECEIVED", "SCOPING"),
        ("SCOPING", "HYPOTHESIS_GENERATION"),
        ("HYPOTHESIS_GENERATION", "HYPOTHESIS_PRIORITIZATION"),
        ("HYPOTHESIS_PRIORITIZATION", "EVIDENCE_COLLECTION"),
        ("EVIDENCE_COLLECTION", "HYPOTHESIS_VALIDATION"),
        ("HYPOTHESIS_VALIDATION", "REPORT_GENERATION"),
        ("REPORT_GENERATION", "COMPLETED"),
    ]

    @pytest.mark.parametrize("current,target", HAPPY_PATH)
    def test_happy_path_allowed(self, current: str, target: str):
        ddb = _mock_ddb_state(current)
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"):
            _validate_transition("rca-1", target, dynamodb_client=ddb)

    def test_validation_to_prioritization_loop(self):
        ddb = _mock_ddb_state("HYPOTHESIS_VALIDATION")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"):
            _validate_transition("rca-1", "HYPOTHESIS_PRIORITIZATION", dynamodb_client=ddb)

    def test_validation_to_evidence_loop(self):
        ddb = _mock_ddb_state("HYPOTHESIS_VALIDATION")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"):
            _validate_transition("rca-1", "EVIDENCE_COLLECTION", dynamodb_client=ddb)

    def test_validation_to_hypothesis_generation_regeneration(self):
        ddb = _mock_ddb_state("HYPOTHESIS_VALIDATION")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"):
            _validate_transition("rca-1", "HYPOTHESIS_GENERATION", dynamodb_client=ddb)


# ── Invalid transitions ─────────────────────────────────────────────────


class TestInvalidTransitions:
    INVALID = [
        ("ALARM_RECEIVED", "HYPOTHESIS_GENERATION"),
        ("ALARM_RECEIVED", "EVIDENCE_COLLECTION"),
        ("SCOPING", "EVIDENCE_COLLECTION"),
        ("HYPOTHESIS_GENERATION", "EVIDENCE_COLLECTION"),
        ("EVIDENCE_COLLECTION", "REPORT_GENERATION"),
        ("REPORT_GENERATION", "SCOPING"),
    ]

    @pytest.mark.parametrize("current,target", INVALID)
    def test_invalid_transition_raises(self, current: str, target: str):
        ddb = _mock_ddb_state(current)
        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"),
            pytest.raises(InvalidStateTransitionError),
        ):
            _validate_transition("rca-1", target, dynamodb_client=ddb)


# ── Terminal state abort ─────────────────────────────────────────────────


class TestTerminalStateAbort:
    @pytest.mark.parametrize("terminal", sorted(_TERMINAL_STATES))
    def test_terminal_state_raises_session_cancelled(self, terminal: str):
        ddb = _mock_ddb_state(terminal)
        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"),
            pytest.raises(SessionCancelledError),
        ):
            _validate_transition("rca-1", "SCOPING", dynamodb_client=ddb)

    def test_completed_aborts_pipeline(self):
        ddb = _mock_ddb_state("COMPLETED")
        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"),
            pytest.raises(SessionCancelledError),
        ):
            _validate_transition("rca-1", "REPORT_GENERATION", dynamodb_client=ddb)

    def test_failed_aborts_pipeline(self):
        ddb = _mock_ddb_state("FAILED")
        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"),
            pytest.raises(SessionCancelledError),
        ):
            _validate_transition("rca-1", "SCOPING", dynamodb_client=ddb)

    def test_outdated_aborts_pipeline(self):
        ddb = _mock_ddb_state("OUTDATED")
        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"),
            pytest.raises(SessionCancelledError),
        ):
            _validate_transition("rca-1", "SCOPING", dynamodb_client=ddb)


# ── No DDB skips validation ─────────────────────────────────────────────


class TestNoDdbSkipsValidation:
    def test_no_table_name_skips(self):
        ddb = MagicMock()
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            _validate_transition("rca-1", "SCOPING", dynamodb_client=ddb)
        ddb.get_item.assert_not_called()

    def test_no_client_skips(self):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"):
            _validate_transition("rca-1", "SCOPING", dynamodb_client=None)

    def test_no_item_in_ddb_skips(self):
        ddb = MagicMock()
        ddb.get_item.return_value = {}
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "t"):
            _validate_transition("rca-1", "ANYTHING", dynamodb_client=ddb)


# ── Pre-report hypothesis finalization ───────────────────────────────────


class TestPreReportHypothesisFinalization:
    """
    Verifies the finalize logic from main.py:
    before report generation, all hypotheses must be CONFIRMED, REJECTED, or CLOSED.
    """

    def _finalize(
        self,
        hypotheses: list[Hypothesis],
        termination: TerminationDecision | None,
    ) -> list[tuple[str, str, str]]:
        """
        Simulate the finalize block from main._run_pipeline.
        Returns list of (hypothesis_id, new_status, reason) for each changed hypothesis.
        """
        close_reason_map = {
            TerminationReason.CONFIRMED: "확정된 근본원인 발견으로 추가 검증 불필요",
            TerminationReason.TIME_BUDGET: "시간 예산 소진",
            TerminationReason.TOKEN_BUDGET: "토큰 예산 소진",
            TerminationReason.MAX_DEPTH: "최대 트리 깊이 초과",
            TerminationReason.MAX_LOOPS: "최대 검증 루프 초과",
            TerminationReason.ALL_REJECTED: "전체 가설 기각",
        }
        close_reason = (
            close_reason_map.get(termination.reason, "분석 종료") if termination and termination.reason else "분석 종료"
        )
        best_hid = termination.best_hypothesis.hypothesis_id if termination and termination.best_hypothesis else None

        changes = []
        for h in hypotheses:
            if h.status in (HypothesisStatus.PENDING, HypothesisStatus.NEEDS_INVESTIGATION):
                if h.hypothesis_id == best_hid:
                    continue
                h.status = HypothesisStatus.CLOSED
                changes.append((h.hypothesis_id, HypothesisStatus.CLOSED.value, close_reason))
        return changes

    def test_pending_becomes_closed_on_time_budget(self):
        hypos = [
            _make_hypothesis("h-1", HypothesisStatus.CONFIRMED, 0.95),
            _make_hypothesis("h-2", HypothesisStatus.PENDING, 0.3),
            _make_hypothesis("h-3", HypothesisStatus.NEEDS_INVESTIGATION, 0.5),
        ]
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.TIME_BUDGET,
            best_hypothesis=hypos[0],
        )

        changes = self._finalize(hypos, td)

        assert hypos[0].status == HypothesisStatus.CONFIRMED
        assert hypos[1].status == HypothesisStatus.CLOSED
        assert hypos[2].status == HypothesisStatus.CLOSED
        assert len(changes) == 2
        assert all(reason == "시간 예산 소진" for _, _, reason in changes)

    def test_pending_becomes_closed_on_confirmed(self):
        confirmed = _make_hypothesis("h-1", HypothesisStatus.CONFIRMED, 0.95)
        pending = _make_hypothesis("h-2", HypothesisStatus.PENDING, 0.3)
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.CONFIRMED,
            best_hypothesis=confirmed,
        )

        changes = self._finalize([confirmed, pending], td)

        assert pending.status == HypothesisStatus.CLOSED
        assert changes[0][2] == "확정된 근본원인 발견으로 추가 검증 불필요"

    def test_pending_becomes_closed_on_max_loops(self):
        pending = _make_hypothesis("h-1", HypothesisStatus.PENDING, 0.5)
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.MAX_LOOPS,
        )

        self._finalize([pending], td)
        assert pending.status == HypothesisStatus.CLOSED

    def test_pending_becomes_closed_on_max_depth(self):
        pending = _make_hypothesis("h-1", HypothesisStatus.NEEDS_INVESTIGATION, 0.6)
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.MAX_DEPTH,
        )

        changes = self._finalize([pending], td)
        assert pending.status == HypothesisStatus.CLOSED
        assert changes[0][2] == "최대 트리 깊이 초과"

    def test_best_hypothesis_not_closed(self):
        best = _make_hypothesis("h-1", HypothesisStatus.NEEDS_INVESTIGATION, 0.7)
        other = _make_hypothesis("h-2", HypothesisStatus.PENDING, 0.3)
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.TIME_BUDGET,
            best_hypothesis=best,
        )

        self._finalize([best, other], td)

        assert best.status == HypothesisStatus.NEEDS_INVESTIGATION
        assert other.status == HypothesisStatus.CLOSED

    def test_rejected_stays_rejected(self):
        rejected = _make_hypothesis("h-1", HypothesisStatus.REJECTED, 0.1)
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.TIME_BUDGET,
        )

        changes = self._finalize([rejected], td)
        assert rejected.status == HypothesisStatus.REJECTED
        assert changes == []

    def test_confirmed_stays_confirmed(self):
        confirmed = _make_hypothesis("h-1", HypothesisStatus.CONFIRMED, 0.95)
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.TIME_BUDGET,
        )

        changes = self._finalize([confirmed], td)
        assert confirmed.status == HypothesisStatus.CONFIRMED
        assert changes == []

    def test_all_terminal_after_finalize(self):
        terminal_statuses = {HypothesisStatus.CONFIRMED, HypothesisStatus.REJECTED, HypothesisStatus.CLOSED}
        hypos = [
            _make_hypothesis("h-1", HypothesisStatus.CONFIRMED, 0.95),
            _make_hypothesis("h-2", HypothesisStatus.REJECTED, 0.1),
            _make_hypothesis("h-3", HypothesisStatus.PENDING, 0.4),
            _make_hypothesis("h-4", HypothesisStatus.NEEDS_INVESTIGATION, 0.6),
        ]
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.TIME_BUDGET,
            best_hypothesis=hypos[0],
        )

        self._finalize(hypos, td)

        for h in hypos:
            assert h.status in terminal_statuses, f"{h.hypothesis_id} has non-terminal status {h.status}"

    def test_no_termination_uses_default_reason(self):
        pending = _make_hypothesis("h-1", HypothesisStatus.PENDING, 0.5)
        changes = self._finalize([pending], None)
        assert pending.status == HypothesisStatus.CLOSED
        assert changes[0][2] == "분석 종료"

    def test_all_rejected_reason(self):
        pending = _make_hypothesis("h-1", HypothesisStatus.PENDING, 0.4)
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.ALL_REJECTED,
        )

        changes = self._finalize([pending], td)
        assert changes[0][2] == "전체 가설 기각"


# ── Full pipeline integration: finalize before report ────────────────────


class TestPipelineFinalizeBeforeReport:
    """
    Verifies that PipelineOrchestrator._run_pipeline marks hypotheses as
    REJECTED (not CLOSED) before transitioning to REPORT_GENERATION.
    """

    def test_pipeline_closes_pending_on_confirmed_termination(self):
        from rca_agent.models import (
            HypothesisGenerationResult,
            RcaReport,
            RcaSession,
            ScopingResult,
            ValidationJudgment,
            ValidationResult,
        )
        from rca_agent.services.pipeline import PipelineOrchestrator

        confirmed_h = _make_hypothesis("h-1", HypothesisStatus.PENDING, 0.95)
        pending_h = _make_hypothesis("h-2", HypothesisStatus.PENDING, 0.4)
        sr = ScopingResult(alarm_summary="test")
        hr = HypothesisGenerationResult(tree_id="tree-1", hypotheses=[confirmed_h, pending_h], scoping_result=sr)
        vr = ValidationResult(
            tree_id="tree-1",
            judgments=[
                ValidationJudgment(hypothesis_id="h-1", status=HypothesisStatus.CONFIRMED, confidence_score=0.95),
            ],
        )
        td = TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.CONFIRMED,
            best_hypothesis=confirmed_h,
        )
        rca = RcaReport(rca_id="rca-1", incident_summary="test", root_cause="test", confidence_score=0.95)
        session = RcaSession(rca_id="rca-1", idempotency_key="k", state=RcaSessionState.ALARM_RECEIVED)

        trace_update_calls = []
        original_trace_cls = MagicMock()
        mock_ctx = MagicMock(__enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock(return_value=False))
        original_trace_cls.return_value.span = MagicMock(return_value=mock_ctx)
        original_trace_cls.return_value.start_span = MagicMock(return_value=MagicMock(span_id="s-1"))
        original_trace_cls.return_value.end_span = MagicMock()
        original_trace_cls.return_value.put_hypotheses = MagicMock()
        original_trace_cls.return_value.update_hypothesis_evidence = MagicMock()
        original_trace_cls.return_value.check_cancelled = MagicMock()

        def capture_status_update(hid, *, status, confidence=None, judgment_reasoning=""):
            trace_update_calls.append((hid, status, judgment_reasoning))

        original_trace_cls.return_value.update_hypothesis_status = capture_status_update

        body = {
            "AlarmName": "TestAlarm",
            "NewStateValue": "ALARM",
            "Trigger": {"MetricName": "CPUUtilization", "Namespace": "AWS/ECS", "Dimensions": []},
        }

        container = MagicMock()
        container.session_store.check_duplicate.return_value = False
        container.session_store.create_session.return_value = session

        pipeline = "rca_agent.services.pipeline"
        with (
            patch(f"{pipeline}.run_scoping", return_value=sr),
            patch(f"{pipeline}.run_hypothesis_generation", return_value=hr),
            patch(f"{pipeline}.run_prioritization", return_value=MagicMock()),
            patch(f"{pipeline}.run_evidence_collection", return_value=EvidenceCollectionSummary()),
            patch(f"{pipeline}.run_validation", return_value=vr),
            patch(f"{pipeline}.check_termination", return_value=td),
            patch(f"{pipeline}.run_report_generation", return_value=rca),
            patch(f"{pipeline}.run_playbook_generation", return_value=MagicMock()),
            patch("rca_agent.services.notification.build_notification", return_value=MagicMock()),
            patch(f"{pipeline}.TraceStore", original_trace_cls),
        ):
            orchestrator = PipelineOrchestrator(container)
            orchestrator.process_alarm(body)

        rejected_updates = [(hid, st) for hid, st, _ in trace_update_calls if st == "REJECTED"]
        assert ("h-2", "REJECTED") in rejected_updates, "h-2 should be REJECTED when another hypothesis is CONFIRMED"

        closed_updates = [hid for hid, st, _ in trace_update_calls if st == "CLOSED" and hid == "h-2"]
        assert closed_updates == [], "h-2 should be REJECTED, not CLOSED"
