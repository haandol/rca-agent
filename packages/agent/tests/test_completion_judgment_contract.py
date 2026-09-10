"""Deterministic A/B/E regressions; these tests do not measure model quality."""

import itertools
import json
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from rca_agent import eval_adapter
from rca_agent.adapters.secondary.trace import dynamodb_trace_store as trace_module
from rca_agent.adapters.secondary.trace.dynamodb_trace_store import TraceStore
from rca_agent.ports.dto.models import (
    FaultType,
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    TerminationReason,
    ValidationJudgment,
)
from rca_agent.services.pipeline import PipelineOrchestrator, ValidationLoopState, prune_subtree
from rca_agent.services.termination import check_termination

SCENARIO = {
    "observations": [{"id": identifier} for identifier in ("obs-01", "obs-02", "obs-03")],
    "expectation": {"competingCauses": [{"id": "alternative", "requiredEvidenceIds": ["obs-03"]}]},
}


def _hypothesis(identifier, *, parent=None):
    """Create a pending node whose lifecycle is controlled by actual pipeline code."""
    return Hypothesis(
        hypothesis_id=identifier,
        tree_id="tree",
        parent_id=parent,
        depth=1 if parent else 0,
        description=f"Falsifiable claim {identifier}",
        confidence_score=0.5,
        category=HypothesisCategory.DEPENDENCY,
    )


def _judgment(identifier, status, score, *, reasoning="", evidence=None):
    """Build a direct judgment without deriving its proof from effective tree state."""
    return ValidationJudgment(
        hypothesis_id=identifier,
        status=status,
        confidence_score=score,
        reasoning=reasoning,
        evidence_summary=evidence or [],
    )


@pytest.fixture
def persisted_trace(monkeypatch):
    """Use an in-process DynamoDB emulator, never a real AWS endpoint."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        monkeypatch.setattr(trace_module, "DYNAMODB_TABLE_NAME", "judgment-test")
        client.create_table(
            TableName="judgment-test",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.put_item(
            TableName="judgment-test",
            Item={
                "PK": {"S": "RCA#rca-test"},
                "SK": {"S": "ANALYSIS#SESSION"},
                "state": {"S": "HYPOTHESIS_VALIDATION"},
            },
        )
        yield TraceStore("rca-test", dynamodb_client=client), client


def _read(client):
    """Read actual persisted records through the production deserializer."""
    return TraceStore.get_trace("rca-test", dynamodb_client=client)["hypotheses"]


@pytest.mark.parametrize("selected_score", [0.82, 0.92])
def test_selection_type_and_evidence_are_order_independent(selected_score):
    """The selected cause wins even when another confirmed node has higher confidence."""
    selected = {
        "hypothesis_id": "selected",
        "status": "CONFIRMED",
        "confidence_score": selected_score,
        "validated_fault_type": "UNSUPPORTED",
        "judgment_reasoning": "[obs-01] lock held",
        "validation_evidence_summary": "[obs-02] request blocked",
    }
    other = {
        "hypothesis_id": "other",
        "status": "CONFIRMED",
        "confidence_score": 0.95,
        "validated_fault_type": "SLOW_QUERY",
        "judgment_reasoning": "[obs-03] only belongs to the other claim",
    }
    for order in itertools.permutations([selected, other]):
        assert eval_adapter._root_fault_type(list(order), "selected") == "unsupported"
        assert eval_adapter._root_cause_evidence_ids(SCENARIO, list(order), "selected") == ["obs-01", "obs-02"]


@pytest.mark.parametrize("selected_id", ["", "missing", "closed", "rejected", "duplicate"])
def test_invalid_selection_never_falls_back(selected_id):
    """An absent or invalid selection cannot borrow another confirmed cause or its citations."""
    rows = [
        {
            "hypothesis_id": "valid",
            "status": "CONFIRMED",
            "validated_fault_type": "HIGH_CPU",
            "judgment_reasoning": "[obs-03]",
        },
        {"hypothesis_id": "closed", "status": "CLOSED"},
        {"hypothesis_id": "rejected", "status": "REJECTED"},
        {"hypothesis_id": "duplicate", "status": "CONFIRMED"},
        {"hypothesis_id": "duplicate", "status": "CONFIRMED"},
    ]
    assert eval_adapter._root_fault_type(rows, selected_id) == "unsupported"
    assert eval_adapter._root_cause_evidence_ids(SCENARIO, rows, selected_id) == []


@pytest.mark.parametrize("status,score", [(HypothesisStatus.CONFIRMED, 0.92), (HypothesisStatus.REJECTED, 0.12)])
def test_full_judgment_round_trip_preserves_late_citations(persisted_trace, status, score):
    """Real persistence preserves reasoning and evidence beyond both UI truncation boundaries."""
    trace, client = persisted_trace
    hypothesis = _hypothesis("h")
    trace.put_hypotheses([hypothesis])
    judgment = _judgment(
        "h", status, score, reasoning="판단 " * 500 + "[obs-01]", evidence=["측정 " * 500 + "[obs-03]"]
    )
    judgment.validated_fault_type = FaultType.UNSUPPORTED
    state = ValidationLoopState(hypotheses=[hypothesis], all_judgments=[judgment])
    PipelineOrchestrator(MagicMock())._apply_judgments(state, trace)
    rows = _read(client)
    assert rows[0]["validation_record"] == judgment.model_dump(mode="json")
    assert len(rows[0]["judgment_reasoning"]) == 500
    assert len(rows[0]["validation_evidence_summary"]) == 500
    assert "[obs-03]" not in rows[0]["validation_evidence_summary"]
    if status == HypothesisStatus.CONFIRMED:
        assert eval_adapter._root_cause_evidence_ids(SCENARIO, rows, "h") == ["obs-01", "obs-03"]
    else:
        assert eval_adapter._competing_cause_judgments(SCENARIO, rows)[0]["judgment"] == "rejected"


@pytest.mark.parametrize("score", [0.2, 0.5])
def test_closed_node_keeps_judgment_and_separate_closure(persisted_trace, score):
    """Closing an inconclusive judgment retains its provenance and never creates a rejection."""
    trace, client = persisted_trace
    h = _hypothesis("h")
    trace.put_hypotheses([h])
    judgment = _judgment(
        "h", HypothesisStatus.NEEDS_INVESTIGATION, score, reasoning="Uncertain " * 100, evidence=["[obs-03]"]
    )
    state = ValidationLoopState(hypotheses=[h], all_judgments=[judgment])
    pipeline = PipelineOrchestrator(MagicMock())
    pipeline._apply_judgments(state, trace)
    decision = check_termination(
        hypotheses=[h], judgments=[judgment], start_time=time.monotonic(), validation_loop_count=3
    )
    pipeline._finalize_hypotheses([h], decision, [judgment], trace=trace)
    row = _read(client)[0]
    assert row["status"] == "CLOSED"
    assert row["closure_reason"] == "최대 검증 루프 초과"
    assert row["judgment_reasoning"] == judgment.reasoning[:500]
    assert row["validation_record"] == judgment.model_dump(mode="json")
    assert eval_adapter._competing_cause_judgments(SCENARIO, [row])[0]["judgment"] == "inconclusive"


def test_legacy_status_update_does_not_backfill_a_judgment(persisted_trace):
    """A legacy summary remains legacy and survives a closure-only update unchanged."""
    trace, client = persisted_trace
    trace.put_hypotheses([_hypothesis("old")])
    trace.update_hypothesis_status("old", status="NEEDS_INVESTIGATION", judgment_reasoning="Legacy summary")
    trace.update_hypothesis_status("old", status="CLOSED", closure_reason="Time budget")
    row = _read(client)[0]
    assert "validation_record" not in row
    assert row["judgment_reasoning"] == "Legacy summary"
    assert row["closure_reason"] == "Time budget"


@pytest.mark.parametrize("field", ["reasoning", "evidence_summary"])
def test_oversize_judgment_fails_before_any_write(persisted_trace, field):
    """UTF-8 overflow raises explicitly and leaves the prior complete judgment untouched."""
    trace, client = persisted_trace
    h = _hypothesis("h")
    trace.put_hypotheses([h])
    original = _judgment("h", HypothesisStatus.NEEDS_INVESTIGATION, 0.5, reasoning="Original", evidence=["[obs-01]"])
    trace.update_hypothesis_status("h", status=original.status.value, validation_record=original)
    oversized = original.model_copy(deep=True)
    text = "측" * (trace_module._HYPOTHESIS_WRITE_MAX_BYTES // 2)
    setattr(oversized, field, [text] if field == "evidence_summary" else text)
    with pytest.raises(ValueError, match="Refusing to truncate"):
        trace.update_hypothesis_status("h", status="CONFIRMED", validation_record=oversized)
    row = _read(client)[0]
    assert row["status"] == "NEEDS_INVESTIGATION"
    assert row["validation_record"] == original.model_dump(mode="json")


def test_large_fitting_record_is_not_rejected_or_truncated(persisted_trace):
    """A substantial model response fits the bounded inline record without a new S3 path."""
    trace, client = persisted_trace
    trace.put_hypotheses([_hypothesis("h")])
    judgment = _judgment("h", HypothesisStatus.REJECTED, 0.1, reasoning="근거" * 8000, evidence=["[obs-03]"])
    trace.update_hypothesis_status("h", status="REJECTED", validation_record=judgment)
    assert _read(client)[0]["validation_record"] == json.loads(judgment.model_dump_json())


def test_authoritative_write_failure_propagates(monkeypatch):
    """An unavailable store cannot silently discard a complete validation."""
    monkeypatch.setattr(trace_module, "DYNAMODB_TABLE_NAME", "judgment-test")
    client = MagicMock()
    client.get_item.return_value = {"Item": {"state": {"S": "HYPOTHESIS_VALIDATION"}}}
    client.update_item.side_effect = ClientError({"Error": {"Code": "InternalServerError"}}, "UpdateItem")
    trace = TraceStore("rca-test", dynamodb_client=client)
    judgment = _judgment("h", HypothesisStatus.CONFIRMED, 0.92)
    with pytest.raises(ClientError):
        trace.update_hypothesis_status("h", status="CONFIRMED", validation_record=judgment)


@pytest.mark.parametrize("reverse_judgments", [False, True])
@pytest.mark.parametrize("reverse_nodes", [False, True])
@pytest.mark.parametrize("valid_sibling", [False, True])
def test_pruned_raw_confirmation_cannot_terminate_or_become_rejection_proof(
    persisted_trace, reverse_judgments, reverse_nodes, valid_sibling
):
    """Pruning beats a raw child confirmation regardless of iteration order."""
    trace, client = persisted_trace
    nodes = [_hypothesis("p"), _hypothesis("c", parent="p")]
    judgments = [
        _judgment("p", HypothesisStatus.REJECTED, 0.1, reasoning="Parent disproved"),
        _judgment("c", HypothesisStatus.CONFIRMED, 0.99, reasoning="Child supported", evidence=["[obs-03]"]),
    ]
    if valid_sibling:
        nodes.append(_hypothesis("sibling"))
        judgments.append(_judgment("sibling", HypothesisStatus.CONFIRMED, 0.92, evidence=["[obs-01]"]))
    if reverse_judgments:
        judgments.reverse()
    if reverse_nodes:
        nodes.reverse()
    trace.put_hypotheses(nodes)
    state = ValidationLoopState(hypotheses=nodes, all_judgments=judgments)
    pipeline = PipelineOrchestrator(MagicMock())
    pipeline._apply_judgments(state, trace)
    decision = check_termination(
        hypotheses=nodes, judgments=judgments, start_time=time.monotonic(), validation_loop_count=1
    )
    rows = _read(client)
    child = next(h for h in rows if h["hypothesis_id"] == "c")
    assert child["status"] == "REJECTED"
    assert child["rejection_inherited_from"] == "p"
    assert child["validation_record"]["status"] == "CONFIRMED"
    assert child["validation_record"]["reasoning"] == "Child supported"
    assert eval_adapter._competing_cause_judgments(SCENARIO, rows)[0]["judgment"] == "inconclusive"
    if valid_sibling:
        assert decision.reason == TerminationReason.CONFIRMED
        assert decision.best_hypothesis.hypothesis_id == "sibling"
    else:
        assert not decision.should_terminate
        forced = check_termination(
            hypotheses=nodes, judgments=judgments, start_time=time.monotonic(), validation_loop_count=3
        )
        assert forced.reason == TerminationReason.MAX_LOOPS
        assert forced.best_hypothesis.hypothesis_id != "c"


def test_selected_complete_record_has_priority_over_ui_summary():
    """Full type and evidence travel together even if a display field is stale."""
    judgment = _judgment("h", HypothesisStatus.CONFIRMED, 0.92, evidence=["[obs-01]"])
    row = {
        "hypothesis_id": "h",
        "status": "CONFIRMED",
        "validated_fault_type": "SLOW_QUERY",
        "judgment_reasoning": "[obs-03] stale UI summary",
        "validation_record": judgment.model_dump(mode="json"),
    }
    assert eval_adapter._root_fault_type([row], "h") == "unsupported"
    assert eval_adapter._root_cause_evidence_ids(SCENARIO, [row], "h") == ["obs-01"]


def test_trace_reads_selection_on_a_later_page(monkeypatch):
    """Larger complete records must not cause first-page-only loss of the selected cause."""
    monkeypatch.setattr(trace_module, "DYNAMODB_TABLE_NAME", "judgment-test")
    client = MagicMock()
    last_key = {"PK": {"S": "RCA#rca-test"}, "SK": {"S": "strands#HYPO#other"}}
    judgment = _judgment("selected", HypothesisStatus.CONFIRMED, 0.92, evidence=["[obs-02]"])
    client.query.side_effect = [
        {
            "Items": [
                {
                    **last_key,
                    "status": {"S": "CONFIRMED"},
                    "validated_fault_type": {"S": "SLOW_QUERY"},
                }
            ],
            "LastEvaluatedKey": last_key,
        },
        {
            "Items": [
                {
                    "SK": {"S": "strands#HYPO#selected"},
                    "status": {"S": "CONFIRMED"},
                    "validation_record": {"S": judgment.model_dump_json()},
                }
            ]
        },
    ]
    rows = _read(client)
    assert client.query.call_count == 2
    assert client.query.call_args.kwargs["ExclusiveStartKey"] == last_key
    assert client.query.call_args.kwargs["ConsistentRead"] is True
    assert eval_adapter._root_fault_type(rows, "selected") == "unsupported"
    assert eval_adapter._root_cause_evidence_ids(SCENARIO, rows, "selected") == ["obs-02"]


def test_pruning_crosses_an_already_rejected_intermediate_node():
    """An earlier rejection cannot shield a later descendant from an invalid premise."""
    root = _hypothesis("root")
    middle = _hypothesis("middle", parent="root")
    child = _hypothesis("child", parent="middle")
    middle.status = HypothesisStatus.REJECTED
    child.status = HypothesisStatus.CONFIRMED
    assert prune_subtree("root", [child, middle, root]) == ["child"]
    assert child.status == HypothesisStatus.REJECTED


def test_effectively_rejected_tree_keeps_existing_regeneration_path():
    """Invalidating a raw confirmation must not strand an exhausted tree before regeneration."""
    nodes = [_hypothesis("p"), _hypothesis("c", parent="p")]
    judgments = [
        _judgment("p", HypothesisStatus.REJECTED, 0.1, reasoning="Parent disproved"),
        _judgment("c", HypothesisStatus.CONFIRMED, 0.99, evidence=["Child support"]),
    ]
    state = ValidationLoopState(hypotheses=nodes, all_judgments=judgments)
    pipeline = PipelineOrchestrator(MagicMock())
    trace = MagicMock()
    pipeline._apply_judgments(state, trace)
    run = MagicMock(trace=trace)
    gate = MagicMock(expansion_blocked=False)
    replacement = [_hypothesis(f"new-{i}") for i in range(3)]
    with patch(
        "rca_agent.services.pipeline.run_hypothesis_generation",
        return_value=MagicMock(hypotheses=replacement),
    ) as generate:
        action = pipeline._maybe_regenerate(state, MagicMock(), MagicMock(all_rejected=False), gate, run, MagicMock())
    generate.assert_called_once()
    assert action.name == "CONTINUE"
    assert state.regeneration_count == 1
    assert state.hypotheses == replacement
    assert judgments[1].status == HypothesisStatus.CONFIRMED
    assert "No direct rejection judgment recorded" in "\n".join(state.rejection_feedback.values())
