from __future__ import annotations

import signal
import time
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest

from rca_agent.di.app_container import AppContainer
from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    ValidationJudgment,
)
from rca_agent.ports.interfaces.session_store import ClaimDisposition, SessionClaim
from rca_agent.services.pipeline import PipelineOrchestrator, ValidationLoopState
from rca_agent.services.validation import validate_hypothesis
from rca_agent.utils.timeout import call_with_timeout


def _hypothesis(hypothesis_id: str, confidence: float = 0.5) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        description=f"Hypothesis {hypothesis_id}",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=confidence,
        tree_id=f"tree-{hypothesis_id}",
    )


def _alarm_body(alarm_name: str) -> dict:
    return {
        "AlarmName": alarm_name,
        "NewStateValue": "ALARM",
        "Trigger": {
            "MetricName": "CPUUtilization",
            "Namespace": "AWS/ECS",
        },
    }


class TestExecutionStateIsolation:
    def test_validation_loop_mutable_defaults_are_per_execution(self):
        first = ValidationLoopState(hypotheses=[_hypothesis("first")])
        second = ValidationLoopState(hypotheses=[_hypothesis("second")])

        first.all_judgments.append(
            ValidationJudgment(
                hypothesis_id="first",
                status=HypothesisStatus.CONFIRMED,
                confidence_score=0.91,
            )
        )
        first.rejected_descriptions.append("rejected in first run")
        first.evidence_map["first"] = "first-run evidence"
        first.evidence_failed_ids.add("first")
        first.timeline.append("first-run event")

        assert second.all_judgments == []
        assert second.rejected_descriptions == []
        assert second.evidence_map == {}
        assert second.evidence_failed_ids == set()
        assert second.timeline == []

    def test_hypothesis_lists_are_not_copied_or_shared_between_states(self):
        first_hypotheses = [_hypothesis("first")]
        second_hypotheses = [_hypothesis("second")]
        first = ValidationLoopState(hypotheses=first_hypotheses)
        second = ValidationLoopState(hypotheses=second_hypotheses)

        first.hypotheses[0].status = HypothesisStatus.REJECTED
        first.hypotheses.append(_hypothesis("first-child"))

        assert first.hypotheses is first_hypotheses
        assert second.hypotheses is second_hypotheses
        assert [h.hypothesis_id for h in second.hypotheses] == ["second"]
        assert second.hypotheses[0].status == HypothesisStatus.PENDING

    def test_app_containers_do_not_share_lazy_runtime_caches(self):
        first = AppContainer("https://sqs.example/first")
        second = AppContainer("https://sqs.example/second")
        first._validation_agent = object()
        first._evidence_mcp_clients = [object()]
        first._s3_vectors_client = object()

        assert second._validation_agent is None
        assert second._evidence_mcp_clients is None
        assert second._s3_vectors_client is None

    def test_each_alarm_receives_its_own_monotonic_start_time(self):
        container = MagicMock()
        container.session_store.claim_session.side_effect = [
            SessionClaim(ClaimDisposition.CLAIMED, "claim-first", 1),
            SessionClaim(ClaimDisposition.CLAIMED, "claim-second", 1),
        ]
        orchestrator = PipelineOrchestrator(container)

        with (
            patch("rca_agent.services.pipeline.time.monotonic", side_effect=[101.0, 202.0]),
            patch("rca_agent.services.pipeline.TraceStore"),
            patch.object(orchestrator, "_run_pipeline") as run_pipeline,
        ):
            orchestrator.process_alarm(_alarm_body("FirstAlarm"))
            orchestrator.process_alarm(_alarm_body("SecondAlarm"))

        assert [call.kwargs["start_time"] for call in run_pipeline.call_args_list] == [101.0, 202.0]
        assert len({call.kwargs["rca_id"] for call in run_pipeline.call_args_list}) == 2


class TestWallClockTimeoutContract:
    def test_timeout_helper_returns_result(self):
        assert call_with_timeout(lambda: "completed", 0.1) == "completed"

    def test_timeout_helper_reraises_operation_exception(self):
        def fail():
            raise ValueError("operation failed")

        with pytest.raises(ValueError, match="operation failed"):
            call_with_timeout(fail, 0.1)

    def test_timeout_helper_interrupts_operation_without_late_mutation(self):
        state = []

        def slow_operation():
            state.append("started")
            time.sleep(0.25)
            state.append("finished")

        started = time.perf_counter()
        with pytest.raises(TimeoutError, match="operation timed out"):
            call_with_timeout(slow_operation, 0.04)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.2
        time.sleep(0.1)
        assert state == ["started"]

    def test_operation_cannot_swallow_timeout_with_exception_handler(self):
        state = []

        def slow_operation():
            state.append("started")
            try:
                time.sleep(0.25)
            except Exception:
                state.append("caught")
            state.append("finished")

        with pytest.raises(TimeoutError, match="operation timed out"):
            call_with_timeout(slow_operation, 0.04)

        time.sleep(0.1)
        assert state == ["started"]

    def test_non_positive_timeout_does_not_start_operation(self):
        operation = MagicMock()

        with pytest.raises(TimeoutError, match="operation timed out"):
            call_with_timeout(operation, 0)

        operation.assert_not_called()

    def test_non_main_thread_fails_closed_without_starting_operation(self):
        operation = MagicMock()
        errors = []

        def invoke():
            try:
                call_with_timeout(operation, 0.1)
            except Exception as exc:
                errors.append(exc)

        thread = Thread(target=invoke)
        thread.start()
        thread.join()

        operation.assert_not_called()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert "POSIX main thread" in str(errors[0])

    def test_active_timer_fails_closed_without_changing_handler_or_timer(self):
        original_handler = signal.getsignal(signal.SIGALRM)
        original_timer = signal.getitimer(signal.ITIMER_REAL)
        operation = MagicMock()

        def existing_handler(signum, frame):  # noqa: ARG001
            return None

        try:
            signal.signal(signal.SIGALRM, existing_handler)
            signal.setitimer(signal.ITIMER_REAL, 0.5, 0.5)
            before_delay, before_interval = signal.getitimer(signal.ITIMER_REAL)

            with pytest.raises(RuntimeError, match="ITIMER_REAL is active"):
                call_with_timeout(operation, 0.1)

            after_delay, after_interval = signal.getitimer(signal.ITIMER_REAL)
            operation.assert_not_called()
            assert signal.getsignal(signal.SIGALRM) is existing_handler
            assert 0 < after_delay <= before_delay
            assert after_delay > before_delay - 0.1
            assert after_interval == before_interval
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, original_handler)
            if original_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, *original_timer)

    def test_existing_handler_without_timer_is_restored_after_timeout(self):
        original_handler = signal.getsignal(signal.SIGALRM)
        original_timer = signal.getitimer(signal.ITIMER_REAL)

        def existing_handler(signum, frame):  # noqa: ARG001
            return None

        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, existing_handler)

            with pytest.raises(TimeoutError, match="operation timed out"):
                call_with_timeout(lambda: time.sleep(0.2), 0.03)

            assert signal.getsignal(signal.SIGALRM) is existing_handler
            assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, original_handler)
            if original_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, *original_timer)

    def test_nested_timeout_fails_closed_before_starting_inner_operation(self):
        inner_operation = MagicMock()

        with pytest.raises(RuntimeError, match="ITIMER_REAL is active"):
            call_with_timeout(
                lambda: call_with_timeout(inner_operation, 0.1),
                0.2,
            )

        inner_operation.assert_not_called()

    def test_validation_zero_timeout_does_not_start_agent(self):
        hypothesis = _hypothesis("slow")
        agent = MagicMock()

        started = time.perf_counter()
        judgment = validate_hypothesis(
            hypothesis,
            "evidence",
            agent,
            timeout_seconds=0,
        )
        elapsed = time.perf_counter() - started

        assert elapsed < 0.15
        agent.assert_not_called()
        assert judgment.status == HypothesisStatus.NEEDS_INVESTIGATION
        assert judgment.confidence_score == hypothesis.confidence_score

    def test_validation_judgment_confidence_is_carried_into_next_loop(self):
        hypothesis = _hypothesis("accepted", confidence=0.81)
        state = ValidationLoopState(
            hypotheses=[hypothesis],
            all_judgments=[
                ValidationJudgment(
                    hypothesis_id=hypothesis.hypothesis_id,
                    status=HypothesisStatus.CONFIRMED,
                    confidence_score=0.93,
                )
            ],
        )

        PipelineOrchestrator(MagicMock())._apply_judgments(state, MagicMock())

        assert hypothesis.status == HypothesisStatus.CONFIRMED
        assert hypothesis.confidence_score == 0.93
