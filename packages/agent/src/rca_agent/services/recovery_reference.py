"""Construct current-incident reference steps for the model, never publication authority."""

from __future__ import annotations

import shlex
from copy import deepcopy

from rca_agent.services.analysis_parts import validate_recovery_operations
from rca_agent.services.deployment_baseline import build_rollback_context, validate_observed_plan
from rca_agent.services.runbook_contract import validate_runbook


def build_recovery_reference(scoping, verification: dict) -> dict:
    """Validate a five-step example from existing proof; the model still authors the final plan.

    Step construction follows Headless recovery_plan semantics. This helper has no
    model, storage, approval or execution capability and never creates a proof.
    """
    context = deepcopy(verification.get("rollback_context"))
    if verification.get("valid") is not True or not context or context != build_rollback_context(scoping):
        raise ValueError("verified current recovery context unavailable for reference")
    original = scoping.incident_observations.baseline
    metrics = {name: deepcopy(original["metrics"][name]) for name in ("attempts", "failures")}
    scope = context["scope"]
    control = {
        "account_id": scope["account_id"],
        "region": scope["region"],
        "cluster": scope["cluster_arn"],
        "service": scope["service_arn"],
        "container_name": scope["container_name"],
        "desired_count": scope["desired_count"],
    }
    alarm_name = scoping.raw_alarm.alarm_name if scoping.raw_alarm else None
    if not alarm_name or not context.get("write_accounting"):
        raise ValueError("alarm or observed completed-write accounting unavailable")
    guard = {
        **control,
        "expected_task_definition": context["current"]["task_definition_arn"],
        "expected_image_digest": context["current"]["image_digest"],
        "expected_deployment_id": context["current"]["deployment_id"],
        "service_settings": context["service_settings"],
    }
    wait = {
        **control,
        "action_step_id": "rollback",
        "task_definition": context["normal"]["task_definition_arn"],
        "image_digest": context["normal"]["image_digest"],
        "max_wait_seconds": 900,
    }

    def command(service, operation, *args):
        """Quote observed arguments as a single immutable AWS argv; this builder does not execute it."""
        return shlex.join(
            [
                "aws",
                service,
                operation,
                *args,
                "--region",
                scope["region"],
                *([] if operation == "update-service" else ["--output", "json"]),
            ]
        )

    steps = [
        {
            "step_id": "observe",
            "intent": "현재 승인 대상 확인",
            "action": "현재 서비스 배포를 조회한다",
            "success_criteria": (
                "DescribeServices 응답의 serviceArn·clusterArn이 승인 scope와 일치하고, "
                "taskDefinition·PRIMARY deployments[].id가 승인 current의 task_definition_arn·deployment_id와 일치함"
            ),
            "commands": [
                command("ecs", "describe-services", "--cluster", control["cluster"], "--services", control["service"])
            ],
        },
        {
            "step_id": "rollback",
            "intent": "검증된 정상 버전으로 복원",
            "action": "정상 태스크 정의로 서비스를 갱신한다",
            "success_criteria": "정확한 정상 태스크 정의의 배포 요청이 접수됨",
            "ecs_service_precondition": guard,
            "commands": [
                command(
                    "ecs",
                    "update-service",
                    "--cluster",
                    control["cluster"],
                    "--service",
                    control["service"],
                    "--task-definition",
                    wait["task_definition"],
                )
            ],
        },
        {
            "step_id": "converge",
            "intent": "정상 배포와 결함 버전 종료 확인",
            "action": "서비스와 실제 앱 태스크의 수렴을 기다린다",
            "success_criteria": "정상 정의·이미지로 수렴하고 결함 태스크가 종료됨",
            "deployment_wait": wait,
        },
        {
            "step_id": "discover",
            "intent": "현재 지표·알람 좌표 확인",
            "action": "지표 목록과 대상 알람을 조회한다",
            "success_criteria": "승인된 지표 및 알람 메타데이터를 조회함",
            "commands": [
                command("cloudwatch", "list-metrics", "--namespace", metrics["attempts"]["namespace"]),
                command("cloudwatch", "describe-alarms", "--alarm-names", alarm_name),
            ],
        },
        {
            "step_id": "verify",
            "intent": "실제 저장 회복 확인",
            "action": "최초 배포 수렴 이후 고정된 두 구간을 검증한다",
            "success_criteria": (
                f"첫 두 완결 60초 구간에서 {metrics['attempts']['metric_name']} > 0, "
                f"{metrics['failures']['metric_name']} = 0, 완료 쓰기 재개와 {alarm_name} OK를 확인함"
            ),
            "metric_wait": {
                "deployment_step_id": "converge",
                "region": scope["region"],
                "metrics": metrics,
                "failure_alarm_name": alarm_name,
                "max_wait_seconds": 900,
                "completed_work_evidence": {
                    "record_index": "approved_context",
                    "json_pointer": "/playbook/rollback_context/write_accounting",
                },
            },
        },
    ]
    validate_runbook(steps)
    validate_observed_plan(steps, context, scoping)
    validate_recovery_operations({"execution_steps": steps, "rollback_context": context})
    return {"execution_steps": steps}
