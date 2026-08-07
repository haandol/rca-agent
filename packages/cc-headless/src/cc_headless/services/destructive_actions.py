"""되돌릴 수 없는 조치의 판정.

실행 경계는 대상 리소스를 제한하지 않는다. 대신 되돌릴 수 없는 작업을 거부한다.
실행 명령 판정 기준은 제어 평면 작업 이름의 어휘이며, 이 모듈이 그 어휘의 단일
소스다.

두 소비처가 있다. 분석 단계는 플레이북 절차의 자연어 서술에서 파괴적 의도를 찾아
평가에 쓰고, 실행 단계는 실제 명령에서 작업 이름을 추출해 실행 여부를 결정한다. 자연어
의미 판정은 명령 거부 동사와 분리한다. `close`, `release`, `disable` 같은 제어 평면
동사는 보수적으로 실행을 거부하지만, 세션 정리나 기능 비활성화 절차 자체는 되돌릴 수
있으므로 평가에서 파괴적으로 보지 않는다.
"""

from __future__ import annotations

import re

# 되돌릴 수 없는 제어 평면 작업의 동사. AWS API 이름은 동사+명사 형태이므로 동사만
# 보면 서비스가 늘어나도 같은 판정이 적용된다.
DESTRUCTIVE_OPERATION_VERBS: frozenset[str] = frozenset(
    {
        "delete",
        "terminate",
        "destroy",
        "purge",
        "erase",
        "remove",
        "revoke",
        "deregister",
        "disassociate",
        "detach",
        "release",
        "cancel",
        "abort",
        "expire",
        "wipe",
        "truncate",
        "drop",
        "shutdown",
        "deactivate",
        "disable",
        "close",
        "unsubscribe",
        "reject",
    }
)

# 계정·조직·청구 범위는 실행 대상이 아니다. 이 범위의 작업은 동사와 무관하게 거부한다.
OUT_OF_SCOPE_SERVICE_PREFIXES: frozenset[str] = frozenset(
    {
        "organizations",
        "account",
        "billing",
        "budgets",
        "ce",
        "cur",
        "iam",
        "sts",
        "sso",
        "identitystore",
    }
)

# 실행 워커 자신의 승인·상태·컴퓨트 경계를 조작하거나 권한을 확장할 수 있는 호출.
# 이 작업들은 되돌릴 수 있더라도 장애 복구 절차가 아니며, 실행 경로가 자기 통제
# 평면을 우회하는 수단이 된다.
SELF_CONTROL_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("sqs", "send-message"),
        ("sqs", "send-message-batch"),
        ("ecs", "register-task-definition"),
        ("ecs", "run-task"),
        ("ecs", "start-task"),
        ("ecs", "execute-command"),
    }
)
_DYNAMODB_READ_OPERATIONS = frozenset(
    {
        "batch-get-item",
        "describe-backup",
        "describe-continuous-backups",
        "describe-contributor-insights",
        "describe-endpoints",
        "describe-export",
        "describe-global-table",
        "describe-global-table-settings",
        "describe-import",
        "describe-kinesis-streaming-destination",
        "describe-limits",
        "describe-table",
        "describe-table-replica-auto-scaling",
        "describe-time-to-live",
        "get-item",
        "list-backups",
        "list-contributor-insights",
        "list-exports",
        "list-global-tables",
        "list-imports",
        "list-tables",
        "list-tags-of-resource",
        "query",
        "scan",
        "transact-get-items",
    }
)

# 셸 합성과 중첩 호출은 작업 이름을 신뢰할 수 없게 만든다. 판정 불가는 거부로 처리한다.
_SHELL_COMPOSITION = re.compile(r"[;&|`]|\$\(|\|\||&&|>\s|>>")

# awscli 형태: `aws <service> <kebab-operation> ...`
_AWSCLI_CALL = re.compile(r"\baws\s+([a-z0-9-]+)\s+([a-z0-9-]+)")

IRREVERSIBLE_ACTION_ENGLISH: frozenset[str] = frozenset(
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
IRREVERSIBLE_ACTION_KOREAN: tuple[str, ...] = (
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
)

_IRREVERSIBLE_ACTION_ENGLISH_PATTERN = re.compile(
    rf"\b(?:{'|'.join(re.escape(term) for term in sorted(IRREVERSIBLE_ACTION_ENGLISH))})\b",
    re.IGNORECASE,
)

# 연결과 세션의 종료는 리소스 파기가 아니라 장애 복구를 위한 되돌릴 수 있는 정리다.
_REVERSIBLE_SESSION_ACTION_ENGLISH = re.compile(
    r"\b(?:"
    r"(?:close|release|terminate|remove)\s+"
    r"(?:(?:the|a|an)\s+)?(?:(?:stale|idle|leaked|database|pooled|open)\s+)*"
    r"(?:connections?|sessions?)(?:\s+(?:and|or)\s+(?:connections?|sessions?))?"
    r"|(?:closure|release|termination|removal)\s+of\s+"
    r"(?:(?:the|a|an)\s+)?(?:(?:stale|idle|leaked|database|pooled|open)\s+)*"
    r"(?:connections?|sessions?)(?:\s+(?:and|or)\s+(?:connections?|sessions?))?"
    r"|(?:connections?|sessions?)\s+(?:closure|close|release|termination|removal)"
    r")\b",
    re.IGNORECASE,
)
_REVERSIBLE_SESSION_ACTION_KOREAN = re.compile(
    r"(?:연결|커넥션)(?:\s*풀)?(?:을|를)?\s*"
    r"(?:닫(?:는다|기|고)?|해제(?:한다|하기|하고)?|종료(?:한다|하기|하고|시킨다|시키기|시키고)?)"
    r"|세션(?:을|를)?\s*"
    r"(?:닫(?:는다|기|고)?|해제(?:한다|하기|하고)?|종료(?:한다|하기|하고|시킨다|시키기|시키고)?)"
)
_IRREVERSIBLE_KOREAN_RESOURCE_TERMINATION = re.compile(
    r"(?:EC2|RDS|인스턴스|클러스터|데이터베이스|테이블|리소스|계정).{0,20}(?:종료|폐쇄)",
    re.IGNORECASE,
)


class UndecidableCommandError(ValueError):
    """작업 이름을 신뢰할 수 있게 추출할 수 없는 명령."""


def _verb_of(operation: str) -> str:
    """kebab-case 또는 CamelCase 작업 이름의 첫 동사를 소문자로 돌려준다."""
    head = operation.split("-", 1)[0]
    if head != operation.lower() and "-" not in operation:
        # CamelCase: 첫 대문자 경계까지가 동사다.
        match = re.match(r"[A-Z][a-z]+", operation)
        if match:
            return match.group(0).lower()
    return head.lower()


def is_destructive_operation(service: str, operation: str) -> bool:
    if service.lower() in OUT_OF_SCOPE_SERVICE_PREFIXES:
        return True
    return _verb_of(operation) in DESTRUCTIVE_OPERATION_VERBS


def is_self_control_operation(service: str, operation: str) -> bool:
    normalized = (service.lower(), operation.lower())
    if normalized in SELF_CONTROL_OPERATIONS:
        return True
    return normalized[0] == "dynamodb" and normalized[1] not in _DYNAMODB_READ_OPERATIONS


def classify_command(command: str) -> tuple[str, str]:
    """명령에서 (service, operation) 을 추출한다.

    Raises:
        UndecidableCommandError: 셸 합성이 섞였거나 작업 이름을 찾을 수 없을 때.
            판정 불가를 허용으로 해석하면 거부 목록이 무력화되므로 호출자는 이를
            거부로 처리해야 한다.
    """
    text = command.strip()
    if not text:
        raise UndecidableCommandError("command is empty")
    if _SHELL_COMPOSITION.search(text):
        raise UndecidableCommandError("command composes multiple shell operations")

    match = _AWSCLI_CALL.search(text)
    if match is None:
        raise UndecidableCommandError("command does not name an AWS service and operation")
    return match.group(1), match.group(2)


def refusal_reason(command: str) -> str | None:
    """명령을 거부해야 하는 사유. 실행해도 되면 None."""
    try:
        service, operation = classify_command(command)
    except UndecidableCommandError as exc:
        return f"command could not be classified: {exc}"
    if service.lower() in OUT_OF_SCOPE_SERVICE_PREFIXES:
        return f"{service} is outside the execution scope"
    if _verb_of(operation) in DESTRUCTIVE_OPERATION_VERBS:
        return f"{service} {operation} is an irreversible operation"
    return None


def describes_destructive_action(action: object) -> bool:
    """플레이북 절차의 자연어 서술이 되돌릴 수 없는 조치를 요구하는지.

    실행 전 정적 판정이므로 명령이 아니라 의도를 본다. 확실하지 않으면 파괴적이지
    않다고 본다 — 이 판정은 평가 신호이고, 실제 차단은 실행 시점에 명령 단위로
    다시 이루어진다.
    """
    if not isinstance(action, str) or not action.strip():
        return False
    without_reversible_sessions = _REVERSIBLE_SESSION_ACTION_ENGLISH.sub("", action)
    without_reversible_sessions = _REVERSIBLE_SESSION_ACTION_KOREAN.sub("", without_reversible_sessions)
    if _IRREVERSIBLE_ACTION_ENGLISH_PATTERN.search(without_reversible_sessions):
        return True
    return any(token in without_reversible_sessions for token in IRREVERSIBLE_ACTION_KOREAN) or bool(
        _IRREVERSIBLE_KOREAN_RESOURCE_TERMINATION.search(without_reversible_sessions)
    )
