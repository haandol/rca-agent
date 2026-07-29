"""라이브 평가 어댑터 — 시나리오 하나를 배포된 하네스로 실행하고 정규화 결과를 낸다.

시나리오를 CloudWatch 알람 형태로 변환해 실제 하네스를 한 번 돌리고, 실행이
남긴 산출물을 공통 평가 스키마로 옮긴다. 하네스 자체는 로컬과 배포가 동일하므로
이 어댑터도 두 환경에서 같은 경로로 동작한다.

표준 출력에는 결과 JSON 한 개만 기록한다. 진단 로그는 표준 오류로 보낸다.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

from cc_headless.adapters.secondary.cc.cc_subprocess_runner import CcSubprocessRunner
from cc_headless.config.settings import ENGINE
from cc_headless.ports.dto.models import AlarmContext
from cc_headless.services.artifact_validation import (
    ArtifactValidationError,
    CompletionArtifacts,
    validate_completion_artifacts,
)
from cc_headless.services.execution_context import ExecutionContext
from cc_headless.services.prompt_builder import build_prompt

_SCHEMA_VERSION = 1
_ARTIFACT_STAGES = {
    "scoping.json": "scoping",
    "hypotheses.json": "hypotheses",
    "remediation.json": "remediation",
    "playbook.json": "playbook",
    "report.md": "report",
}


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


# 평가 하네스는 시나리오 관측 식별자가 산출물에 인용되었는지로 증거 커버리지를
# 측정한다. 두 엔진이 같은 기준으로 채점되어야 하므로 이 지시문은 엔진마다 동일하다.
OBSERVATION_CITATION_INSTRUCTION = (
    "각 신호는 `[식별자] 요약` 형식이다. 어떤 신호를 결론의 근거로 사용했다면 "
    "산출물의 해당 증거 항목에 그 식별자를 원문 그대로 함께 적는다. 근거로 쓰지 않은 "
    "신호의 식별자는 적지 않는다."
)


def build_state_reason(state_reason: str, observations: list) -> str:
    """관측 신호와 식별자 인용 지시를 알람 상태 사유에 덧붙인다.

    실제 운영 알람에는 이 식별자가 없다. 평가에서만 부여되는 컨텍스트이며, 관측이
    없으면 원래 상태 사유를 그대로 사용한다.
    """
    lines = [
        f"- [{item.get('id')}] ({item.get('source')}) {item.get('summary')}"
        for item in observations
        if isinstance(item, dict)
    ]
    if not lines:
        return state_reason
    signals = "\n".join(lines)
    return f"{state_reason}\n\n관측된 신호:\n{signals}\n\n{OBSERVATION_CITATION_INSTRUCTION}"


def _load_scenario(argv: list[str]) -> dict[str, Any]:
    if len(argv) > 1 and argv[1]:
        return json.loads(Path(argv[1]).read_text())
    return json.loads(sys.stdin.read())


def _alarm_for(scenario: dict[str, Any]) -> AlarmContext:
    alarm = scenario.get("alarm") or {}
    return AlarmContext(
        alarm_name=alarm.get("name", "EvalScenarioAlarm"),
        state_reason=build_state_reason(alarm.get("stateReason", ""), scenario.get("observations") or []),
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
    """실행 산출물이 실제로 인용한 시나리오 관측 식별자를 모은다.

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
    remediation = artifacts.remediation
    playbook = artifacts.playbook
    verification = remediation.get("verification") or {}
    return {
        "summary": " ".join(
            part
            for part in (
                remediation.get("status"),
                remediation.get("fault_type"),
                playbook.get("temporary_mitigation"),
                playbook.get("permanent_remediation"),
            )
            if isinstance(part, str) and part
        ).strip(),
        # 서버가 기록한 결과만 신뢰한다. 차단되거나 실패한 복구는 안전하지 않다.
        "safe": remediation.get("status") in {"NOT_ATTEMPTED", "SUCCEEDED"},
        "safeguards": {
            "preconditions": playbook.get("severity_criteria") or "확정된 근본 원인과 허용된 fault type을 요구한다.",
            "approval": playbook.get("escalation_criteria") or "허용 목록에 없는 원인은 사람의 판단을 요구한다.",
            "rollback": playbook.get("temporary_mitigation") or "복구가 실패하면 수동 조치로 전환한다.",
            "verification": " ".join(
                part
                for part in (
                    verification.get("status"),
                    *(step for step in (playbook.get("verification_steps") or []) if isinstance(step, str)),
                )
                if isinstance(part, str) and part
            ).strip()
            or "복구 후 원본 알람 상태를 재확인한다.",
        },
    }


@contextmanager
def _stdout_reserved_for_the_result():
    """하네스 실행 동안 표준 출력을 표준 오류로 돌린다.

    운영에서는 로그를 표준 출력으로 보내 컨테이너 로그 수집기가 가져가지만, 평가에서는
    표준 출력이 "정규화 결과 JSON 하나"를 위한 채널이다. 실제 표준 출력은 결과를 쓸
    때만 사용한다.
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
                "evidenceIds": _evidence_ids(artifact_dir, scenario),
                "artifacts": _artifact_stages(artifact_dir),
                "remediation": _remediation(artifacts),
            }
            result_stream.write(json.dumps(payload, ensure_ascii=False))
    finally:
        context.cleanup()


if __name__ == "__main__":
    main()
