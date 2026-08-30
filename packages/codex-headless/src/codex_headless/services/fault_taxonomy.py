"""장애 원인 유형 분류.

이 어휘는 가설과 검증 판정이 원인을 구조화해 기록하기 위한 것이며, 실행 허용
목록이 아니다. 실행 대상은 플레이북 절차가 정하고, 실행 계층은 되돌릴 수 없는
조치만 거부한다.
"""

from __future__ import annotations

from enum import StrEnum


class FaultType(StrEnum):
    DB_LEAK = "db-leak"
    HIGH_CPU = "high-cpu"
    HIGH_MEMORY = "high-memory"
    SLOW_QUERY = "slow-query"
    UNSUPPORTED = "unsupported"


def parse_fault_type(value: object) -> FaultType | None:
    if not isinstance(value, str):
        return None
    try:
        return FaultType(value)
    except ValueError:
        return None
