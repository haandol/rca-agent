import json

import pytest

from headless_codex.services.execution_evidence import (
    PROCEDURE_DEFECT_CLASSES,
    CommandAttempt,
    ExecutionEvidence,
    FailureClass,
    capture_command_output,
    parse_failure_class,
    redact,
    redact_arguments,
    retrospective_evidence_json,
)


@pytest.mark.parametrize(
    "text",
    [
        "aws rds modify-db-instance --master-user-password hunter2",
        "aws configure set aws_secret_access_key=AKIAIOSFODNN7EXAMPLE",
        "SECRET_TOKEN=abc123 aws ecs describe-services",
        "aws ssm put-parameter --api-key=sk-live-1234",
        "aws ssm put-parameter --apiKey sk-live-1234",
        "aws ssm put-parameter --access-key sk-live-1234",
    ],
)
def test_credentials_are_redacted_from_commands(text):
    redacted = redact(text)

    for secret in ("hunter2", "AKIAIOSFODNN7EXAMPLE", "abc123", "sk-live-1234"):
        assert secret not in redacted


def test_redaction_keeps_non_secret_arguments_readable():
    redacted = redact("aws ecs update-service --cluster demo --service api")

    assert redacted == "aws ecs update-service --cluster demo --service api"


def test_argument_maps_redact_by_name_regardless_of_value():
    redacted = redact_arguments({"cluster": "demo", "DbPassword": "hunter2", "token": "t-1"})

    assert redacted["cluster"] == "demo"
    assert "hunter2" not in redacted["DbPassword"]
    assert "t-1" not in redacted["token"]


def test_unknown_failure_classes_do_not_become_procedure_defects():
    """분류를 확정할 수 없는 실패를 절차 결함으로 두면 회고가 잘못 교정한다."""
    assert parse_failure_class("nonsense") is FailureClass.UNKNOWN
    assert FailureClass.UNKNOWN not in PROCEDURE_DEFECT_CLASSES
    assert FailureClass.TRANSIENT not in PROCEDURE_DEFECT_CLASSES
    assert FailureClass.THROTTLED not in PROCEDURE_DEFECT_CLASSES
    assert FailureClass.TIMEOUT not in PROCEDURE_DEFECT_CLASSES


def _attempt(**overrides) -> CommandAttempt:
    payload = {
        "step_id": "step-1",
        "command": "aws ecs describe-services",
        "arguments": {},
        "exit_status": "0",
        "succeeded": True,
        "attempt_index": 1,
    }
    payload.update(overrides)
    return CommandAttempt(**payload)


def test_evidence_summary_counts_blocked_and_failed_steps():
    evidence = ExecutionEvidence(execution_id="e1", rca_id="r1", playbook_id="p1")
    evidence.record_attempt(_attempt(step_id="step-1"))
    evidence.record_attempt(
        _attempt(
            step_id="step-2",
            succeeded=False,
            blocked=True,
            block_reason="irreversible",
            failure_class=FailureClass.BLOCKED_DESTRUCTIVE,
        )
    )
    evidence.record_attempt(_attempt(step_id="step-3", succeeded=False, failure_class=FailureClass.INVALID_ARGUMENT))

    summary = evidence.summary()

    assert summary["attempted_step_count"] == 3
    assert summary["blocked_count"] == 1
    assert summary["failed_step_count"] == 2


def test_a_retried_failure_stays_in_the_evidence_after_success():
    """재시도로 성공했다는 사실 자체가 회고의 판단 근거이므로 실패를 지우지 않는다."""
    evidence = ExecutionEvidence(execution_id="e1", rca_id="r1", playbook_id="p1")
    evidence.record_attempt(_attempt(succeeded=False, failure_class=FailureClass.INVALID_ARGUMENT, attempt_index=1))
    evidence.record_attempt(_attempt(succeeded=True, attempt_index=2))

    step = evidence.step("step-1")

    assert step.succeeded
    assert len(step.attempts) == 2
    assert [attempt.step_id for attempt in step.procedure_defects] == ["step-1"]


def test_serialized_evidence_keeps_block_reasons():
    evidence = ExecutionEvidence(execution_id="e1", rca_id="r1", playbook_id="p1")
    evidence.record_attempt(
        _attempt(
            succeeded=False,
            blocked=True,
            block_reason="ecs delete-service is an irreversible operation",
            failure_class=FailureClass.BLOCKED_DESTRUCTIVE,
        )
    )

    rendered = evidence.to_dict()
    attempt = rendered["steps"][0]["attempts"][0]

    assert attempt["blocked"] is True
    assert "irreversible" in attempt["block_reason"]
    assert attempt["failure_class"] == "BLOCKED_DESTRUCTIVE"


@pytest.mark.parametrize(
    "text",
    [
        '{"password": "CANARY quoted value with spaces", "taskArn": "task/keep"}',
        '{"SecretAccessKey": "CANARY-key", "SessionToken": "CANARY-session"}',
        '{"environment": [{"name": "DB_PASSWORD", "value": "CANARY-env"}]}',
        '{"environment": [{"value": "CANARY-reversed", "name": "AWS_SECRET_ACCESS_KEY"}]}',
        '{"environment": [{"value": {"nested": "CANARY-object"}, "name": "API_KEY"}]}',
        'diagnostic: {"name": "DATABASE_PASSWORD", "value": "CANARY mixed whitespace"}',
        'diagnostic: {"value": "CANARY reverse whitespace", "name": "DATABASE_PASSWORD"}',
        "name=DB_PASSWORD,value=CANARY-pair",
        "value=CANARY-pair,name=DB_PASSWORD",
        "Authorization: Bearer CANARY-bearer",
        "Bearer CANARY-bare",
        '{"Authorization": "Bearer CANARY-json-bearer"}',
        "failed connecting to postgresql://CANARY-user:CANARY-password@db.internal:5432/app",
        '--master-user-password "CANARY shell space"',
        "API_KEY='CANARY inline space'",
        '{"password": "CANARY escaped \\" quote"}',
        '{"message": "{\\"password\\": \\"CANARY nested json\\"}"}',
    ],
)
def test_output_redaction_canaries_cover_structured_and_diagnostic_credentials(text):
    """All synthetic credential values disappear, including whitespace and alternate env ordering."""
    cleaned = redact(text)
    assert "CANARY" not in cleaned
    assert "***REDACTED***" in cleaned
    assert redact(cleaned) == cleaned
    if text.startswith("{"):
        json.loads(cleaned)


@pytest.mark.parametrize(
    "name",
    [
        "nextToken",
        "NextToken",
        "continuation_token",
        "--starting-token",
        "clientToken",
        "taskArn",
        "taskId",
        "requestId",
        "author",
        "authorizerId",
        "secretArn",
    ],
)
def test_output_redaction_preserves_benign_pagination_and_resource_identity(name):
    """Noncredential values remain useful for pagination and exact target attribution."""
    value = "keep-target-or-page-123"
    assert redact_arguments({name: value}) == {name: value}
    structured = json.dumps({name: value})
    assert json.loads(redact(structured)) == {name: value}
    assert value in redact(f"{name}= {value}")


def test_redaction_happens_before_the_capture_boundary():
    """A credential crossing the output cap cannot leak a retained prefix of its secret."""
    output = "x" * 19_980 + ' password="CANARY-' + "secret" * 100 + '"'
    captured = capture_command_output(output, b"Authorization: Bearer CANARY-stderr")
    assert "CANARY" not in json.dumps(captured)
    assert len(captured["stdout"]) <= 20_000
    assert captured["stdout_chars"] == len(redact(output))
    assert captured["stdout_omitted_chars"] == captured["stdout_chars"] - captured["stdout_retained_chars"]


def test_retrospective_projection_preserves_all_metadata_and_outcomes_in_valid_bounded_json():
    """Large previews shrink; all steps, attempts, timestamps and causal outcome text survive."""
    evidence = ExecutionEvidence(
        execution_id="exec-1",
        rca_id="rca-1",
        playbook_id="pb-1",
        resolution_confirmed=True,
        resolution_observation="target stopped; two fresh windows healthy",
        final_state="RESOLVED",
        resolution_records=[
            {"resolved": True, "observation": "resolution tail", "recorded_at": "2026-09-10T01:03:00Z"}
        ],
    )
    for index in range(12):
        step_id = f"step-{index}"
        evidence.record_attempt(
            _attempt(
                step_id=step_id,
                command=f"aws ecs describe-tasks --tasks task-{index}",
                arguments={"service": "ecs", "operation": "describe-tasks"},
                started_at="2026-09-10T01:00:00Z",
                ended_at="2026-09-10T01:00:01Z",
                recorded_at="2026-09-10T01:00:02Z",
                captured_output=capture_command_output('"\n\\측정' * 6000, "warning\n" * 3000),
            )
        )
        step = evidence.step(step_id)
        step.resolved = True
        step.observation = f"confirmed task-{index} identity"
        step.outcomes.append({"criteria_met": True, "observation": step.observation})
    original = evidence.to_dict()

    rendered = retrospective_evidence_json(evidence)
    projected = json.loads(rendered)

    assert len(rendered) <= 60_000
    assert projected["projection"]["output_previews_omitted"] is True
    assert projected["projection"]["omitted_chars"] > 0
    assert evidence.to_dict() == original
    for actual, source in zip(projected["steps"], original["steps"], strict=True):
        assert actual["step_id"] == source["step_id"]
        assert actual["resolved"] is True
        assert actual["observation"] == source["observation"]
        assert actual["outcomes"] == source["outcomes"]
        assert actual["succeeded"] == source["succeeded"]
        attempt = actual["attempts"][0]
        original_attempt = source["attempts"][0]
        for key in ("command", "arguments", "exit_status", "succeeded", "started_at", "ended_at", "recorded_at"):
            assert attempt[key] == original_attempt[key]
        for name in ("stdout", "stderr"):
            omissions = attempt["projection_omissions"][name]
            assert omissions["omitted"] is True
            assert omissions["omitted_chars"] == len(original_attempt[name]) - len(attempt[name])
    assert projected["resolution_observation"] == original["resolution_observation"]
    assert projected["resolution_records"] == original["resolution_records"]
    assert projected["resolution_confirmed"] is True
    assert projected["final_state"] == "RESOLVED"


def test_small_retrospective_projection_retains_complete_output():
    """Small evidence gets no unnecessary output loss and explicitly reports zero omissions."""
    evidence = ExecutionEvidence(execution_id="e", rca_id="r", playbook_id="p")
    evidence.record_attempt(_attempt(captured_output=capture_command_output('{"taskArn":"keep"}', "")))
    projected = json.loads(retrospective_evidence_json(evidence))
    assert projected["steps"][0]["attempts"][0]["stdout"] == '{"taskArn":"keep"}'
    assert projected["projection"]["output_previews_omitted"] is False
    assert projected["projection"]["omitted_chars"] == 0


def test_retrospective_budget_failure_cannot_silently_drop_step_metadata():
    """Impossible metadata budgets raise before generating truncated JSON or deleting steps."""
    evidence = ExecutionEvidence(execution_id="e", rca_id="r", playbook_id="p")
    evidence.record_attempt(_attempt(command="approved command " * 5000))
    original = evidence.to_dict()
    with pytest.raises(ValueError, match="metadata exceeds JSON budget"):
        retrospective_evidence_json(evidence)
    assert evidence.to_dict() == original
