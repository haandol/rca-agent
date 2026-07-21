from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from rca_agent.di.app_container import AppContainer
from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    RcaSession,
    ValidationJudgment,
)
from rca_agent.services.pipeline import PipelineOrchestrator, ValidationLoopState
from rca_agent.services.validation import validate_hypothesis


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
        container.session_store.check_duplicate.return_value = False
        container.session_store.create_session.side_effect = [
            RcaSession(rca_id="rca-first", idempotency_key="first"),
            RcaSession(rca_id="rca-second", idempotency_key="second"),
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
        assert [call.kwargs["rca_id"] for call in run_pipeline.call_args_list] == ["rca-first", "rca-second"]


class TestWallClockTimeoutContract:
    def test_validation_timeout_returns_without_waiting_for_worker_completion(self):
        hypothesis = _hypothesis("slow")

        def slow_agent(*args, **kwargs):  # noqa: ARG001
            time.sleep(0.4)

        started = time.perf_counter()
        judgment = validate_hypothesis(
            hypothesis,
            "evidence",
            MagicMock(side_effect=slow_agent),
            timeout_seconds=0,
        )
        elapsed = time.perf_counter() - started

        assert elapsed < 0.15
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
