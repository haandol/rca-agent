"""Generated-only validation feeds the existing SDK correction path; historical domain reads stay permissive."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from rca_agent.agent_factory import create_playbook_agent
from rca_agent.ports.dto.models import ExecutionStep, Playbook
from rca_agent.services.playbook_gen import ExecutionStepOutput, PlaybookOutput
from rca_agent.services.runbook_contract import validate_runbook
from rca_agent.utils.agent_invocation import invoke_agent
from tests.test_agent_invocation import LocalStream
from tests.test_deployment_baseline import observed_plan

pytest_plugins = ["tests.test_incident_observation"]


def step():
    """A complete synthetic observation command, never executed by these tests."""
    return {
        "step_id": "observe",
        "action": "Read the target alarm",
        "success_criteria": "Alarm response observed",
        "commands": ["aws cloudwatch describe-alarms --alarm-names SECRET_COMMAND_VALUE --region us-east-1"],
    }


def output(steps):
    return {"failure_type": "test", "symptom_pattern": "test", "execution_steps": steps}


@pytest.mark.parametrize("field", ["step_id", "action", "success_criteria"])
@pytest.mark.parametrize("value", ["missing", "", "   "])
def test_required_generated_fields_fail_before_sdk_acceptance(field, value):
    candidate = step()
    if value == "missing":
        candidate.pop(field)
    else:
        candidate[field] = value
    with pytest.raises(ValidationError) as error:
        PlaybookOutput.model_validate(output([candidate]))
    details = json.dumps(error.value.errors(include_input=False, include_context=False))
    assert field in details
    assert "SECRET_COMMAND_VALUE" not in details


def test_generated_json_schema_advertises_required_step_fields():
    schema = ExecutionStepOutput.model_json_schema()
    assert {"step_id", "action", "success_criteria"} <= set(schema["required"])
    assert all("default" not in schema["properties"][k] for k in ["step_id", "action", "success_criteria"])


@pytest.mark.parametrize("mutation", ["duplicate", "no-operation", "two-operations"])
def test_whole_generated_runbook_validation_rejects_ambiguous_structure(mutation):
    steps = [step()]
    if mutation == "duplicate":
        steps.append(step())
    elif mutation == "no-operation":
        steps[0]["commands"] = []
    else:
        steps[0]["metric_wait"] = {}
    with pytest.raises(ValidationError):
        PlaybookOutput.model_validate(output(steps))


def test_generated_guarded_rollback_requires_recovery_chain(setup_observations):
    _, _, steps = observed_plan(setup_observations)
    assert len(PlaybookOutput.model_validate(output(steps)).execution_steps) == 3
    for invalid in (steps[:2], [steps[0], steps[2], steps[1]]):
        with pytest.raises(ValidationError):
            PlaybookOutput.model_validate(output(invalid))


def test_legacy_domain_read_and_empty_non_executable_output_remain_supported():
    historical = ExecutionStep(step_id="old")
    assert historical.action == historical.success_criteria == "" and historical.commands == []
    book = Playbook(playbook_id="old", failure_type="old", symptom_pattern="old", execution_steps=[historical])
    assert Playbook.model_validate(book.model_dump()).execution_steps[0].action == ""
    assert PlaybookOutput.model_validate(output([])).execution_steps == []


def test_runbook_diagnostics_identify_step_and_field_without_echoing_commands():
    first = step()
    second = deepcopy(first)
    second["step_id"] = "second"
    second["success_criteria"] = ""
    with pytest.raises(ValueError) as error:
        validate_runbook([first, second])
    assert "2" in str(error.value) and "success_criteria" in str(error.value)
    assert "SECRET_COMMAND_VALUE" not in str(error.value)
    second["success_criteria"] = "observed"
    second["step_id"] = first["step_id"]
    with pytest.raises(ValueError, match="2.*duplicate"):
        validate_runbook([first, second])


def test_actual_strands_sdk_corrects_missing_fields_without_an_application_retry_loop(monkeypatch):
    """Actual SDK tool validation returns safe field errors to the next model turn."""
    agent = create_playbook_agent()
    agent.callback_handler = lambda **_: None
    packets = []
    bad = output([{"commands": step()["commands"]}])
    good = output([step()])
    candidates = [bad, good]

    def transport(operation, request, context):
        assert operation.name == "ConverseStream"
        packet = json.loads(request["body"])
        packets.append(packet)
        tool = next(t["toolSpec"]["name"] for t in packet["toolConfig"]["tools"] if "toolSpec" in t)
        assert len(packets) <= 2, "existing SDK correction should finish with the second valid response"
        candidate = candidates[len(packets) - 1]
        events = [
            {"messageStart": {"role": "assistant"}},
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": f"output-{len(packets)}", "name": tool}},
                }
            },
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": json.dumps(candidate)}}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "tool_use"}},
            {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}},
        ]
        from types import SimpleNamespace

        return SimpleNamespace(status_code=200, headers={}), {"stream": LocalStream(events, delay=0)}

    monkeypatch.setattr(agent.model.client, "_make_request", transport)
    generated = invoke_agent(agent, "Produce a complete test runbook", PlaybookOutput, 10)
    assert len(generated.execution_steps) == 1 and len(packets) == 2
    validate_runbook([s.model_dump() for s in generated.execution_steps])
    errors = [
        block["toolResult"]
        for message in packets[1]["messages"]
        for block in message["content"]
        if "toolResult" in block and block["toolResult"].get("status") == "error"
    ]
    assert errors
    diagnostic = json.dumps(errors)
    assert all(field in diagnostic for field in ["execution_steps", "step_id", "action", "success_criteria"])
    assert "SECRET_COMMAND_VALUE" not in diagnostic
