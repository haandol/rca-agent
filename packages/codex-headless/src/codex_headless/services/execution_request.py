"""실행 요청의 해석.

승인이 곧 메시지다. 대시보드가 큐에 발행한 요청만이 실행의 진입점이므로, 요청이
누구의 승인인지와 어떤 리포트를 실행하는지를 여기서 확정한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

_ENGINES = frozenset({"strands", "codex-headless", "cc-headless"})


class InvalidExecutionRequestError(ValueError):
    """실행 요청으로 해석할 수 없는 메시지."""


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    rca_id: str
    engine: str
    approval_id: str
    requested_by: str
    report_s3_key: str
    approved_playbook_s3_key: str
    playbook_digest: str


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

    engine = _required_string(payload, "engine")
    if engine not in _ENGINES:
        raise InvalidExecutionRequestError(
            "execution request engine must be strands, codex-headless, or legacy cc-headless"
        )

    digest = _required_string(payload, "playbook_digest").lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise InvalidExecutionRequestError("execution request playbook_digest must be a SHA-256 hex digest")

    return ExecutionRequest(
        execution_id=_required_string(payload, "execution_id"),
        rca_id=_required_string(payload, "rca_id"),
        engine=engine,
        approval_id=_required_string(payload, "approval_id"),
        requested_by=_required_string(payload, "requested_by"),
        report_s3_key=_required_string(payload, "report_s3_key"),
        approved_playbook_s3_key=_required_string(payload, "approved_playbook_s3_key"),
        playbook_digest=digest,
    )
