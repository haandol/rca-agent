"""벡터 인덱스가 공유하는 임베딩 텍스트 렌더러.

두 분석 엔진이 같은 인덱스를 읽고 쓰므로, 같은 장애에 대해 글자 그대로 같은 텍스트를
만들어야 한다. 한쪽이 필드를 다르게 자르거나 라벨을 다르게 붙이면 임베딩 공간이
갈라져, 같은 증상의 플레이북이 서로를 찾지 못한다.
"""

from __future__ import annotations

EMBED_FIELD_MAX = 80


def build_embed_key(*, failure_type: str = "", symptom: str = "", metric_name: str = "") -> str:
    parts = {
        "장애유형": _truncate(failure_type),
        "증상": _truncate(symptom),
        "메트릭": _truncate(metric_name),
    }
    return " | ".join(f"{label}: {value}" for label, value in parts.items() if value)


def _truncate(text: str) -> str:
    return text[:EMBED_FIELD_MAX].strip() if text else ""
