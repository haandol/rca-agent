from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from headless_codex.services.fault_taxonomy import FaultType, parse_fault_type

_VALIDATION_NAME = re.compile(r"validation-([1-9][0-9]*)\.json")
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


@dataclass(frozen=True)
class _HypothesisContext:
    fault_type: FaultType
    tree_id: str
    depth: int


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


def _validate_hypotheses(base: Path) -> tuple[dict[str, _HypothesisContext], str]:
    artifact = _load_object(base / "hypotheses.json", "hypotheses.json")
    _require_fields(artifact, strings=("stage", "tree_id", "summary", "output_summary"), lists=("hypotheses",))
    if artifact["stage"] != "HYPOTHESIS_GENERATION" or not artifact["hypotheses"]:
        raise ArtifactValidationError("hypotheses.json must contain generated hypotheses")

    hypotheses_by_id: dict[str, _HypothesisContext] = {}
    parent_by_id: dict[str, str | None] = {}
    for hypothesis in artifact["hypotheses"]:
        if not isinstance(hypothesis, dict):
            raise ArtifactValidationError("hypotheses.json entries must be objects")
        _require_fields(
            hypothesis,
            strings=("hypothesis_id", "tree_id", "title", "description", "category", "status", "fault_type"),
            lists=("required_evidence",),
        )
        fault_type = parse_fault_type(hypothesis["fault_type"])
        if fault_type is None:
            raise ArtifactValidationError("hypothesis fault_type is invalid")
        if hypothesis["hypothesis_id"] in hypotheses_by_id:
            raise ArtifactValidationError("hypotheses.json hypothesis_id values must be unique")
        confidence = hypothesis.get("confidence_score")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ArtifactValidationError("hypothesis confidence_score must be between 0 and 1")
        depth = hypothesis.get("depth")
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ArtifactValidationError("hypothesis depth must be a non-negative integer")
        if hypothesis["tree_id"] != artifact["tree_id"]:
            raise ArtifactValidationError("hypothesis tree_id must match hypotheses.json tree_id")
        if any(not isinstance(item, str) or not item.strip() for item in hypothesis["required_evidence"]):
            raise ArtifactValidationError("hypothesis required_evidence entries must be non-empty strings")
        parent_id = hypothesis.get("parent_id")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id.strip()):
            raise ArtifactValidationError("hypothesis parent_id must be null or a non-empty string")
        hypotheses_by_id[hypothesis["hypothesis_id"]] = _HypothesisContext(
            fault_type=fault_type,
            tree_id=hypothesis["tree_id"],
            depth=depth,
        )
        parent_by_id[hypothesis["hypothesis_id"]] = parent_id
    if any(parent_id not in hypotheses_by_id for parent_id in parent_by_id.values() if parent_id is not None):
        raise ArtifactValidationError("hypothesis parent_id references an unknown hypothesis")
    return hypotheses_by_id, artifact["tree_id"]


def _validation_candidates(base: Path, through_loop_index: int | None = None) -> list[tuple[int, Path]]:
    candidates: list[tuple[int, Path]] = []
    for path in base.iterdir():
        match = _VALIDATION_NAME.fullmatch(path.name)
        if not match:
            continue
        loop_index = int(match.group(1))
        if through_loop_index is None or loop_index <= through_loop_index:
            candidates.append((loop_index, path))
    if not candidates:
        raise ArtifactValidationError("validation-{N}.json is missing")
    return sorted(candidates)


def _validate_validation_loop(
    path: Path,
    artifact: dict,
    hypotheses_by_id: dict[str, _HypothesisContext],
    tree_id: str,
) -> tuple[dict[str, _HypothesisContext], dict[str, str]]:
    _require_fields(
        artifact,
        strings=("stage", "summary", "output_summary"),
        lists=("confirmed", "rejected", "needs_investigation", "closed", "new_hypotheses"),
    )
    loop_index = artifact.get("loop_index")
    expected_loop_index = int(_VALIDATION_NAME.fullmatch(path.name).group(1))
    if (
        artifact["stage"] != "VALIDATION"
        or isinstance(loop_index, bool)
        or not isinstance(loop_index, int)
        or loop_index != expected_loop_index
    ):
        raise ArtifactValidationError(f"{path.name} has an invalid stage or loop_index")

    referenced_ids: set[str] = set()
    classifications: dict[str, str] = {}
    for bucket in ("confirmed", "rejected", "needs_investigation", "closed"):
        for entry in artifact[bucket]:
            if not isinstance(entry, dict):
                raise ArtifactValidationError(f"{path.name} {bucket} entries must be objects")
            _require_fields(entry, strings=("hypothesis_id", "reasoning"))
            hypothesis_id = entry["hypothesis_id"]
            if hypothesis_id not in hypotheses_by_id:
                raise ArtifactValidationError(f"{path.name} references an unknown hypothesis")
            if hypothesis_id in referenced_ids:
                raise ArtifactValidationError(f"{path.name} references a hypothesis in multiple result buckets")
            referenced_ids.add(hypothesis_id)
            classifications[hypothesis_id] = bucket
            confidence = entry.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise ArtifactValidationError(f"{path.name} confidence must be between 0 and 1")
            if bucket == "confirmed":
                confirmed_fault = parse_fault_type(entry.get("fault_type"))
                if confirmed_fault is None:
                    raise ArtifactValidationError(f"{path.name} confirmed fault_type is invalid")
                if confirmed_fault is not hypotheses_by_id[hypothesis_id].fault_type:
                    raise ArtifactValidationError(f"{path.name} confirmed fault_type disagrees with hypothesis")
    confirmed_fault_types = {parse_fault_type(entry["fault_type"]) for entry in artifact["confirmed"]}
    if len(confirmed_fault_types) > 1:
        raise ArtifactValidationError(f"{path.name} confirmed entries disagree on fault_type")

    new_hypotheses: dict[str, _HypothesisContext] = {}
    for hypothesis in artifact["new_hypotheses"]:
        if not isinstance(hypothesis, dict):
            raise ArtifactValidationError(f"{path.name} new_hypotheses entries must be objects")
        _require_fields(
            hypothesis,
            strings=(
                "hypothesis_id",
                "tree_id",
                "title",
                "description",
                "category",
                "status",
                "fault_type",
                "parent_id",
            ),
            lists=("required_evidence",),
        )
        hypothesis_id = hypothesis["hypothesis_id"]
        if hypothesis_id in hypotheses_by_id or hypothesis_id in new_hypotheses:
            raise ArtifactValidationError(f"{path.name} new hypothesis IDs must be unique")
        if hypothesis["tree_id"] != tree_id:
            raise ArtifactValidationError(f"{path.name} new hypothesis tree_id is invalid")
        parent = hypotheses_by_id.get(hypothesis["parent_id"])
        if parent is None:
            raise ArtifactValidationError(f"{path.name} new hypothesis parent_id is unknown")
        if parse_fault_type(hypothesis["fault_type"]) is None:
            raise ArtifactValidationError(f"{path.name} new hypothesis fault_type is invalid")
        confidence = hypothesis.get("confidence_score")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ArtifactValidationError(f"{path.name} new hypothesis confidence_score must be between 0 and 1")
        depth = hypothesis.get("depth")
        if isinstance(depth, bool) or not isinstance(depth, int) or depth != parent.depth + 1:
            raise ArtifactValidationError(f"{path.name} new hypothesis depth must equal parent depth + 1")
        if any(not isinstance(item, str) or not item.strip() for item in hypothesis["required_evidence"]):
            raise ArtifactValidationError(
                f"{path.name} new hypothesis required_evidence entries must be non-empty strings"
            )
        new_hypotheses[hypothesis_id] = _HypothesisContext(
            fault_type=parse_fault_type(hypothesis["fault_type"]),
            tree_id=hypothesis["tree_id"],
            depth=depth,
        )
    return new_hypotheses, classifications


def _validate_validation(
    base: Path,
    hypotheses_by_id: dict[str, _HypothesisContext],
    tree_id: str,
    *,
    through_loop_index: int | None = None,
    classifications_by_id: dict[str, str] | None = None,
) -> tuple[Path, dict]:
    latest: tuple[Path, dict] | None = None
    for _, path in _validation_candidates(base, through_loop_index):
        artifact = _load_object(path, path.name)
        new_hypotheses, classifications = _validate_validation_loop(path, artifact, hypotheses_by_id, tree_id)
        hypotheses_by_id.update(new_hypotheses)
        if classifications_by_id is not None:
            for hypothesis_id in new_hypotheses:
                classifications_by_id[hypothesis_id] = "unclassified"
            classifications_by_id.update(classifications)
        latest = path, artifact
    if latest is None:
        raise ArtifactValidationError("validation-{N}.json is missing")
    return latest


def validate_validation_artifacts(
    base: Path,
    *,
    through_loop_index: int | None = None,
) -> tuple[Path, dict]:
    hypotheses_by_id, tree_id = _validate_hypotheses(base)
    return _validate_validation(
        base,
        hypotheses_by_id,
        tree_id,
        through_loop_index=through_loop_index,
    )


def _validate_playbook_shape(artifact: dict) -> list[str]:
    """Check what the playbook owns and return its execution step IDs in order."""
    _require_fields(artifact, strings=_PLAYBOOK_STRING_FIELDS, lists=_PLAYBOOK_LIST_FIELDS)
    if artifact["stage"] != "PLAYBOOK":
        raise ArtifactValidationError("playbook.json stage must be PLAYBOOK")
    # A playbook is only a draft until an execution and its retrospective have
    # exercised the procedure, so analysis may not claim any other status.
    if artifact["verification_status"] != _PLAYBOOK_DRAFT_STATUS:
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


def _validate_report(base: Path, playbook_step_ids: list[str]) -> str:
    try:
        markdown = (base / "report.md").read_text()
    except OSError as exc:
        raise ArtifactValidationError("report.md is missing") from exc
    sections = _validate_report_sections(markdown)

    # The prose is what a person approves and the structure is what runs, so a
    # step present in one and absent from the other means the approved procedure
    # and the executed procedure differ.
    playbook_section = sections["대응 플레이북"]
    missing = [step_id for step_id in playbook_step_ids if step_id not in playbook_section]
    if missing:
        raise ArtifactValidationError(f"report.md playbook section is missing execution steps: {', '.join(missing)}")
    # Order matters as much as presence: a person reads the prose top to bottom
    # and approves that sequence, so the structure must run it in the same order.
    positions = [playbook_section.index(step_id) for step_id in playbook_step_ids]
    if positions != sorted(positions):
        raise ArtifactValidationError("report.md playbook section lists execution steps out of order")
    return markdown


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

    if filename == "hypotheses.json":
        _require_fields(artifact, strings=("stage", "tree_id", "summary", "output_summary"), lists=("hypotheses",))
        if artifact["stage"] != "HYPOTHESIS_GENERATION" or not artifact["hypotheses"]:
            raise ArtifactValidationError("hypotheses.json must contain generated hypotheses")
        return


def validate_completion_artifacts(base: Path) -> CompletionArtifacts:
    _validate_scoping(base)
    hypotheses_by_id, tree_id = _validate_hypotheses(base)
    _, validation = _validate_validation(base, hypotheses_by_id, tree_id)
    confirmed = bool(validation["confirmed"])
    playbook, playbook_step_ids = _validate_playbook(base)
    # An unconfirmed root cause has no verified procedure to run, so proposing
    # steps for it would put guesswork behind the approval button.
    if not confirmed and playbook_step_ids:
        raise ArtifactValidationError("unconfirmed RCA must not declare playbook execution steps")
    report_markdown = _validate_report(base, playbook_step_ids)
    return CompletionArtifacts(
        report_markdown=report_markdown,
        playbook=playbook,
        confirmed=confirmed,
    )
