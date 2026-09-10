import json
import subprocess
import uuid
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastmcp.exceptions import ValidationError

from headless_codex import execution_mcp_server, retrospective_mcp_server
from headless_codex.services import execution_workspace
from headless_codex.services.execution_outcome import assemble_evidence, judge_resolution
from headless_codex.services.execution_state import ExecutionState
from headless_codex.services.execution_workspace import (
    APPROVED_STEP_IDS_ENV,
    APPROVED_SUCCESS_CRITERIA_ENV,
    EXECUTION_TOKEN_ENV,
    ExecutionWorkspace,
)


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    token = uuid.uuid4().hex
    monkeypatch.setattr(execution_workspace, "_WORKSPACE_ROOT", tmp_path / "executions")
    monkeypatch.setenv(EXECUTION_TOKEN_ENV, token)
    monkeypatch.setenv(APPROVED_STEP_IDS_ENV, json.dumps(["step-1"]))
    monkeypatch.setenv(
        APPROVED_SUCCESS_CRITERIA_ENV,
        json.dumps({"step-1": "DatabaseConnections 20 이하"}),
    )
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


def test_waiter_uses_existing_command_timeout_and_cannot_alone_resolve(workspace, spawned, monkeypatch):
    """A successful waiter remains one attempt and cannot replace fresh outcome observations."""
    monkeypatch.setattr(execution_mcp_server, "_COMMAND_TIMEOUT_SECONDS", 17)
    command = "aws cloudwatch wait alarm-exists --alarm-names observed --state-value OK --region us-east-1"
    result = json.loads(execution_mcp_server.run_playbook_command("step-1", command))
    assert result["ok"]
    assert spawned.call_count == 1
    assert spawned.call_args.args[0] == command.split()
    assert spawned.call_args.kwargs["timeout"] == 17
    resolution = json.loads(execution_mcp_server.record_resolution("waiter returned OK", resolved=True))
    assert not resolution["ok"]
    assert [record["type"] for record in workspace.read_records()] == ["attempt"]


def test_waiter_timeout_keeps_existing_timeout_evidence(workspace, spawned, monkeypatch):
    """A read-only wait does not extend the command budget or turn timeout into recovery."""
    monkeypatch.setattr(execution_mcp_server, "_COMMAND_TIMEOUT_SECONDS", 17)
    spawned.side_effect = subprocess.TimeoutExpired(cmd="aws cloudwatch wait", timeout=17)
    result = json.loads(
        execution_mcp_server.run_playbook_command(
            "step-1", "aws cloudwatch wait alarm-exists --alarm-names observed --state-value OK"
        )
    )
    assert not result["ok"]
    assert spawned.call_count == 1
    assert workspace.read_records()[0]["failure_class"] == "TIMEOUT"
    assert workspace.read_records()[0]["succeeded"] is False


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


def test_a_verification_only_step_succeeds_after_a_read_only_cli_attempt(workspace, spawned):
    attempt = json.loads(
        execution_mcp_server.run_playbook_command(
            "step-1",
            "aws cloudwatch describe-alarms --alarm-names VitalIngestFailure",
            "verify ingest recovery",
        )
    )
    outcome = json.loads(
        execution_mcp_server.record_step_outcome(
            "step-1",
            "DatabaseConnections 20 이하",
            "DatabaseConnections 12",
            criteria_met=True,
        )
    )
    resolution = json.loads(execution_mcp_server.record_resolution("증상 지표 정상", resolved=True))

    records = workspace.read_records()

    assert attempt["ok"] is True
    assert outcome["ok"] is True
    assert resolution["ok"] is True
    assert [record["type"] for record in records] == ["attempt", "step_outcome", "resolution"]
    assert records[0]["arguments"] == {"service": "cloudwatch", "operation": "describe-alarms"}
    assert records[1]["criteria_met"] is True
    assert records[2]["resolved"] is True


def test_long_approved_criterion_survives_public_recording_replay_and_final_judgment(workspace, spawned, monkeypatch):
    criterion = (
        " \n" + "The recorded observation must satisfy this approved requirement. " * 90 + "\nTAIL: failures = 0 "
    )
    assert len(criterion) > 4000
    monkeypatch.setenv(APPROVED_SUCCESS_CRITERIA_ENV, json.dumps({"step-1": criterion}))
    execution_mcp_server.run_playbook_command("step-1", "aws cloudwatch describe-alarms")
    before = workspace.read_records()
    for incorrect in (criterion[:4000], criterion.replace("TAIL: failures = 0", "TAIL: failures <= 10")):
        rejected = json.loads(
            execution_mcp_server.record_step_outcome("step-1", incorrect, "Observed", criteria_met=True)
        )
        assert rejected["ok"] is False
        assert workspace.read_records() == before
    outcome = json.loads(execution_mcp_server.record_step_outcome("step-1", criterion, "Observed", criteria_met=True))
    resolution = json.loads(execution_mcp_server.record_resolution("Recovery observed", resolved=True))
    assert outcome["ok"] is True
    assert resolution["ok"] is True, resolution

    records = workspace.read_records()
    assert records[1]["success_criteria"] == criterion
    evidence = assemble_evidence(
        records,
        execution_id="exec-1",
        rca_id="rca-1",
        engine="headless-codex",
        playbook={"execution_steps": [{"step_id": "step-1", "success_criteria": criterion}]},
    )
    assert evidence.step("step-1").success_criteria == criterion
    assert evidence.to_dict()["steps"][0]["success_criteria"] == criterion
    assert judge_resolution(evidence, agent_succeeded=True).state is ExecutionState.RESOLVED


def test_long_step_ids_with_shared_prefix_remain_distinct_required_identities(workspace, spawned, monkeypatch):
    prefix = "step-" + "x" * 195
    first, second = prefix + "-first", prefix + "-second"
    criteria = {first: "Alarm state OK", second: "Failures 0"}
    playbook_id = "playbook-" + "p" * 220
    playbook = {
        "playbook_id": playbook_id,
        "execution_steps": [
            {"step_id": identifier, "success_criteria": criterion} for identifier, criterion in criteria.items()
        ],
    }
    monkeypatch.setenv(APPROVED_STEP_IDS_ENV, json.dumps([first, second]))
    monkeypatch.setenv(APPROVED_SUCCESS_CRITERIA_ENV, json.dumps(criteria))

    def assembled(records):
        return assemble_evidence(
            records, execution_id="exec-1", rca_id="rca-1", engine="headless-codex", playbook=playbook
        )

    assert json.loads(execution_mcp_server.run_playbook_command(first, "aws cloudwatch describe-alarms"))["ok"]
    outcome = json.loads(
        execution_mcp_server.record_step_outcome(first, criteria[first], "Observed first criterion", criteria_met=True)
    )
    assert outcome["ok"] is True, outcome
    premature = json.loads(execution_mcp_server.record_resolution("Recovered", resolved=True))
    assert premature["ok"] is False
    assert premature["missing_attempt_step_ids"] == [second]
    assert premature["missing_outcome_step_ids"] == [second]

    # A replayed positive model claim cannot collapse the missing second step into the first.
    incomplete = assembled(
        [*workspace.read_records(), {"type": "resolution", "observation": "Recovered", "resolved": True}]
    )
    assert incomplete.playbook_id == playbook_id
    assert [step.step_id for step in incomplete.steps] == [first, second]
    assert [len(step.attempts) for step in incomplete.steps] == [1, 0]
    assert judge_resolution(incomplete, agent_succeeded=True).state is ExecutionState.UNRESOLVED

    assert json.loads(execution_mcp_server.run_playbook_command(second, "aws cloudwatch get-metric-data"))["ok"]
    assert json.loads(
        execution_mcp_server.record_step_outcome(
            second, criteria[second], "Observed second criterion", criteria_met=True
        )
    )["ok"]
    assert json.loads(execution_mcp_server.record_resolution("Recovered", resolved=True))["ok"]

    complete = assembled(workspace.read_records())
    assert complete.playbook_id == playbook_id
    assert [step.step_id for step in complete.steps] == [first, second]
    assert [step.success_criteria for step in complete.steps] == list(criteria.values())
    for step in complete.steps:
        assert len(step.attempts) == 1
        assert step.attempts[0].step_id == step.step_id
        assert step.attempts[0].attempt_index == 1
        assert step.outcomes[0]["step_id"] == step.step_id
    assert judge_resolution(complete, agent_succeeded=True).state is ExecutionState.RESOLVED


@pytest.mark.parametrize("contradiction", ["failed_stop", "blocked", "manual"])
@pytest.mark.parametrize("recording", ["step_outcome", "resolution"])
def test_positive_recording_refuses_execution_contradictions(workspace, spawned, contradiction, recording):
    if contradiction == "failed_stop":
        spawned.return_value = subprocess.CompletedProcess(
            args=[], returncode=254, stdout="", stderr="AccessDenied: not authorized to perform ecs:StopTask"
        )
    command = (
        "aws ecs delete-service --service api"
        if contradiction == "blocked"
        else "aws ecs stop-task --cluster demo --task owner"
    )
    execution_mcp_server.run_playbook_command("step-1", command)
    if recording == "resolution":
        # Replay a previously accepted contradictory record, bypassing the public step guard.
        execution_mcp_server._append_record(
            {
                "type": "step_outcome",
                "step_id": "step-1",
                "success_criteria": "DatabaseConnections 20 이하",
                "observation": "DatabaseConnections 12",
                "criteria_met": True,
                "manual_action_required": contradiction == "manual",
            }
        )
    before = workspace.read_records()

    if recording == "step_outcome":
        result = json.loads(
            execution_mcp_server.record_step_outcome(
                "step-1",
                "DatabaseConnections 20 이하",
                "DatabaseConnections 12",
                criteria_met=True,
                manual_action_required=contradiction == "manual",
            )
        )
    else:
        result = json.loads(execution_mcp_server.record_resolution("Recovered", resolved=True))

    assert result["ok"] is False
    assert workspace.read_records() == before


def test_failed_read_can_be_retried_and_revalidated_without_poisoning_the_step(workspace, spawned):
    spawned.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=254, stdout="", stderr="ThrottlingException"),
        subprocess.CompletedProcess(args=[], returncode=0, stdout='{"healthy":true}', stderr=""),
    ]
    command = "aws rds describe-db-instances"
    assert not json.loads(execution_mcp_server.run_playbook_command("step-1", command))["ok"]
    assert not json.loads(
        execution_mcp_server.record_step_outcome(
            "step-1", "DatabaseConnections 20 이하", "Model claims success", criteria_met=True
        )
    )["ok"]
    assert json.loads(
        execution_mcp_server.record_step_outcome(
            "step-1", "DatabaseConnections 20 이하", "Read throttled", criteria_met=False, failure_class="THROTTLED"
        )
    )["ok"]
    assert json.loads(execution_mcp_server.run_playbook_command("step-1", command))["ok"]
    assert json.loads(
        execution_mcp_server.record_step_outcome(
            "step-1", "DatabaseConnections 20 이하", "DatabaseConnections 12", criteria_met=True
        )
    )["ok"]
    assert json.loads(execution_mcp_server.record_resolution("Recovered", resolved=True))["ok"]
    assert [r["type"] for r in workspace.read_records()] == [
        "attempt",
        "step_outcome",
        "attempt",
        "step_outcome",
        "resolution",
    ]


@pytest.mark.parametrize("flag", ["criteria_met", "manual_action_required", "resolved"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_public_recording_requires_real_booleans(workspace, spawned, flag, value):
    execution_mcp_server.run_playbook_command("step-1", "aws rds describe-db-instances")
    if flag == "resolved":
        execution_mcp_server.record_step_outcome(
            "step-1", "DatabaseConnections 20 이하", "DatabaseConnections 12", criteria_met=True
        )
    before = workspace.read_records()
    if flag == "resolved":
        result = json.loads(execution_mcp_server.record_resolution("Recovered", resolved=value))
    else:
        arguments = {
            "step_id": "step-1",
            "success_criteria": "DatabaseConnections 20 이하",
            "observation": "DatabaseConnections 12",
            "criteria_met": True,
            "manual_action_required": False,
        }
        arguments[flag] = value
        result = json.loads(execution_mcp_server.record_step_outcome(**arguments))

    assert result["ok"] is False
    assert workspace.read_records() == before


@pytest.mark.parametrize("flag", ["criteria_met", "manual_action_required", "resolved"])
@pytest.mark.parametrize("value", ["true", 1])
@pytest.mark.asyncio
async def test_mcp_validation_does_not_coerce_nonboolean_flags_before_recording(workspace, flag, value):
    if flag == "resolved":
        tool = "record_resolution"
        arguments = {"observation": "Recovered", "resolved": value}
    else:
        tool = "record_step_outcome"
        arguments = {
            "step_id": "step-1",
            "success_criteria": "DatabaseConnections 20 이하",
            "observation": "DatabaseConnections 12",
            "criteria_met": True,
            "manual_action_required": False,
        }
        arguments[flag] = value
    with pytest.raises(ValidationError, match="bool"):
        await execution_mcp_server.mcp.call_tool(tool, arguments)
    assert workspace.read_records() == []


@pytest.mark.parametrize("criteria_met", [False, "true"])
def test_resolution_rechecks_replayed_outcome_flags(workspace, spawned, criteria_met):
    execution_mcp_server.run_playbook_command("step-1", "aws rds describe-db-instances")
    execution_mcp_server._append_record(
        {
            "type": "step_outcome",
            "step_id": "step-1",
            "success_criteria": "DatabaseConnections 20 이하",
            "observation": "DatabaseConnections 12",
            "criteria_met": criteria_met,
        }
    )
    before = workspace.read_records()

    assert not json.loads(execution_mcp_server.record_resolution("Recovered", resolved=True))["ok"]
    assert workspace.read_records() == before


def test_resolution_rejects_outcome_replayed_before_attempt(workspace, spawned):
    execution_mcp_server._append_record(
        {
            "type": "step_outcome",
            "step_id": "step-1",
            "success_criteria": "DatabaseConnections 20 이하",
            "observation": "DatabaseConnections 12",
            "criteria_met": True,
        }
    )
    execution_mcp_server.run_playbook_command("step-1", "aws rds describe-db-instances")
    assert not json.loads(execution_mcp_server.record_resolution("Recovered", resolved=True))["ok"]


def test_positive_outcomes_require_nonblank_observations_and_no_unobservable_claim(workspace, spawned):
    execution_mcp_server.run_playbook_command("step-1", "aws rds describe-db-instances")
    assert not json.loads(
        execution_mcp_server.record_step_outcome("step-1", "DatabaseConnections 20 이하", " ", criteria_met=True)
    )["ok"]
    assert json.loads(
        execution_mcp_server.record_step_outcome(
            "step-1", "DatabaseConnections 20 이하", "DatabaseConnections 12", criteria_met=True
        )
    )["ok"]
    assert not json.loads(
        execution_mcp_server.record_resolution("Recovered", resolved=True, unobservable_reason="cannotverify")
    )["ok"]
    assert [r["type"] for r in workspace.read_records()] == ["attempt", "step_outcome"]


def test_a_verification_only_outcome_without_an_attempt_is_rejected(workspace):
    result = json.loads(
        execution_mcp_server.record_step_outcome(
            "step-1",
            "DatabaseConnections 20 이하",
            "DatabaseConnections 12",
            criteria_met=True,
        )
    )

    assert result["ok"] is False
    assert result["missing_attempt_step_ids"] == ["step-1"]
    assert "preceding run_playbook_command attempt" in result["error"]
    assert workspace.read_records() == []


def test_resolved_true_reports_every_approved_step_missing_attempt_or_outcome(
    workspace,
    spawned,
    monkeypatch,
):
    monkeypatch.setenv(APPROVED_STEP_IDS_ENV, json.dumps(["step-1", "step-2", "step-3"]))
    monkeypatch.setenv(
        APPROVED_SUCCESS_CRITERIA_ENV,
        json.dumps(
            {
                "step-1": "DatabaseConnections 20 이하",
                "step-2": "VitalIngestFailure 0",
                "step-3": "Alarm state OK",
            }
        ),
    )
    execution_mcp_server.run_playbook_command("step-1", "aws rds describe-db-instances")
    execution_mcp_server.record_step_outcome(
        "step-1",
        "DatabaseConnections 20 이하",
        "DatabaseConnections 12",
        criteria_met=True,
    )
    execution_mcp_server.run_playbook_command(
        "step-3",
        "aws cloudwatch describe-alarms --alarm-names VitalIngestFailure",
    )

    result = json.loads(execution_mcp_server.record_resolution("증상 지표 정상", resolved=True))

    assert result["ok"] is False
    assert result["missing_attempt_step_ids"] == ["step-2"]
    assert result["missing_outcome_step_ids"] == ["step-2", "step-3"]
    assert not any(record["type"] == "resolution" for record in workspace.read_records())


def test_a_step_outcome_must_use_the_exact_approved_success_criteria(workspace, spawned):
    execution_mcp_server.run_playbook_command("step-1", "aws rds describe-db-instances")

    result = json.loads(
        execution_mcp_server.record_step_outcome(
            "step-1",
            "DatabaseConnections 30 이하",
            "DatabaseConnections 12",
            criteria_met=True,
        )
    )

    assert result["ok"] is False
    assert "exactly match" in result["error"]
    assert [record["type"] for record in workspace.read_records()] == ["attempt"]


def test_an_unobservable_resolution_keeps_its_reason(workspace):
    result = json.loads(
        execution_mcp_server.record_resolution(
            "메트릭 조회 실패",
            resolved=False,
            unobservable_reason="지표 반영 지연",
        )
    )

    assert result["ok"] is True
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


def test_command_output_tail_and_actual_server_boundaries_survive_assembly(workspace, spawned):
    """A large task JSON keeps tail identity and server times rather than assembly-time substitutes."""
    stdout = json.dumps(
        {"padding": "x" * 9000, "tasks": [{"taskArn": "task/customer-approved-stop", "lastStatus": "STOPPED"}]}
    )
    inside_run = []

    def complete(*args, **kwargs):
        """Observe actual runner boundaries without AWS, sleep, or model execution."""
        inside_run.append(datetime.now(UTC))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="nonfatal warning")

    spawned.side_effect = complete
    before = datetime.now(UTC)
    result = json.loads(execution_mcp_server.run_playbook_command("step-1", "aws ecs describe-tasks --tasks target"))
    after = datetime.now(UTC)
    record = workspace.read_records()[0]
    assert before <= datetime.fromisoformat(record["started_at"]) <= inside_run[0]
    assert inside_run[0] <= datetime.fromisoformat(record["ended_at"])
    assert datetime.fromisoformat(record["ended_at"]) <= datetime.fromisoformat(record["recorded_at"]) <= after
    assert result["stdout"] == record["stdout"] == stdout
    assert record["stdout_truncated"] is False
    assert record["stdout_chars"] == record["stdout_retained_chars"] == len(stdout)
    assert record["stdout_omitted_chars"] == 0
    assert record["observation_truncated"] is True
    assert len(record["observation"]) == 2000

    evidence = assemble_evidence(
        workspace.read_records(),
        execution_id="exec-1",
        rca_id="rca-1",
        engine="headless-codex",
        playbook={"execution_steps": [{"step_id": "step-1"}]},
    )
    attempt = evidence.to_dict()["steps"][0]["attempts"][0]
    assert json.loads(attempt["stdout"])["tasks"][0]["taskArn"] == "task/customer-approved-stop"
    assert attempt["stderr"] == "nonfatal warning"
    for name in ("started_at", "ended_at", "recorded_at"):
        assert attempt[name] == record[name]


def test_output_cap_and_omission_counts_survive_assembly(workspace, spawned):
    """Streams over 20k explicitly report discarded redacted characters to both consumers."""
    spawned.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="x" * 21_000,
        stderr="y" * 22_000,
    )
    result = json.loads(execution_mcp_server.run_playbook_command("step-1", "aws ecs describe-tasks"))
    evidence = assemble_evidence(
        workspace.read_records(),
        execution_id="exec-1",
        rca_id="rca-1",
        engine="headless-codex",
        playbook={"execution_steps": [{"step_id": "step-1"}]},
    )
    attempt = evidence.to_dict()["steps"][0]["attempts"][0]
    for name, count in (("stdout", 21_000), ("stderr", 22_000)):
        assert result[name] == attempt[name]
        assert len(attempt[name]) == 20_000
        assert attempt[f"{name}_chars"] == count
        assert attempt[f"{name}_retained_chars"] == 20_000
        assert attempt[f"{name}_omitted_chars"] == count - 20_000
        assert attempt[f"{name}_truncated"] is True
    assert attempt["observation_truncated"] is True
    assert attempt["observation_omitted_chars"] == 19_000
    assert attempt["error_output_truncated"] is True


def test_server_retention_redacts_large_structured_output_and_intent(workspace, spawned):
    """Additional persisted stdout cannot expose synthetic credentials past the old 2k preview."""
    stdout = json.dumps(
        {
            "padding": "x" * 5000,
            "environment": [{"name": "PASSWORD", "value": "CANARY-secret-env"}],
            "authorization": "Bearer CANARY-auth",
            "url": "https://CANARY-user:CANARY-password@example.test/path",
            "nextToken": "safe-page",
            "taskArn": "task/customer-approved-stop",
        }
    )
    spawned.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr="Authorization: Bearer CANARY-stderr"
    )
    result = execution_mcp_server.run_playbook_command(
        "step-1", "aws ecs describe-tasks", intent="inspect password=CANARY-intent"
    )
    assert "CANARY" not in result
    assert "CANARY" not in json.dumps(workspace.read_records())
    captured = json.loads(workspace.read_records()[0]["stdout"])
    assert captured["nextToken"] == "safe-page"
    assert captured["taskArn"] == "task/customer-approved-stop"


@pytest.mark.parametrize("failure", ["timeout", "spawn_failed"])
def test_failed_command_attempts_have_server_times_and_redacted_partial_output(workspace, spawned, failure):
    """Failed subprocess attempts retain their real boundaries; timeout output is explicitly incomplete."""
    if failure == "timeout":
        spawned.side_effect = subprocess.TimeoutExpired(
            cmd="aws ecs describe-tasks",
            timeout=17,
            output=b'{"password": "CANARY-timeout"}',
            stderr=b"Bearer CANARY-stderr",
        )
    else:
        spawned.side_effect = OSError("failed with password=CANARY-spawn")
    before = datetime.now(UTC)
    result = execution_mcp_server.run_playbook_command("step-1", "aws ecs describe-tasks")
    after = datetime.now(UTC)
    record = workspace.read_records()[0]
    assert record["exit_status"] == failure
    assert before <= datetime.fromisoformat(record["started_at"])
    assert record["started_at"] <= record["ended_at"] <= record["recorded_at"]
    assert datetime.fromisoformat(record["recorded_at"]) <= after
    assert "CANARY" not in result + json.dumps(record)
    if failure == "timeout":
        assert record["output_incomplete"] is True
        assert record["output_incomplete_reason"] == "timeout"


def test_blocked_commands_have_record_time_but_no_fabricated_subprocess_times(workspace, spawned):
    """A gate refusal is recorded without pretending that a subprocess ran."""
    execution_mcp_server.run_playbook_command("step-1", "aws ecs delete-service --service api")
    record = workspace.read_records()[0]
    assert datetime.fromisoformat(record["recorded_at"]).tzinfo is not None
    assert "started_at" not in record
    assert "ended_at" not in record
    spawned.assert_not_called()
