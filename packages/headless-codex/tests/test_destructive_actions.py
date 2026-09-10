import pytest

from headless_codex.services.destructive_actions import (
    DESTRUCTIVE_OPERATION_VERBS,
    IRREVERSIBLE_ACTION_ENGLISH,
    IRREVERSIBLE_ACTION_KOREAN,
    UndecidableCommandError,
    classify_command,
    describes_destructive_action,
    is_destructive_operation,
    refusal_reason,
)


@pytest.mark.parametrize(
    "command",
    [
        (
            """aws logs filter-log-events --filter-pattern """
            """'{ $.event = "db_operation" && $.operation = "ingest" && $.outcome = "ok" }'"""
        ),
        """'aws' "logs" 'filter-log-events' --filter-pattern "literal && data" """,
    ],
)
def test_classifier_reads_the_leading_shlex_operation_with_literal_argument_data(command):
    assert classify_command(command) == ("logs", "filter-log-events")
    assert refusal_reason(command) is None


@pytest.mark.parametrize(
    "command",
    [
        """echo 'aws ecs describe-services'""",
        """'aws ecs describe-services' aws ecs delete-service""",
        """sh -c 'aws ecs describe-services'""",
        """aws --region us-east-1 ecs describe-services""",
        """aws --profile describe-services ecs delete-service""",
        """aws ecs --region us-east-1 describe-services""",
        """aws ecs describe-services --query 'unterminated""",
        "aws ecs describe-services\n",
    ],
)
def test_classifier_rejects_quoted_prefixes_global_options_and_invalid_syntax(command):
    with pytest.raises(UndecidableCommandError):
        classify_command(command)
    assert refusal_reason(command) is not None


@pytest.mark.parametrize("verb", sorted(DESTRUCTIVE_OPERATION_VERBS))
def test_literal_arguments_cannot_hide_any_destructive_operation_verb(verb):
    command = f"""aws example '{verb}-resource' --value 'aws logs filter-log-events && literal'"""

    assert classify_command(command) == ("example", f"{verb}-resource")
    assert "irreversible operation" in refusal_reason(command)


def test_natural_language_contract_uses_the_shared_irreversible_members():
    assert (
        frozenset(
            {
                "delete",
                "deletion",
                "terminate",
                "termination",
                "destroy",
                "destruction",
                "purge",
                "erase",
                "remove",
                "removal",
                "revoke",
                "deregister",
                "wipe",
                "truncate",
                "drop",
                "shutdown",
                "decommission",
            }
        )
        == IRREVERSIBLE_ACTION_ENGLISH
    )
    assert set(IRREVERSIBLE_ACTION_KOREAN) == {
        "삭제",
        "제거",
        "지운다",
        "지우고",
        "파기",
        "폐기",
        "종료한다",
        "종료하고",
        "말소",
        "영구 제거",
        "드롭",
    }
    assert len(IRREVERSIBLE_ACTION_KOREAN) == len(set(IRREVERSIBLE_ACTION_KOREAN))


@pytest.mark.parametrize(
    "action",
    [
        "Close stale database sessions to release pooled connections.",
        "Release the leaked connection and disable the faulty feature flag.",
        "Terminate idle database sessions, then verify the connection count.",
        "Remove the stale connection from the pool.",
        "Termination of database sessions is the temporary mitigation.",
        "Connection removal followed by a metric check.",
        "유휴 데이터베이스 세션을 종료하고 기능 플래그를 비활성화한다.",
        "누수된 커넥션을 해제하고 연결 수를 확인한다.",
        "연결 닫기 후 지표를 확인한다.",
        "기존 정책을 삭제하지 않고 누락된 권한만 추가한다.",
        "Do not delete the policy; add only logs:StartQuery.",
        "without deleting the role, restore the permission",
    ],
)
def test_reversible_natural_language_procedures_are_not_destructive(action):
    assert describes_destructive_action(action) is False


@pytest.mark.parametrize(
    "action",
    [
        "데이터베이스 리소스나 데이터를 삭제·종료하지 않는다.",
        "데이터베이스 리소스나 데이터를 삭제 · 종료하지 않는다.",
        "데이터를 삭제·제거·파기하지 않습니다.",
        "리소스를 삭제·종료하지 않고 상태만 확인한다.",
    ],
)
def test_tightly_coordinated_actions_share_their_explicit_negation(action):
    assert describes_destructive_action(action) is False


@pytest.mark.parametrize(
    "action",
    [
        "데이터베이스 리소스나 데이터를 삭제·종료한다.",
        "데이터를 삭제·제거·파기한다.",
        "리소스를 삭제·종료하지 않고 스냅샷을 파기한다.",
        "리소스를 삭제·종료하지 않는다. 스냅샷을 파기한다.",
        "리소스를 삭제·종료하지 않는다.\n스냅샷을 파기한다.",
        "스냅샷을 파기하고 리소스를 삭제·종료하지 않는다.",
        "데이터를 삭제·파기한다. 리소스는 종료하지 않는다.",
        "데이터를 삭제·파기하고 리소스는 종료하지 않는다.",
        "데이터를 삭제, 리소스는 종료하지 않는다.",
        "데이터를 삭제; 리소스는 종료하지 않는다.",
        "데이터를 삭제·폐기\n하지 않는다.",
        "데이터를 삭제·\n폐기하지 않는다.",
        "데이터를 삭제/폐기하지 않는다.",
        "데이터를 삭제, 폐기하지 않는다.",
        "데이터를 삭제 및 폐기하지 않는다.",
        "데이터를 삭제한다·리소스는 종료하지 않는다.",
        "데이터를 삭제·폐기하지 않고서는 복구할 수 없다.",
        "데이터를 삭제·폐기하지 않는 것은 아니다.",
    ],
)
def test_coordinated_negation_does_not_hide_positive_or_separate_actions(action):
    assert describes_destructive_action(action) is True


@pytest.mark.parametrize(
    "action",
    [
        "Delete the RDS instance.",
        "Terminate the EC2 instance.",
        "Destroy the production cluster.",
        "Revoke and deregister the production resource.",
        "Shutdown and decommission the cluster.",
        "RDS 인스턴스를 삭제한다.",
        "EC2 인스턴스를 종료한다.",
        "RDS 종료 후 새 인스턴스를 생성한다.",
        "클러스터 종료 절차를 수행한다.",
        "리소스 종료를 요청한다.",
        "클러스터를 파기한다.",
        "리소스를 영구 제거한다.",
        "인증서를 말소한다.",
        "세션을 종료하고 RDS 리소스를 삭제한다.",
        "세션을 종료하되 RDS 인스턴스를 삭제한다.",
        "do not delete the snapshot, then terminate the instance",
    ],
)
def test_obviously_destructive_natural_language_procedures_remain_unsafe(action):
    assert describes_destructive_action(action) is True


@pytest.mark.parametrize("operation", ["close-thing", "release-resource", "disable-feature"])
def test_command_deny_verbs_remain_conservative_when_natural_language_is_safe(operation):
    assert is_destructive_operation("example", operation) is True
