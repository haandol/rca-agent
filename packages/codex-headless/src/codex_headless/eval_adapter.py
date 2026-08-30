"""모델 평가 어댑터 — 시나리오 하나를 공용 분석 하네스로 실행한다.

이 진입점은 루트 시나리오의 ``executionModes`` 에 ``model-eval`` 이 명시된 경우만
받는다. 시나리오가 제공한 관측을 알람 사유에 의도적으로 덧붙여 모델의 분석 결과를
채점하므로, 배포 환경의 E2E 동작이나 증거 소스에서 관측을 찾아내는 능력을 검증하지
않는다.

표준 출력에는 결과 JSON 한 개만 기록한다. 진단 로그는 표준 오류로 보낸다.
"""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

from codex_headless.adapters.secondary.codex.codex_harness import MODEL_EVAL_PROFILE
from codex_headless.adapters.secondary.codex.codex_subprocess_runner import CodexSubprocessRunner
from codex_headless.config.settings import ENGINE
from codex_headless.ports.dto.models import AlarmContext, CodexResult
from codex_headless.services.artifact_validation import (
    ArtifactValidationError,
    CompletionArtifacts,
    validate_completion_artifacts,
)
from codex_headless.services.destructive_actions import describes_destructive_action
from codex_headless.services.execution_context import ExecutionContext
from codex_headless.services.execution_evidence import redact
from codex_headless.services.prompt_builder import build_prompt

_SCHEMA_VERSION = 2
_MODEL_EVAL_MODE = "model-eval"
_FAILURE_DIR_ENV = "RCA_EVAL_FAILURE_DIR"
_MAX_DIAGNOSTIC_CHARS = 20_000
_MAX_VALIDATION_ERROR_CHARS = 4_000
_MAX_ARTIFACT_CHARS = 1_000_000
_MAX_ARTIFACT_TOTAL_CHARS = 5_000_000
_MAX_ARTIFACT_FILES = 100
_REDACTED = "***REDACTED***"
_SECRET_KEY_MARKERS = (
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "apikey",
    "accesskey",
    "privatekey",
    "authorization",
)
_ARTIFACT_STAGES = {
    "scoping.json": "scoping",
    "hypotheses.json": "hypotheses",
    "playbook.json": "playbook",
    "report.md": "report",
}
_VALIDATION_ARTIFACT = re.compile(r"validation-[1-9][0-9]*\.json")
_VALIDATION_CLASSIFICATIONS = (
    "confirmed",
    "rejected",
    "needs_investigation",
    "closed",
)
_CANONICAL_FAULT_TYPES = (
    "db-leak",
    "high-cpu",
    "high-memory",
    "slow-query",
    "unsupported",
)


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for raw_name, raw_value in value.items():
            name = str(raw_name)
            normalized_name = re.sub(r"[-_.]", "", name).lower()
            safe[name] = (
                _REDACTED
                if any(marker in normalized_name for marker in _SECRET_KEY_MARKERS)
                else _redact_json_value(raw_value)
            )
        return safe
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def _bounded_redacted(value: object, limit: int) -> tuple[str, bool]:
    rendered = value if isinstance(value, str) else str(value)
    try:
        parsed = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        safe_value = redact(rendered)
    else:
        safe_value = (
            json.dumps(_redact_json_value(parsed), ensure_ascii=False)
            if isinstance(parsed, (dict, list))
            else redact(rendered)
        )
    return safe_value[:limit], len(safe_value) > limit


def _write_private_text(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _is_diagnostic_artifact(path: Path) -> bool:
    return (
        not path.is_symlink()
        and path.is_file()
        and (path.name in _ARTIFACT_STAGES or _VALIDATION_ARTIFACT.fullmatch(path.name) is not None)
    )


def _copy_failure_artifacts(source_dir: Path, destination_dir: Path) -> list[dict[str, Any]]:
    destination_dir.mkdir(mode=0o700)
    copied: list[dict[str, Any]] = []
    remaining = _MAX_ARTIFACT_TOTAL_CHARS
    for source in sorted(source_dir.iterdir()):
        if not _is_diagnostic_artifact(source):
            continue
        if remaining <= 0 or len(copied) >= _MAX_ARTIFACT_FILES:
            break
        with source.open(encoding="utf-8", errors="replace") as handle:
            content = handle.read(min(_MAX_ARTIFACT_CHARS, remaining) + 1)
        limit = min(_MAX_ARTIFACT_CHARS, remaining)
        safe_content, redaction_truncated = _bounded_redacted(content, limit)
        source_truncated = redaction_truncated or len(content) > limit
        _write_private_text(destination_dir / source.name, safe_content)
        copied.append({"name": source.name, "truncated": source_truncated})
        remaining -= len(safe_content)
    return copied


def _persist_failure_diagnostics(
    failure_root: Path,
    context: ExecutionContext,
    artifact_dir: Path,
    result: CodexResult,
    validation_error: ArtifactValidationError | None,
) -> Path:
    failure_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = failure_root / context.token
    destination.mkdir(mode=0o700)
    copied_artifacts = _copy_failure_artifacts(artifact_dir, destination / "artifacts")
    safe_result, result_truncated = _bounded_redacted(result.result, _MAX_DIAGNOSTIC_CHARS)
    safe_raw_output, raw_output_truncated = _bounded_redacted(result.raw_output, _MAX_DIAGNOSTIC_CHARS)
    safe_validation_error, validation_error_truncated = _bounded_redacted(
        validation_error,
        _MAX_VALIDATION_ERROR_CHARS,
    )
    diagnostic = {
        "schemaVersion": 1,
        "codexResult": {
            "success": result.success,
            "result": safe_result,
            "resultTruncated": result_truncated,
            "rawOutput": safe_raw_output,
            "rawOutputTruncated": raw_output_truncated,
        },
        "validationError": safe_validation_error if validation_error is not None else None,
        "validationErrorTruncated": validation_error_truncated,
        "artifacts": copied_artifacts,
    }
    _write_private_text(destination / "diagnostic.json", json.dumps(diagnostic, ensure_ascii=False, indent=2))
    return destination


def _preserve_failure_diagnostics(
    context: ExecutionContext,
    artifact_dir: Path,
    result: CodexResult,
    validation_error: ArtifactValidationError | None = None,
) -> None:
    configured_root = os.environ.get(_FAILURE_DIR_ENV)
    if not configured_root:
        return
    try:
        destination = _persist_failure_diagnostics(
            Path(configured_root),
            context,
            artifact_dir,
            result,
            validation_error,
        )
    except Exception as error:
        safe_error, _ = _bounded_redacted(error, _MAX_VALIDATION_ERROR_CHARS)
        print(f"failed to preserve eval failure diagnostics: {safe_error}", file=sys.stderr)
        return
    print(f"eval failure diagnostics preserved at {destination}", file=sys.stderr)


# model-eval 하네스는 제공된 관측 식별자가 산출물에 인용되었는지로 커버리지를
# 측정한다. 두 엔진이 같은 기준으로 채점되어야 하므로 이 지시문은 엔진마다 동일하다.
OBSERVATION_CITATION_INSTRUCTION = (
    "각 신호는 `[식별자] 요약` 형식이다. 어떤 신호를 결론의 근거로 사용했다면 "
    "산출물의 해당 증거 항목에 그 식별자를 원문 그대로 함께 적는다. 근거로 쓰지 않은 "
    "신호의 식별자는 적지 않는다. 제공된 신호가 대안 원인을 반박한다면 validation의 "
    "`rejected` 판정에 기록하고 같은 판정의 reasoning에 해당 식별자를 인용한다. "
    "증거가 불충분하면 `rejected`로 기록하지 않는다."
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
        if isinstance(identifier, str) and identifier and _contains_exact_identifier(corpus, identifier):
            cited.append(identifier)
    return cited


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _contains_exact_identifier(text: str, identifier: str) -> bool:
    pattern = rf"(?<![0-9A-Za-z_-]){re.escape(identifier)}(?![0-9A-Za-z_-])"
    return re.search(pattern, text) is not None


def _observation_ids(scenario: dict[str, Any]) -> list[str]:
    return [
        identifier
        for observation in scenario.get("observations") or []
        if isinstance(observation, dict) and isinstance((identifier := observation.get("id")), str) and identifier
    ]


def _validation_artifacts(artifact_dir: Path) -> list[dict[str, Any]]:
    candidates: list[tuple[int, Path]] = []
    for path in artifact_dir.glob("validation-*.json"):
        match = _VALIDATION_ARTIFACT.fullmatch(path.name)
        if match is not None:
            candidates.append((int(path.stem.removeprefix("validation-")), path))
    return [artifact for _, path in sorted(candidates) if (artifact := _read_json_object(path)) is not None]


def _latest_validation(artifact_dir: Path) -> dict[str, Any]:
    validations = _validation_artifacts(artifact_dir)
    return validations[-1] if validations else {}


def _latest_effective_entries(artifact_dir: Path, classification: str) -> list[dict[str, Any]]:
    effective: dict[str, tuple[int, str, dict[str, Any]]] = {}
    sequence = 0
    for validation in _validation_artifacts(artifact_dir):
        for bucket in _VALIDATION_CLASSIFICATIONS:
            for entry in validation.get(bucket) or []:
                if not isinstance(entry, dict):
                    continue
                hypothesis_id = entry.get("hypothesis_id")
                if not isinstance(hypothesis_id, str) or not hypothesis_id:
                    continue
                effective[hypothesis_id] = (sequence, bucket, entry)
                sequence += 1
    return [
        entry
        for _, bucket, entry in sorted(effective.values(), reverse=True, key=lambda item: item[0])
        if bucket == classification
    ]


def _root_fault_type(artifact_dir: Path) -> str:
    for entry in _latest_validation(artifact_dir).get("confirmed") or []:
        if isinstance(entry, dict) and entry.get("fault_type") in _CANONICAL_FAULT_TYPES:
            return entry["fault_type"]
    return "unsupported"


def _root_cause_evidence_ids(artifact_dir: Path, scenario: dict[str, Any]) -> list[str]:
    reasoning = "\n".join(
        entry["reasoning"]
        for entry in _latest_validation(artifact_dir).get("confirmed") or []
        if isinstance(entry, dict) and isinstance(entry.get("reasoning"), str)
    )
    return [
        identifier for identifier in _observation_ids(scenario) if _contains_exact_identifier(reasoning, identifier)
    ]


def _competing_cause_judgments(
    artifact_dir: Path,
    scenario: dict[str, Any],
) -> list[dict[str, Any]]:
    expectation = scenario.get("expectation")
    if not isinstance(expectation, dict):
        return []
    causes = [cause for cause in expectation.get("competingCauses") or [] if isinstance(cause, dict)]
    if not causes:
        return []

    rejected_entries = _latest_effective_entries(artifact_dir, "rejected")
    judgments: list[dict[str, Any]] = []
    for cause in causes:
        cause_id = cause.get("id")
        if not isinstance(cause_id, str) or not cause_id:
            continue
        required_evidence_ids = [
            identifier
            for identifier in cause.get("requiredEvidenceIds") or []
            if isinstance(identifier, str) and identifier
        ]
        supported_index = next(
            (
                index
                for index, entry in enumerate(rejected_entries)
                if required_evidence_ids
                and isinstance(entry.get("reasoning"), str)
                and all(
                    _contains_exact_identifier(entry["reasoning"], identifier) for identifier in required_evidence_ids
                )
            ),
            None,
        )
        if supported_index is not None:
            supported = rejected_entries.pop(supported_index)
            rationale = supported["reasoning"].strip()
            judgments.append(
                {
                    "causeId": cause_id,
                    "judgment": "rejected",
                    "rationale": rationale,
                    "evidenceIds": required_evidence_ids,
                }
            )
            continue

        judgments.append(
            {
                "causeId": cause_id,
                "judgment": "inconclusive",
                "rationale": "No effective rejected validation entry cites all required evidence.",
                "evidenceIds": [],
            }
        )
    return judgments


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
    playbook = artifacts.playbook if isinstance(artifacts.playbook, dict) else {}
    available = bool(playbook)
    steps = [step for step in playbook.get("execution_steps") or [] if isinstance(step, dict)]
    destructive_steps = [step for step in steps if describes_destructive_action(step.get("action"))]
    destructive = [step["step_id"] for step in destructive_steps if isinstance(step.get("step_id"), str)]
    execution_steps = [
        {
            "stepId": step.get("step_id"),
            "intent": step.get("intent"),
            "action": step.get("action"),
            "successCriteria": step.get("success_criteria"),
        }
        for step in steps
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
        ).strip()
        or "No remediation playbook is available.",
        "available": available,
        "verificationStatus": playbook.get("verification_status") or "DRAFT",
        "executionSteps": execution_steps,
        "safe": available and not destructive_steps,
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
            result = CodexSubprocessRunner().run(
                build_prompt(_alarm_for(scenario)),
                execution_token=context.token,
                profile=MODEL_EVAL_PROFILE,
            )
            if not result.success:
                _preserve_failure_diagnostics(context, artifact_dir, result)
                safe_result, _ = _bounded_redacted(result.result, _MAX_DIAGNOSTIC_CHARS)
                _fail(f"harness run failed: {safe_result}")

            try:
                artifacts = validate_completion_artifacts(artifact_dir)
            except ArtifactValidationError as error:
                _preserve_failure_diagnostics(context, artifact_dir, result, error)
                safe_error, _ = _bounded_redacted(error, _MAX_VALIDATION_ERROR_CHARS)
                _fail(f"harness produced invalid artifacts: {safe_error}")

            payload = {
                "schemaVersion": _SCHEMA_VERSION,
                "scenarioId": scenario_id,
                "engine": ENGINE,
                "rootCause": _root_cause(artifacts),
                "rootCauseConfirmed": artifacts.confirmed,
                "rootFaultType": _root_fault_type(artifact_dir),
                "rootCauseEvidenceIds": _root_cause_evidence_ids(artifact_dir, scenario),
                "evidenceIds": _evidence_ids(artifact_dir, scenario),
                "artifacts": _artifact_stages(artifact_dir),
                "remediation": _remediation(artifacts),
            }
            payload["competingCauseJudgments"] = _competing_cause_judgments(artifact_dir, scenario)
            result_stream.write(json.dumps(payload, ensure_ascii=False))
    finally:
        context.cleanup()


if __name__ == "__main__":
    main()
