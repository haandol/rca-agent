"""플레이북 자연어가 되돌릴 수 없는 조치를 요구하는지 판정한다.

실행 계층의 command deny 규칙과 달리 자연어 평가는 되돌릴 수 있는 운영 조치를
허용한다. 연결이나 세션을 닫고 해제하는 조치와 feature disable은 안전하며, 리소스
삭제, 종료, 파기처럼 비가역 의도가 명시된 경우만 파괴적으로 판정한다.
"""

from __future__ import annotations

import re

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

IRREVERSIBLE_ACTION_KOREAN: frozenset[str] = frozenset(
    {
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
)

_ENGLISH_TARGET = r"(?:connections?|sessions?)"
_ENGLISH_TARGET_PREFIX = r"(?:(?:the|a|an|leaked|stale|idle|open|unused|affected|database|db)\s+){0,6}"
_ENGLISH_REVERSIBLE_VERB = (
    r"(?:clos(?:e|es|ed|ing|ure)|releas(?:e|es|ed|ing)|"
    r"terminat(?:e|es|ed|ing|ion)|remov(?:e|es|ed|ing|al))"
)
_ENGLISH_REVERSIBLE_ACTIONS = (
    re.compile(
        rf"\b{_ENGLISH_REVERSIBLE_VERB}\s+{_ENGLISH_TARGET_PREFIX}{_ENGLISH_TARGET}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_ENGLISH_TARGET}\s+{_ENGLISH_REVERSIBLE_VERB}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:closure|release|termination|removal)\s+of\s+"
        rf"{_ENGLISH_TARGET_PREFIX}{_ENGLISH_TARGET}\b",
        re.IGNORECASE,
    ),
)
_ENGLISH_IRREVERSIBLE = re.compile(
    rf"\b(?:{'|'.join(sorted(IRREVERSIBLE_ACTION_ENGLISH, key=len, reverse=True))})\b",
    re.IGNORECASE,
)

_KOREAN_REVERSIBLE_ACTION = re.compile(
    r"(?:연결|커넥션|세션)(?:들)?(?:을|를|은|는|의)?\s*"
    r"(?:(?:안전하게|강제로|즉시|모두)\s*)*"
    r"(?:닫(?:기|는다|고)|해제(?:하기|한다|하고)?|종료(?:하기|한다|하고)?)"
)


def _without_reversible_actions(action: str) -> str:
    redacted = action
    for pattern in _ENGLISH_REVERSIBLE_ACTIONS:
        redacted = pattern.sub(" ", redacted)
    return _KOREAN_REVERSIBLE_ACTION.sub(" ", redacted)


def describes_destructive_action(action: object) -> bool:
    """자연어 절차가 명백한 비가역 조치를 요구하면 ``True``를 반환한다."""
    if not isinstance(action, str) or not action.strip():
        return False

    candidate = _without_reversible_actions(action)
    if _ENGLISH_IRREVERSIBLE.search(candidate):
        return True
    return any(token in candidate for token in IRREVERSIBLE_ACTION_KOREAN)
