"""실행 요청의 해석.

승인이 곧 메시지다. 대시보드가 큐에 발행한 요청만이 실행의 진입점이므로, 요청이
누구의 승인인지와 어떤 리포트를 실행하는지를 여기서 확정한다.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

# 실행 식별자는 요청 내용에서 결정론적으로 파생한다. 같은 승인이 재전달되어도 같은
# 식별자가 나와야 claim 이 중복 실행을 걸러낼 수 있다.
_EXECUTION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "rca-agent/playbook-execution")


class InvalidExecutionRequestError(ValueError):
    """실행 요청으로 해석할 수 없는 메시지."""


@dataclass(frozen=True)
class ExecutionRequest:
    rca_id: str
    engine: str
    approval_id: str
    requested_by: str = ""
    report_s3_key: str = ""

    @property
    def execution_id(self) -> str:
        return str(uuid.uuid5(_EXECUTION_NAMESPACE, f"{self.rca_id}#{self.engine}#{self.approval_id}"))


def _required_string(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidExecutionRequestError(f"execution request is missing {field}")
    return value.strip()


def parse_execution_request(message_body: str) -> ExecutionRequest:
    """큐 메시지를 실행 요청으로 해석한다.

    Raises:
        InvalidExecutionRequestError: 승인 주체나 대상을 확정할 수 없을 때. 실행은
            승인 없이 시작될 수 없으므로 판정 불가 요청은 실행하지 않는다.
    """
    try:
        payload = json.loads(message_body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidExecutionRequestError("execution request is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidExecutionRequestError("execution request must be a JSON object")

    # 알람 큐와 같은 토픽 구독으로 흘러 들어온 메시지를 실행 요청으로 오인하지
    # 않는다. 승인 게이트가 경로 자체에 있다는 전제가 여기서 무너지면 안 된다.
    if "AlarmName" in payload or "Trigger" in payload:
        raise InvalidExecutionRequestError("alarm notifications are not execution requests")

    return ExecutionRequest(
        rca_id=_required_string(payload, "rca_id"),
        engine=_required_string(payload, "engine"),
        approval_id=_required_string(payload, "approval_id"),
        requested_by=str(payload.get("requested_by") or ""),
        report_s3_key=str(payload.get("report_s3_key") or ""),
    )
