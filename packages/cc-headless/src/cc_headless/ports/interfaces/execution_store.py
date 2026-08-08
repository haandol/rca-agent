from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from cc_headless.services.execution_state import ExecutionState


class ExecutionClaimDisposition(StrEnum):
    CLAIMED = "CLAIMED"
    # 이미 종료된 실행의 재전달. 확인 응답으로 큐에서 지운다.
    TERMINAL_DUPLICATE = "TERMINAL_DUPLICATE"
    # 다른 워커가 실행 중이거나 claim 이 아직 유효하다.
    CONTENDED = "CONTENDED"
    # 사전 예약이 없거나 큐 요청과 예약 내용이 일치하지 않는다.
    REJECTED = "REJECTED"
    # 만료된 실행을 재수행하지 않고 실패로 종결했다.
    EXPIRED_FAILED = "EXPIRED_FAILED"


@dataclass(frozen=True)
class ExecutionClaim:
    disposition: ExecutionClaimDisposition
    claim_token: str | None = None
    attempt: int = 1

    @property
    def acquired(self) -> bool:
        return self.disposition is ExecutionClaimDisposition.CLAIMED and self.claim_token is not None


@dataclass(frozen=True)
class ExecutionTarget:
    """실행이 근거로 쓰는 승인된 리포트의 플레이북.

    절차의 작업 서술은 자연어이고 대상 리소스 식별자와 리전은 실행 시점에 결정되므로,
    플레이북과 알람 컨텍스트를 함께 보유해야 실행할 대상이 확정된다.
    """

    rca_id: str
    engine: str
    alarm_name: str
    playbook: dict
    alarm_data: dict = field(default_factory=dict)
    report_s3_key: str = ""

    @property
    def metric_name(self) -> str:
        trigger = self.alarm_data.get("Trigger")
        if not isinstance(trigger, dict):
            return ""
        return str(trigger.get("MetricName") or "")


class ExecutionClaimLostError(RuntimeError):
    """claim 을 잃은 뒤의 쓰기 시도. 다른 워커가 같은 실행을 이어받았다."""


class ExecutionTargetUnavailableError(RuntimeError):
    """승인된 리포트의 플레이북을 로드할 수 없다."""


class ExecutionStorePort(ABC):
    @abstractmethod
    def claim_execution(
        self,
        execution_id: str,
        *,
        rca_id: str,
        engine: str,
        approval_id: str,
        requested_by: str,
        report_s3_key: str,
        approved_playbook_s3_key: str,
        playbook_digest: str,
        claim_seconds: int,
    ) -> ExecutionClaim: ...

    @abstractmethod
    def load_target(
        self,
        rca_id: str,
        engine: str,
        *,
        report_s3_key: str,
        playbook: dict,
    ) -> ExecutionTarget: ...

    @abstractmethod
    def update_state(
        self,
        execution_id: str,
        *,
        rca_id: str,
        state: ExecutionState,
        claim_token: str,
        summary: dict | None = None,
        error_reason: str = "",
        evidence_s3_key: str = "",
        retrospective_failure_reason: str = "",
    ) -> None: ...

    @abstractmethod
    def load_state(self, execution_id: str, *, rca_id: str) -> ExecutionState | None: ...

    @abstractmethod
    def claim_retrospective(self, execution_id: str, *, rca_id: str, claim_token: str) -> bool: ...

    @abstractmethod
    def record_retrospective(
        self,
        execution_id: str,
        *,
        rca_id: str,
        claim_token: str,
        status: str,
        summary: str,
        playbook_snapshot_s3_key: str = "",
        diff_s3_key: str = "",
    ) -> None: ...

    @abstractmethod
    def save_playbook_revision(
        self,
        rca_id: str,
        engine: str,
        playbook: dict,
        *,
        execution_id: str,
    ) -> None: ...

    @abstractmethod
    def publish_playbook_revision(
        self,
        rca_id: str,
        engine: str,
        playbook: dict,
        *,
        execution_id: str,
    ) -> None: ...
