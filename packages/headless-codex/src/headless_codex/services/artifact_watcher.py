from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import structlog

from headless_codex.config.settings import DYNAMODB_TABLE_NAME, ENGINE, SESSION_TTL_DAYS
from headless_codex.services.artifact_validation import (
    ArtifactValidationError,
    validate_validation_artifacts,
)

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
    "severity_criteria",
    "temporary_mitigation",
    "permanent_remediation",
    "escalation_criteria",
    "verification_status",
)
_PLAYBOOK_LIST_FIELDS = (
    "verification_steps",
    "prevention_measures",
    "related_metrics",
    "tags",
)
_EXECUTION_STEP_FIELDS = ("step_id", "intent", "action", "success_criteria")


def _safe_metadata_string(value, *, max_length: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return str(value)[:max_length]


def _build_execution_steps_metadata(steps: object) -> list[dict] | None:
    """Keep the steps a person approves so the dashboard can show them."""
    if not isinstance(steps, list) or not steps:
        return None
    rendered = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        rendered.append(
            {
                "M": {
                    field: {"S": _safe_metadata_string(step.get(field), max_length=500)}
                    for field in _EXECUTION_STEP_FIELDS
                }
            }
        )
    return rendered or None


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
    steps = _build_execution_steps_metadata(artifact.get("execution_steps"))
    if steps:
        meta["execution_steps"] = {"L": steps}
    return meta


def _write_span(
    ddb,
    rca_id: str,
    span_type: str,
    artifact: dict | None,
    *,
    claim_token: str,
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
        _transact_claimed(
            ddb,
            rca_id,
            claim_token,
            [{"Put": {"TableName": DYNAMODB_TABLE_NAME, "Item": item}}],
        )
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
        artifact = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("artifact_json_malformed", path=str(path), raw=raw[:300])
        return {"error": "Malformed JSON artifact"}
    if not isinstance(artifact, dict):
        logger.warning("artifact_json_not_object", path=str(path))
        return {"error": "JSON artifact must be an object"}
    return artifact


def _artifact_sort_key(path: Path) -> tuple[int, int, str]:
    if path.name.startswith(VALIDATION_PATTERN) and path.suffix == ".json":
        try:
            loop_index = int(path.stem.removeprefix(VALIDATION_PATTERN))
        except ValueError:
            loop_index = 0
        return 1, loop_index, path.name
    return 0, 0, path.name


def _claim_check(rca_id: str, claim_token: str) -> dict:
    return {
        "ConditionCheck": {
            "TableName": DYNAMODB_TABLE_NAME,
            "Key": {
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": "ANALYSIS#SESSION"},
            },
            "ConditionExpression": "attribute_exists(SK) AND claim_token = :claim",
            "ExpressionAttributeValues": {":claim": {"S": claim_token}},
        }
    }


def _transact_claimed(ddb, rca_id: str, claim_token: str, writes: list[dict]) -> None:
    if not DYNAMODB_TABLE_NAME or not ddb:
        return
    for index in range(0, len(writes), 24):
        ddb.transact_write_items(
            TransactItems=[
                _claim_check(rca_id, claim_token),
                *writes[index : index + 24],
            ]
        )


def _save_hypotheses_to_ddb(ddb, rca_id: str, artifact: dict, *, claim_token: str) -> None:
    if not DYNAMODB_TABLE_NAME or not ddb:
        return

    hypotheses = artifact.get("hypotheses", [])
    if not hypotheses:
        return

    now = _now_iso()
    ttl = _ttl()

    writes = []
    for h in hypotheses:
        hid = h.get("hypothesis_id", str(uuid.uuid4()))
        item = {
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": f"{ENGINE}#HYPO#{hid}"},
            "engine": {"S": ENGINE},
            "tree_id": {"S": h.get("tree_id", "")},
            "depth": {"N": str(h.get("depth", 0))},
            "title": {"S": h.get("title", "")[:120]},
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
        }
        writes.append(
            {
                "Put": {
                    "TableName": DYNAMODB_TABLE_NAME,
                    "Item": item,
                }
            },
        )

    try:
        _transact_claimed(ddb, rca_id, claim_token, writes)
    except Exception:
        logger.exception("hypothesis_batch_write_failed", count=len(writes))


def _update_hypotheses_from_validation(ddb, rca_id: str, artifact: dict, *, claim_token: str) -> None:
    if not DYNAMODB_TABLE_NAME or not ddb:
        return

    now = _now_iso()
    writes: list[dict] = []
    for bucket in ("confirmed", "rejected", "closed", "needs_investigation"):
        status_map = {
            "confirmed": "CONFIRMED",
            "rejected": "REJECTED",
            "closed": "CLOSED",
            "needs_investigation": "NEEDS_INVESTIGATION",
        }
        for h in artifact.get(bucket, []):
            hid = h if isinstance(h, str) else h.get("hypothesis_id", "")
            confidence = h.get("confidence", 0) if isinstance(h, dict) else 0
            reasoning = h.get("reasoning", "") if isinstance(h, dict) else ""
            if not hid:
                continue
            writes.append(
                {
                    "Update": {
                        "TableName": DYNAMODB_TABLE_NAME,
                        "Key": {
                            "PK": {"S": f"RCA#{rca_id}"},
                            "SK": {"S": f"{ENGINE}#HYPO#{hid}"},
                        },
                        "UpdateExpression": (
                            "SET #st = :status, confidence_score = :cs, judgment_reasoning = :jr, updated_at = :now"
                        ),
                        "ConditionExpression": "attribute_exists(SK)",
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": {
                            ":status": {"S": status_map[bucket]},
                            ":cs": {"N": str(confidence)},
                            ":jr": {"S": str(reasoning)[:500]},
                            ":now": {"S": now},
                        },
                    }
                }
            )

    try:
        _transact_claimed(ddb, rca_id, claim_token, writes)
    except Exception:
        logger.warning("hypothesis_updates_skipped", count=len(writes))

    new_hypotheses = artifact.get("new_hypotheses", [])
    if new_hypotheses:
        _save_hypotheses_to_ddb(
            ddb,
            rca_id,
            {"hypotheses": new_hypotheses},
            claim_token=claim_token,
        )


def _scan_once(
    artifact_dir: Path,
    rca_id: str,
    ddb,
    seen: dict[str, tuple[int, int]],
    validation_ctx: dict,
    claim_token: str,
) -> None:
    if not artifact_dir.exists():
        return

    for path in sorted(artifact_dir.iterdir(), key=_artifact_sort_key):
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_size)
        if seen.get(path.name) == version:
            continue

        artifact = _parse_artifact(path)
        if artifact is None:
            continue
        span_type = ARTIFACT_SPAN_MAP.get(path.name)

        if span_type:
            _write_span(ddb, rca_id, span_type, artifact, claim_token=claim_token)
            if span_type == "HYPOTHESIS_GENERATION" and artifact and not artifact.get("error"):
                _save_hypotheses_to_ddb(ddb, rca_id, artifact, claim_token=claim_token)
            seen[path.name] = version
            logger.info("artifact_detected", file=path.name, span_type=span_type)

        elif path.name.startswith(VALIDATION_PATTERN) and path.suffix == ".json":
            idx_str = path.stem.replace(VALIDATION_PATTERN, "")
            try:
                loop_index = int(idx_str)
            except ValueError:
                loop_index = 0

            if artifact and not artifact.get("error"):
                try:
                    validation_path, _ = validate_validation_artifacts(
                        artifact_dir,
                        through_loop_index=loop_index,
                    )
                    if validation_path != path:
                        raise ArtifactValidationError(
                            f"{path.name} is not the latest validation through loop {loop_index}"
                        )
                    validation_ctx[path.name] = {"valid": True}
                except ArtifactValidationError as exc:
                    validation_ctx[path.name] = {"valid": False, "error": str(exc)}
                    artifact = {
                        "summary": path.name,
                        "output_summary": str(exc),
                        "error": str(exc),
                    }

            _write_span(
                ddb,
                rca_id,
                "VALIDATION_LOOP",
                artifact,
                claim_token=claim_token,
                loop_index=loop_index,
            )

            if artifact and not artifact.get("error"):
                _update_hypotheses_from_validation(
                    ddb,
                    rca_id,
                    artifact,
                    claim_token=claim_token,
                )

            seen[path.name] = version
            logger.info("artifact_detected", file=path.name, span_type="VALIDATION_LOOP", loop_index=loop_index)


def _watch_loop(artifact_dir: Path, rca_id: str, claim_token: str, ddb, stop_event: Event) -> None:
    seen: dict[str, tuple[int, int]] = {}
    validation_ctx: dict = {}

    while not stop_event.is_set():
        _scan_once(artifact_dir, rca_id, ddb, seen, validation_ctx, claim_token)
        stop_event.wait(_POLL_INTERVAL)

    _scan_once(artifact_dir, rca_id, ddb, seen, validation_ctx, claim_token)
    logger.info("watcher_final_scan_complete", seen_count=len(seen))


def start_watcher(artifact_dir: Path, rca_id: str, claim_token: str, ddb) -> tuple[Thread, Event]:
    stop_event = Event()
    thread = Thread(
        target=_watch_loop,
        args=(artifact_dir, rca_id, claim_token, ddb, stop_event),
        daemon=True,
    )
    thread.start()
    return thread, stop_event
