"""rca-progress MCP server — 스팬/가설/산출물 관리."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import boto3
from fastmcp import FastMCP

mcp = FastMCP("rca-progress")

_ENGINE = "cc-headless"
_SESSION_ID_PATH = Path("/tmp/rca-session-id")
_TABLE = os.environ.get("DYNAMODB_TABLE_NAME", "")
_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "90"))

_ddb = boto3.client("dynamodb") if _TABLE else None

_SUMMARY_MAX = 500

_VALID_SPAN_TYPES = {
    "SCOPING",
    "HYPOTHESIS_GENERATION",
    "PRIORITIZATION",
    "EVIDENCE_COLLECTION",
    "VALIDATION",
    "BRANCHING",
    "TERMINATION",
    "REPORT",
    "PLAYBOOK",
    "REMEDIATION",
    "VERIFICATION",
    "NOTIFICATION",
    "VALIDATION_LOOP",
}
_VALID_SPAN_STATUSES = {"RUNNING", "COMPLETED", "FAILED", "TIMED_OUT"}


def _rca_id() -> str:
    try:
        return _SESSION_ID_PATH.read_text().strip()
    except FileNotFoundError:
        return ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ttl() -> str:
    return str(int(time.time()) + _TTL_DAYS * 86400)


# ── Span persistence ──────────────────────────────────────────────


@mcp.tool()
def start_span(
    span_type: str,
    input_summary: str = "",
    parent_span_id: str = "",
    loop_index: int = -1,
) -> str:
    """분석 단계 시작을 DDB에 기록한다. 반환된 span_id를 end_span에 전달해야 한다.

    Args:
        span_type: 단계 유형. SCOPING, HYPOTHESIS_GENERATION, PRIORITIZATION,
                   EVIDENCE_COLLECTION, VALIDATION, BRANCHING, REPORT,
                   REMEDIATION, VERIFICATION, VALIDATION_LOOP 중 하나.
        input_summary: 단계 입력 요약 (500자 이내).
        parent_span_id: 부모 스팬 ID (검증 루프 내 하위 단계일 때).
        loop_index: 검증 루프 인덱스 (1-based, 해당 없으면 -1).
    """
    rca_id = _rca_id()
    span_id = str(uuid.uuid4())

    if not _ddb or not _TABLE or not rca_id:
        return json.dumps({"ok": True, "skipped": True, "span_id": span_id})

    if span_type not in _VALID_SPAN_TYPES:
        return json.dumps({"ok": False, "error": f"invalid span_type: {span_type}"})

    now = _now_iso()
    ttl = _ttl()

    item: dict = {
        "PK": {"S": f"RCA#{rca_id}"},
        "SK": {"S": f"{_ENGINE}#SPAN#{span_id}"},
        "engine": {"S": _ENGINE},
        "span_type": {"S": span_type},
        "span_status": {"S": "RUNNING"},
        "start_time": {"S": now},
        "input_summary": {"S": input_summary[:_SUMMARY_MAX]},
        "output_summary": {"S": ""},
        "ttl": {"N": ttl},
    }
    if parent_span_id:
        item["parent_span_id"] = {"S": parent_span_id}
    if loop_index >= 0:
        item["loop_index"] = {"N": str(loop_index)}

    _ddb.put_item(TableName=_TABLE, Item=item)
    return json.dumps({"ok": True, "span_id": span_id})


@mcp.tool()
def end_span(
    span_id: str,
    status: str = "COMPLETED",
    output_summary: str = "",
    error: str = "",
) -> str:
    """분석 단계 완료를 DDB에 기록한다.

    Args:
        span_id: start_span이 반환한 스팬 ID.
        status: COMPLETED, FAILED, TIMED_OUT 중 하나.
        output_summary: 단계 출력 요약 (500자 이내).
        error: 오류 메시지 (실패 시).
    """
    rca_id = _rca_id()
    if not _ddb or not _TABLE or not rca_id:
        return json.dumps({"ok": True, "skipped": True})

    if status not in _VALID_SPAN_STATUSES:
        status = "COMPLETED"

    now = _now_iso()

    expr_parts = [
        "span_status = :status",
        "end_time = :end_time",
        "output_summary = :out",
    ]
    attr_values: dict = {
        ":status": {"S": status},
        ":end_time": {"S": now},
        ":out": {"S": output_summary[:_SUMMARY_MAX]},
    }
    if error:
        expr_parts.append("error = :err")
        attr_values[":err"] = {"S": error[:_SUMMARY_MAX]}

    _ddb.update_item(
        TableName=_TABLE,
        Key={
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": f"{_ENGINE}#SPAN#{span_id}"},
        },
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeValues=attr_values,
    )
    return json.dumps({"ok": True, "span_id": span_id, "status": status})


# ── Hypothesis persistence ─────────────────────────────────────────


@mcp.tool()
def save_hypotheses(hypotheses_json: str) -> str:
    """가설 목록을 DDB에 저장한다.

    Args:
        hypotheses_json: JSON 배열. 각 원소는
            {hypothesis_id, tree_id, description, category, confidence_score,
             required_evidence, status, parent_id, depth} 형태.
    """
    rca_id = _rca_id()
    if not _ddb or not _TABLE or not rca_id:
        return json.dumps({"ok": True, "skipped": True})

    hypotheses = json.loads(hypotheses_json)
    now = _now_iso()
    ttl = _ttl()

    items = []
    for h in hypotheses:
        item = {
            "PutRequest": {
                "Item": {
                    "PK": {"S": f"RCA#{rca_id}"},
                    "SK": {"S": f"{_ENGINE}#HYPO#{h['hypothesis_id']}"},
                    "engine": {"S": _ENGINE},
                    "tree_id": {"S": h.get("tree_id", "")},
                    "depth": {"N": str(h.get("depth", 0))},
                    "description": {"S": h.get("description", "")[:_SUMMARY_MAX]},
                    "category": {"S": h.get("category", "")},
                    "confidence_score": {"N": str(h.get("confidence_score", 0))},
                    "status": {"S": h.get("status", "PENDING")},
                    "required_evidence": {"L": [{"S": e} for e in h.get("required_evidence", [])]},
                    "parent_id": {"S": h["parent_id"]} if h.get("parent_id") else {"NULL": True},
                    "evidence_summary": {"S": ""},
                    "judgment_reasoning": {"S": ""},
                    "created_at": {"S": now},
                    "updated_at": {"S": now},
                    "ttl": {"N": ttl},
                },
            },
        }
        items.append(item)

    for i in range(0, len(items), 25):
        chunk = items[i : i + 25]
        _ddb.batch_write_item(RequestItems={_TABLE: chunk})

    return json.dumps({"ok": True, "count": len(items)})


@mcp.tool()
def update_hypothesis(
    hypothesis_id: str,
    status: str,
    confidence_score: float,
    reasoning: str = "",
    evidence_summary: str = "",
) -> str:
    """가설의 검증 결과를 DDB에 반영한다.

    Args:
        hypothesis_id: 가설 UUID.
        status: CONFIRMED, REJECTED, NEEDS_INVESTIGATION 중 하나.
        confidence_score: 0.0-1.0 신뢰도.
        reasoning: 판단 근거 (500자 이내).
        evidence_summary: 증거 요약 (500자 이내).
    """
    rca_id = _rca_id()
    if not _ddb or not _TABLE or not rca_id:
        return json.dumps({"ok": True, "skipped": True})

    now = _now_iso()
    _ddb.update_item(
        TableName=_TABLE,
        Key={
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": f"{_ENGINE}#HYPO#{hypothesis_id}"},
        },
        UpdateExpression=(
            "SET #st = :status, confidence_score = :cs, judgment_confidence = :cs, "
            "judgment_reasoning = :jr, evidence_summary = :es, updated_at = :now"
        ),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":status": {"S": status},
            ":cs": {"N": str(confidence_score)},
            ":jr": {"S": reasoning[:_SUMMARY_MAX]},
            ":es": {"S": evidence_summary[:_SUMMARY_MAX]},
            ":now": {"S": now},
        },
    )
    return json.dumps({"ok": True, "hypothesis_id": hypothesis_id, "status": status})


# ── Artifact persistence ───────────────────────────────────────────


@mcp.tool()
def save_artifact(filename: str, content: str) -> str:
    """분석 산출물을 /tmp 아래에 마크다운 파일로 저장한다.

    Args:
        filename: 파일명 (예: hypotheses.md, validation-1.md, report.md).
                  /tmp/rca-{RCA_ID}/ 아래에 저장된다.
        content: 마크다운 내용.
    """
    rca_id = _rca_id()
    base = f"/tmp/rca-{rca_id}" if rca_id else "/tmp/rca-unknown"
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, filename)
    with open(path, "w") as f:
        f.write(content)
    return json.dumps({"ok": True, "path": path})
