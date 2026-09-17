"""Use actual Strands correction and local transport to verify safe recovery diagnostics."""

import json
import logging
from copy import deepcopy

import pytest

from rca_agent.services.analysis_roles import recovery_result
from rca_agent.services.recovery_evidence import prepare_recovery_evidence
from rca_agent.utils import agent_invocation as invocation
from rca_agent.utils.recovery_diagnostics import recovery_diagnostics
from tests.test_agent_invocation import Output, local_agent
from tests.test_deployment_baseline import observed_plan

pytest_plugins = ["tests.test_recovery_evidence"]
SECRET = "PRIVATE_CANDIDATE_COMMAND_TOKEN"


def events(caplog):
    """Read only the added diagnostic records, whose payloads exclude model and exception text."""
    return [
        json.loads(record.getMessage().removeprefix("recovery_diagnostic "))
        for record in caplog.records
        if record.name == "rca_agent.utils.recovery_diagnostics"
    ]


@pytest.mark.parametrize("invalid", ["missing", "target", "success_criteria"])
def test_actual_sdk_correction_reports_rejection_then_acceptance_without_input_leak(
    compatible, monkeypatch, caplog, invalid
):
    """A real structured tool rejection can correct in the same invocation with unchanged validators."""
    scoping, context, steps = observed_plan(compatible)
    wait = steps[-1]["metric_wait"]
    steps[-1]["success_criteria"] = (
        f"{wait['failure_alarm_name']} OK; {wait['metrics']['failures']['metric_name']} zero"
    )
    candidate = {
        "title": SECRET,
        "summary": SECRET,
        "reason": SECRET,
        "recommendation": "ROLLBACK",
        "playbook": {"failure_type": SECRET, "symptom_pattern": SECRET, "execution_steps": steps},
    }
    bad = deepcopy(candidate)
    if invalid == "missing":
        del bad["playbook"]["execution_steps"][0]["step_id"]
    elif invalid == "target":
        bad["playbook"]["execution_steps"][0]["ecs_service_precondition"]["expected_deployment_id"] = SECRET
    else:
        bad["playbook"]["execution_steps"][-1]["success_criteria"] = SECRET
    agent, packets, streams = local_agent(monkeypatch, delay=0, structured=True)
    transport = agent.model.client._make_request

    def responses(*args):
        """Replace only received provider bytes, keeping SDK validation and correction real."""
        response, parsed = transport(*args)
        choice = bad if len(packets) == 1 else candidate
        assert len(packets) <= 2, "unexpected extra SDK correction"
        parsed["stream"].events[2]["contentBlockDelta"]["delta"]["toolUse"]["input"] = json.dumps(choice)
        return response, parsed

    monkeypatch.setattr(agent.model.client, "_make_request", responses)
    prepared = prepare_recovery_evidence(scoping)
    with caplog.at_level(logging.INFO):
        result = recovery_result(
            "diagnostic-test",
            scoping,
            {**prepared["verification"], "valid": True, "rollback_context": context},
            agent,
            timeout_seconds=10,
        )
    rows = events(caplog)
    assert [row["request"] for row in rows if row["event"] == "model_request_admitted"] == [1, 2]
    assert sum(row["event"] == "output_rejected" for row in rows) == 1
    assert sum(row["event"] == "output_validated" for row in rows) == 1
    assert rows[-1]["event"] == "invocation_completed"
    assert len({row["diagnostic_id"] for row in rows}) == 1
    if invalid == "missing":
        rejected = next(row for row in rows if row["event"] == "output_rejected")
        assert rejected["errors"][0]["path"] == ["playbook", "execution_steps", 0, "step_id"]
    else:
        assert any(row.get("phase") == ("observed_plan" if invalid == "target" else "success_criteria") for row in rows)
    assert result["playbook"]["execution_steps"][0]["step_id"] == steps[0]["step_id"]
    assert SECRET not in caplog.text
    assert all(stream.ended.is_set() and not stream.worker.is_alive() for stream in streams)


def test_plain_response_then_denied_forced_request_is_distinct_from_validation_failure(monkeypatch, caplog):
    """A plain end_turn can exhaust admission without ever producing an invalid structured candidate."""
    agent, packets, streams = local_agent(monkeypatch, delay=0.03)
    with caplog.at_level(logging.INFO), recovery_diagnostics(), pytest.raises(Exception, match="budget exhausted"):
        invocation.invoke_agent(agent, SECRET, Output, 0.06)
    rows = events(caplog)
    assert any(row.get("stop_reason") == "end_turn" for row in rows)
    assert any(row["event"] == "model_request_rejected" and row["request"] == 2 for row in rows)
    assert not any(row["event"] == "output_rejected" for row in rows)
    assert len(packets) == 1 and streams[0].ended.is_set()
    assert SECRET not in caplog.text


def test_diagnostics_scope_does_not_leak_to_later_invocations(monkeypatch, caplog):
    """An unrelated role produces no recovery diagnostics after the scope resets."""
    agent, _, _ = local_agent(monkeypatch, delay=0, structured=True)
    with caplog.at_level(logging.INFO):
        with recovery_diagnostics():
            invocation.invoke_agent(agent, SECRET, Output, 2)
        before = len(events(caplog))
        invocation.invoke_agent(agent, SECRET, Output, 2)
    assert len(events(caplog)) == before


def test_failure_keeps_exception_chain_without_values_and_resets_scope(caplog):
    """A failed invocation logs causal types and frames, never sensitive exception values."""
    with pytest.raises(RuntimeError), caplog.at_level(logging.INFO), recovery_diagnostics(SECRET):
        try:
            raise invocation.WorkAdmissionError(SECRET)
        except invocation.WorkAdmissionError as error:
            raise RuntimeError(SECRET) from error
    rows = events(caplog)
    assert rows[0]["rca_id"] is None
    assert rows[-1]["event"] == "invocation_failed"
    assert "WorkAdmissionError" in caplog.text and "Traceback" in caplog.text
    assert SECRET not in caplog.text
