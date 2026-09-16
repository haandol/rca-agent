"""Public command/guard/wait regression with large histories; subprocesses are local doubles only."""

import hashlib
import json
import shlex
import subprocess

import pytest
from test_service_deployment import plan as original_plan
from test_service_deployment import runtime as original_runtime

from headless_codex import execution_mcp_server as server
from headless_codex.services.execution_evidence import MAX_OUTPUT_CHARS, redact
from headless_codex.services.execution_outcome import assemble_evidence


@pytest.fixture
def plan():
    """Keep the existing complete approval contract and all target guards."""
    return original_plan.__wrapped__()


@pytest.fixture
def runtime(plan, monkeypatch, tmp_path):
    """Reuse real public validation/workspace/guard code, with no AWS transport."""
    return original_runtime.__wrapped__(plan, monkeypatch, tmp_path)


def history():
    """Replicate a large service history without copying private live service output."""
    return [
        {"id": f"history-{n}", "message": "historical service event " * 20 + " password=EVENT_CANARY"}
        for n in range(100)
    ]


def transport(monkeypatch, ecs, stage=None, defect=None):
    """Change only subprocess response bytes; keep actual command parsing, gating and receipts."""
    outputs, calls = [], []

    def process(argv, budget):
        budget.remaining()
        calls.append(list(argv))
        response = ecs.execute(shlex.join(argv), budget)
        payload = json.loads(response["stdout"])
        operation = argv[2]
        if operation in {"describe-services", "update-service"}:
            service = payload["services"][0] if operation == "describe-services" else payload["service"]
            service["events"] = history()
            payload["safe_extension"] = {"preserved": True, "password": "ROOT_CANARY"}
            if operation == stage:
                if defect == "configuration":
                    service["deploymentConfiguration"]["minimumHealthyPercent"] = 99
                elif defect == "failures":
                    payload["failures"] = [{"arn": "missing", "reason": "MISSING"}]
                elif defect == "still_oversized":
                    service["control_extension"] = "control-data" * 3000
        if operation == stage and defect == "pagination":
            payload["nextToken"] = "PRESERVE_PAGE_TOKEN"
        raw = json.dumps(payload, ensure_ascii=False)
        if operation == stage and defect == "malformed":
            raw = raw[:-3]
        outputs.append((operation, raw))
        return subprocess.CompletedProcess(argv, 0, raw, "")

    monkeypatch.setattr(server, "_run_observation_process", process)
    return outputs, calls


def test_public_large_history_guard_write_convergence_and_assembled_audit(plan, runtime, monkeypatch):
    """Events alone cannot prevent a matching guarded write; all control and audit fields survive."""
    workspace, ecs = runtime
    raw, calls = transport(monkeypatch, ecs)
    command = plan["execution_steps"][0]["commands"][0]
    assert json.loads(server.run_playbook_command("rollback", command))["ok"]
    converged = json.loads(server.wait_for_service_deployment("converge"))
    assert converged["ok"]
    before = len(calls)
    assert (
        json.loads(server.wait_for_service_deployment("converge"))["first_converged_at"]
        == converged["first_converged_at"]
    )
    assert len(calls) == before
    assert [c for c in calls if c[2] == "update-service"] == [shlex.split(command)]
    records = workspace.read_records()
    attempts = [r for r in records if r.get("type") == "attempt"]
    write = next(r for r in attempts if "update-service" in r["command"])
    assert write["command"] == command and write["succeeded"]
    projected = [r for r in attempts if "ecs_control_projection" in r]
    assert projected and all(not r["stdout_truncated"] for r in projected)
    sources = {hashlib.sha256(redact(value).encode()).hexdigest(): value for _, value in raw}
    for row in projected:
        meta = row["ecs_control_projection"]
        source = sources[meta["redacted_source_sha256"]]
        assert meta["redacted_source_chars"] == len(redact(source)) > MAX_OUTPUT_CHARS
        expected = json.loads(redact(source))
        if "services" in expected:
            del expected["services"][0]["events"]
            assert meta["omitted_fields"] == ["/services/0/events"]
        else:
            del expected["service"]["events"]
            assert meta["omitted_fields"] == ["/service/events"]
        assert json.loads(row["stdout"]) == expected
        assert "CANARY" not in json.dumps(row)
    evidence = assemble_evidence(
        records, execution_id="exec-1", rca_id="rca", engine="headless-codex", playbook=plan
    ).to_dict()
    persisted = [a for step in evidence["steps"] for a in step["attempts"] if "ecs_control_projection" in a]
    assert [a["ecs_control_projection"] for a in persisted] == [r["ecs_control_projection"] for r in projected]
    assert not evidence["resolution_confirmed"]


@pytest.mark.parametrize("defect", ["configuration", "pagination", "failures", "still_oversized", "malformed"])
def test_public_precondition_still_rejects_incomplete_or_changed_control(plan, runtime, monkeypatch, defect):
    """Pagination uses the actual ListTasks shape; service failures use DescribeServices."""
    workspace, ecs = runtime
    stage = "list-tasks" if defect == "pagination" else "describe-services"
    _, calls = transport(monkeypatch, ecs, stage, defect)
    command = plan["execution_steps"][0]["commands"][0]
    assert not json.loads(server.run_playbook_command("rollback", command))["ok"]
    assert not [c for c in calls if c[2] == "update-service"]
    observed = next(r for r in workspace.read_records() if r.get("type") == "attempt" and stage in r.get("command", ""))
    if defect == "still_oversized":
        assert observed["stdout_truncated"] and observed["stdout_omitted_chars"] > 0
    if defect == "pagination":
        assert json.loads(observed["stdout"])["nextToken"] == "PRESERVE_PAGE_TOKEN"
    if defect == "failures":
        assert json.loads(observed["stdout"])["failures"]
    if defect == "malformed":
        assert "ecs_control_projection" not in observed


@pytest.mark.parametrize("defect", ["configuration", "still_oversized", "malformed"])
def test_public_write_acknowledgement_failure_remains_nonrepeatable(plan, runtime, monkeypatch, defect):
    """The UpdateService output model has only service; invalid acknowledgement cannot justify another write."""
    workspace, ecs = runtime
    _, calls = transport(monkeypatch, ecs, "update-service", defect)
    command = plan["execution_steps"][0]["commands"][0]
    assert not json.loads(server.run_playbook_command("rollback", command))["ok"]
    assert len([c for c in calls if c[2] == "update-service"]) == 1
    assert not json.loads(server.run_playbook_command("rollback", command))["ok"]
    assert len([c for c in calls if c[2] == "update-service"]) == 1
    record = next(
        r for r in workspace.read_records() if r.get("type") == "attempt" and "update-service" in r.get("command", "")
    )
    assert not record["succeeded"] and record["retry_forbidden"]
    if defect == "still_oversized":
        assert record["stdout_truncated"]


def test_ordinary_approved_read_keeps_preview_cap(plan, runtime, monkeypatch):
    """Projection must stay limited to server control reads and guarded writes."""
    workspace, ecs = runtime
    command = (
        f"aws ecs describe-services --cluster {ecs.guard['cluster']} "
        f"--services {ecs.guard['service']} --region us-east-1"
    )
    plan["execution_steps"].insert(
        0, {"step_id": "inspect", "action": "Inspect service", "success_criteria": "Read state", "commands": [command]}
    )
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name="observed-errors")
    service = ecs.service()
    service["events"] = history()
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, json.dumps({"services": [service]}), ""),
    )
    result = json.loads(server.run_playbook_command("inspect", command))
    assert result["ok"] and result["stdout_truncated"]
    assert "ecs_control_projection" not in result
    record = next(r for r in workspace.read_records() if r.get("type") == "attempt")
    assert record["command"] == command and record["stdout_retained_chars"] == MAX_OUTPUT_CHARS
