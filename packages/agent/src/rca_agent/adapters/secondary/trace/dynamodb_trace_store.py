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

from rca_agent.adapters.secondary.session.dynamodb_session_store import (
    TERMINAL_STATES as _TERMINAL_STATES,
)
from rca_agent.adapters.secondary.session.dynamodb_session_store import (
    SessionCancelledError,
)
from rca_agent.config.settings import DYNAMODB_TABLE_NAME, ENGINE, SESSION_TTL_DAYS
from rca_agent.ports.interfaces.session_store import SessionOwnershipCheckError

if TYPE_CHECKING:
    from rca_agent.ports.dto.models import Hypothesis

logger = logging.getLogger(__name__)

_SUMMARY_MAX_LEN = 500
_BATCH_WRITE_CHUNK = 25


def _pk(rca_id: str) -> dict:
    return {"S": f"RCA#{rca_id}"}


def _session_sk() -> dict:
    return {"S": f"{ENGINE}#SESSION"}


def _span_sk(span_id: str) -> dict:
    return {"S": f"{ENGINE}#SPAN#{span_id}"}


def _hypo_sk(hypothesis_id: str) -> dict:
    return {"S": f"{ENGINE}#HYPO#{hypothesis_id}"}


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
    def __init__(
        self,
        rca_id: str,
        *,
        claim_token: str | None = None,
        attempt: int | None = None,
        dynamodb_client=None,
    ):
        self._rca_id = rca_id
        self._claim_token = claim_token
        self._attempt = attempt
        self._dynamodb = dynamodb_client
        self._enabled = bool(DYNAMODB_TABLE_NAME and dynamodb_client)

    def check_cancelled(self) -> None:
        if not self._enabled:
            return
        try:
            resp = self._dynamodb.get_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={"PK": _pk(self._rca_id), "SK": _session_sk()},
                ConsistentRead=True,
                ProjectionExpression="#st, claim_token",
                ExpressionAttributeNames={"#st": "state"},
            )
            item = resp.get("Item")
            if not item:
                raise SessionOwnershipCheckError(f"{self._rca_id}: session item is missing")
            state = item.get("state", {}).get("S", "")
            owner = item.get("claim_token", {}).get("S")
            if state in _TERMINAL_STATES or (self._claim_token and owner != self._claim_token):
                raise SessionCancelledError(self._rca_id)
        except (SessionCancelledError, SessionOwnershipCheckError):
            raise
        except ClientError as exc:
            logger.exception("Failed to check cancellation for %s", self._rca_id)
            if self._claim_token:
                raise SessionOwnershipCheckError(self._rca_id) from exc

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

        self.check_cancelled()
        now = datetime.now(UTC).isoformat()
        ttl = int(time.time()) + SESSION_TTL_DAYS * 86400

        items = []
        for h in hypotheses:
            item = {
                "PutRequest": {
                    "Item": {
                        "PK": _pk(self._rca_id),
                        "SK": _hypo_sk(h.hypothesis_id),
                        "engine": {"S": ENGINE},
                        "tree_id": {"S": h.tree_id},
                        "depth": {"N": str(h.depth)},
                        "title": {"S": (h.title or h.description.splitlines()[0] if h.description else "")[:200]},
                        "description": {"S": h.description[:_SUMMARY_MAX_LEN]},
                        "category": {"S": h.category.value},
                        "fault_type": {"S": h.fault_type.value},
                        "validated_fault_type": {"S": h.validated_fault_type.value},
                        "confidence_score": {"N": str(h.confidence_score)},
                        "status": {"S": h.status.value},
                        "required_evidence": {"L": [{"S": e} for e in h.required_evidence]},
                        "evidence_summary": {"S": ""},
                        "validation_evidence_summary": {"S": ""},
                        "judgment_reasoning": {
                            "S": h.judgment_reasoning[:_SUMMARY_MAX_LEN],
                        },
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

        if self._claim_token:
            writes = [{"Put": request["PutRequest"] | {"TableName": DYNAMODB_TABLE_NAME}} for request in items]
            self._transact_claimed(writes)
            return

        for i in range(0, len(items), _BATCH_WRITE_CHUNK):
            chunk = items[i : i + _BATCH_WRITE_CHUNK]
            try:
                self._dynamodb.batch_write_item(
                    RequestItems={DYNAMODB_TABLE_NAME: chunk},
                )
            except ClientError:
                logger.exception("Failed to batch write %d hypothesis nodes", len(chunk))

    def _update_hypothesis_item(
        self,
        hypothesis_id: str,
        *,
        set_parts: list[str],
        attr_values: dict,
        attr_names: dict | None = None,
        error_log: str,
    ) -> None:
        if not self._enabled:
            return
        self.check_cancelled()
        kwargs = {
            "TableName": DYNAMODB_TABLE_NAME,
            "Key": {"PK": _pk(self._rca_id), "SK": _hypo_sk(hypothesis_id)},
            "UpdateExpression": "SET " + ", ".join(set_parts),
            "ExpressionAttributeValues": attr_values,
        }
        if attr_names:
            kwargs["ExpressionAttributeNames"] = attr_names
        if self._claim_token:
            update = {
                "TableName": DYNAMODB_TABLE_NAME,
                "Key": kwargs["Key"],
                "UpdateExpression": kwargs["UpdateExpression"],
                "ConditionExpression": "attribute_exists(SK)",
                "ExpressionAttributeValues": attr_values,
            }
            if attr_names:
                update["ExpressionAttributeNames"] = attr_names
            self._transact_claimed([{"Update": update}])
            return
        try:
            self._dynamodb.update_item(**kwargs)
        except ClientError:
            logger.exception(error_log, hypothesis_id)

    def update_hypothesis_status(
        self,
        hypothesis_id: str,
        *,
        status: str,
        confidence: float | None = None,
        judgment_reasoning: str = "",
        validated_fault_type: str | None = None,
        validation_evidence_summary: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        set_parts = ["#st = :status", "updated_at = :now", "judgment_reasoning = :jr"]
        attr_values: dict = {
            ":status": {"S": status},
            ":now": {"S": now},
            ":jr": {"S": judgment_reasoning[:_SUMMARY_MAX_LEN]},
        }
        if confidence is not None:
            set_parts.append("judgment_confidence = :jc")
            set_parts.append("confidence_score = :jc")
            attr_values[":jc"] = {"N": str(confidence)}
        if validated_fault_type is not None:
            set_parts.append("validated_fault_type = :vft")
            attr_values[":vft"] = {"S": validated_fault_type}
        if validation_evidence_summary is not None:
            set_parts.append("validation_evidence_summary = :ves")
            attr_values[":ves"] = {
                "S": validation_evidence_summary[:_SUMMARY_MAX_LEN],
            }

        self._update_hypothesis_item(
            hypothesis_id,
            set_parts=set_parts,
            attr_values=attr_values,
            attr_names={"#st": "status"},
            error_log="Failed to update hypothesis status for %s",
        )

    def update_hypothesis_evidence(
        self,
        hypothesis_id: str,
        *,
        evidence_summary: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._update_hypothesis_item(
            hypothesis_id,
            set_parts=["evidence_summary = :es", "updated_at = :now"],
            attr_values={
                ":es": {"S": evidence_summary[:_SUMMARY_MAX_LEN]},
                ":now": {"S": now},
            },
            error_log="Failed to update hypothesis evidence for %s",
        )

    # ── Query ───────────────────────────────────────────────────────

    @staticmethod
    def get_trace(rca_id: str, *, dynamodb_client=None) -> dict:
        if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
            return {"session": None, "spans": [], "hypotheses": []}

        try:
            result = dynamodb_client.query(
                TableName=DYNAMODB_TABLE_NAME,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": _pk(rca_id)},
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

    @staticmethod
    def get_playbook_metadata(rca_id: str, playbook_id: str, *, dynamodb_client=None) -> dict | None:
        """Return the recorded playbook fields for ``playbook_id`` under ``rca_id``.

        The playbook span carries the full field set as its metadata, so no
        secondary index is needed. Returns None when the record is gone (TTL) or
        the query fails — callers must treat the detail as unavailable, not empty.
        """
        if not DYNAMODB_TABLE_NAME or dynamodb_client is None or not rca_id or not playbook_id:
            return None

        try:
            result = dynamodb_client.query(
                TableName=DYNAMODB_TABLE_NAME,
                KeyConditionExpression="PK = :pk",
                FilterExpression="span_type = :span_type",
                ExpressionAttributeValues={
                    ":pk": _pk(rca_id),
                    ":span_type": {"S": SpanType.PLAYBOOK.value},
                },
            )
        except ClientError:
            logger.exception("Failed to query playbook span for %s", rca_id)
            return None

        for item in result.get("Items", []):
            metadata = _deserialize_metadata(item.get("metadata", {}).get("M"))
            if metadata and metadata.get("playbook_id") == playbook_id:
                return metadata
        return None

    # ── Private helpers ─────────────────────────────────────────────

    def _write_span(self, span: Span) -> None:
        if not self._enabled:
            return

        ttl = int(time.time()) + SESSION_TTL_DAYS * 86400
        item: dict = {
            "PK": _pk(span.rca_id),
            "SK": _span_sk(span.span_id),
            "engine": {"S": ENGINE},
            "span_type": {"S": span.span_type.value},
            "span_status": {"S": span.status.value},
            "start_time": {"S": span.start_time.isoformat()},
            "input_summary": {"S": span.input_summary},
            "output_summary": {"S": ""},
            "ttl": {"N": str(ttl)},
        }
        if self._claim_token:
            item["claim_token"] = {"S": self._claim_token}
        if self._attempt is not None:
            item["attempt"] = {"N": str(self._attempt)}
        if span.parent_span_id:
            item["parent_span_id"] = {"S": span.parent_span_id}
        if span.loop_index is not None:
            item["loop_index"] = {"N": str(span.loop_index)}

        if self._claim_token:
            self._transact_claimed(
                [{"Put": {"TableName": DYNAMODB_TABLE_NAME, "Item": item}}],
            )
            return
        try:
            self._dynamodb.put_item(TableName=DYNAMODB_TABLE_NAME, Item=item)
        except ClientError:
            logger.exception("Failed to write span %s", span.span_id)

    def _update_span_end(self, span: Span) -> None:
        if not self._enabled:
            return

        # "error" and "metadata" are DynamoDB reserved words, so every updated
        # attribute goes through an alias rather than appearing literally.
        expr_parts = [
            "span_status = :status",
            "end_time = :end",
            "duration_ms = :dur",
            "output_summary = :out",
        ]
        attr_names: dict[str, str] = {}
        attr_values: dict = {
            ":status": {"S": span.status.value},
            ":end": {"S": span.end_time.isoformat() if span.end_time else ""},
            ":dur": {"N": str(span.duration_ms or 0)},
            ":out": {"S": span.output_summary},
        }
        if span.error:
            expr_parts.append("#error = :err")
            attr_names["#error"] = "error"
            attr_values[":err"] = {"S": span.error}
        if span.metadata:
            expr_parts.append("#metadata = :meta")
            attr_names["#metadata"] = "metadata"
            attr_values[":meta"] = {"M": _serialize_metadata(span.metadata)}

        if self._claim_token:
            update: dict = {
                "TableName": DYNAMODB_TABLE_NAME,
                "Key": {
                    "PK": _pk(span.rca_id),
                    "SK": _span_sk(span.span_id),
                },
                "UpdateExpression": "SET " + ", ".join(expr_parts),
                "ConditionExpression": "attribute_exists(SK)",
                "ExpressionAttributeValues": attr_values,
            }
            if attr_names:
                update["ExpressionAttributeNames"] = attr_names
            self._transact_claimed([{"Update": update}])
            return
        try:
            request: dict = {
                "TableName": DYNAMODB_TABLE_NAME,
                "Key": {"PK": _pk(span.rca_id), "SK": _span_sk(span.span_id)},
                "UpdateExpression": "SET " + ", ".join(expr_parts),
                "ExpressionAttributeValues": attr_values,
            }
            if attr_names:
                request["ExpressionAttributeNames"] = attr_names
            self._dynamodb.update_item(**request)
        except ClientError:
            logger.exception("Failed to update span end %s", span.span_id)

    def _claim_check(self) -> dict:
        return {
            "ConditionCheck": {
                "TableName": DYNAMODB_TABLE_NAME,
                "Key": {
                    "PK": _pk(self._rca_id),
                    "SK": _session_sk(),
                },
                "ConditionExpression": "attribute_exists(SK) AND claim_token = :claim",
                "ExpressionAttributeValues": {
                    ":claim": {"S": self._claim_token},
                },
            },
        }

    def _transact_claimed(self, writes: list[dict]) -> None:
        if not self._enabled or not self._claim_token:
            raise SessionOwnershipCheckError(f"{self._rca_id}: claimed trace store is unavailable")
        try:
            for index in range(0, len(writes), 24):
                self._dynamodb.transact_write_items(
                    TransactItems=[
                        self._claim_check(),
                        *writes[index : index + 24],
                    ],
                )
        except ClientError as exc:
            logger.exception("Claim-fenced trace write failed for %s", self._rca_id)
            raise SessionOwnershipCheckError(self._rca_id) from exc


def _serialize_metadata(meta: dict) -> dict:
    result = {}
    for k, v in meta.items():
        if isinstance(v, str):
            result[k] = {"S": v}
        elif isinstance(v, bool):
            result[k] = {"BOOL": v}
        elif isinstance(v, (int, float)):
            result[k] = {"N": str(v)}
        elif isinstance(v, list):
            result[k] = {"L": [{"S": str(i)} for i in v]}
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
        "title": item.get("title", {}).get("S", ""),
        "description": item.get("description", {}).get("S", ""),
        "category": item.get("category", {}).get("S", ""),
        "fault_type": item.get("fault_type", {}).get("S", "UNSUPPORTED"),
        "validated_fault_type": item.get("validated_fault_type", {}).get("S", "UNSUPPORTED"),
        "confidence_score": float(item.get("confidence_score", {}).get("N", "0")),
        "status": item.get("status", {}).get("S", "PENDING"),
        "required_evidence": required,
        "referenced_playbook_id": item.get("referenced_playbook_id", {}).get("S"),
        "evidence_summary": item.get("evidence_summary", {}).get("S", ""),
        "validation_evidence_summary": item.get("validation_evidence_summary", {}).get("S", ""),
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
        elif "L" in v:
            result[k] = [i.get("S", str(i)) for i in v["L"]]
        else:
            result[k] = str(v)
    return result
