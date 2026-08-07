import pytest

from rca_agent.services.destructive_actions import (
    IRREVERSIBLE_ACTION_ENGLISH,
    IRREVERSIBLE_ACTION_KOREAN,
    describes_destructive_action,
)

EXPECTED_ENGLISH = {
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

EXPECTED_KOREAN = {
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


def test_public_irreversible_action_vocabulary_matches_the_cross_engine_contract() -> None:
    assert IRREVERSIBLE_ACTION_ENGLISH == EXPECTED_ENGLISH
    assert IRREVERSIBLE_ACTION_KOREAN == EXPECTED_KOREAN


@pytest.mark.parametrize(
    "action",
    [
        "close the leaked database session",
        "release idle connections back to the pool",
        "terminate the stale session",
        "database session termination",
        "termination of the affected sessions",
        "remove unused connections",
        "removal of the connection",
        "disable the faulty feature flag",
        "누수된 세션을 종료한다",
        "연결을 종료하고 풀을 재설정한다",
        "커넥션 종료",
        "세션 닫기",
        "연결 해제",
        "문제가 된 기능을 비활성화한다",
    ],
)
def test_reversible_operational_actions_are_not_scored_as_destructive(action: str) -> None:
    assert describes_destructive_action(action) is False


@pytest.mark.parametrize(
    "action",
    [
        "delete the RDS instance",
        "deletion of the cluster",
        "terminate the EC2 instance",
        "resource termination",
        "destroy the database",
        "cluster destruction",
        "purge the resource",
        "erase the RDS data",
        "remove the EC2 instance",
        "resource removal",
        "revoke the resource",
        "deregister the resource",
        "wipe the database",
        "truncate the table",
        "drop the database",
        "shutdown the cluster",
        "decommission the resource",
        "RDS 인스턴스를 삭제한다",
        "EC2 리소스를 제거한다",
        "클러스터를 종료한다",
        "고객 키를 파기한다",
    ],
)
def test_clearly_irreversible_actions_are_scored_as_destructive(action: str) -> None:
    assert describes_destructive_action(action) is True


def test_reversible_phrase_does_not_hide_a_later_irreversible_action() -> None:
    action = "terminate the stale session, then delete the RDS instance"

    assert describes_destructive_action(action) is True


@pytest.mark.parametrize("action", [None, 1, "", "   "])
def test_non_text_or_blank_actions_are_not_destructive(action: object) -> None:
    assert describes_destructive_action(action) is False
