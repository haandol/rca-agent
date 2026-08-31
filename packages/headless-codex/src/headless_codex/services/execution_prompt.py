"""실행·회고 에이전트에 전달하는 프롬프트.

플레이북 절차와 알람 컨텍스트를 프롬프트로 전달한다. 절차의 `action` 은 자연어이고
리소스 식별자와 리전은 실행 시점 컨텍스트에서 결정되므로, 둘을 함께 주지 않으면
에이전트가 무엇을 조작할지 확정할 수 없다.
"""

from __future__ import annotations

import json

from headless_codex.ports.interfaces.execution_store import ExecutionTarget
from headless_codex.services.execution_evidence import ExecutionEvidence

_EXECUTION_STEP_FIELDS = ("step_id", "intent", "action", "success_criteria")


def _render_steps(playbook: dict) -> str:
    steps = playbook.get("execution_steps")
    if not isinstance(steps, list) or not steps:
        return ""
    lines: list[str] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        lines.append(f"### {index}. {step.get('step_id', '')}")
        lines.append(f"- 의도: {step.get('intent', '')}")
        lines.append(f"- 수행할 작업: {step.get('action', '')}")
        lines.append(f"- 성공 판정 기준: {step.get('success_criteria', '')}")
        lines.append("")
    return "\n".join(lines).strip()


def build_execution_prompt(target: ExecutionTarget, *, execution_id: str) -> str:
    steps = _render_steps(target.playbook)
    if not steps:
        raise ValueError("playbook has no execution steps to run")

    alarm_summary = json.dumps(target.alarm_data, ensure_ascii=False, indent=2)[:4000]

    return f"""# 플레이북 실행 요청

사용자가 아래 플레이북 절차의 실행을 승인했다. 절차를 순서대로 수행하고 이슈 해소
여부를 관측해 기록한다.

- 실행 식별자: {execution_id}
- 분석 식별자: {target.rca_id}
- 분석 엔진: {target.engine}
- 알람: {target.alarm_name}
- 플레이북: {target.playbook.get("playbook_id", "")}
- 장애 유형: {target.playbook.get("failure_type", "")}

## 증상 패턴

{target.playbook.get("symptom_pattern", "")}

## 관련 메트릭

{json.dumps(target.playbook.get("related_metrics", []), ensure_ascii=False)}

## 실행 절차

{steps}

## 알람 컨텍스트

절차의 작업 서술은 자연어이므로 대상 리소스 식별자와 리전을 여기서 결정한다.

```json
{alarm_summary}
```

## 수행 계약

1. 절차마다 `run_playbook_command` 로 명령을 실행한다. 실패하면 오류 출력으로 인자를
   교정해 다시 시도하고, 거부된 명령은 우회하지 않는다. verification-only 절차도
   안전한 읽기 전용 AWS CLI 명령을 최소 한 번 이 도구로 실행한다. CloudWatch MCP 직접
   조회는 성공 기준 관측에는 사용할 수 있지만 attempt 증거가 아니므로 이를 대신하지
   못한다.
2. 절차마다 `record_step_outcome` 으로 `success_criteria` 관측 결과를 기록한다.
3. 마지막에 `record_resolution` 으로 이슈 해소 여부를 기록한다. 관측으로 확정할 수
   없으면 `resolved=false` 와 사유를 남긴다. `resolved=true` 호출이
   `missing_attempt_step_ids` 또는 `missing_outcome_step_ids` 를 반환하면 최종 응답
   전에 해당 절차 기록을 보완하고 `record_resolution` 을 다시 호출한다.
"""


def build_retrospective_prompt(
    target: ExecutionTarget,
    evidence: ExecutionEvidence,
    *,
    execution_id: str,
) -> str:
    playbook_steps = json.dumps(
        [
            {name: step.get(name, "") for name in _EXECUTION_STEP_FIELDS}
            for step in target.playbook.get("execution_steps", [])
            if isinstance(step, dict)
        ],
        ensure_ascii=False,
        indent=2,
    )
    evidence_json = json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2)[:60_000]

    return f"""# 플레이북 회고 요청

아래 실행은 이슈를 해소했다. 실행 증거에서 **절차의 결함으로 환원되는 실패**를 찾아
플레이북 절차를 교정한다.

- 실행 식별자: {execution_id}
- 분석 식별자: {target.rca_id}
- 플레이북: {target.playbook.get("playbook_id", "")}
- 장애 유형: {target.playbook.get("failure_type", "")}

## 실행 전 플레이북 절차

```json
{playbook_steps}
```

## 실행 증거

```json
{evidence_json}
```

## 판단 기준

- 인자 오류·선행 조건 누락·순서 오류·권한 부족은 절차의 결함이다.
- 재시도로 같은 명령이 성공했다면 그 실패는 절차 결함이 아니다.
- `failure_class` 가 `TRANSIENT`·`THROTTLED`·`TIMEOUT`·`UNKNOWN` 인 실패는 교정하지
  않는다.
- 차단된 절차는 실행 계층이 앞으로도 거부하므로 영구 조치 권고로 옮기는 교정만
  제안한다.

`save_playbook_update` 로 갱신안과 근거를 저장한다. 교정할 결함이 없으면 저장하지
않고 그 사실을 응답으로 남긴다.
"""
