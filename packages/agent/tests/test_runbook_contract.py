"""Approval data must be complete and survive rendering/storage without inference."""

import json
from copy import deepcopy

import pytest

from rca_agent.services.runbook_contract import render_step_operation, validate_runbook


def command_step():
    return {
        "step_id": "stop-owner",
        "intent": "release lock",
        "action": "stop the observed owner",
        "success_criteria": "owner rollback recorded",
        "commands": ["aws ecs stop-task --cluster incident-cluster --task incident-owner --region us-east-1"],
    }


def wait_step():
    return {
        "step_id": "observe",
        "intent": "verify recovery",
        "action": "observe completed writes",
        "success_criteria": "Writes complete; Failures 0; failure-alarm OK in both post-action bins",
        "commands": [],
        "metric_wait": {
            "action_step_id": "stop-owner",
            "region": "us-east-1",
            "failure_alarm_name": "failure-alarm",
            "max_wait_seconds": 300,
            "metrics": {
                role: {
                    "namespace": "Observed/App",
                    "metric_name": metric,
                    "dimensions": {"Service": "current-service", "Operation": "ingest"},
                }
                for role, metric in (("attempts", "Attempts"), ("failures", "Failures"))
            },
        },
    }


def test_fixed_operations_preserve_exact_approval_values():
    steps = [command_step(), wait_step()]
    before = deepcopy(steps)
    validate_runbook(steps)
    for step in steps:
        rendered = "\n".join(render_step_operation(step))
        blocks = rendered.split("```json\n")[1:]
        assert json.loads(blocks[0].split("```", 1)[0]) == step["commands"]
        if step.get("metric_wait"):
            assert json.loads(blocks[1].split("```", 1)[0]) == step["metric_wait"]
    assert steps == before


@pytest.mark.parametrize(
    "command",
    [
        "aws ecs stop-task --region us-east-1",
        "aws ecs stop-task --cluster c --task t",
        "aws ecs stop-task --cluster c --task $TASK --region us-east-1",
        "aws ecs stop-task --cluster c --task <TASK> --region us-east-1",
        "aws ecs stop-task --cluster c --task t --region us-east-1 && echo done",
        "aws lambda update-function-configuration --region us-east-1",
        "aws cloudwatch get-metric-statistics --namespace App --region us-east-1",
    ],
)
def test_rejects_incomplete_or_unfixed_commands(command):
    with pytest.raises(ValueError):
        validate_runbook([{**command_step(), "commands": [command]}])


@pytest.mark.parametrize(
    "change",
    [
        {"commands": []},
        {"commands": None},
        {"commands": "aws ecs stop-task"},
        {"metric_wait": wait_step()["metric_wait"]},
    ],
)
def test_legacy_or_ambiguous_plan_is_not_new_executable_input(change):
    with pytest.raises(ValueError):
        validate_runbook([{**command_step(), **change}])


@pytest.mark.parametrize(
    "change",
    [
        {"action_step_id": "missing"},
        {"step_id": "observe"},
        {"max_wait_seconds": 901},
        {"max_wait_seconds": True},
        {"region": "$REGION"},
        {"metrics": {}},
        {"latency_alarm_name": "latency-without-metric"},
    ],
)
def test_rejects_incomplete_wait_or_changed_action_reference(change):
    wait = wait_step()
    wait["metric_wait"].update(change)
    with pytest.raises(ValueError):
        validate_runbook([command_step(), wait])


def test_new_plan_round_trips_through_strands_dto_store_and_report():
    from rca_agent.adapters.secondary.report.s3_report_store import _render_playbook_section
    from rca_agent.adapters.secondary.trace.dynamodb_trace_store import _deserialize_metadata, _serialize_metadata
    from rca_agent.ports.dto.models import ExecutionStep, Playbook
    from rca_agent.services.playbook_gen import ExecutionStepOutput, build_execution_steps

    steps = build_execution_steps([ExecutionStepOutput(**s) for s in [command_step(), wait_step()]], confirmed=True)
    metadata = {"execution_steps": [s.model_dump() for s in steps]}
    decoded = _deserialize_metadata(_serialize_metadata(metadata))
    # The published-detail adapter now reconstructs the public DTO directly.
    restored = Playbook.model_validate(
        {"playbook_id": "pb", "failure_type": "lock", "symptom_pattern": "blocked writes", **decoded}
    )
    assert restored.execution_steps == steps
    report = "\n".join(_render_playbook_section(restored))
    assert command_step()["commands"][0] in report
    assert json.dumps(wait_step()["metric_wait"], ensure_ascii=False, indent=2) in report
    legacy = [ExecutionStep.model_validate({k: v for k, v in command_step().items() if k != "commands"})]
    assert legacy[0].commands == []
    assert build_execution_steps([ExecutionStepOutput(**legacy[0].model_dump())], confirmed=True) == []


def test_trace_metadata_preserves_null_and_literal_none_as_distinct_values():
    from rca_agent.adapters.secondary.trace.dynamodb_trace_store import _deserialize_metadata, _serialize_metadata

    metadata = {"optional_operation": None, "source_text": "None", "values": [None, False, 0.0000001]}
    encoded = _serialize_metadata(metadata)
    assert encoded["optional_operation"] == {"NULL": True}
    assert encoded["source_text"] == {"S": "None"}
    assert _deserialize_metadata(encoded) == metadata


@pytest.mark.parametrize(
    "command",
    [
        "aws ecs describe-tasks --cluster c --tasks t --region us-east-1",
        "aws ecs update-service --cluster c --service s --force-new-deployment --region us-east-1",
        "aws ecs list-tasks --region us-east-1 --query 'stop-task'",
    ],
)
def test_wait_rejects_prior_step_without_stop_task(command):
    action = {**command_step(), "commands": [command]}
    with pytest.raises(ValueError, match="prior aws ecs stop-task"):
        validate_runbook([action, wait_step()])


@pytest.mark.parametrize("reference", ["observe", "later", "second-wait"])
def test_wait_rejects_self_future_or_prior_wait_reference(reference):
    first_wait = wait_step()
    second_wait = {**wait_step(), "step_id": "second-wait"}
    later = {**command_step(), "step_id": "later"}
    first_wait["metric_wait"]["action_step_id"] = reference
    steps = [command_step(), second_wait, first_wait, later]
    with pytest.raises(ValueError, match="prior aws ecs stop-task"):
        validate_runbook(steps)


@pytest.mark.parametrize(
    "command",
    [
        'aws ecs stop-task --cluster "incident-cluster" --task "incident-owner" --region "us-east-1"',
        "aws ecs stop-task --cluster=incident-cluster --task=incident-owner --region=us-east-1",
        'aws --region "us-east-1" ecs stop-task --cluster incident-cluster --task incident-owner',
        "aws --region=us-east-1 ecs stop-task --cluster incident-cluster --task incident-owner",
    ],
)
def test_wait_accepts_quoted_stop_task_and_region_forms_without_rewriting(command):
    action = {**command_step(), "commands": ["aws ecs list-tasks --region us-east-1", command]}
    steps = [action, wait_step()]
    before = deepcopy(steps)
    validate_runbook(steps)
    assert steps == before


@pytest.mark.parametrize("expression", ["$.event", "$.detail.event"])
def test_fixed_cloudwatch_jsonpath_filter_is_valid(expression):
    command = (
        "aws logs filter-log-events --log-group-name owner-logs --region us-east-1 "
        "--start-time 1750000000000 --end-time 1750000060000 "
        f"--filter-pattern '{{ {expression} = \"maintenance_released\" }}'"
    )
    steps = [{**command_step(), "commands": [command]}]
    before = deepcopy(steps)
    validate_runbook(steps)
    assert steps == before
    assert json.loads("\n".join(render_step_operation(steps[0])).split("```json\n")[1].split("```", 1)[0]) == [command]


@pytest.mark.parametrize("unfixed", ["$VAR", "${VAR}", "$(whoami)", "`whoami`", "{{owner}}", "<OWNER>"])
def test_jsonpath_filter_does_not_allow_substitutions_or_placeholders(unfixed):
    command = (
        "aws logs filter-log-events --log-group-name owner-logs --region us-east-1 "
        f"--filter-pattern '{{ $.event = \"{unfixed}\" }}'"
    )
    with pytest.raises(ValueError):
        validate_runbook([{**command_step(), "commands": [command]}])


@pytest.mark.parametrize(
    "pointer",
    ["/rollback_context/write_accounting", "/model_authored", "/playbook/rollback_context/write_accounting/", ""],
)
def test_wrong_approved_context_pointer_rejected_before_publication(pointer):
    """A model-written context shortcut must not survive runbook structural validation."""
    wait = wait_step()
    wait["metric_wait"]["completed_work_evidence"] = {"record_index": "approved_context", "json_pointer": pointer}
    with pytest.raises(ValueError):
        validate_runbook([command_step(), wait])


@pytest.mark.parametrize(
    "index,pointer",
    [
        ("approved_context", "/playbook/rollback_context/write_accounting"),
        (0, "/events/0/message/accounting"),
        (3, "/rollback_context/write_accounting"),
        (100, "/custom~1key"),
    ],
)
def test_completed_work_reference_preserves_numeric_record_pointer_semantics(index, pointer):
    """Only the named context pointer is fixed; numeric record existence is a runtime check."""
    wait = wait_step()
    reference = {"record_index": index, "json_pointer": pointer}
    wait["metric_wait"]["completed_work_evidence"] = reference.copy()
    validate_runbook([command_step(), wait])
    assert wait["metric_wait"]["completed_work_evidence"] == reference


@pytest.mark.parametrize("duration", [1, 300, 900])
def test_approved_metric_wait_bound_and_default_are_900(duration):
    """Retain explicit older budgets while admitting the new boundary and resolving omission consistently."""
    wait = wait_step()
    wait["metric_wait"]["max_wait_seconds"] = duration
    validate_runbook([command_step(), wait])
    del wait["metric_wait"]["max_wait_seconds"]
    validate_runbook([command_step(), wait])
