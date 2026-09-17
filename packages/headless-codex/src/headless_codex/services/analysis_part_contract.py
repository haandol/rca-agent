"""Validate role-owned results without allowing model output to choose identity or source authority."""

from __future__ import annotations

import difflib
import hashlib
from copy import deepcopy


def _text(value: object, label: str) -> str:
    """Require meaningful prose while leaving source bytes and proposal text unmodified."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _strings(value: object, label: str) -> list[str]:
    """Keep explicit evidence references; absent or non-text entries are not fabricated."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string list")
    return value


def validate_recovery_narrative(result: dict) -> dict:
    """Recovery authors explain observations; only the server may supply a rollback plan or verification."""
    allowed = {"title", "summary", "reason", "evidence_refs", "limitations", "recommendation"}
    if not isinstance(result, dict) or set(result) != allowed:
        raise ValueError(
            "recovery result must contain only title, summary, reason, evidence_refs, limitations and recommendation"
        )
    if result["recommendation"] not in {"ROLLBACK", "UNAVAILABLE"}:
        raise ValueError("recommendation must be ROLLBACK or UNAVAILABLE")
    for name in ("title", "summary", "reason"):
        _text(result[name], name)
    for name in ("evidence_refs", "limitations"):
        _strings(result[name], name)
    return deepcopy(result)


def validate_operations(result: dict) -> dict:
    """Distinguish proposed controls from observed configuration and never invent passing CI tests."""
    if not isinstance(result, dict) or set(result) != {
        "title",
        "summary",
        "findings",
        "recommendations",
        "limitations",
    }:
        raise ValueError("operations result fields are incomplete or unsupported")
    for name in ("title", "summary"):
        _text(result[name], name)
    _strings(result["limitations"], "limitations")
    for group in ("findings", "recommendations"):
        if not isinstance(result[group], list):
            raise ValueError(f"{group} must be a list")
    for item in result["findings"]:
        if not isinstance(item, dict) or set(item) != {"statement", "status", "evidence_refs"}:
            raise ValueError("invalid operations finding")
        _text(item["statement"], "statement")
        if item["status"] not in {"OBSERVED", "UNVERIFIED"}:
            raise ValueError("finding status must be OBSERVED or UNVERIFIED")
        refs = _strings(item["evidence_refs"], "evidence_refs")
        if item["status"] == "OBSERVED" and not refs:
            raise ValueError("observed finding requires source references")
    required = {
        "title",
        "description",
        "stage",
        "priority",
        "check",
        "failure_condition",
        "verification_plan",
        "validation_status",
        "evidence_refs",
    }
    for item in result["recommendations"]:
        if not isinstance(item, dict) or not required <= set(item) or set(item) - required - {"owner"}:
            raise ValueError("invalid operations recommendation")
        for name in required - {"evidence_refs", "validation_status"}:
            _text(item[name], name)
        _strings(item["evidence_refs"], "evidence_refs")
        # This read-only profile has no test execution tool. Source text is not a test receipt.
        if item["validation_status"] != "NOT_RUN":
            raise ValueError("operations proposals require NOT_RUN without server-recorded test execution")
    return deepcopy(result)


def unavailable_proposal(reason: str) -> dict:
    """Expose missing exact source as unavailable rather than manufacturing a plausible patch."""
    return {
        "status": "UNAVAILABLE",
        "title": "코드 수정 미리보기 제공 불가",
        "files": [],
        "test_plan": [],
        "tests_status": "NOT_RUN",
        "limitations": [reason],
    }


def validate_code_proposal(result: dict, sources: list[dict]) -> dict:
    """Bind every patch to an actually read source, exact location, original bytes and computed diff."""
    if not isinstance(result, dict):
        raise ValueError("code proposal must be an object")
    required = {"status", "title", "files", "test_plan", "tests_status", "limitations"}
    if not required <= set(result) or set(result) - required - {"repository", "base_revision"}:
        raise ValueError("invalid code proposal fields")
    _text(result["title"], "title")
    _strings(result["test_plan"], "test_plan")
    _strings(result["limitations"], "limitations")
    if result["tests_status"] != "NOT_RUN":
        raise ValueError("code preview cannot claim tests ran without server-owned execution evidence")
    if result["status"] == "UNAVAILABLE":
        if result["files"] or not result["limitations"]:
            raise ValueError("unavailable proposal requires no files and an explicit limitation")
        return deepcopy(result)
    if result["status"] != "PROPOSED" or not isinstance(result["files"], list) or not result["files"]:
        raise ValueError("proposed code requires source-bound file changes")
    seen = set()
    for item in result["files"]:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "start_line",
            "end_line",
            "original",
            "proposed",
            "unified_diff",
            "evidence_refs",
        }:
            raise ValueError("invalid code proposal file")
        if item["path"] in seen:
            return unavailable_proposal("같은 파일의 중복 변경은 제공할 수 없습니다.")
        seen.add(item["path"])
        matches = [
            source
            for source in sources
            if source.get("path") == item["path"]
            and source.get("source_ref") in item["evidence_refs"]
            and (not result.get("repository") or source.get("repository") == result["repository"])
            and (not result.get("base_revision") or source.get("base_revision") == result["base_revision"])
        ]
        if len(matches) != 1:
            return unavailable_proposal("정확히 일치하는 저장소·리비전·파일 원본이 없습니다.")
        source = matches[0]
        phase = str(source.get("source_phase", source.get("phase", "supplied_snapshot"))).lower()
        if phase not in {"incident", "fault", "current", "supplied_snapshot", "snapshot"}:
            return unavailable_proposal(
                "정상 기준 또는 단계 미확인 소스를 장애 배포의 수정 원문으로 사용할 수 없습니다."
            )
        text = source.get("text")
        if not isinstance(text, str) or source.get("sha256") != hashlib.sha256(text.encode()).hexdigest():
            return unavailable_proposal("읽은 파일 원본의 내용 지문을 확인할 수 없습니다.")
        start, end = item["start_line"], item["end_line"]
        lines = text.splitlines(keepends=True)
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            return unavailable_proposal("제안한 행 범위가 실제 파일과 일치하지 않습니다.")
        original = "".join(lines[start - 1 : end])
        if item["original"] != original or not isinstance(item["proposed"], str):
            return unavailable_proposal("제안의 비교 원문이 실제로 읽은 파일과 다릅니다.")
        refs = _strings(item["evidence_refs"], "evidence_refs")
        if not source.get("source_ref") or source["source_ref"] not in refs:
            return unavailable_proposal("코드 변경이 실제 읽은 원본 참조에 연결되지 않았습니다.")
        updated = "".join(lines[: start - 1]) + item["proposed"] + "".join(lines[end:])
        expected = "".join(
            difflib.unified_diff(
                lines, updated.splitlines(keepends=True), fromfile=f"a/{item['path']}", tofile=f"b/{item['path']}"
            )
        )
        if not expected or item["unified_diff"] != expected:
            return unavailable_proposal("diff가 지정된 실제 원문과 제안 변경의 차이와 일치하지 않습니다.")
    return deepcopy(result)
