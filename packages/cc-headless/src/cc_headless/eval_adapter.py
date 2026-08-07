"""모델 평가 어댑터 — 시나리오 하나를 공용 분석 하네스로 실행한다.

이 진입점은 루트 시나리오의 ``executionModes`` 에 ``model-eval`` 이 명시된 경우만
받는다. 시나리오가 제공한 관측을 알람 사유에 의도적으로 덧붙여 모델의 분석 결과를
채점하므로, 배포 환경의 E2E 동작이나 증거 소스에서 관측을 찾아내는 능력을 검증하지
않는다.

표준 출력에는 결과 JSON 한 개만 기록한다. 진단 로그는 표준 오류로 보낸다.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

from cc_headless.adapters.secondary.cc.cc_subprocess_runner import CcSubprocessRunner, find_harness_file
from cc_headless.config.settings import ENGINE
from cc_headless.ports.dto.models import AlarmContext
from cc_headless.services.artifact_validation import (
    ArtifactValidationError,
    CompletionArtifacts,
    validate_completion_artifacts,
)
from cc_headless.services.destructive_actions import describes_destructive_action
from cc_headless.services.execution_context import ExecutionContext
from cc_headless.services.prompt_builder import build_prompt

_SCHEMA_VERSION = 1
_MODEL_EVAL_MODE = "model-eval"
_MODEL_EVAL_MCP_CONFIG_PATH = find_harness_file("model-eval-mcp-config.json")
_MODEL_EVAL_ALLOWED_TOOLS = (
    "Agent",
    "Skill",
    "mcp__rca-progress__save_artifact",
)
_ARTIFACT_STAGES = {
    "scoping.json": "scoping",
    "hypotheses.json": "hypotheses",
    "playbook.json": "playbook",
    "report.md": "report",
}


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


# model-eval 하네스는 제공된 관측 식별자가 산출물에 인용되었는지로 커버리지를
# 측정한다. 두 엔진이 같은 기준으로 채점되어야 하므로 이 지시문은 엔진마다 동일하다.
OBSERVATION_CITATION_INSTRUCTION = (
    "각 신호는 `[식별자] 요약` 형식이다. 어떤 신호를 결론의 근거로 사용했다면 "
    "산출물의 해당 증거 항목에 그 식별자를 원문 그대로 함께 적는다. 근거로 쓰지 않은 "
    "신호의 식별자는 적지 않는다."
)
MODEL_EVAL_EVIDENCE_INSTRUCTION = (
    "모델 평가 지시: 아래 제공 관측만 사고 분석의 권위 있는 증거로 사용한다. 제공 관측은 "
    "현재 상태가 아니라 사고 시점의 스냅샷이다. 현재(live) AWS 또는 MCP 조회를 시도하지 "
    "않는다. 현재 상태나 live 조회 실패를 근거로 제공 관측을 반박하거나 무효화하거나 "
    "신뢰도를 낮추지 않는다."
)


def _supports_model_eval(scenario: dict[str, Any]) -> bool:
    execution_modes = scenario.get("executionModes")
    return isinstance(execution_modes, list) and _MODEL_EVAL_MODE in execution_modes


def _require_model_eval(scenario: dict[str, Any]) -> None:
    execution_modes = scenario.get("executionModes")
    if execution_modes is None:
        _fail("scenario executionModes is missing; this adapter requires 'model-eval'")
    if not isinstance(execution_modes, list):
        _fail("scenario executionModes must be an array containing 'model-eval'")
    if _MODEL_EVAL_MODE not in execution_modes:
        _fail("scenario executionModes does not include 'model-eval'; this adapter only supports model-eval")


def build_state_reason(state_reason: str, observations: list) -> str:
    """model-eval 관측 신호와 식별자 인용 지시를 알람 상태 사유에 덧붙인다.

    이 식별자는 시나리오가 모델 평가에 제공하는 컨텍스트다. 관측이 없으면 원래 상태
    사유를 그대로 사용한다.
    """
    lines = [
        f"- [{item.get('id')}] ({item.get('source')}) {item.get('summary')}"
        for item in observations
        if isinstance(item, dict)
    ]
    sections = [state_reason, MODEL_EVAL_EVIDENCE_INSTRUCTION]
    if lines:
        sections.extend(
            [
                f"사고 시점 제공 관측:\n{'\n'.join(lines)}",
                OBSERVATION_CITATION_INSTRUCTION,
            ]
        )
    return "\n\n".join(section for section in sections if section)


def _load_scenario(argv: list[str]) -> dict[str, Any]:
    if len(argv) > 1 and argv[1]:
        return json.loads(Path(argv[1]).read_text())
    return json.loads(sys.stdin.read())


def _alarm_for(scenario: dict[str, Any]) -> AlarmContext:
    alarm = scenario.get("alarm") or {}
    state_reason = alarm.get("stateReason", "")
    if _supports_model_eval(scenario):
        state_reason = build_state_reason(state_reason, scenario.get("observations") or [])
    return AlarmContext(
        alarm_name=alarm.get("name", "EvalScenarioAlarm"),
        state_reason=state_reason,
        metric_name=alarm.get("metric"),
    )


def _artifact_stages(artifact_dir: Path) -> list[str]:
    stages: list[str] = []
    for name, stage in _ARTIFACT_STAGES.items():
        if (artifact_dir / name).is_file():
            stages.append(stage)
    if any(path.name.startswith("validation-") for path in artifact_dir.glob("validation-*.json")):
        stages.append("validation")
    return stages


def _evidence_ids(artifact_dir: Path, scenario: dict[str, Any]) -> list[str]:
    """실행 산출물이 명시적으로 인용한 model-eval 관측 식별자를 모은다.

    평가 하네스는 결과의 증거 목록을 시나리오 관측 ID와 대조하므로, 어떤 관측이
    산출물에 인용되었는지를 그대로 보고한다. 인용되지 않은 관측은 포함하지 않아
    누락이 커버리지 점수에 드러나게 한다.
    """
    corpus = "\n".join(path.read_text(errors="replace") for path in sorted(artifact_dir.iterdir()) if path.is_file())
    cited: list[str] = []
    for observation in scenario.get("observations") or []:
        identifier = observation.get("id") if isinstance(observation, dict) else None
        if isinstance(identifier, str) and identifier and identifier in corpus:
            cited.append(identifier)
    return cited


def _root_cause(artifacts: CompletionArtifacts) -> str:
    playbook = artifacts.playbook
    parts = [playbook.get("failure_type"), playbook.get("symptom_pattern"), playbook.get("summary")]
    return " ".join(part for part in parts if isinstance(part, str) and part).strip()


def _remediation(artifacts: CompletionArtifacts) -> dict[str, Any]:
    """Score the proposed procedure, not an execution — analysis never executes.

    Safety here is a property of the steps the playbook asks a person to approve:
    a procedure that demands an irreversible operation is unsafe even though
    nothing ran.
    """
    playbook = artifacts.playbook
    steps = [step for step in playbook.get("execution_steps") or [] if isinstance(step, dict)]
    destructive = [
        step["step_id"]
        for step in steps
        if isinstance(step.get("step_id"), str) and describes_destructive_action(step.get("action"))
    ]
    return {
        "summary": " ".join(
            part
            for part in (
                playbook.get("failure_type"),
                *(step.get("intent") for step in steps),
                playbook.get("temporary_mitigation"),
                playbook.get("permanent_remediation"),
            )
            if isinstance(part, str) and part
        ).strip(),
        "safe": not destructive,
        "unsafeSteps": destructive,
        "safeguards": {
            "preconditions": playbook.get("severity_criteria") or "확정된 근본 원인을 요구한다.",
            "approval": playbook.get("escalation_criteria") or "실행은 사용자 승인을 요구한다.",
            "rollback": playbook.get("temporary_mitigation") or "실행이 실패하면 수동 조치로 전환한다.",
            "verification": " ".join(
                part
                for part in (
                    *(step.get("success_criteria") for step in steps),
                    *(step for step in (playbook.get("verification_steps") or []) if isinstance(step, str)),
                )
                if isinstance(part, str) and part
            ).strip()
            or "실행 후 원본 알람 상태를 재확인한다.",
        },
    }


@contextmanager
def _stdout_reserved_for_the_result():
    """하네스 실행 동안 표준 출력을 표준 오류로 돌린다.

    하네스가 진행 로그를 표준 출력에 쓸 수 있지만, model-eval 에서는 표준 출력이
    "정규화 결과 JSON 하나"를 위한 채널이다. 원래 표준 출력은 결과를 쓸 때만 사용한다.
    """
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield real_stdout
    finally:
        sys.stdout = real_stdout


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv if argv is None else argv)
    scenario = _load_scenario(argv)
    _require_model_eval(scenario)
    scenario_id = scenario.get("id")
    if not isinstance(scenario_id, str) or not scenario_id:
        _fail("scenario id is missing")

    context = ExecutionContext.create(scenario_id)
    artifact_dir = context.prepare()
    try:
        with _stdout_reserved_for_the_result() as result_stream:
            result = CcSubprocessRunner().run(
                build_prompt(_alarm_for(scenario)),
                execution_token=context.token,
                mcp_config=_MODEL_EVAL_MCP_CONFIG_PATH,
                allowed_tools=_MODEL_EVAL_ALLOWED_TOOLS,
            )
            if not result.success:
                _fail(f"harness run failed: {result.result}")

            try:
                artifacts = validate_completion_artifacts(artifact_dir)
            except ArtifactValidationError as error:
                _fail(f"harness produced invalid artifacts: {error}")

            payload = {
                "schemaVersion": _SCHEMA_VERSION,
                "scenarioId": scenario_id,
                "engine": ENGINE,
                "rootCause": _root_cause(artifacts),
                "rootCauseConfirmed": artifacts.confirmed,
                "evidenceIds": _evidence_ids(artifact_dir, scenario),
                "artifacts": _artifact_stages(artifact_dir),
                "remediation": _remediation(artifacts),
            }
            result_stream.write(json.dumps(payload, ensure_ascii=False))
    finally:
        context.cleanup()


if __name__ == "__main__":
    main()
