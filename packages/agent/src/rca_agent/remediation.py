from __future__ import annotations

import logging
import urllib.request

from rca_agent.models import (
    Playbook,
    RcaReport,
    RemediationAction,
    RemediationResult,
)

logger = logging.getLogger(__name__)

HEALTHCARE_SERVICE_PORT = 8000


def _call_fault_reset(service_host: str, endpoint: str) -> tuple[bool, str]:
    url = f"http://{service_host}:{HEALTHCARE_SERVICE_PORT}{endpoint}"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return True, body
    except Exception as e:
        return False, str(e)


def _determine_reset_endpoint(root_cause: str, playbook: Playbook | None) -> str | None:
    root_lower = root_cause.lower()
    playbook_text = ""
    if playbook:
        playbook_text = (
            playbook.failure_type + " " + playbook.symptom_pattern + " " + playbook.temporary_mitigation
        ).lower()

    combined = root_lower + " " + playbook_text

    db_keywords = ["connection leak", "connection pool", "db leak", "too many connections", "database connection"]
    if any(kw in combined for kw in db_keywords):
        return "/fault/db-leak/reset"
    if any(kw in combined for kw in ["cpu", "high cpu", "cpu utilization", "cpu spike"]):
        return "/fault/high-cpu/reset"
    if any(kw in combined for kw in ["memory", "high memory", "memory pressure", "oom"]):
        return "/fault/high-memory/reset"
    if any(kw in combined for kw in ["slow query", "query latency", "read latency", "pg_sleep"]):
        return "/fault/slow-query/reset"

    return None


def execute_remediation(
    *,
    report: RcaReport,
    playbook: Playbook | None,
    service_host: str,
    ecs_cluster: str = "",
    ecs_service: str = "",
) -> RemediationResult:
    actions: list[RemediationAction] = []

    endpoint = _determine_reset_endpoint(report.root_cause, playbook)

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
        if ecs_cluster and ecs_service:
            action = _force_ecs_deployment(ecs_cluster, ecs_service)
            actions.append(action)
        else:
            actions.append(
                RemediationAction(
                    action_type="no_action",
                    description="Could not determine remediation action from root cause",
                    executed=False,
                )
            )

    overall = all(a.success for a in actions if a.executed)
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


def _force_ecs_deployment(cluster: str, service: str) -> RemediationAction:
    import boto3

    action = RemediationAction(
        action_type="ecs_force_deploy",
        description=f"Force new deployment on {cluster}/{service}",
        target=f"{cluster}/{service}",
        parameters={"cluster": cluster, "service": service},
    )
    try:
        ecs = boto3.client("ecs")
        ecs.update_service(
            cluster=cluster,
            service=service,
            forceNewDeployment=True,
        )
        action.executed = True
        action.success = True
        logger.info("ECS force deployment triggered", extra={"cluster": cluster, "service": service})
    except Exception as e:
        action.executed = True
        action.success = False
        action.error = str(e)
        logger.error("ECS force deployment failed", extra={"error": str(e)})

    return action
