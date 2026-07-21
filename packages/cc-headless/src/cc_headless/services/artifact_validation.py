from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from cc_headless.services.remediation_policy import RESET_PATHS, HealthcareFaultType, parse_fault_type

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
    "복구 결과",
    "검증 상태",
    "Action Items",
)
_REMEDIATION_STATUSES = {"SUCCEEDED", "FAILED", "BLOCKED"}
_VERIFICATION_STATUSES = {"NORMALIZED", "FAILED", "PENDING"}
_PLAYBOOK_STRING_FIELDS = (
    "stage",
    "playbook_id",
    "failure_type",
    "symptom_pattern",
    "severity_criteria",
    "temporary_mitigation",
    "permanent_remediation",
    "escalation_criteria",
    "summary",
    "output_summary",
)
_PLAYBOOK_LIST_FIELDS = (
    "related_metrics",
    "verification_steps",
    "prevention_measures",
    "tags",
)


class ArtifactValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CompletionArtifacts:
    report_markdown: str
    playbook: dict
    remediation: dict
    confirmed: bool


@dataclass(frozen=True)
class RemediationEvidence:
    validation_artifact: str
    fault_type: HealthcareFaultType
    confirmed_hypothesis_ids: tuple[str, ...]


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


def _validate_scoping(base: Path) -> None:
    artifact = _load_object(base / "scoping.json", "scoping.json")
    _require_fields(artifact, strings=("stage", "alarm_name", "impact_scope", "severity", "summary", "output_summary"))
    if artifact["stage"] != "SCOPING":
        raise ArtifactValidationError("scoping.json stage must be SCOPING")
    if artifact["impact_scope"] not in {"single", "service", "regional"}:
        raise ArtifactValidationError("scoping.json impact_scope is invalid")
    if artifact["severity"] not in {"low", "medium", "high", "critical"}:
        raise ArtifactValidationError("scoping.json severity is invalid")
    if not isinstance(artifact.get("metric_snapshot"), dict):
        raise ArtifactValidationError("scoping.json metric_snapshot must be an object")


def _validate_hypotheses(base: Path) -> tuple[dict[str, HealthcareFaultType], str]:
    artifact = _load_object(base / "hypotheses.json", "hypotheses.json")
    _require_fields(artifact, strings=("stage", "tree_id", "summary", "output_summary"), lists=("hypotheses",))
    if artifact["stage"] != "HYPOTHESIS_GENERATION" or not artifact["hypotheses"]:
        raise ArtifactValidationError("hypotheses.json must contain generated hypotheses")

    hypotheses_by_id: dict[str, HealthcareFaultType] = {}
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
        hypotheses_by_id[hypothesis["hypothesis_id"]] = fault_type
        parent_by_id[hypothesis["hypothesis_id"]] = parent_id
    if any(parent_id not in hypotheses_by_id for parent_id in parent_by_id.values() if parent_id is not None):
        raise ArtifactValidationError("hypothesis parent_id references an unknown hypothesis")
    return hypotheses_by_id, artifact["tree_id"]


def _latest_validation(base: Path) -> tuple[Path, dict]:
    candidates: list[tuple[int, Path]] = []
    for path in base.iterdir():
        match = _VALIDATION_NAME.fullmatch(path.name)
        if match:
            candidates.append((int(match.group(1)), path))
    if not candidates:
        raise ArtifactValidationError("validation-{N}.json is missing")
    path = max(candidates)[1]
    return path, _load_object(path, path.name)


def _validate_validation(
    base: Path,
    hypotheses_by_id: dict[str, HealthcareFaultType],
    tree_id: str,
) -> tuple[Path, dict]:
    path, artifact = _latest_validation(base)
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
            confidence = entry.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise ArtifactValidationError(f"{path.name} confidence must be between 0 and 1")
            if bucket == "confirmed":
                confirmed_fault = parse_fault_type(entry.get("fault_type"))
                if confirmed_fault is None:
                    raise ArtifactValidationError(f"{path.name} confirmed fault_type is invalid")
                if confirmed_fault is not hypotheses_by_id[hypothesis_id]:
                    raise ArtifactValidationError(f"{path.name} confirmed fault_type disagrees with hypothesis")
    confirmed_fault_types = {parse_fault_type(entry["fault_type"]) for entry in artifact["confirmed"]}
    if len(confirmed_fault_types) > 1:
        raise ArtifactValidationError(f"{path.name} confirmed entries disagree on fault_type")

    new_hypothesis_ids: set[str] = set()
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
        if hypothesis_id in hypotheses_by_id or hypothesis_id in new_hypothesis_ids:
            raise ArtifactValidationError(f"{path.name} new hypothesis IDs must be unique")
        if hypothesis["tree_id"] != tree_id:
            raise ArtifactValidationError(f"{path.name} new hypothesis tree_id is invalid")
        if hypothesis["parent_id"] not in hypotheses_by_id:
            raise ArtifactValidationError(f"{path.name} new hypothesis parent_id is unknown")
        if parse_fault_type(hypothesis["fault_type"]) is None:
            raise ArtifactValidationError(f"{path.name} new hypothesis fault_type is invalid")
        confidence = hypothesis.get("confidence_score")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ArtifactValidationError(f"{path.name} new hypothesis confidence_score must be between 0 and 1")
        depth = hypothesis.get("depth")
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
            raise ArtifactValidationError(f"{path.name} new hypothesis depth must be a positive integer")
        if any(not isinstance(item, str) or not item.strip() for item in hypothesis["required_evidence"]):
            raise ArtifactValidationError(
                f"{path.name} new hypothesis required_evidence entries must be non-empty strings"
            )
        new_hypothesis_ids.add(hypothesis_id)
    return path, artifact


def validate_remediation_evidence(base: Path) -> RemediationEvidence:
    hypotheses_by_id, tree_id = _validate_hypotheses(base)
    validation_path, validation = _validate_validation(base, hypotheses_by_id, tree_id)
    confirmed = validation["confirmed"]
    if not confirmed:
        raise ArtifactValidationError(f"{validation_path.name} has no confirmed root cause")
    if any(entry["confidence"] < 0.8 for entry in confirmed):
        raise ArtifactValidationError(f"{validation_path.name} confirmed confidence must be at least 0.8")

    fault_type = parse_fault_type(confirmed[0]["fault_type"])
    if fault_type is None:
        raise ArtifactValidationError(f"{validation_path.name} confirmed fault_type is invalid")
    return RemediationEvidence(
        validation_artifact=validation_path.name,
        fault_type=fault_type,
        confirmed_hypothesis_ids=tuple(entry["hypothesis_id"] for entry in confirmed),
    )


def _default_remediation(validation_artifact: str) -> dict:
    return {
        "stage": "REMEDIATION",
        "status": "NOT_ATTEMPTED",
        "fault_type": None,
        "endpoint_path": None,
        "validation_artifact": validation_artifact,
        "confirmed_hypothesis_ids": [],
        "summary": "unconfirmed root cause",
        "output_summary": "NOT_ATTEMPTED: unconfirmed root cause",
        "verification": {
            "status": "PENDING",
            "reason": "reset was not attempted",
        },
    }


def _validate_remediation(base: Path, validation_artifact: str, confirmed: bool) -> dict:
    path = base / "remediation.json"
    if not path.is_file():
        if confirmed:
            raise ArtifactValidationError("confirmed RCA requires server-owned remediation.json")
        return _default_remediation(validation_artifact)

    artifact = _load_object(path, "remediation.json")
    _require_fields(artifact, strings=("stage", "status", "summary", "output_summary"))
    if artifact["stage"] != "REMEDIATION" or artifact["status"] not in _REMEDIATION_STATUSES:
        raise ArtifactValidationError("remediation.json has an invalid stage or status")
    if not confirmed:
        raise ArtifactValidationError("unconfirmed RCA must not contain remediation.json")

    fault_type = artifact.get("fault_type")
    endpoint_path = artifact.get("endpoint_path")
    if not isinstance(fault_type, str) or not fault_type:
        raise ArtifactValidationError("remediation.json fault_type must be a non-empty string")
    parsed_fault_type = parse_fault_type(fault_type)
    if parsed_fault_type is None:
        raise ArtifactValidationError("remediation.json fault_type is invalid")
    if artifact["status"] == "BLOCKED":
        if endpoint_path is not None:
            raise ArtifactValidationError("blocked remediation must not declare an endpoint_path")
    elif parsed_fault_type not in RESET_PATHS or endpoint_path != RESET_PATHS[parsed_fault_type]:
        raise ArtifactValidationError("remediation.json fault_type and endpoint_path do not match")
    if artifact.get("validation_artifact") != validation_artifact:
        raise ArtifactValidationError("remediation.json does not reference the latest validation")
    if not isinstance(artifact.get("confirmed_hypothesis_ids"), list) or not artifact["confirmed_hypothesis_ids"]:
        raise ArtifactValidationError("remediation.json must record confirmed hypothesis IDs")

    verification = artifact.get("verification")
    if not isinstance(verification, dict) or verification.get("status") not in _VERIFICATION_STATUSES:
        raise ArtifactValidationError("remediation.json verification status is missing or invalid")
    return artifact


def _validate_playbook(base: Path, remediation: dict) -> dict:
    artifact = _load_object(base / "playbook.json", "playbook.json")
    _require_fields(artifact, strings=_PLAYBOOK_STRING_FIELDS, lists=_PLAYBOOK_LIST_FIELDS)
    if artifact["stage"] != "PLAYBOOK":
        raise ArtifactValidationError("playbook.json stage must be PLAYBOOK")

    reported = artifact.get("remediation_result")
    if not isinstance(reported, dict):
        raise ArtifactValidationError("playbook.json remediation_result must be an object")
    expected = {
        "status": remediation["status"],
        "fault_type": remediation.get("fault_type"),
        "endpoint_path": remediation.get("endpoint_path"),
        "validation_artifact": remediation.get("validation_artifact"),
    }
    for field, value in expected.items():
        if reported.get(field) != value:
            raise ArtifactValidationError(f"playbook remediation_result.{field} disagrees with server result")
    if reported.get("reason") != remediation["summary"]:
        raise ArtifactValidationError("playbook remediation_result.reason disagrees with server result")

    verification = reported.get("verification")
    if not isinstance(verification, dict):
        raise ArtifactValidationError("playbook remediation_result.verification must be an object")
    if verification.get("status") != remediation["verification"]["status"]:
        raise ArtifactValidationError("playbook verification status disagrees with server result")
    return artifact


def _section(markdown: str, title: str) -> str:
    match = re.search(
        rf"^## {re.escape(title)}\s*\n(?P<body>.*?)(?=^## |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    if not match or not match.group("body").strip():
        raise ArtifactValidationError(f"report.md required section is missing or empty: {title}")
    return match.group("body")


def _validate_report(base: Path, remediation: dict) -> str:
    try:
        markdown = (base / "report.md").read_text()
    except OSError as exc:
        raise ArtifactValidationError("report.md is missing") from exc
    sections = {title: _section(markdown, title) for title in _REPORT_SECTIONS}

    evidence_window = sections["증거 시간 범위"].lower()
    if "current alarm window" not in evidence_window or "historical comparison window" not in evidence_window:
        raise ArtifactValidationError("report.md must distinguish current and historical evidence windows")
    for label in ("current alarm window", "historical comparison window"):
        line = next((line for line in evidence_window.splitlines() if label in line), "")
        if len(_ISO_TIMESTAMP.findall(line)) < 2:
            raise ArtifactValidationError(f"report.md {label} must include ISO-8601 start and end timestamps")

    remediation_text = sections["복구 결과"]
    verification_text = sections["검증 상태"]
    expected_values = (
        remediation["status"],
        remediation.get("fault_type"),
        remediation.get("endpoint_path"),
        remediation.get("validation_artifact"),
    )
    for value in expected_values:
        rendered = "N/A" if value is None else str(value)
        if rendered not in remediation_text:
            raise ArtifactValidationError(f"report.md remediation section is missing server value: {rendered}")
    verification_status = remediation["verification"]["status"]
    if verification_status not in verification_text:
        raise ArtifactValidationError("report.md verification section disagrees with server result")
    return markdown


def validate_completion_artifacts(base: Path) -> CompletionArtifacts:
    _validate_scoping(base)
    hypotheses_by_id, tree_id = _validate_hypotheses(base)
    validation_path, validation = _validate_validation(base, hypotheses_by_id, tree_id)
    confirmed = bool(validation["confirmed"])
    remediation = _validate_remediation(base, validation_path.name, confirmed)
    playbook = _validate_playbook(base, remediation)
    report_markdown = _validate_report(base, remediation)
    return CompletionArtifacts(
        report_markdown=report_markdown,
        playbook=playbook,
        remediation=remediation,
        confirmed=confirmed,
    )
