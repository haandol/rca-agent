"""Watch /tmp/rca-{id}/ for artifact JSON files and write DDB spans."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import structlog

from cc_headless.config import DYNAMODB_TABLE_NAME, ENGINE, SESSION_TTL_DAYS

logger = structlog.get_logger()

_POLL_INTERVAL = 3

ARTIFACT_SPAN_MAP: dict[str, str] = {
    "scoping.json": "SCOPING",
    "hypotheses.json": "HYPOTHESIS_GENERATION",
    "playbook.json": "PLAYBOOK",
    "report.md": "REPORT",
}

VALIDATION_PATTERN = "validation-"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ttl() -> str:
    return str(int(time.time()) + SESSION_TTL_DAYS * 86400)


_PLAYBOOK_STR_FIELDS = (
    "playbook_id",
    "failure_type",
    "symptom_pattern",
    "temporary_mitigation",
    "permanent_remediation",
)
_PLAYBOOK_LIST_FIELDS = ("verification_steps", "prevention_measures", "tags")


def _build_playbook_metadata(artifact: dict) -> dict:
    meta: dict = {}
    for k in _PLAYBOOK_STR_FIELDS:
        v = artifact.get(k)
        if v:
            meta[k] = {"S": str(v)}
    for k in _PLAYBOOK_LIST_FIELDS:
        v = artifact.get(k)
        if isinstance(v, list) and v:
            meta[k] = {"L": [{"S": str(i)} for i in v]}
    return meta


def _write_span(
    ddb,
    rca_id: str,
    span_type: str,
    artifact: dict | None,
    *,
    parent_span_id: str | None = None,
    loop_index: int | None = None,
) -> str:
    span_id = str(uuid.uuid4())
    if not DYNAMODB_TABLE_NAME or not ddb:
        return span_id

    now = _now_iso()
    ttl = _ttl()

    input_summary = ""
    output_summary = ""
    status = "COMPLETED"
    error_msg = None

    if artifact is not None:
        input_summary = artifact.get("summary", "")[:500]
        output_summary = artifact.get("output_summary", input_summary)[:500]
        if artifact.get("error"):
            status = "FAILED"
            error_msg = str(artifact["error"])[:500]
    elif span_type == "REPORT":
        output_summary = "보고서 생성 완료"

    item: dict = {
        "PK": {"S": f"RCA#{rca_id}"},
        "SK": {"S": f"{ENGINE}#SPAN#{span_id}"},
        "engine": {"S": ENGINE},
        "span_type": {"S": span_type},
        "span_status": {"S": status},
        "start_time": {"S": now},
        "end_time": {"S": now},
        "output_summary": {"S": output_summary},
        "input_summary": {"S": input_summary},
        "ttl": {"N": ttl},
    }
    if parent_span_id:
        item["parent_span_id"] = {"S": parent_span_id}
    if loop_index is not None:
        item["loop_index"] = {"N": str(loop_index)}
    if error_msg:
        item["error"] = {"S": error_msg}

    if span_type == "PLAYBOOK" and artifact:
        meta = _build_playbook_metadata(artifact)
        if meta:
            item["metadata"] = {"M": meta}

    try:
        ddb.put_item(TableName=DYNAMODB_TABLE_NAME, Item=item)
    except Exception:
        logger.exception("span_write_failed", span_id=span_id, span_type=span_type)

    return span_id


def _parse_artifact(path: Path) -> dict | None:
    try:
        raw = path.read_text()
    except Exception:
        logger.exception("artifact_read_failed", path=str(path))
        return None

    if path.suffix == ".md":
        return {"summary": raw[:500], "output_summary": raw[:500]}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("artifact_json_malformed", path=str(path), raw=raw[:300])
        return {"summary": raw[:500], "output_summary": raw[:500], "error": f"malformed JSON: {path.name}"}


def _save_hypotheses_to_ddb(ddb, rca_id: str, artifact: dict) -> None:
    if not DYNAMODB_TABLE_NAME or not ddb:
        return

    hypotheses = artifact.get("hypotheses", [])
    if not hypotheses:
        return

    now = _now_iso()
    ttl = _ttl()

    items = []
    for h in hypotheses:
        hid = h.get("hypothesis_id", str(uuid.uuid4()))
        item = {
            "PutRequest": {
                "Item": {
                    "PK": {"S": f"RCA#{rca_id}"},
                    "SK": {"S": f"{ENGINE}#HYPO#{hid}"},
                    "engine": {"S": ENGINE},
                    "tree_id": {"S": h.get("tree_id", "")},
                    "depth": {"N": str(h.get("depth", 0))},
                    "description": {"S": h.get("description", "")[:500]},
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
        try:
            ddb.batch_write_item(RequestItems={DYNAMODB_TABLE_NAME: chunk})
        except Exception:
            logger.exception("hypothesis_batch_write_failed", count=len(chunk))


def _update_hypotheses_from_validation(ddb, rca_id: str, artifact: dict) -> None:
    if not DYNAMODB_TABLE_NAME or not ddb:
        return

    now = _now_iso()
    for bucket in ("confirmed", "rejected", "needs_investigation"):
        status_map = {
            "confirmed": "CONFIRMED",
            "rejected": "REJECTED",
            "needs_investigation": "NEEDS_INVESTIGATION",
        }
        for h in artifact.get(bucket, []):
            hid = h if isinstance(h, str) else h.get("hypothesis_id", "")
            confidence = h.get("confidence", 0) if isinstance(h, dict) else 0
            reasoning = h.get("reasoning", "") if isinstance(h, dict) else ""
            if not hid:
                continue
            try:
                ddb.update_item(
                    TableName=DYNAMODB_TABLE_NAME,
                    Key={
                        "PK": {"S": f"RCA#{rca_id}"},
                        "SK": {"S": f"{ENGINE}#HYPO#{hid}"},
                    },
                    UpdateExpression=(
                        "SET #st = :status, confidence_score = :cs, judgment_reasoning = :jr, updated_at = :now"
                    ),
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={
                        ":status": {"S": status_map[bucket]},
                        ":cs": {"N": str(confidence)},
                        ":jr": {"S": str(reasoning)[:500]},
                        ":now": {"S": now},
                    },
                )
            except Exception:
                logger.exception("hypothesis_update_failed", hypothesis_id=hid)

    new_hypotheses = artifact.get("new_hypotheses", [])
    if new_hypotheses:
        _save_hypotheses_to_ddb(ddb, rca_id, {"hypotheses": new_hypotheses})


def _watch_loop(artifact_dir: Path, rca_id: str, ddb, stop_event: Event) -> None:
    seen: set[str] = set()
    validation_loop_span_id: str | None = None

    while not stop_event.is_set():
        if not artifact_dir.exists():
            stop_event.wait(_POLL_INTERVAL)
            continue

        for path in sorted(artifact_dir.iterdir()):
            if path.name in seen:
                continue

            artifact = _parse_artifact(path)
            span_type = ARTIFACT_SPAN_MAP.get(path.name)

            if span_type:
                _write_span(ddb, rca_id, span_type, artifact)
                if span_type == "HYPOTHESIS_GENERATION" and artifact and not artifact.get("error"):
                    _save_hypotheses_to_ddb(ddb, rca_id, artifact)
                seen.add(path.name)
                logger.info("artifact_detected", file=path.name, span_type=span_type)

            elif path.name.startswith(VALIDATION_PATTERN) and path.suffix == ".json":
                idx_str = path.stem.replace(VALIDATION_PATTERN, "")
                try:
                    loop_index = int(idx_str)
                except ValueError:
                    loop_index = 0

                if validation_loop_span_id is None or loop_index == 1:
                    validation_loop_span_id = _write_span(
                        ddb,
                        rca_id,
                        "VALIDATION_LOOP",
                        artifact,
                        loop_index=loop_index,
                    )
                else:
                    _write_span(
                        ddb,
                        rca_id,
                        "VALIDATION_LOOP",
                        artifact,
                        loop_index=loop_index,
                    )

                if artifact and not artifact.get("error"):
                    _update_hypotheses_from_validation(ddb, rca_id, artifact)

                seen.add(path.name)
                logger.info("artifact_detected", file=path.name, span_type="VALIDATION_LOOP", loop_index=loop_index)

        stop_event.wait(_POLL_INTERVAL)


def start_watcher(artifact_dir: Path, rca_id: str, ddb) -> tuple[Thread, Event]:
    stop_event = Event()
    thread = Thread(
        target=_watch_loop,
        args=(artifact_dir, rca_id, ddb, stop_event),
        daemon=True,
    )
    thread.start()
    return thread, stop_event
