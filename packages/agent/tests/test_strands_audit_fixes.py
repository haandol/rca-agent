"""Offline regressions for branching capacity, validation isolation, and regeneration feedback."""

import copy
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from strands import Agent
from strands.event_loop._retry import ModelRetryStrategy
from strands.models.model import Model
from strands.types.exceptions import ModelThrottledException

from rca_agent.agent_factory import create_validation_agent
from rca_agent.di.app_container import AppContainer
from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    ScopingResult,
    ValidationJudgment,
    ValidationResult,
)
from rca_agent.services.branching import BranchingOutput, _ChildItem, run_branching
from rca_agent.services.hypothesis import (
    MAX_REJECTION_FEEDBACK_ITEMS,
    HypothesisOutput,
    _build_user_prompt,
    _HypothesisItem,
    build_rejection_feedback,
    run_hypothesis_generation,
)
from rca_agent.services.pipeline import PipelineOrchestrator, ValidationLoopState
from rca_agent.services.review_gate import run_review_gate
from rca_agent.services.validation import run_validation, validate_hypothesis


def _hypothesis(hypothesis_id="parent", **kwargs):
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        description=kwargs.pop("description", f"Direction {hypothesis_id}"),
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=0.5,
        tree_id=kwargs.pop("tree_id", "tree-1"),
        **kwargs,
    )


def _children(*descriptions):
    return MagicMock(
        structured_output=BranchingOutput(
            children=[
                _ChildItem(description=description, category=HypothesisCategory.DEPLOYMENT, confidence_score=0.5)
                for description in descriptions
            ]
        )
    )


def _branch_state():
    parent = _hypothesis(status=HypothesisStatus.NEEDS_INVESTIGATION)
    return ValidationLoopState(
        hypotheses=[parent],
        all_judgments=[
            ValidationJudgment(
                hypothesis_id=parent.hypothesis_id,
                status=HypothesisStatus.NEEDS_INVESTIGATION,
                confidence_score=0.5,
            )
        ],
    )


def test_repeated_parent_six_duplicate_proposals_then_remaining_capacity():
    state = _branch_state()
    container = MagicMock()
    container.branching_agent.side_effect = [
        _children("First", "Second"),
        _children(" FIRST ", "second"),
        _children("first", " SECOND "),
        _children("Third", "Fourth", "Fifth"),
    ]
    orchestrator = PipelineOrchestrator(container)
    trace = MagicMock()

    # Six proposals across three rounds produce only two siblings. Duplicate-only
    # output must not end exploration while those children still need validation.
    for _ in range(3):
        assert orchestrator._loop_branching(
            state,
            run_review_gate(state.hypotheses, state.all_judgments, consecutive_blocked_loops=0),
            trace,
            MagicMock(),
        )
        assert len(state.hypotheses) == 3
    first_children = state.hypotheses[1:].copy()

    assert orchestrator._loop_branching(
        state, run_review_gate(state.hypotheses, state.all_judgments, consecutive_blocked_loops=0), trace, MagicMock()
    )
    assert state.hypotheses[1:3] == first_children
    assert [child.description for child in state.hypotheses[1:]] == ["First", "Second", "Third"]

    # The lifetime cap now blocks model invocation, without discarding pending work.
    assert orchestrator._loop_branching(
        state, run_review_gate(state.hypotheses, state.all_judgments, consecutive_blocked_loops=0), trace, MagicMock()
    )
    assert container.branching_agent.call_count == 4
    assert trace.put_hypotheses.call_count == 2


def test_six_distinct_proposals_cannot_create_six_children_for_one_parent():
    state = _branch_state()
    container = MagicMock()
    container.branching_agent.side_effect = [_children("A", "B", "C"), _children("D", "E", "F")]
    orchestrator = PipelineOrchestrator(container)
    for _ in range(2):
        assert orchestrator._loop_branching(
            state,
            run_review_gate(state.hypotheses, state.all_judgments, consecutive_blocked_loops=0),
            MagicMock(),
            MagicMock(),
        )
    assert len(state.hypotheses) == 4
    container.branching_agent.assert_called_once()


def test_same_batch_duplicates_use_existing_case_insensitive_equality():
    parent = _hypothesis()
    agent = MagicMock(return_value=_children("Unique", " UNIQUE ", "Other"))
    result = run_branching(parent, "", [], agent)
    assert [child.description for child in result.children] == ["Unique", "Other"]


@pytest.mark.parametrize("status", list(HypothesisStatus))
def test_existing_children_consume_capacity_regardless_of_status(status):
    parent = _hypothesis()
    siblings = [_hypothesis(str(i), parent_id=parent.hypothesis_id, status=status) for i in range(3)]
    agent = MagicMock()
    assert run_branching(parent, "", [], agent, existing_children=siblings).children == []
    agent.assert_not_called()


def test_no_new_children_ends_only_when_no_pending_work_remains():
    state = ValidationLoopState(hypotheses=[_hypothesis(status=HypothesisStatus.REJECTED)])
    orchestrator = PipelineOrchestrator(MagicMock())
    gate = run_review_gate(state.hypotheses, [], consecutive_blocked_loops=0)
    assert not orchestrator._loop_branching(state, gate, MagicMock(), MagicMock())
    state.hypotheses.append(_hypothesis("pending"))
    assert orchestrator._loop_branching(state, gate, MagicMock(), MagicMock())


@pytest.mark.parametrize("failure", [False, True])
def test_no_new_children_with_only_current_unresolved_parent_terminates(failure):
    state = _branch_state()
    container = MagicMock()
    if failure:
        container.branching_agent.side_effect = RuntimeError("branch failed")
    else:
        container.branching_agent.return_value = _children("Direction parent", " DIRECTION PARENT ")
    gate = run_review_gate(state.hypotheses, state.all_judgments, consecutive_blocked_loops=0)
    assert not PipelineOrchestrator(container)._loop_branching(state, gate, MagicMock(), MagicMock())
    container.branching_agent.assert_called_once()


@pytest.mark.parametrize("has_pending_child", [False, True])
def test_exhausted_capacity_continues_only_with_pending_child(has_pending_child):
    state = _branch_state()
    state.hypotheses.extend(
        _hypothesis(
            str(index),
            parent_id="parent",
            status=HypothesisStatus.PENDING if has_pending_child and index == 0 else HypothesisStatus.REJECTED,
            depth=1,
        )
        for index in range(3)
    )
    container = MagicMock()
    gate = run_review_gate(state.hypotheses, state.all_judgments, consecutive_blocked_loops=0)
    assert PipelineOrchestrator(container)._loop_branching(state, gate, MagicMock(), MagicMock()) == has_pending_child
    container.branching_agent.assert_not_called()


class RecordingValidationModel(Model):
    """Exercise installed Strands message handling without a provider connection."""

    def __init__(self):
        self.inputs = []
        self.failure = None
        self.delay = 0

    def update_config(self, **kwargs):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("Unexpected legacy structured-output path")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.inputs.append(copy.deepcopy(messages))
        if self.delay:
            delay, self.delay = self.delay, 0
            time.sleep(delay)
        if self.failure:
            failure, self.failure = self.failure, None
            raise failure
        data = {
            "judgment": {
                "status": "NEEDS_INVESTIGATION",
                "confidence_score": 0.5,
                "reasoning": "A bounded fake judgment",
                "evidence_summary": ["Observed summary"],
                "validated_fault_type": "UNSUPPORTED",
            }
        }
        yield {"messageStart": {"role": "assistant"}}
        yield {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": str(len(self.inputs)), "name": tool_specs[-1]["name"]}},
                "contentBlockIndex": 0,
            }
        }
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(data)}}, "contentBlockIndex": 0}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "metrics": {"latencyMs": 0},
            }
        }


def test_installed_strands_isolates_hypotheses_rounds_and_sessions_in_same_container():
    model = RecordingValidationModel()
    agent = create_validation_agent(model=model)
    agent.callback_handler = lambda **_: None
    container = AppContainer("unused")
    container._validation_agent = agent
    for session in range(2):
        hypotheses = [_hypothesis(f"session-{session}-h-{i}") for i in range(3)]
        for round_number in range(2):
            evidence = {
                hypothesis.hypothesis_id: f"UNIQUE_SUMMARY_{session}_{round_number}_{i}"
                for i, hypothesis in enumerate(hypotheses)
            }
            result = run_validation(hypotheses, evidence, container.validation_agent)
            assert len(result.judgments) == 3
            assert agent.messages == []
    assert len(model.inputs) == 12
    assert container.validation_agent is agent
    assert agent.model is model
    for index, messages in enumerate(model.inputs):
        assert len(messages) == 1
        prompt = json.dumps(messages)
        session, within_session = divmod(index, 6)
        round_number, hypothesis_number = divmod(within_session, 3)
        assert f"UNIQUE_SUMMARY_{session}_{round_number}_{hypothesis_number}" in prompt
        assert prompt.count("UNIQUE_SUMMARY_") == 1


@pytest.mark.parametrize("failure", [RuntimeError("fake failure"), TimeoutError("fake timeout")])
def test_failed_validation_does_not_leak_partial_conversation(failure):
    model = RecordingValidationModel()
    model.failure = failure
    agent = create_validation_agent(model=model)
    agent.callback_handler = lambda **_: None
    failed = validate_hypothesis(_hypothesis("failed"), "FAILED_EVIDENCE", agent)
    assert failed.status == HypothesisStatus.NEEDS_INVESTIGATION
    assert agent.messages == []
    validate_hypothesis(_hypothesis("next"), "NEXT_EVIDENCE", agent)
    assert "FAILED_EVIDENCE" not in json.dumps(model.inputs[-1])
    assert "NEXT_EVIDENCE" in json.dumps(model.inputs[-1])
    assert agent.messages == []


def test_validation_preserves_sdk_retry_strategy_for_each_invocation():
    model = RecordingValidationModel()
    agent = Agent(
        model=model,
        callback_handler=None,
        retry_strategy=ModelRetryStrategy(max_attempts=2, initial_delay=0, max_delay=0),
    )
    for index in range(2):
        model.failure = ModelThrottledException("offline throttle")
        judgment = validate_hypothesis(_hypothesis(str(index)), f"RETRY_EVIDENCE_{index}", agent)
        assert judgment.reasoning == "A bounded fake judgment"
        assert agent.messages == []
    assert len(model.inputs) == 4
    for index, messages in enumerate(model.inputs):
        assert len(messages) == 1
        assert f"RETRY_EVIDENCE_{index // 2}" in json.dumps(messages)


def test_hard_timeout_discards_partial_validation_messages():
    agent = MagicMock()
    agent.messages = []

    def slow_invocation(prompt, **kwargs):
        agent.messages.append({"role": "user", "content": [{"text": prompt}]})
        time.sleep(0.1)

    agent.side_effect = slow_invocation
    judgment = validate_hypothesis(_hypothesis(), "TIMEOUT_EVIDENCE", agent, timeout_seconds=0.01)
    assert judgment.status == HypothesisStatus.NEEDS_INVESTIGATION
    assert agent.messages == []


def test_installed_strands_short_budget_admission_and_reuse():
    """Reject an undrainable invocation before starting Strands, then reuse its clean conversation."""
    model = RecordingValidationModel()
    model.delay = 0.5
    agent = create_validation_agent(model=model)
    agent.callback_handler = lambda **_: None
    started = time.monotonic()
    from rca_agent.utils.agent_invocation import invocation_scope

    with invocation_scope(admission_deadline=time.monotonic() - 1):
        judgment = validate_hypothesis(_hypothesis("timed-out"), "TIMEOUT_EVIDENCE", agent, timeout_seconds=0.02)
    assert time.monotonic() - started < 0.02
    assert model.inputs == []
    assert model.delay == 0.5
    assert "timed out or failed" in judgment.reasoning
    assert agent.messages == []
    # No first request was started; the next invocation must have only its own input.
    next_judgment = validate_hypothesis(_hypothesis("next"), "NEXT_EVIDENCE", agent)
    assert next_judgment.reasoning == "A bounded fake judgment"
    assert len(model.inputs) == 1
    assert len(model.inputs[-1]) == 1
    assert "TIMEOUT_EVIDENCE" not in json.dumps(model.inputs[-1])
    assert agent.messages == []


def _generation_agent():
    return MagicMock(
        return_value=MagicMock(
            structured_output=HypothesisOutput(
                hypotheses=[
                    _HypothesisItem(
                        description=f"Direction {i}", category=HypothesisCategory.DEPLOYMENT, confidence_score=0.5
                    )
                    for i in range(3)
                ]
            )
        )
    )


def test_regeneration_feedback_is_bounded_and_does_not_reject_repeated_descriptions():
    hypotheses = [
        _hypothesis(str(i), status=HypothesisStatus.REJECTED, judgment_reasoning="reason" * 1000) for i in range(20)
    ]
    hypotheses[-1].description = "Direction 0"  # A generated description may legitimately repeat.
    judgments = [
        ValidationJudgment(
            hypothesis_id="19",
            status=HypothesisStatus.REJECTED,
            confidence_score=0.1,
            reasoning="Contradicted by the observed deployment time",
            evidence_summary=["s3://evidence/rejection.txt: deployment predates the anomaly"],
        )
    ]
    feedback = build_rejection_feedback(hypotheses, judgments, {"19": "collected summary " * 1000})
    assert len(feedback) == MAX_REJECTION_FEEDBACK_ITEMS
    assert all(len(entry) < 2400 for entry in feedback)
    prompt = _build_user_prompt(ScopingResult(alarm_summary="CPU alarm"), feedback)
    assert "Hypothesis ID: 19 (tree: tree-1)" in prompt
    assert judgments[0].reasoning in prompt
    assert judgments[0].evidence_summary[0] in prompt
    assert "collected summary" in prompt
    assert "Previous rejected hypotheses" not in _build_user_prompt(ScopingResult(alarm_summary="CPU alarm"))
    agent = _generation_agent()
    result = run_hypothesis_generation(ScopingResult(alarm_summary="CPU alarm"), agent, rejection_feedback=feedback)
    assert len(result.hypotheses) == 3
    assert result.hypotheses[0].description == "Direction 0"
    assert all(h.status == HypothesisStatus.PENDING for h in result.hypotheses)
    assert all(h.hypothesis_id not in {old.hypothesis_id for old in hypotheses} for h in result.hypotheses)


def test_pipeline_forwards_feedback_across_regenerations_and_preserves_trace_history():
    container = MagicMock()
    container.hypothesis_agent = _generation_agent()
    orchestrator = PipelineOrchestrator(container)
    state = ValidationLoopState(hypotheses=[_hypothesis("old", status=HypothesisStatus.REJECTED)])
    state.evidence_map["old"] = "The old evidence remains linked to old"
    run = MagicMock()
    previous_ids = []
    for round_number in range(2):
        rejected = state.hypotheses[0]
        previous_ids.append(rejected.hypothesis_id)
        # Regeneration requires every current hypothesis to be rejected, not
        # just the first member of the previously generated three-root set.
        state.all_judgments = [
            ValidationJudgment(
                hypothesis_id=hypothesis.hypothesis_id,
                status=HypothesisStatus.REJECTED,
                confidence_score=0.1,
                reasoning=f"Rejected direction in round {round_number}",
                evidence_summary=[f"Evidence reference in round {round_number}"],
            )
            for hypothesis in state.hypotheses
        ]
        orchestrator._apply_judgments(state, run.trace)
        validation = ValidationResult(tree_id=rejected.tree_id, judgments=state.all_judgments, all_rejected=True)
        orchestrator._maybe_regenerate(
            state,
            ScopingResult(alarm_summary="CPU alarm"),
            validation,
            run_review_gate([], [], consecutive_blocked_loops=0),
            run,
            MagicMock(),
        )
    prompt = container.hypothesis_agent.call_args.args[0]
    for round_number in range(2):
        assert f"Rejected direction in round {round_number}" in prompt
        assert f"Evidence reference in round {round_number}" in prompt
        assert previous_ids[round_number] in prompt
    assert "The old evidence remains linked to old" in prompt
    assert run.trace.put_hypotheses.call_count == 2
    persisted = [call.args[0] for call in run.trace.put_hypotheses.call_args_list]
    assert persisted[0][0].hypothesis_id == previous_ids[1]
    assert persisted[0][0].status == HypothesisStatus.REJECTED
    assert persisted[0][0].judgment_reasoning == "Rejected direction in round 1"
    assert persisted[0][0].tree_id != persisted[1][0].tree_id
    # Evidence remains keyed by identity; matching descriptions cannot reuse it.
    assert all(h.hypothesis_id not in state.evidence_map for h in state.hypotheses)


def test_validation_keeps_per_invocation_timeout():
    agent = MagicMock()
    with patch("rca_agent.services.validation.invoke_agent", side_effect=TimeoutError) as timeout:
        run_validation([_hypothesis("one"), _hypothesis("two")], {}, agent, timeout_seconds=17)
    assert timeout.call_count == 2
    assert all(call.args[3] == 17 for call in timeout.call_args_list)


def test_regeneration_preserves_rejection_evidence_from_an_earlier_validation_loop():
    container = MagicMock()
    container.hypothesis_agent = _generation_agent()
    orchestrator = PipelineOrchestrator(container)
    state = ValidationLoopState(hypotheses=[_hypothesis(hypothesis_id) for hypothesis_id in ("A", "B", "C")])
    scoping = ScopingResult(alarm_summary="CPU alarm")
    run = MagicMock()
    first = ValidationResult(
        tree_id="tree-1",
        judgments=[
            ValidationJudgment(
                hypothesis_id=hypothesis_id,
                status=HypothesisStatus.REJECTED if hypothesis_id == "A" else HypothesisStatus.NEEDS_INVESTIGATION,
                confidence_score=0.1 if hypothesis_id == "A" else 0.5,
                reasoning=f"Loop 1 judgment for {hypothesis_id}",
                evidence_summary=[f"s3://evidence/loop-1/{hypothesis_id}.md"],
            )
            for hypothesis_id in ("A", "B", "C")
        ],
    )
    second = ValidationResult(
        tree_id="tree-1",
        all_rejected=True,
        judgments=[
            ValidationJudgment(
                hypothesis_id=hypothesis_id,
                status=HypothesisStatus.REJECTED,
                confidence_score=0.1,
                reasoning=f"Loop 2 judgment for {hypothesis_id}",
                evidence_summary=[f"s3://evidence/loop-2/{hypothesis_id}.md"],
            )
            for hypothesis_id in ("B", "C")
        ],
    )
    with patch("rca_agent.services.pipeline.run_validation", side_effect=[first, second]):
        state.loop_count = 1
        orchestrator._loop_validation(state, state.hypotheses, scoping, run, MagicMock())
        orchestrator._apply_judgments(state, run.trace)
        state.loop_count = 2
        validation = orchestrator._loop_validation(state, state.hypotheses[1:], scoping, run, MagicMock())
        orchestrator._apply_judgments(state, run.trace)
    assert [judgment.hypothesis_id for judgment in state.all_judgments] == ["B", "C"]
    orchestrator._maybe_regenerate(
        state, scoping, validation, run_review_gate([], [], consecutive_blocked_loops=0), run, MagicMock()
    )
    prompt = container.hypothesis_agent.call_args.args[0]
    assert "Loop 1 judgment for A" in prompt
    assert "s3://evidence/loop-1/A.md" in prompt
    for hypothesis_id in ("B", "C"):
        assert f"Loop 2 judgment for {hypothesis_id}" in prompt
        assert f"s3://evidence/loop-2/{hypothesis_id}.md" in prompt
    for hypothesis_id in ("A", "B", "C"):
        assert prompt.count(f"Hypothesis ID: {hypothesis_id} ") == 1
    assert set(state.rejection_feedback) == {"A", "B", "C"}


def test_rejection_feedback_retains_latest_per_id_with_bounded_history():
    orchestrator = PipelineOrchestrator(MagicMock())
    hypotheses = [_hypothesis(str(index)) for index in range(MAX_REJECTION_FEEDBACK_ITEMS + 1)]
    state = ValidationLoopState(hypotheses=hypotheses)
    trace = MagicMock()

    def reject(hypothesis_id, evidence_reference):
        state.all_judgments = [
            ValidationJudgment(
                hypothesis_id=hypothesis_id,
                status=HypothesisStatus.REJECTED,
                confidence_score=0.1,
                reasoning=f"Reason for {evidence_reference}",
                evidence_summary=[evidence_reference],
            )
        ]
        orchestrator._apply_judgments(state, trace)

    for index in range(MAX_REJECTION_FEEDBACK_ITEMS):
        reject(str(index), f"original-reference-{index}")
    for version in range(3):
        reject("0", f"updated-reference-{version}")
        assert len(state.rejection_feedback) == MAX_REJECTION_FEEDBACK_ITEMS
    reject(str(MAX_REJECTION_FEEDBACK_ITEMS), "new-reference")
    assert len(state.rejection_feedback) == MAX_REJECTION_FEEDBACK_ITEMS
    assert "1" not in state.rejection_feedback
    assert "updated-reference-2" in state.rejection_feedback["0"]
    assert "original-reference-0" not in state.rejection_feedback["0"]
    assert "updated-reference-1" not in state.rejection_feedback["0"]
    # Recapturing cannot resurrect an evicted fallback ahead of newer judgments.
    orchestrator._capture_rejection_feedback(state)
    assert "1" not in state.rejection_feedback
    assert "updated-reference-2" in state.rejection_feedback["0"]


@pytest.mark.parametrize("child_judgment_in_same_loop", [False, True])
def test_pruned_child_feedback_never_treats_inconclusive_judgment_as_rejection(child_judgment_in_same_loop):
    parent = _hypothesis("parent")
    child = _hypothesis("child", parent_id="parent", depth=1)
    cached = _hypothesis("cached")
    state = ValidationLoopState(hypotheses=[parent, child, cached])
    orchestrator = PipelineOrchestrator(MagicMock())
    trace = MagicMock()
    child_judgment = ValidationJudgment(
        hypothesis_id="child",
        status=HypothesisStatus.NEEDS_INVESTIGATION,
        confidence_score=0.5,
        reasoning="INCONCLUSIVE_CHILD_REASON",
        evidence_summary=["INCONCLUSIVE_CHILD_REFERENCE"],
    )
    state.all_judgments = [
        child_judgment,
        ValidationJudgment(
            hypothesis_id="cached",
            status=HypothesisStatus.REJECTED,
            confidence_score=0.1,
            reasoning="Earlier direct rejection",
            evidence_summary=["CACHED_DIRECT_REFERENCE"],
        ),
    ]
    orchestrator._apply_judgments(state, trace)
    state.all_judgments = [
        ValidationJudgment(
            hypothesis_id="parent",
            status=HypothesisStatus.REJECTED,
            confidence_score=0.1,
            reasoning="Parent contradicted by measured evidence",
            evidence_summary=["PARENT_DIRECT_REFERENCE"],
        ),
        *([child_judgment] if child_judgment_in_same_loop else []),
    ]
    orchestrator._apply_judgments(state, trace)
    assert child.status == HypothesisStatus.REJECTED
    assert child.judgment_reasoning == "INCONCLUSIVE_CHILD_REASON"
    prompt = _build_user_prompt(ScopingResult(alarm_summary="CPU alarm"), list(state.rejection_feedback.values()))
    assert "INCONCLUSIVE_CHILD_REASON" not in prompt
    assert "INCONCLUSIVE_CHILD_REFERENCE" not in prompt
    assert "No direct rejection judgment recorded." in state.rejection_feedback["child"]
    assert "inspect parent hypothesis parent" in state.rejection_feedback["child"]
    assert "Parent contradicted by measured evidence" in prompt
    assert "PARENT_DIRECT_REFERENCE" in prompt
    assert "Earlier direct rejection" in prompt
    assert "CACHED_DIRECT_REFERENCE" in prompt
