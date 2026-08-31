from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from headless_codex.services.analysis_contract import (
    AnalysisContractError,
    AnalysisResult,
    generation_round_for_filename,
    replay_analysis,
    validate_analysis_completion,
)
from headless_codex.services.fault_taxonomy import FaultType

_ISO_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
    re.IGNORECASE,
)
_REPORT_SECTIONS = (
    "인시던트 요약",
    "영향",
    "증거 시간 범위",
    "근본 원인",
    "5 Whys",
    "뒷받침 증거",
    "가설 분석 경로",
    "대응 플레이북",
    "Action Items",
)
_PLAYBOOK_DRAFT_STATUS = "DRAFT"
_PLAYBOOK_STRING_FIELDS = (
    "stage",
    "playbook_id",
    "failure_type",
    "symptom_pattern",
    "severity_criteria",
    "temporary_mitigation",
    "permanent_remediation",
    "escalation_criteria",
    "verification_status",
    "summary",
    "output_summary",
)
_PLAYBOOK_LIST_FIELDS = (
    "related_metrics",
    "verification_steps",
    "prevention_measures",
    "tags",
    "execution_steps",
)
_EXECUTION_STEP_FIELDS = ("step_id", "intent", "action", "success_criteria")

# 관측 형태를 계약으로 두지 않으면 모델이 조회한 시계열을 스스로 두 숫자로 요약하고,
# 하류 단계는 지속 상승과 급증을 구별할 근거를 잃는다. 두 엔진이 같은 형태를 쓴다 —
# 한쪽만 시퀀스를 보유하면 판정 차이가 관측 품질 차이로 오염된다.
#
# 추세 해석은 모델이 하고 게이트는 근거 없는 단정만 막는다. 어휘에 담기지 않는 형태는
# `shape_note` 로 서술하므로, 다섯 항목이 관측의 표현력을 제한하지 않는다.
_METRIC_OBSERVATION_TRENDS = ("rising", "falling", "flat", "spike", "unknown")
_MIN_OBSERVATION_DATAPOINTS = 2


class ArtifactValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CompletionArtifacts:
    report_markdown: str
    playbook: dict
    confirmed: bool
    root_cause: str = ""
    selected_hypothesis_id: str = ""
    confidence: float = 0.0
    root_fault_type: FaultType = FaultType.UNSUPPORTED


def _load_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except OSError as exc:
        raise ArtifactValidationError(f"{label} is missing") from exc
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ArtifactValidationError(f"{label} must be a JSON object")
    return value


def _require_fields(artifact: dict, *, strings: tuple[str, ...] = (), lists: tuple[str, ...] = ()) -> None:
    for field in strings:
        if not isinstance(artifact.get(field), str) or not artifact[field].strip():
            raise ArtifactValidationError(f"required string field is missing: {field}")
    for field in lists:
        if not isinstance(artifact.get(field), list):
            raise ArtifactValidationError(f"required list field is missing: {field}")


def _validate_scoping_shape(artifact: dict) -> None:
    """저장 시점과 완료 게이트가 공유하는 scoping.json 검사.

    두 계층이 각자 검사하면 한쪽만 필드를 요구해, 저장은 통과하고 완료에서 버려지는
    리포트가 생긴다.
    """
    _require_fields(
        artifact,
        strings=("stage", "alarm_name", "impact_scope", "severity", "summary", "output_summary"),
        lists=("metric_observations", "concurrent_alarms"),
    )
    if artifact["stage"] != "SCOPING":
        raise ArtifactValidationError("scoping.json stage must be SCOPING")
    if artifact["impact_scope"] not in {"single", "service", "regional"}:
        raise ArtifactValidationError("scoping.json impact_scope is invalid")
    if artifact["severity"] not in {"low", "medium", "high", "critical"}:
        raise ArtifactValidationError("scoping.json severity is invalid")

    for observation in artifact["metric_observations"]:
        if not isinstance(observation, dict):
            raise ArtifactValidationError("scoping.json metric_observations entries must be objects")
        _require_fields(observation, strings=("metric_name", "trend"), lists=("datapoints",))
        if observation["trend"] not in _METRIC_OBSERVATION_TRENDS:
            raise ArtifactValidationError(f"scoping.json observation trend is invalid: {observation['trend']}")
        datapoints = observation["datapoints"]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in datapoints):
            raise ArtifactValidationError("scoping.json datapoints must be numbers")
        # 두 점 미만이면 형태를 알 수 없다. 그것이 unknown 이라는 값이 있는 이유이고,
        # 시퀀스를 요구한 이유이기도 하다.
        if len(datapoints) < _MIN_OBSERVATION_DATAPOINTS and observation["trend"] != "unknown":
            raise ArtifactValidationError(
                f"scoping.json observation claims trend '{observation['trend']}' "
                f"from {len(datapoints)} datapoint(s); a trend needs at least {_MIN_OBSERVATION_DATAPOINTS}"
            )

    for alarm in artifact["concurrent_alarms"]:
        if not isinstance(alarm, dict):
            raise ArtifactValidationError("scoping.json concurrent_alarms entries must be objects")
        _require_fields(alarm, strings=("alarm_name", "state"))


def _validate_scoping(base: Path) -> None:
    _validate_scoping_shape(_load_object(base / "scoping.json", "scoping.json"))


def validate_validation_artifacts(
    base: Path,
    *,
    through_loop_index: int | None = None,
) -> tuple[Path, dict]:
    try:
        result = replay_analysis(
            base,
            through_loop_index=through_loop_index,
        )
    except AnalysisContractError as exc:
        raise ArtifactValidationError(str(exc)) from exc
    loop_index = result.latest_validation.get("loop_index")
    if not isinstance(loop_index, int):
        raise ArtifactValidationError("validation-{N}.json is missing")
    return base / f"validation-{loop_index}.json", result.latest_validation


def _validate_playbook_shape(artifact: dict, *, allow_verified: bool = False) -> list[str]:
    """Check what the playbook owns and return its execution step IDs in order."""
    _require_fields(artifact, strings=_PLAYBOOK_STRING_FIELDS, lists=_PLAYBOOK_LIST_FIELDS)
    if artifact["stage"] != "PLAYBOOK":
        raise ArtifactValidationError("playbook.json stage must be PLAYBOOK")
    # A playbook is only a draft until an execution and its retrospective have
    # exercised the procedure, so analysis may not claim any other status.
    allowed_statuses = {_PLAYBOOK_DRAFT_STATUS, "VERIFIED"} if allow_verified else {_PLAYBOOK_DRAFT_STATUS}
    if artifact["verification_status"] not in allowed_statuses:
        raise ArtifactValidationError(f"playbook.json verification_status must be {_PLAYBOOK_DRAFT_STATUS}")

    step_ids: list[str] = []
    for step in artifact["execution_steps"]:
        if not isinstance(step, dict):
            raise ArtifactValidationError("playbook.json execution_steps entries must be objects")
        _require_fields(step, strings=_EXECUTION_STEP_FIELDS)
        if step["step_id"] in step_ids:
            raise ArtifactValidationError("playbook.json execution step IDs must be unique")
        step_ids.append(step["step_id"])
    return step_ids


def _validate_playbook(base: Path) -> tuple[dict, list[str]]:
    artifact = _load_object(base / "playbook.json", "playbook.json")
    return artifact, _validate_playbook_shape(artifact)


def _section(markdown: str, title: str) -> str:
    match = re.search(
        rf"^## {re.escape(title)}\s*\n(?P<body>.*?)(?=^## |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    if not match or not match.group("body").strip():
        raise ArtifactValidationError(f"report.md required section is missing or empty: {title}")
    return match.group("body")


def _validate_report_sections(markdown: str) -> dict[str, str]:
    sections = {title: _section(markdown, title) for title in _REPORT_SECTIONS}

    evidence_window = sections["증거 시간 범위"].lower()
    if "current alarm window" not in evidence_window or "historical comparison window" not in evidence_window:
        raise ArtifactValidationError("report.md must distinguish current and historical evidence windows")
    for label in ("current alarm window", "historical comparison window"):
        line = next((line for line in evidence_window.splitlines() if label in line), "")
        if len(_ISO_TIMESTAMP.findall(line)) < 2:
            raise ArtifactValidationError(f"report.md {label} must include ISO-8601 start and end timestamps")
    return sections


def _replace_section(markdown: str, title: str, body: str) -> str:
    pattern = re.compile(
        rf"^## {re.escape(title)}\s*\n.*?(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    replacement = f"## {title}\n{body.strip()}\n\n"
    if pattern.search(markdown) is None:
        raise ArtifactValidationError(f"report.md required section is missing or empty: {title}")
    return pattern.sub(replacement, markdown, count=1).rstrip() + "\n"


def _render_root_cause(analysis: AnalysisResult) -> str:
    selected = analysis.selected_hypothesis
    status = "확정" if analysis.confirmed else "미확정 — 가장 유력한 후보"
    return "\n".join(
        [
            f"- **상태**: {status}",
            f"- **신뢰도**: {analysis.selected_confidence:.2f}",
            f"- **선택 가설 ID**: `{selected.hypothesis_id}`",
            f"- **가설 제목**: {selected.title}",
            "",
            selected.description,
        ]
    )


def _render_playbook(playbook: dict, *, confirmed: bool) -> str:
    steps = playbook["execution_steps"]
    if not confirmed:
        return (
            "확정된 근본 원인이 없어 실행 절차를 만들지 않았다. 이 리포트의 조치 항목은 "
            "추가 조사와 사람의 판단을 위한 권고이며 실행 대상이 아니다."
        )
    if not steps:
        return (
            "플레이북 생성 결과에 실행 절차가 없다. 분석 결과는 유효하지만 승인할 절차가 없으므로 실행 대상이 아니다."
        )

    lines = [
        "이 플레이북은 **초안(DRAFT)**이며 아직 실행으로 검증되지 않았다.",
        "",
    ]
    for index, step in enumerate(steps, start=1):
        lines.extend(
            [
                f"### {index}. {step['step_id']}",
                "",
                f"- **의도**: {step['intent']}",
                f"- **수행할 작업**: {step['action']}",
                f"- **성공 판정 기준**: {step['success_criteria']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def _validate_report(base: Path, playbook: dict, analysis: AnalysisResult) -> str:
    try:
        markdown = (base / "report.md").read_text()
    except OSError as exc:
        raise ArtifactValidationError("report.md is missing") from exc
    _validate_report_sections(markdown)
    markdown = _replace_section(markdown, "근본 원인", _render_root_cause(analysis))
    markdown = _replace_section(
        markdown,
        "대응 플레이북",
        _render_playbook(playbook, confirmed=analysis.confirmed),
    )
    _validate_report_sections(markdown)
    return markdown


def render_completion_report(base: Path, playbook: dict) -> str:
    """Render the report again from the final search-first playbook.

    The report specialist writes the candidate playbook. Search-first enrichment
    may replace it with the accumulated playbook, and the report, completed
    session, notification, and search index must all use that same final value.
    """
    _validate_playbook_shape(playbook, allow_verified=True)
    try:
        analysis = validate_analysis_completion(base)
    except AnalysisContractError as exc:
        raise ArtifactValidationError(str(exc)) from exc
    if not analysis.confirmed and playbook["execution_steps"]:
        raise ArtifactValidationError("unconfirmed RCA must not declare playbook execution steps")
    return _validate_report(base, playbook, analysis)


def validate_artifact_shape(filename: str, content: str) -> None:
    """Reject an artifact whose own shape the completion gate would reject.

    This runs while the writing agent can still act on the error. It checks only
    fields the artifact owns — cross-artifact agreement with server-owned results
    is not knowable yet at save time and stays in the completion gate.
    """
    if filename == "report.md":
        _validate_report_sections(content)
        return

    try:
        artifact = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(f"{filename} is not valid JSON: {exc}") from exc
    if not isinstance(artifact, dict):
        raise ArtifactValidationError(f"{filename} must be a JSON object")

    if filename == "playbook.json":
        _validate_playbook_shape(artifact)
        return

    if filename == "scoping.json":
        _validate_scoping_shape(artifact)
        return

    if generation_round_for_filename(filename) is not None:
        _require_fields(artifact, strings=("stage", "tree_id", "summary", "output_summary"), lists=("hypotheses",))
        if artifact["stage"] != "HYPOTHESIS_GENERATION":
            raise ArtifactValidationError(f"{filename} stage must be HYPOTHESIS_GENERATION")
        return


def validate_completion_artifacts(base: Path) -> CompletionArtifacts:
    _validate_scoping(base)
    try:
        analysis = validate_analysis_completion(base)
    except AnalysisContractError as exc:
        raise ArtifactValidationError(str(exc)) from exc
    playbook, playbook_step_ids = _validate_playbook(base)
    # An unconfirmed root cause has no verified procedure to run, so proposing
    # steps for it would put guesswork behind the approval button.
    if not analysis.confirmed and playbook_step_ids:
        raise ArtifactValidationError("unconfirmed RCA must not declare playbook execution steps")
    report_markdown = _validate_report(base, playbook, analysis)
    return CompletionArtifacts(
        report_markdown=report_markdown,
        playbook=playbook,
        confirmed=analysis.confirmed,
        root_cause=analysis.selected_hypothesis.description,
        selected_hypothesis_id=analysis.selected_hypothesis.hypothesis_id,
        confidence=analysis.selected_confidence,
        root_fault_type=analysis.selected_fault_type,
    )
