import pytest

from headless_codex.services.command_gate import evaluate_command


@pytest.mark.parametrize(
    "command",
    [
        "aws ecs update-service --cluster demo --service api --force-new-deployment",
        "aws cloudwatch get-metric-statistics --namespace AWS/ECS --metric-name CPUUtilization",
        "aws ecs describe-services --cluster demo --services api",
        "aws application-autoscaling register-scalable-target --service-namespace ecs",
        "aws rds modify-db-instance --db-instance-identifier demo --max-allocated-storage 200",
    ],
)
def test_reversible_control_plane_calls_are_allowed(command):
    verdict = evaluate_command(command)

    assert verdict.allowed, verdict.reason


@pytest.mark.parametrize(
    "command",
    [
        "aws ecs delete-service --cluster demo --service api",
        "aws ec2 terminate-instances --instance-ids i-1234567890abcdef0",
        "aws rds delete-db-snapshot --db-snapshot-identifier demo-snap",
        "aws s3api delete-objects --bucket demo --delete file.json",
        "aws ecs deregister-task-definition --task-definition demo:1",
        "aws cloudwatch disable-alarm-actions --alarm-names demo",
    ],
)
def test_irreversible_operations_are_refused(command):
    verdict = evaluate_command(command)

    assert not verdict.allowed
    assert not verdict.undecidable
    assert "irreversible" in verdict.reason


@pytest.mark.parametrize(
    "command",
    [
        "aws iam get-user --user-name demo",
        "aws organizations describe-account --account-id 1234",
        "aws sts get-caller-identity",
        "aws budgets describe-budget --account-id 1234 --budget-name demo",
    ],
)
def test_account_and_credential_scope_is_refused_regardless_of_verb(command):
    verdict = evaluate_command(command)

    assert not verdict.allowed
    assert "outside the execution scope" in verdict.reason


@pytest.mark.parametrize(
    "command",
    [
        "aws sqs send-message --queue-url https://example --message-body forged",
        "aws dynamodb put-item --table-name sessions --item '{}'",
        "aws dynamodb update-item --table-name sessions --key '{}'",
        "aws dynamodb transact-write-items --transact-items '[]'",
        "aws ecs register-task-definition --family escalated",
        "aws ecs run-task --cluster demo --task-definition escalated",
        "aws ecs start-task --cluster demo --task-definition escalated --container-instances i-1",
        "aws ecs execute-command --cluster demo --task task-1 --command sh --interactive",
    ],
)
def test_self_control_and_privilege_escalation_operations_are_refused(command):
    verdict = evaluate_command(command)

    assert not verdict.allowed


def test_legitimate_ecs_service_rollback_remains_allowed():
    verdict = evaluate_command("aws ecs update-service --cluster demo --service api --task-definition healthcare:41")

    assert verdict.allowed


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "aws ecs describe-services | aws ecs delete-service --service api",
        "aws ecs describe-services && aws ec2 terminate-instances --instance-ids i-1",
        "aws ecs describe-services; aws ecs delete-service",
        "aws ecs describe-services > /tmp/out.json",
        "aws ecs describe-services $(aws ecs list-services)",
        "aws ecs describe-services `whoami`",
        "kubectl delete pod demo",
        "python -c 'import boto3'",
        "sh -c 'aws ecs describe-services'",
        "aws",
        "aws ecs",
        "aws 'ecs",
    ],
)
def test_undecidable_commands_are_refused(command):
    """판정 불가를 허용으로 해석하면 거부 목록이 무력화된다."""
    verdict = evaluate_command(command)

    assert not verdict.allowed
    assert verdict.undecidable


def test_a_destructive_call_cannot_hide_behind_a_harmless_leading_token():
    # 어휘 대조만 하면 문자열 어딘가의 무해한 작업 이름을 근거로 통과할 수 있다.
    # 전역 옵션이 작업 이름의 위치를 밀어내므로 판정 불가로 거부해야 한다.
    verdict = evaluate_command("aws --profile describe-services ecs delete-service --service api")

    assert not verdict.allowed
    assert verdict.undecidable


def test_global_options_before_the_operation_are_undecidable():
    verdict = evaluate_command("aws --region us-east-1 ecs describe-services --cluster demo")

    assert not verdict.allowed
    assert verdict.undecidable


def test_the_gate_returns_argv_so_the_executor_never_uses_a_shell_string():
    verdict = evaluate_command("aws ecs update-service --cluster demo --service api")

    assert verdict.argv == (
        "aws",
        "ecs",
        "update-service",
        "--cluster",
        "demo",
        "--service",
        "api",
    )
