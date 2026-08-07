import json
import subprocess
import uuid
from unittest.mock import Mock

import pytest

from cc_headless import execution_mcp_server, retrospective_mcp_server
from cc_headless.services import execution_workspace
from cc_headless.services.execution_workspace import (
    APPROVED_STEP_IDS_ENV,
    EXECUTION_TOKEN_ENV,
    ExecutionWorkspace,
)


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    token = uuid.uuid4().hex
    monkeypatch.setattr(execution_workspace, "_WORKSPACE_ROOT", tmp_path / "executions")
    monkeypatch.setenv(EXECUTION_TOKEN_ENV, token)
    monkeypatch.setenv(APPROVED_STEP_IDS_ENV, json.dumps(["step-1", "step-2"]))
    created = ExecutionWorkspace(execution_id="exec-1", token=token)
    created.prepare()
    yield created
    created.cleanup()


@pytest.fixture
def spawned(monkeypatch):
    """실행된 argv 를 붙잡는다. 게이트를 통과한 명령만 여기 도달해야 한다."""
    runs = Mock(return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr=""))
    monkeypatch.setattr(execution_mcp_server.subprocess, "run", runs)
    return runs


@pytest.mark.parametrize(
    "command",
    [
        "aws ecs delete-service --cluster demo --service api",
        "aws ec2 terminate-instances --instance-ids i-1",
        "aws iam delete-role --role-name demo",
        "aws organizations describe-account --account-id 1",
        "aws ecs describe-services && aws ecs delete-service",
        "kubectl delete pod demo",
    ],
)
def test_a_refused_command_is_never_spawned(workspace, spawned, command):
    result = json.loads(execution_mcp_server.run_playbook_command("step-1", command))

    assert result["ok"] is False
    assert result["blocked"] is True
    spawned.assert_not_called()


def test_a_refusal_is_recorded_in_the_evidence_with_its_reason(workspace, spawned):
    execution_mcp_server.run_playbook_command("step-1", "aws ecs delete-service --service api")

    records = workspace.read_records()

    assert records[0]["blocked"] is True
    assert "irreversible" in records[0]["block_reason"]
    assert records[0]["failure_class"] == "BLOCKED_DESTRUCTIVE"


def test_an_undecidable_refusal_is_distinguished_from_a_destructive_one(workspace, spawned):
    execution_mcp_server.run_playbook_command("step-1", "aws ecs describe-services | grep api")

    records = workspace.read_records()

    assert records[0]["failure_class"] == "BLOCKED_UNDECIDABLE"


def test_a_refusal_tells_the_agent_not_to_retry_or_work_around_it(workspace, spawned):
    result = json.loads(execution_mcp_server.run_playbook_command("step-1", "aws ecs delete-service --service api"))

    assert "manual action" in result["guidance"]
    assert "Do not retry" in result["guidance"]


def test_an_allowed_command_runs_as_argv_not_as_a_shell_string(workspace, spawned):
    execution_mcp_server.run_playbook_command(
        "step-1", "aws ecs update-service --cluster demo --service api --force-new-deployment"
    )

    argv = spawned.call_args.args[0]

    assert argv == [
        "aws",
        "ecs",
        "update-service",
        "--cluster",
        "demo",
        "--service",
        "api",
        "--force-new-deployment",
    ]


def test_a_command_without_a_step_id_is_rejected(workspace, spawned):
    result = json.loads(execution_mcp_server.run_playbook_command("", "aws ecs describe-services"))

    assert result["ok"] is False
    spawned.assert_not_called()


def test_an_undeclared_step_id_cannot_run_or_record_an_outcome(workspace, spawned):
    command = json.loads(
        execution_mcp_server.run_playbook_command("step-unknown", "aws ecs update-service --service api")
    )
    outcome = json.loads(
        execution_mcp_server.record_step_outcome("step-unknown", "healthy", "healthy", criteria_met=True)
    )

    assert command["ok"] is False
    assert outcome["ok"] is False
    assert workspace.read_records() == []
    spawned.assert_not_called()


def test_credentials_in_a_recorded_command_are_redacted(workspace, spawned):
    execution_mcp_server.run_playbook_command(
        "step-1", "aws rds modify-db-instance --db-instance-identifier demo --master-user-password hunter2"
    )

    records = workspace.read_records()

    assert "hunter2" not in records[0]["command"]


def test_a_failing_command_is_classified_for_the_retrospective(workspace, monkeypatch):
    monkeypatch.setattr(
        execution_mcp_server.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=254,
                stdout="",
                stderr="An error occurred (AccessDeniedException): not authorized to perform ecs:UpdateService",
            )
        ),
    )

    result = json.loads(execution_mcp_server.run_playbook_command("step-1", "aws ecs update-service --service api"))

    assert result["ok"] is False
    assert result["failure_class"] == "PERMISSION_DENIED"
    assert workspace.read_records()[0]["failure_class"] == "PERMISSION_DENIED"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("ThrottlingException: Rate exceeded", "THROTTLED"),
        ("ValidationError: invalid parameter value", "INVALID_ARGUMENT"),
        ("ServiceNotFoundException: service does not exist", "TARGET_NOT_FOUND"),
        ("InvalidStateException: not in a valid state", "MISSING_PRECONDITION"),
        ("something nobody has seen before", "UNKNOWN"),
    ],
)
def test_failure_classification_separates_procedure_defects_from_transient_errors(
    workspace, monkeypatch, stderr, expected
):
    monkeypatch.setattr(
        execution_mcp_server.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)),
    )

    result = json.loads(execution_mcp_server.run_playbook_command("step-1", "aws ecs update-service --service api"))

    assert result["failure_class"] == expected


def test_a_step_outcome_and_resolution_are_recorded(workspace):
    execution_mcp_server.record_step_outcome(
        "step-1",
        "DatabaseConnections 20 이하",
        "DatabaseConnections 12",
        criteria_met=True,
    )
    execution_mcp_server.record_resolution("증상 지표 정상", resolved=True)

    records = workspace.read_records()

    assert records[0]["type"] == "step_outcome"
    assert records[0]["criteria_met"] is True
    assert records[1]["type"] == "resolution"
    assert records[1]["resolved"] is True


def test_an_unobservable_resolution_keeps_its_reason(workspace):
    execution_mcp_server.record_resolution("메트릭 조회 실패", resolved=False, unobservable_reason="지표 반영 지연")

    assert workspace.read_records()[0]["unobservable_reason"] == "지표 반영 지연"


def test_resolved_true_requires_a_nonblank_observation(workspace):
    result = json.loads(execution_mcp_server.record_resolution("  ", resolved=True))

    assert result["ok"] is False
    assert workspace.read_records() == []


def test_tools_refuse_to_act_without_an_execution_context(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_workspace, "_WORKSPACE_ROOT", tmp_path / "executions")
    monkeypatch.delenv(EXECUTION_TOKEN_ENV, raising=False)

    outcome = json.loads(execution_mcp_server.record_step_outcome("step-1", "c", "o", criteria_met=True))
    resolution = json.loads(execution_mcp_server.record_resolution("o", resolved=True))

    assert outcome["ok"] is False
    assert resolution["ok"] is False


def test_a_retrospective_update_needs_a_rationale(workspace):
    result = json.loads(retrospective_mcp_server.save_playbook_update('{"symptom_pattern": "x"}', ""))

    assert result["ok"] is False


def test_a_retrospective_step_without_a_step_id_is_rejected(workspace):
    result = json.loads(
        retrospective_mcp_server.save_playbook_update(
            json.dumps({"execution_steps": [{"action": "무언가"}]}),
            "근거",
        )
    )

    assert result["ok"] is False
    assert "step_id" in result["error"]


def test_a_retrospective_step_with_unknown_fields_is_rejected(workspace):
    result = json.loads(
        retrospective_mcp_server.save_playbook_update(
            json.dumps({"execution_steps": [{"step_id": "step-1", "command": "aws ecs delete-service"}]}),
            "근거",
        )
    )

    assert result["ok"] is False
    assert "command" in result["error"]


def test_a_valid_retrospective_update_is_saved_with_its_rationale(workspace):
    result = json.loads(
        retrospective_mcp_server.save_playbook_update(
            json.dumps({"execution_steps": [{"step_id": "step-1", "action": "재배포 후 30초 대기"}]}),
            "첫 시도가 대기 없이 지표를 조회해 실패했다",
        )
    )
    saved = workspace.read_retrospective()

    assert result["ok"] is True
    assert saved["update"]["execution_steps"][0]["action"] == "재배포 후 30초 대기"
    assert "대기 없이" in saved["rationale"]


def test_the_retrospective_tool_cannot_execute_anything(workspace):
    """회고는 증거를 읽고 갱신안을 쓸 뿐이므로 실행 도구를 갖지 않는다."""
    tool_names = {name for name in dir(retrospective_mcp_server) if not name.startswith("_")}

    assert "run_playbook_command" not in tool_names
