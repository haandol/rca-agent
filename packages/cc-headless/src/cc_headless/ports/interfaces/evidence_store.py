from __future__ import annotations

from abc import ABC, abstractmethod


class EvidenceStorePort(ABC):
    """실행 증거와 갱신 전 플레이북 사본의 주 보관소.

    실행 증거는 명령 단위로 누적되어 크기가 예측되지 않으므로 상태 저장소가 아니라
    오브젝트 저장소가 원본을 보유한다. 실행이 실패해도 지우지 않는다.
    """

    @abstractmethod
    def save_execution_evidence(self, execution_id: str, *, rca_id: str, evidence: dict) -> str: ...

    @abstractmethod
    def load_approved_playbook(self, approved_playbook_s3_key: str, *, playbook_digest: str) -> dict: ...

    @abstractmethod
    def save_playbook_snapshot(self, execution_id: str, *, rca_id: str, playbook: dict) -> str: ...

    @abstractmethod
    def save_retrospective_diff(self, execution_id: str, *, rca_id: str, diff: dict) -> str: ...
