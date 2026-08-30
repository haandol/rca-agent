import pytest

from codex_headless.services.execution_evidence import (
    PROCEDURE_DEFECT_CLASSES,
    CommandAttempt,
    ExecutionEvidence,
    FailureClass,
    parse_failure_class,
    redact,
    redact_arguments,
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
