"""Approval data must be complete and survive rendering/storage without inference."""

import json
from copy import deepcopy

import pytest

from headless_codex.services.runbook_contract import render_step_operation, validate_runbook


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


def test_watcher_and_server_report_preserve_nested_wait_and_long_commands():
    from boto3.dynamodb.types import TypeDeserializer

    from headless_codex.services.artifact_validation import _render_playbook
    from headless_codex.services.artifact_watcher import _build_playbook_metadata

    step = command_step()
    step["commands"][0] += " --reason '" + "observed owner " * 80 + "'"
    steps = [step, wait_step()]
    artifact = {"playbook_id": "pb", "verification_status": "DRAFT", "execution_steps": steps}
    metadata = _build_playbook_metadata(artifact)
    decoded = TypeDeserializer().deserialize(metadata["execution_steps"])
    assert decoded[0]["commands"] == step["commands"]
    assert decoded[1]["metric_wait"] == steps[1]["metric_wait"]
    report = _render_playbook(artifact, confirmed=True)
    assert step["commands"][0] in report
    assert json.dumps(steps[1]["metric_wait"], indent=2) in report


def test_retrospective_operation_changes_are_recorded_without_mutating_snapshot():
    from headless_codex.services.playbook_merge import merge_playbook_update

    original = {
        "playbook_id": "pb",
        "verification_status": "VERIFIED",
        "execution_steps": [command_step(), wait_step()],
    }
    snapshot = deepcopy(original)
    update = {
        "execution_steps": [
            {
                "step_id": "stop-owner",
                "commands": [
                    "aws ecs stop-task --cluster incident-cluster --task corrected-owner --region us-east-1",
                ],
            }
        ]
    }
    result, diff = merge_playbook_update(original, update)
    assert original == snapshot
    assert result["execution_steps"][0]["commands"] == update["execution_steps"][0]["commands"]
    assert diff.corrected_steps[0]["changes"]["commands"]
    assert result["execution_steps"][1] == snapshot["execution_steps"][1]


def test_retrospective_switches_operation_without_leaving_old_commands():
    from headless_codex.services.playbook_merge import merge_playbook_update

    original = {"execution_steps": [command_step(), {**command_step(), "step_id": "observe"}]}
    result, diff = merge_playbook_update(original, {"execution_steps": [wait_step()]})
    assert result["execution_steps"][1]["commands"] == []
    assert result["execution_steps"][1]["metric_wait"] == wait_step()["metric_wait"]
    assert diff.corrected_steps


def test_engine_completeness_contracts_are_in_parity():
    from pathlib import Path

    import headless_codex.services.runbook_contract as contract

    other = Path(__file__).resolve().parents[2] / "agent/src/rca_agent/services/runbook_contract.py"
    assert other.read_text() == Path(contract.__file__).read_text()


@pytest.mark.parametrize("change", ["commands", "metric_wait"])
def test_retrospective_publishes_changed_operation_as_draft(change):
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    from headless_codex.services.execution_pipeline import ExecutionOrchestrator

    original = {
        "playbook_id": "pb",
        "verification_status": "VERIFIED",
        "execution_steps": [command_step(), wait_step()],
    }
    if change == "commands":
        updated = command_step()
        updated["commands"] = [updated["commands"][0].replace("incident-owner", "corrected-owner")]
    else:
        updated = wait_step()
        updated["metric_wait"]["max_wait_seconds"] = 240
    saved = {"update": {"execution_steps": [updated]}, "rationale": "Recorded observation identifies this correction"}
    container = SimpleNamespace(
        execution_store=Mock(),
        evidence_store=Mock(),
        execution_runner=Mock(),
    )
    container.execution_store.claim_retrospective.return_value = True
    container.evidence_store.save_retrospective_diff.return_value = "diff.json"
    container.execution_runner.run_retrospective.return_value = SimpleNamespace(success=True)
    runner = ExecutionOrchestrator(container)
    runner._publish_playbook = Mock()
    before = deepcopy(original)
    with (
        patch("headless_codex.services.execution_pipeline.build_retrospective_prompt", return_value="review"),
        patch("headless_codex.services.retrospective_reader.write_reference"),
        patch("headless_codex.services.retrospective_reader.require_successful_reads"),
    ):
        runner._retrospect(
            "exec",
            SimpleNamespace(rca_id="rca", playbook_digest="a" * 64),
            SimpleNamespace(playbook=original),
            "claim",
            SimpleNamespace(token="token", read_retrospective=lambda: saved),
            "snapshot.json",
            Mock(),
        )
    published = runner._publish_playbook.call_args.args[2]
    assert published["verification_status"] == "DRAFT"
    index = 0 if change == "commands" else 1
    assert published["execution_steps"][index][change] == updated[change]
    assert original == before
    assert container.execution_store.record_retrospective.call_args.kwargs["status"] == "UPDATED"


def test_retrospective_save_preserves_operations_and_rejects_ambiguous_updates(
    monkeypatch, tmp_path, retrospective_reads
):
    from headless_codex import retrospective_mcp_server

    path = tmp_path / "retrospective.json"
    monkeypatch.setattr(retrospective_mcp_server, "_target_path", lambda: path)
    update = {"execution_steps": [command_step(), wait_step()]}
    result = json.loads(retrospective_mcp_server.save_playbook_update(json.dumps(update), "observed source"))
    assert result["ok"]
    assert json.loads(path.read_text())["update"] == update
    original = path.read_bytes()
    update["execution_steps"][0]["metric_wait"] = wait_step()["metric_wait"]
    rejected = json.loads(retrospective_mcp_server.save_playbook_update(json.dumps(update), "invalid operation"))
    assert not rejected["ok"]
    assert path.read_bytes() == original


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
    from headless_codex.services.execution_contract import approved_wait

    assert approved_wait(wait)["max_wait_seconds"] == 900
