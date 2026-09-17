"""Approved service operations must remain identifiable to the execution operator."""

import copy
import json
import re

from test_service_deployment import plan as plan

from headless_codex.ports.interfaces.execution_store import ExecutionTarget
from headless_codex.services.execution_prompt import build_execution_prompt


def render(book):
    return build_execution_prompt(
        ExecutionTarget(
            rca_id="rca",
            engine="strands",
            alarm_name="alarm",
            playbook=book,
            alarm_data={"confirmed": False},
            source_part="recovery",
        ),
        execution_id="execution",
    )


def operations(prompt):
    steps = prompt.split("## 실행 절차\n", 1)[1].split("## 알람 컨텍스트", 1)[0]
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", steps, re.S)]


def test_approved_rollback_wait_and_metrics_survive_prompt_projection(plan):
    original = copy.deepcopy(plan)
    prompt = render(plan)
    projected = operations(prompt)
    assert projected[0]["commands"] == plan["execution_steps"][0]["commands"]
    assert projected[1] == {"deployment_wait": plan["execution_steps"][1]["deployment_wait"]}
    assert projected[2] == {"metric_wait": plan["execution_steps"][2]["metric_wait"]}
    assert "wait_for_service_deployment(step_id)" in prompt
    assert plan == original


def test_approved_precondition_is_explanatory_and_never_inferred_from_alarm(plan):
    prompt = render(plan)
    assert operations(prompt)[0]["ecs_service_precondition"] == plan["execution_steps"][0]["ecs_service_precondition"]
    assert "읽기 전용 설명" in prompt and "모델의 판단은 실행 권한이 아니다" in prompt
    del plan["execution_steps"][0]["ecs_service_precondition"]
    assert "ecs_service_precondition" not in operations(render(plan))[0]


def test_legacy_command_only_has_no_invented_wait_or_precondition():
    book = {"execution_steps": [{"step_id": "read", "commands": ["aws ecs describe-services --services app"]}]}
    assert operations(render(book)) == [{"commands": ["aws ecs describe-services --services app"]}]
