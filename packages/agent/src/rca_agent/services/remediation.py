from __future__ import annotations

import json
import logging
import urllib.request

from rca_agent.config.settings import REMEDIATION_RESET_TIMEOUT_SECONDS
from rca_agent.ports.dto.models import (
    FaultType,
    RcaReport,
    RemediationAction,
    RemediationResult,
)

logger = logging.getLogger(__name__)

HEALTHCARE_SERVICE_PORT = 8000
_MAX_RESET_RESPONSE_BYTES = 64 * 1024
_SUCCESSFUL_RESET_STATUSES = {"stopped", "not_running"}


def _call_fault_reset(service_host: str, endpoint: str) -> tuple[bool, str]:
    url = f"http://{service_host}:{HEALTHCARE_SERVICE_PORT}{endpoint}"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=REMEDIATION_RESET_TIMEOUT_SECONDS) as resp:
            payload = resp.read(_MAX_RESET_RESPONSE_BYTES + 1)
            if len(payload) > _MAX_RESET_RESPONSE_BYTES:
                return False, "response body exceeds size limit"
            try:
                response_text = payload.decode("utf-8")
                body = json.loads(response_text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False, "response body is not valid JSON"
            if not isinstance(body, dict):
                return False, "response JSON must be an object"

            reset_status = body.get("status")
            if reset_status is not None and reset_status not in _SUCCESSFUL_RESET_STATUSES:
                return False, f"non-success status: {reset_status}"
            return True, response_text
    except Exception as e:
        return False, str(e)


_RESET_ENDPOINTS = {
    FaultType.DB_CONNECTION_LEAK: "/fault/db-leak/reset",
    FaultType.HIGH_CPU: "/fault/high-cpu/reset",
    FaultType.HIGH_MEMORY: "/fault/high-memory/reset",
    FaultType.SLOW_QUERY: "/fault/slow-query/reset",
}


def _determine_reset_endpoint(fault_type: FaultType) -> str | None:
    return _RESET_ENDPOINTS.get(fault_type)


def execute_remediation(
    *,
    report: RcaReport,
    fault_type: FaultType = FaultType.UNSUPPORTED,
    service_host: str,
) -> RemediationResult:
    actions: list[RemediationAction] = []

    endpoint = _determine_reset_endpoint(fault_type)

    if endpoint:
        action = RemediationAction(
            action_type="fault_reset_api",
            description=f"Call {endpoint} on healthcare service",
            target=f"{service_host}:{HEALTHCARE_SERVICE_PORT}",
            parameters={"endpoint": endpoint},
        )
        success, response = _call_fault_reset(service_host, endpoint)
        action.executed = True
        action.success = success
        if not success:
            action.error = response
        logger.info(
            "Remediation action executed",
            extra={"endpoint": endpoint, "success": success, "response": response[:500]},
        )
        actions.append(action)
    else:
        actions.append(
            RemediationAction(
                action_type="no_action",
                description="Unsupported remediation action; manual intervention required",
                executed=False,
            )
        )

    executed_actions = [action for action in actions if action.executed]
    overall = bool(executed_actions) and all(action.success for action in executed_actions)
    summary_parts = []
    for a in actions:
        status = "SUCCESS" if a.success else ("FAILED" if a.executed else "SKIPPED")
        summary_parts.append(f"[{status}] {a.description}")

    return RemediationResult(
        rca_id=report.rca_id,
        actions_taken=actions,
        overall_success=overall,
        summary="; ".join(summary_parts),
    )
