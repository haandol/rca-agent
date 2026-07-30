"""되돌릴 수 없는 조치의 판정 — 분석 측 어휘.

플레이북 절차의 자연어 서술이 되돌릴 수 없는 조치를 요구하는지 판정한다. 평가가
절차의 안전성을 채점할 때 쓰는 신호이며, 실제 차단은 실행 시점에 명령 단위로 다시
이루어진다.

**이 어휘는 실행 계층의 어휘와 같아야 한다.** 두 엔진이 같은 기준으로 채점되어야
하고, 분석이 안전하다고 판정한 절차가 실행에서 차단되면 안 된다. 두 패키지는 서로
의존하지 않으므로 import 로 공유할 수 없고, 대신 저장소 계약 테스트가 두 어휘의
동일성을 강제한다. 한쪽만 고치면 그 테스트가 실패한다.
"""

from __future__ import annotations

import re

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

_DESTRUCTIVE_KOREAN = (
    "삭제",
    "제거",
    "지운다",
    "지우고",
    "파기",
    "폐기",
    "종료한다",
    "종료하고",
    "회수",
    "박탈",
    "드롭",
    "비활성화",
)


def describes_destructive_action(action: object) -> bool:
    """절차의 자연어 서술이 되돌릴 수 없는 조치를 요구하는지.

    실행 전 정적 판정이므로 명령이 아니라 의도를 본다. 확실하지 않으면 파괴적이지
    않다고 본다 — 이 판정은 평가 신호이고, 실제 차단은 실행 시점에 명령 단위로
    다시 이루어진다.
    """
    if not isinstance(action, str) or not action.strip():
        return False
    lowered = action.lower()
    for verb in DESTRUCTIVE_OPERATION_VERBS:
        if re.search(rf"\b{verb}\b", lowered):
            return True
    return any(token in action for token in _DESTRUCTIVE_KOREAN)
