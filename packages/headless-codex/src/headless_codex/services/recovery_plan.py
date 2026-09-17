"""Build the early-only fixed rollback from server observations, never model command text."""

from __future__ import annotations

import shlex
import uuid
from copy import deepcopy

from headless_codex.services.execution_contract import validate_steps
from headless_codex.services.runbook_contract import validate_runbook


def build_recovery_plan(rca_id: str, alarm_data: dict, prepared: dict) -> dict:
    """Require verified input compatibility and exact normal/current context before exposing READY."""
    verification = deepcopy(prepared.get("verification", {}))
    if verification.get("status") != "VERIFIED" or not prepared.get("context"):
        return {"approval_status": "UNAVAILABLE", "verification": {**verification, "valid": False}}
    context = deepcopy(prepared["context"])
    try:
        original = prepared["frozen_observations"]["baseline"]
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
        alarm_name = alarm_data.get("AlarmName") or alarm_data.get("alarm_name")
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
                "success_criteria": "현재 배포와 서비스가 승인 전제와 일치함",
                "commands": [
                    command(
                        "ecs", "describe-services", "--cluster", control["cluster"], "--services", control["service"]
                    )
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
        book = {
            "stage": "PLAYBOOK",
            "playbook_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{rca_id}:{context['baseline_ref']['sha256']}")),
            "rca_id": rca_id,
            "failure_type": "검증된 배포 롤백",
            "symptom_pattern": str(alarm_name),
            "severity_criteria": "서버가 정상 기준·현재 배포·입력 호환성을 검증함",
            "temporary_mitigation": "고정된 정상 배포로 롤백",
            "permanent_remediation": "근본원인 분석 결과를 별도로 검토",
            "escalation_criteria": "승인 전제 불일치 시 재검토",
            "verification_status": "DRAFT",
            "summary": "원인 확정과 별개인 서버 검증 롤백 제안",
            "output_summary": "미실행 초안이며 사용자 승인이 필요함",
            "execution_steps": steps,
            "rollback_context": context,
            "verification_steps": [steps[-1]["success_criteria"]],
            "prevention_measures": [],
            "related_metrics": [v["metric_name"] for v in metrics.values()],
            "tags": [],
        }
        validate_runbook(steps)
        validate_steps(book)
        from headless_codex.services.analysis_parts import approval_digest, validate_recovery_operations

        validate_recovery_operations(book)
        return {
            "approval_status": "READY",
            "playbook": book,
            "runbook_digest": approval_digest(book),
            "verification": {**verification, "valid": True, "rollback_context": context},
        }
    except (KeyError, ValueError, TypeError) as exc:
        return {"approval_status": "UNAVAILABLE", "verification": {"valid": False, "reason": str(exc)[:300]}}
