import pytest

from cc_headless.services.destructive_actions import (
    IRREVERSIBLE_ACTION_ENGLISH,
    IRREVERSIBLE_ACTION_KOREAN,
    describes_destructive_action,
    is_destructive_operation,
)


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
    ],
)
def test_reversible_natural_language_procedures_are_not_destructive(action):
    assert describes_destructive_action(action) is False


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
    ],
)
def test_obviously_destructive_natural_language_procedures_remain_unsafe(action):
    assert describes_destructive_action(action) is True


@pytest.mark.parametrize("operation", ["close-thing", "release-resource", "disable-feature"])
def test_command_deny_verbs_remain_conservative_when_natural_language_is_safe(operation):
    assert is_destructive_operation("example", operation) is True
