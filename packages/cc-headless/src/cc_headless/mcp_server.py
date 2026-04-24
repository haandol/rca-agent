"""rca-progress MCP server — DynamoDB 상태 업데이트 + span/hypothesis 기록."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from fastmcp import FastMCP

mcp = FastMCP("rca-progress")

_ENGINE = "cc-headless"
_SESSION_ID_PATH = Path("/tmp/rca-session-id")
_TABLE = os.environ.get("DYNAMODB_TABLE_NAME", "")
_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "90"))

_ddb = boto3.client("dynamodb") if _TABLE else None

_SUMMARY_MAX = 500


def _rca_id() -> str:
    try:
        return _SESSION_ID_PATH.read_text().strip()
    except FileNotFoundError:
        return ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ttl() -> str:
    return str(int(time.time()) + _TTL_DAYS * 86400)


# ── Pipeline state ─────────────────────────────────────────────────


@mcp.tool()
def report_progress(stage: str, summary: str) -> str:
    """파이프라인 단계를 DDB 세션 상태에 반영하고 span을 기록한다.

    Args:
        stage: 파이프라인 단계. SCOPING, HYPOTHESIS_GENERATION,
               HYPOTHESIS_PRIORITIZATION, EVIDENCE_COLLECTION,
               HYPOTHESIS_VALIDATION, REPORT_GENERATION,
               REMEDIATION, VERIFICATION 중 하나.
        summary: 이 단계에서 수행한 작업 요약 (500자 이내).
    """
    rca_id = _rca_id()
    if not _ddb or not _TABLE or not rca_id:
        return json.dumps({"ok": True, "skipped": True, "reason": "DDB not configured"})

    now = _now_iso()
    try:
        _ddb.update_item(
            TableName=_TABLE,
            Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{_ENGINE}#SESSION"}},
            UpdateExpression="SET #st = :state, updated_at = :now",
            ConditionExpression="#st <> :cancelled",
            ExpressionAttributeNames={"#st": "state"},
            ExpressionAttributeValues={
                ":state": {"S": stage},
                ":now": {"S": now},
                ":cancelled": {"S": "CANCELLED"},
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return json.dumps({"ok": False, "cancelled": True})
        raise

    span_id = str(uuid.uuid4())
    _ddb.put_item(
        TableName=_TABLE,
        Item={
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": f"{_ENGINE}#SPAN#{span_id}"},
            "engine": {"S": _ENGINE},
            "span_type": {"S": stage},
            "span_status": {"S": "COMPLETED"},
            "start_time": {"S": now},
            "end_time": {"S": now},
            "duration_ms": {"N": "0"},
            "input_summary": {"S": ""},
            "output_summary": {"S": summary[:_SUMMARY_MAX]},
            "ttl": {"N": _ttl()},
        },
    )
    return json.dumps({"ok": True, "cancelled": False, "span_id": span_id})


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
        filename: 파일명 (예: hypotheses.md, validation-1.md).
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


# ── Cancellation check ─────────────────────────────────────────────


@mcp.tool()
def check_cancelled() -> str:
    """현재 세션이 CANCELLED 상태인지 확인한다."""
    rca_id = _rca_id()
    if not _ddb or not _TABLE or not rca_id:
        return json.dumps({"cancelled": False, "skipped": True})

    resp = _ddb.get_item(
        TableName=_TABLE,
        Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{_ENGINE}#SESSION"}},
        ProjectionExpression="#st",
        ExpressionAttributeNames={"#st": "state"},
    )
    state = resp.get("Item", {}).get("state", {}).get("S", "")
    return json.dumps({"cancelled": state == "CANCELLED", "state": state})
