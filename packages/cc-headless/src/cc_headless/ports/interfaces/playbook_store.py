from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PlaybookMatch:
    """벡터 인덱스가 돌려준 플레이북 후보.

    인덱스는 경량 메타데이터만 보유하므로 절차 상세는 여기에 없다. ``rca_id`` 가 상세를
    조회하는 키다.
    """

    playbook_id: str
    similarity: float
    failure_type: str = ""
    symptom_pattern: str = ""
    tags: list[str] = field(default_factory=list)
    rca_id: str = ""
    # 절차가 실행으로 입증되었는지는 상세를 로드하지 않고도 보여야 한다. 값이 없는
    # 레코드는 초안으로 읽는다 — 미검증 절차가 검증됨으로 보이면 안 된다.
    verification_status: str = "DRAFT"


class PlaybookStorePort(ABC):
    @abstractmethod
    def load_playbook(self, artifact_dir: Path) -> dict | None: ...

    @abstractmethod
    def search_similar(self, query_text: str, *, threshold: float) -> list[PlaybookMatch]:
        """임계값 이상으로 닮은 플레이북을 돌려준다.

        임계값을 호출자가 넘기는 이유는 그것이 인덱스의 성질이 아니라 호출 의도의
        성질이기 때문이다 — 병합은 단순 조회보다 엄격한 기준을 요구한다.
        """

    @abstractmethod
    def load_detail(self, match: PlaybookMatch) -> dict | None:
        """후보의 절차 상세를 로드한다. 불가하면 None.

        회고 개정본이 있으면 그것이 현재 절차다. 분석 원본을 보강 대상으로 삼으면
        회고가 교정한 인자와 순서가 같은 식별자로 덮어써진다.
        """

    @abstractmethod
    def save_to_s3_vectors(self, playbook: dict, rca_id: str, *, metric_name: str = "") -> bool: ...
