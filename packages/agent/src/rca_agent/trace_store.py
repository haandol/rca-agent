from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from rca_agent.config import DYNAMODB_TABLE_NAME, ENGINE, SESSION_TTL_DAYS

if TYPE_CHECKING:
    from rca_agent.models import Hypothesis

logger = logging.getLogger(__name__)

_SUMMARY_MAX_LEN = 500
_BATCH_WRITE_CHUNK = 25


class SpanType(StrEnum):
    SCOPING = "SCOPING"
    HYPOTHESIS_GENERATION = "HYPOTHESIS_GENERATION"
    PRIORITIZATION = "PRIORITIZATION"
    EVIDENCE_COLLECTION = "EVIDENCE_COLLECTION"
    VALIDATION = "VALIDATION"
    BRANCHING = "BRANCHING"
    TERMINATION = "TERMINATION"
    REPORT = "REPORT"
    PLAYBOOK = "PLAYBOOK"
    REMEDIATION = "REMEDIATION"
    VERIFICATION = "VERIFICATION"
    NOTIFICATION = "NOTIFICATION"
    VALIDATION_LOOP = "VALIDATION_LOOP"


class SpanStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class Span:
    __slots__ = (
        "span_id",
        "rca_id",
        "span_type",
        "parent_span_id",
        "loop_index",
        "input_summary",
        "output_summary",
        "status",
        "error",
        "metadata",
        "start_time",
        "end_time",
        "duration_ms",
        "_mono_start",
    )

    def __init__(
        self,
        span_id: str,
        rca_id: str,
        span_type: SpanType,
        *,
        parent_span_id: str | None = None,
        loop_index: int | None = None,
        input_summary: str = "",
    ):
        self.span_id = span_id
        self.rca_id = rca_id
        self.span_type = span_type
        self.parent_span_id = parent_span_id
        self.loop_index = loop_index
        self.input_summary = input_summary[:_SUMMARY_MAX_LEN]
        self.output_summary = ""
        self.status = SpanStatus.RUNNING
        self.error: str | None = None
        self.metadata: dict | None = None
        self.start_time = datetime.now(UTC)
        self.end_time: datetime | None = None
        self.duration_ms: int | None = None
        self._mono_start = time.monotonic()


class TraceStore:
    def __init__(self, rca_id: str, *, dynamodb_client=None):
        self._rca_id = rca_id
        self._dynamodb = dynamodb_client
        self._enabled = bool(DYNAMODB_TABLE_NAME and dynamodb_client)

    # ── Span lifecycle ──────────────────────────────────────────────

    def start_span(
        self,
        span_type: SpanType,
        *,
        parent_span_id: str | None = None,
        loop_index: int | None = None,
        input_summary: str = "",
    ) -> Span:
        span = Span(
            span_id=str(uuid.uuid4()),
            rca_id=self._rca_id,
            span_type=span_type,
            parent_span_id=parent_span_id,
            loop_index=loop_index,
            input_summary=input_summary,
        )
        self._write_span(span)
        return span

    def end_span(
        self,
        span: Span,
        *,
        output_summary: str = "",
        status: SpanStatus = SpanStatus.COMPLETED,
        error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        span.end_time = datetime.now(UTC)
        span.duration_ms = int((time.monotonic() - span._mono_start) * 1000)
        span.output_summary = output_summary[:_SUMMARY_MAX_LEN]
        span.status = status
        span.error = error
        if metadata:
            span.metadata = metadata
        self._update_span_end(span)

    @contextmanager
    def span(
        self,
        span_type: SpanType,
        *,
        parent_span_id: str | None = None,
        loop_index: int | None = None,
        input_summary: str = "",
    ) -> Generator[Span, None, None]:
        s = self.start_span(
            span_type,
            parent_span_id=parent_span_id,
            loop_index=loop_index,
            input_summary=input_summary,
        )
        try:
            yield s
            self.end_span(
                s,
                output_summary=s.output_summary,
                metadata=s.metadata,
            )
        except Exception as exc:
            self.end_span(
                s,
                output_summary=s.output_summary,
                status=SpanStatus.FAILED,
                error=str(exc)[:_SUMMARY_MAX_LEN],
                metadata=s.metadata,
            )
            raise

    # ── Hypothesis persistence ──────────────────────────────────────

    def put_hypotheses(self, hypotheses: list[Hypothesis]) -> None:
        if not self._enabled or not hypotheses:
            return

        now = datetime.now(UTC).isoformat()
        ttl = int(time.time()) + SESSION_TTL_DAYS * 86400

        items = []
        for h in hypotheses:
            item = {
                "PutRequest": {
                    "Item": {
                        "PK": {"S": f"RCA#{self._rca_id}"},
                        "SK": {"S": f"{ENGINE}#HYPO#{h.hypothesis_id}"},
                        "engine": {"S": ENGINE},
                        "tree_id": {"S": h.tree_id},
                        "depth": {"N": str(h.depth)},
                        "description": {"S": h.description[:_SUMMARY_MAX_LEN]},
                        "category": {"S": h.category.value},
                        "confidence_score": {"N": str(h.confidence_score)},
                        "status": {"S": h.status.value},
                        "required_evidence": {"L": [{"S": e} for e in h.required_evidence]},
                        "evidence_summary": {"S": ""},
                        "judgment_reasoning": {"S": ""},
                        "created_at": {"S": now},
                        "updated_at": {"S": now},
                        "ttl": {"N": str(ttl)},
                    },
                },
            }
            if h.parent_id:
                item["PutRequest"]["Item"]["parent_id"] = {"S": h.parent_id}
            else:
                item["PutRequest"]["Item"]["parent_id"] = {"NULL": True}
            if h.referenced_playbook_id:
                item["PutRequest"]["Item"]["referenced_playbook_id"] = {"S": h.referenced_playbook_id}
            items.append(item)

        for i in range(0, len(items), _BATCH_WRITE_CHUNK):
            chunk = items[i : i + _BATCH_WRITE_CHUNK]
            try:
                self._dynamodb.batch_write_item(
                    RequestItems={DYNAMODB_TABLE_NAME: chunk},
                )
            except ClientError:
                logger.exception("Failed to batch write %d hypothesis nodes", len(chunk))

    def update_hypothesis_status(
        self,
        hypothesis_id: str,
        *,
        status: str,
        confidence: float | None = None,
        judgment_reasoning: str = "",
    ) -> None:
        if not self._enabled:
            return

        now = datetime.now(UTC).isoformat()
        expr_parts = ["#st = :status", "updated_at = :now", "judgment_reasoning = :jr"]
        attr_names = {"#st": "status"}
        attr_values = {
            ":status": {"S": status},
            ":now": {"S": now},
            ":jr": {"S": judgment_reasoning[:_SUMMARY_MAX_LEN]},
        }
        if confidence is not None:
            expr_parts.append("judgment_confidence = :jc")
            expr_parts.append("confidence_score = :jc")
            attr_values[":jc"] = {"N": str(confidence)}

        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={
                    "PK": {"S": f"RCA#{self._rca_id}"},
                    "SK": {"S": f"{ENGINE}#HYPO#{hypothesis_id}"},
                },
                UpdateExpression="SET " + ", ".join(expr_parts),
                ExpressionAttributeNames=attr_names,
                ExpressionAttributeValues=attr_values,
            )
        except ClientError:
            logger.exception("Failed to update hypothesis status for %s", hypothesis_id)

    def update_hypothesis_evidence(
        self,
        hypothesis_id: str,
        *,
        evidence_summary: str,
    ) -> None:
        if not self._enabled:
            return

        now = datetime.now(UTC).isoformat()
        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={
                    "PK": {"S": f"RCA#{self._rca_id}"},
                    "SK": {"S": f"{ENGINE}#HYPO#{hypothesis_id}"},
                },
                UpdateExpression="SET evidence_summary = :es, updated_at = :now",
                ExpressionAttributeValues={
                    ":es": {"S": evidence_summary[:_SUMMARY_MAX_LEN]},
                    ":now": {"S": now},
                },
            )
        except ClientError:
            logger.exception("Failed to update hypothesis evidence for %s", hypothesis_id)

    # ── Query ───────────────────────────────────────────────────────

    @staticmethod
    def get_trace(rca_id: str, *, dynamodb_client=None) -> dict:
        if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
            return {"session": None, "spans": [], "hypotheses": []}

        try:
            result = dynamodb_client.query(
                TableName=DYNAMODB_TABLE_NAME,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": {"S": f"RCA#{rca_id}"}},
            )
        except ClientError:
            logger.exception("Failed to query trace for %s", rca_id)
            return {"session": None, "spans": [], "hypotheses": []}

        session = None
        spans = []
        hypotheses = []

        for item in result.get("Items", []):
            sk = item["SK"]["S"]
            if sk.endswith("#SESSION") or sk == "SESSION":
                session = _deserialize_session(item)
            elif "#SPAN#" in sk or sk.startswith("SPAN#"):
                spans.append(_deserialize_span(item))
            elif "#HYPO#" in sk or sk.startswith("HYPO#"):
                hypotheses.append(_deserialize_hypothesis(item))

        spans.sort(key=lambda s: s.get("start_time", ""))
        return {"session": session, "spans": spans, "hypotheses": hypotheses}

    # ── Private helpers ─────────────────────────────────────────────

    def _write_span(self, span: Span) -> None:
        if not self._enabled:
            return

        ttl = int(time.time()) + SESSION_TTL_DAYS * 86400
        item: dict = {
            "PK": {"S": f"RCA#{span.rca_id}"},
            "SK": {"S": f"{ENGINE}#SPAN#{span.span_id}"},
            "engine": {"S": ENGINE},
            "span_type": {"S": span.span_type.value},
            "span_status": {"S": span.status.value},
            "start_time": {"S": span.start_time.isoformat()},
            "input_summary": {"S": span.input_summary},
            "output_summary": {"S": ""},
            "ttl": {"N": str(ttl)},
        }
        if span.parent_span_id:
            item["parent_span_id"] = {"S": span.parent_span_id}
        if span.loop_index is not None:
            item["loop_index"] = {"N": str(span.loop_index)}

        try:
            self._dynamodb.put_item(TableName=DYNAMODB_TABLE_NAME, Item=item)
        except ClientError:
            logger.exception("Failed to write span %s", span.span_id)

    def _update_span_end(self, span: Span) -> None:
        if not self._enabled:
            return

        expr_parts = [
            "span_status = :status",
            "end_time = :end",
            "duration_ms = :dur",
            "output_summary = :out",
        ]
        attr_values: dict = {
            ":status": {"S": span.status.value},
            ":end": {"S": span.end_time.isoformat() if span.end_time else ""},
            ":dur": {"N": str(span.duration_ms or 0)},
            ":out": {"S": span.output_summary},
        }
        if span.error:
            expr_parts.append("error = :err")
            attr_values[":err"] = {"S": span.error}
        if span.metadata:
            expr_parts.append("metadata = :meta")
            attr_values[":meta"] = {"M": _serialize_metadata(span.metadata)}

        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={
                    "PK": {"S": f"RCA#{span.rca_id}"},
                    "SK": {"S": f"{ENGINE}#SPAN#{span.span_id}"},
                },
                UpdateExpression="SET " + ", ".join(expr_parts),
                ExpressionAttributeValues=attr_values,
            )
        except ClientError:
            logger.exception("Failed to update span end %s", span.span_id)


def _serialize_metadata(meta: dict) -> dict:
    result = {}
    for k, v in meta.items():
        if isinstance(v, str):
            result[k] = {"S": v}
        elif isinstance(v, bool):
            result[k] = {"BOOL": v}
        elif isinstance(v, (int, float)):
            result[k] = {"N": str(v)}
        else:
            result[k] = {"S": str(v)}
    return result


def _deserialize_session(item: dict) -> dict:
    return {
        "state": item.get("state", {}).get("S", ""),
        "alarm_name": item.get("alarm_name", {}).get("S", ""),
        "alarm_arn": item.get("alarm_arn", {}).get("S", ""),
        "root_cause": item.get("root_cause", {}).get("S", ""),
        "confirmed": item.get("confirmed", {}).get("BOOL", False),
        "error_reason": item.get("error_reason", {}).get("S", ""),
        "created_at": item.get("created_at", {}).get("S", ""),
        "updated_at": item.get("updated_at", {}).get("S", ""),
        "engine": item.get("engine", {}).get("S", "strands"),
    }


def _deserialize_span(item: dict) -> dict:
    sk = item["SK"]["S"]
    span_id = sk.split("#SPAN#")[1] if "#SPAN#" in sk else sk.replace("SPAN#", "")
    return {
        "span_id": span_id,
        "span_type": item.get("span_type", {}).get("S", ""),
        "span_status": item.get("span_status", {}).get("S", ""),
        "parent_span_id": item.get("parent_span_id", {}).get("S"),
        "loop_index": int(item["loop_index"]["N"]) if "loop_index" in item else None,
        "start_time": item.get("start_time", {}).get("S", ""),
        "end_time": item.get("end_time", {}).get("S"),
        "duration_ms": int(item["duration_ms"]["N"]) if "duration_ms" in item else None,
        "input_summary": item.get("input_summary", {}).get("S", ""),
        "output_summary": item.get("output_summary", {}).get("S", ""),
        "error": item.get("error", {}).get("S"),
        "metadata": _deserialize_metadata(item.get("metadata", {}).get("M")) if "metadata" in item else None,
        "engine": item.get("engine", {}).get("S", "strands"),
    }


def _deserialize_hypothesis(item: dict) -> dict:
    required = []
    for e in item.get("required_evidence", {}).get("L", []):
        if "S" in e:
            required.append(e["S"])
    sk = item["SK"]["S"]
    hypo_id = sk.split("#HYPO#")[1] if "#HYPO#" in sk else sk.replace("HYPO#", "")
    return {
        "hypothesis_id": hypo_id,
        "tree_id": item.get("tree_id", {}).get("S", ""),
        "parent_id": item.get("parent_id", {}).get("S"),
        "depth": int(item.get("depth", {}).get("N", "0")),
        "description": item.get("description", {}).get("S", ""),
        "category": item.get("category", {}).get("S", ""),
        "confidence_score": float(item.get("confidence_score", {}).get("N", "0")),
        "status": item.get("status", {}).get("S", "PENDING"),
        "required_evidence": required,
        "referenced_playbook_id": item.get("referenced_playbook_id", {}).get("S"),
        "evidence_summary": item.get("evidence_summary", {}).get("S", ""),
        "judgment_reasoning": item.get("judgment_reasoning", {}).get("S", ""),
        "judgment_confidence": float(item["judgment_confidence"]["N"]) if "judgment_confidence" in item else None,
        "created_at": item.get("created_at", {}).get("S", ""),
        "updated_at": item.get("updated_at", {}).get("S", ""),
        "engine": item.get("engine", {}).get("S", "strands"),
    }


def _deserialize_metadata(meta_map: dict | None) -> dict | None:
    if not meta_map:
        return None
    result = {}
    for k, v in meta_map.items():
        if "S" in v:
            result[k] = v["S"]
        elif "N" in v:
            n = v["N"]
            result[k] = int(n) if "." not in n else float(n)
        elif "BOOL" in v:
            result[k] = v["BOOL"]
        else:
            result[k] = str(v)
    return result
