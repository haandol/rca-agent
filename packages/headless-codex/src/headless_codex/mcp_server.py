"""격리 산출물 저장 MCP server.

이 서버는 분석 실행에만 제공되며 쓰기 도구를 노출하지 않는다. 복구는 사용자 승인
뒤 별도 실행 에이전트가 수행한다.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

import boto3
from fastmcp import FastMCP

from headless_codex.adapters.secondary.session.dynamodb_session_store import DynamoDbSessionStore
from headless_codex.config import settings
from headless_codex.services.analysis_contract import (
    AnalysisContractError,
    generation_round_for_filename,
    normalize_generation_artifact,
    normalize_validation_artifact,
    validate_analysis_completion,
)
from headless_codex.services.artifact_validation import (
    ArtifactValidationError,
    validate_artifact_shape,
)
from headless_codex.services.artifact_watcher import (
    _save_hypotheses_to_ddb,
    _update_hypotheses_from_validation,
    _write_span,
)
from headless_codex.services.execution_context import (
    CLAIM_TOKEN_ENV,
    RCA_ID_ENV,
    RUN_TOKEN_ENV,
    artifact_dir_for_token,
)

mcp = FastMCP("rca-progress")

_VALIDATION_ARTIFACT = re.compile(r"validation-[1-9][0-9]*\.json")

# 산출물은 역할별 도구로 갈라 저장한다. 한 도구가 모든 파일명을 받으면 어느 역할이
# 무엇을 썼는지 서버가 알 수 없고, 분리는 프롬프트 지시로만 남는다. 도구를 나누면
# 에이전트에 부여된 도구 목록이 그대로 경계가 되어, 분석 역할이 리포트를 쓰는 경로가
# 도구 부재로 막힌다.
_ANALYSIS_ARTIFACTS = {
    "scoping.json",
    "hypotheses.json",
}
_REPORT_ARTIFACTS = {
    "playbook.json",
    "report.md",
}


def _artifact_dir() -> Path | None:
    token = os.environ.get(RUN_TOKEN_ENV, "")
    try:
        artifact_dir = artifact_dir_for_token(token)
    except ValueError:
        return None
    return artifact_dir if artifact_dir.is_dir() and not artifact_dir.is_symlink() else None


def _is_allowed_filename(filename: str, allowed: set[str], *, allow_validation: bool) -> bool:
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        return False
    if filename in allowed:
        return True
    if not allow_validation:
        return False
    return _VALIDATION_ARTIFACT.fullmatch(filename) is not None or generation_round_for_filename(filename) is not None


def _write_artifact(base: Path, filename: str, content: str) -> Path:
    path = base / filename
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=base,
            prefix=f".{filename}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return path


def _runtime_session() -> tuple[DynamoDbSessionStore, object, str, str] | None:
    if not settings.DYNAMODB_TABLE_NAME:
        return None
    rca_id = os.environ.get(RCA_ID_ENV, "")
    claim_token = os.environ.get(CLAIM_TOKEN_ENV, "")
    if not rca_id or not claim_token:
        raise AnalysisContractError("missing RCA session ownership context")
    client = boto3.client("dynamodb", region_name=settings.AWS_REGION)
    return DynamoDbSessionStore(client), client, rca_id, claim_token


def _advance_state_path(path: tuple[str, ...]) -> None:
    runtime = _runtime_session()
    if runtime is None:
        return
    store, _, rca_id, claim_token = runtime
    current = store.get_state(rca_id, claim_token=claim_token)
    if current not in path:
        raise AnalysisContractError(
            f"analysis state {current or 'unknown'} is not valid for this artifact; expected one of {', '.join(path)}"
        )
    start = path.index(current)
    for target in path[start + 1 :]:
        store.update_state(rca_id, target, claim_token=claim_token)


def _persist_runtime_trace(filename: str, content: str) -> None:
    runtime = _runtime_session()
    if runtime is None:
        return
    _, ddb, rca_id, claim_token = runtime
    if filename.endswith(".md"):
        artifact = {"summary": content[:500], "output_summary": content[:500]}
    else:
        artifact = json.loads(content)

    round_index = generation_round_for_filename(filename)
    if filename == "scoping.json":
        _write_span(ddb, rca_id, "SCOPING", artifact, claim_token=claim_token, strict=True)
    elif round_index is not None:
        _write_span(
            ddb,
            rca_id,
            "HYPOTHESIS_GENERATION",
            artifact,
            claim_token=claim_token,
            strict=True,
        )
        _save_hypotheses_to_ddb(
            ddb,
            rca_id,
            artifact,
            claim_token=claim_token,
            strict=True,
        )
    elif _VALIDATION_ARTIFACT.fullmatch(filename):
        loop_index = artifact["loop_index"]
        for span_type in ("PRIORITIZATION", "EVIDENCE_COLLECTION", "VALIDATION"):
            _write_span(
                ddb,
                rca_id,
                span_type,
                artifact,
                claim_token=claim_token,
                loop_index=loop_index,
                strict=True,
            )
        _update_hypotheses_from_validation(
            ddb,
            rca_id,
            artifact,
            claim_token=claim_token,
            strict=True,
        )
    elif filename == "playbook.json":
        _write_span(ddb, rca_id, "PLAYBOOK", artifact, claim_token=claim_token, strict=True)
    elif filename == "report.md":
        _write_span(ddb, rca_id, "REPORT", artifact, claim_token=claim_token, strict=True)


def _prepare_analysis_content(base: Path, filename: str, content: str) -> tuple[str, dict | None]:
    round_index = generation_round_for_filename(filename)
    if round_index is not None:
        return normalize_generation_artifact(base, filename, content), None
    if _VALIDATION_ARTIFACT.fullmatch(filename) is not None:
        normalized, decision = normalize_validation_artifact(base, filename, content)
        return normalized, decision.as_dict()
    validate_artifact_shape(filename, content)
    return content, None


def _save(
    filename: str,
    content: str,
    allowed: set[str],
    *,
    allow_validation: bool,
    role: str,
) -> str:
    if not _is_allowed_filename(filename, allowed, allow_validation=allow_validation):
        return json.dumps(
            {
                "ok": False,
                "error": (
                    f"unsupported artifact filename for this role: {filename}. "
                    "이 역할이 저장할 산출물이 아니다 — 담당 역할이 저장해야 한다."
                ),
            },
            ensure_ascii=False,
        )

    base = _artifact_dir()
    if base is None:
        return json.dumps({"ok": False, "error": "missing or invalid RCA execution context"})

    try:
        decision: dict | None = None
        if role == "analysis":
            content, decision = _prepare_analysis_content(base, filename, content)
        else:
            validate_artifact_shape(filename, content)
            analysis = validate_analysis_completion(base)
            if filename == "playbook.json":
                playbook = json.loads(content)
                if not analysis.confirmed and playbook["execution_steps"]:
                    raise AnalysisContractError("unconfirmed RCA must not declare playbook execution steps")
    except (AnalysisContractError, ArtifactValidationError) as exc:
        return json.dumps(
            {"ok": False, "error": f"artifact rejected: {exc}. Fix the content and save again."},
            ensure_ascii=False,
        )

    target = base / filename
    previous = target.read_text() if target.is_file() else None
    try:
        path = _write_artifact(base, filename, content)
        if role == "analysis":
            if filename == "scoping.json":
                _advance_state_path(("SCOPING", "HYPOTHESIS_GENERATION"))
            elif generation_round_for_filename(filename) is not None:
                _advance_state_path(("HYPOTHESIS_GENERATION", "HYPOTHESIS_PRIORITIZATION"))
            elif decision is not None:
                next_state = {
                    "CONTINUE": "HYPOTHESIS_PRIORITIZATION",
                    "REGENERATE": "HYPOTHESIS_GENERATION",
                    "REPORT": "HYPOTHESIS_VALIDATION",
                }[decision["action"]]
                path_states = (
                    "HYPOTHESIS_PRIORITIZATION",
                    "EVIDENCE_COLLECTION",
                    "HYPOTHESIS_VALIDATION",
                )
                if next_state != "HYPOTHESIS_VALIDATION":
                    path_states += (next_state,)
                _advance_state_path(path_states)
        else:
            _advance_state_path(("HYPOTHESIS_VALIDATION", "REPORT_GENERATION"))
        _persist_runtime_trace(filename, content)
    except Exception as exc:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            _write_artifact(base, filename, previous)
        return json.dumps(
            {"ok": False, "error": f"artifact persistence or state transition failed: {exc}"},
            ensure_ascii=False,
        )

    response = {"ok": True, "path": str(path)}
    if decision is not None:
        response["decision"] = decision
    return json.dumps(response, ensure_ascii=False)


@mcp.tool()
def save_analysis_artifact(filename: str, content: str) -> str:
    """RCA 분석 산출물을 현재 실행의 격리된 디렉터리에 저장한다.

    이 도구는 분석 역할의 산출물만 받는다. 리포트와 플레이북은 보고 역할이 자신의
    도구로 저장한다.

    Args:
        filename: scoping.json, hypotheses.json, validation-{N}.json 중 하나.
        content: 파일 내용 (JSON 문자열).
    """
    return _save(
        filename,
        content,
        _ANALYSIS_ARTIFACTS,
        allow_validation=True,
        role="analysis",
    )


@mcp.tool()
def save_report_artifact(filename: str, content: str) -> str:
    """리포트 산출물을 현재 실행의 격리된 디렉터리에 저장한다.

    이 도구는 보고 역할의 산출물만 받는다. 분석 산출물은 분석 역할이 자신의 도구로
    저장한다.

    Args:
        filename: report.md 또는 playbook.json.
        content: 파일 내용 (마크다운 또는 JSON 문자열).
    """
    return _save(
        filename,
        content,
        _REPORT_ARTIFACTS,
        allow_validation=False,
        role="report",
    )
