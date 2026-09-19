"""Verify historical server records that associated a separate recovery with published knowledge."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal

from headless_codex.adapters.secondary.playbook.library import _pack, encoded, original_live
from headless_codex.services.playbook_merge import procedure_identity
from headless_codex.services.recovery_publication import preserve_recovery_procedure

MODE = "LEGACY_SAME_GENERATION"


def _hash(value):
    """Hash typed server data with the canonical domain representation."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def _epoch(value):
    """Require an explicitly timezone-aware recorded instant, never an inferred ordering."""
    instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("legacy generation timestamp is not timezone aware")
    return instant.timestamp()


def _object(pairs):
    """Reject conflicting JSON keys in server summaries and notifications."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate legacy JSON key")
        result[key] = value
    return result


def notification_book(public):
    """Reproduce the existing notification's bounded summary, never an executable source document."""
    fields = (
        "playbook_id",
        "failure_type",
        "symptom_pattern",
        "severity_criteria",
        "verification_steps",
        "temporary_mitigation",
        "permanent_remediation",
        "escalation_criteria",
        "verification_status",
    )
    summary = {
        name: public.get(name, [] if name == "verification_steps" else "DRAFT" if name == "verification_status" else "")
        for name in fields
    }
    return {**summary, "execution_step_count": len(public.get("execution_steps", []))}


def report_references(text, rca_id, engine, public_id, private_id):
    """Accept only the unique known server summary/public section/terminal manifest, not prose mentions."""
    if not text.startswith(f"# RCA Report: {rca_id}\n"):
        raise ValueError("legacy report RCA identity differs")
    summaries = re.findall(r"<!-- rca-summary:v1\n(.*?)\n-->", text, re.S)
    if len(summaries) != 1:
        raise ValueError("legacy report summary missing or ambiguous")
    summary = json.loads(summaries[0], object_pairs_hook=_object)
    if not isinstance(summary, dict) or summary.get("playbook_id") != private_id:
        raise ValueError("legacy report summary does not identify the approved private runbook")
    heading = "\n## 최종 공개 지식 — 실행 권한과 별개\n"
    marker = "\n## 산출물 manifest\n\n"
    if text.count(heading) != 1 or text.count(marker) != 1:
        raise ValueError("legacy report server sections missing or ambiguous")
    public_section, terminal = text.split(heading, 1)[1].split(marker, 1)
    identifiers = re.findall(r"^- \*\*플레이북 ID\*\*: ([^\r\n]+)$", public_section, re.M)
    if identifiers != [public_id]:
        raise ValueError("legacy report public source position differs")
    lines = terminal.splitlines()
    if len(lines) != 3:
        raise ValueError("legacy report manifest is not the exact terminal server manifest")
    refs = {}
    for name, line in zip(("recovery", "root_cause", "operations"), lines, strict=True):
        match = re.fullmatch(r"- " + name + r": `([^`]+)`; SHA-256 `([a-f0-9]{64})`", line)
        if not match:
            raise ValueError("legacy report manifest entry invalid or duplicated")
        key, digest = match.groups()
        if key != f"analysis-parts/{engine}/{rca_id}/{name}/{digest}.json":
            raise ValueError("legacy report manifest contains a foreign source")
        refs[name] = {"key": key, "sha256": digest}
    return refs


def _guard(table, row, expected, *, now, body=False):
    """Fence exact typed source fields and retention without persisting whole source bodies in the binding."""
    names = {f"#f{i}": field for i, field in enumerate(expected)}
    values = {f":f{i}": value for i, value in enumerate(expected.values())}
    condition = " AND ".join(f"#f{i} = :f{i}" for i in range(len(expected)))
    condition += " AND #ttl > :now AND attribute_not_exists(deleting_at)"
    names["#ttl"] = "ttl"
    values[":now"] = now
    if body:
        condition += " AND body_expires_at > :now"
    return {
        "ConditionCheck": {
            "TableName": table,
            "Key": _pack({"PK": row["PK"], "SK": row["SK"]}),
            "ConditionExpression": condition,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": _pack(values),
        }
    }


def inspect_association(worker, row, parent, draft, approved):
    """Derive a binding only from retained same-generation typed records plus the exact server report format."""
    if parent.get("state") != "COMPLETED" or parent.get("analysis_parts_finalized") is not True:
        return None
    if draft.get("execution_steps"):
        return None  # Current embedded representation continues through its normal publisher.
    if (draft.get("comparison") or {}).get("status") in {"UPDATE_PROPOSED", "NO_CHANGE"}:
        return None  # Never turn pending/related knowledge proposals into an automatic application.
    now = int(worker.clock())
    engine, rca = row["engine"], row["rca_id"]
    part = worker._get({"PK": row["PK"], "SK": f"{engine}#ANALYSIS_PART#recovery"})
    if (
        not part
        or not parent.get("claim_token")
        or part.get("claim_token") != parent["claim_token"]
        or part.get("status") != "COMPLETED"
        or part.get("approval_status") != "READY"
        or part.get("rca_id") != rca
        or part.get("engine") != engine
        or part.get("part") != "recovery"
        or part.get("revision") != row["source_part_revision"]
        or part.get("payload_sha256") != row["source_part_revision"]
        or part.get("runbook_digest") != row["playbook_digest"]
        or _epoch(part.get("completed_at")) > _epoch(parent.get("completed_at"))
        or int(part.get("ttl", 0)) <= now
        or int(part.get("body_expires_at", 0)) <= now
    ):
        raise ValueError("legacy recovery is not the exact retained READY part from the completed generation")
    public_id = parent.get("playbook_id")
    report_key = parent.get("report_s3_key")
    attempt = parent.get("attempt", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, (int, Decimal)) or attempt != int(attempt) or attempt < 1:
        raise ValueError("legacy analysis attempt is invalid")
    attempt = int(attempt)
    notification_raw = parent.get("completion_notification")
    notification = (
        json.loads(notification_raw, object_pairs_hook=_object) if isinstance(notification_raw, str) else None
    )
    if (
        not public_id
        or draft.get("playbook_id") != public_id
        or draft.get("rca_id") != rca
        or parent.get("playbook_index_status") != "PUBLISHED"
        or not isinstance(report_key, str)
        or report_key != f"reports/{parent['engine']}/{rca}/attempt-{attempt}-{parent['claim_token']}/report.md"
        or not isinstance(notification, dict)
        or notification.get("rca_id") != rca
        or notification.get("report_s3_key") != report_key
        or not isinstance(notification.get("playbook"), dict)
        or notification["playbook"] != notification_book(draft)
    ):
        raise ValueError("legacy completed source/notification/public identity differs")
    library = worker.playbook_store._library
    revision = f"analysis:{rca}"
    head = library.head(public_id)
    snapshot = library.snapshot(public_id, revision)
    if (
        not head
        or not snapshot
        or not original_live(head)
        or not original_live(snapshot)
        or head.get("revision") != revision
        or head.get("publication_status") != "PUBLISHED"
        or head.get("source_rca_id") != rca
        or head.get("engine") != parent.get("engine")
        or head.get("playbook_json") != snapshot.get("playbook_json")
        or head["playbook_json"] != encoded(draft)
    ):
        raise ValueError("legacy canonical is not the exact completed published source")
    raw_report, report_expiry = worker._read_bytes(report_key)
    report_hash = hashlib.sha256(raw_report).hexdigest()
    refs = report_references(raw_report.decode("utf-8"), rca, engine, public_id, approved["playbook_id"])
    if refs["recovery"] != {"key": part["payload_s3_key"], "sha256": part["payload_sha256"]}:
        raise ValueError("legacy report recovery differs from the reserved part")
    terminal_parts = {}
    for name, ref in refs.items():
        terminal = (
            part
            if name == "recovery"
            else worker._get(
                {
                    "PK": row["PK"],
                    "SK": f"{engine}#ANALYSIS_PART#{name}",
                }
            )
        )
        if (
            not terminal
            or terminal.get("status") not in {"COMPLETED", "FAILED", "SKIPPED"}
            or terminal.get("claim_token") != parent["claim_token"]
            or terminal.get("attempt") != attempt
            or terminal.get("part") != name
            or terminal.get("engine") != engine
            or terminal.get("rca_id") != rca
            or terminal.get("revision") != ref["sha256"]
            or terminal.get("payload_sha256") != ref["sha256"]
            or terminal.get("payload_s3_key") != ref["key"]
            or _epoch(terminal.get("completed_at")) > _epoch(parent["completed_at"])
            or int(terminal.get("ttl", 0)) <= now
            or int(terminal.get("body_expires_at", 0)) <= now
        ):
            raise ValueError("legacy terminal manifest differs from same-generation part records")
        terminal_parts[name] = terminal
    payload, _, part_expiry = worker._read(part["payload_s3_key"], part["payload_sha256"])
    related = payload.get("result", {}).get("playbook")
    if related != approved:
        raise ValueError("legacy associated runbook differs from exact approved domain")
    completed = preserve_recovery_procedure(
        json.loads(head["playbook_json"]), {"record": part, "payload": payload}, rca
    )
    if procedure_identity(completed) != procedure_identity(approved):
        raise ValueError("legacy associated procedure differs")
    generation = {
        "parent_key": {"PK": parent["PK"], "SK": parent["SK"]},
        "claim_sha256": hashlib.sha256(parent["claim_token"].encode()).hexdigest(),
        "parent_completed_at": parent["completed_at"],
        "report_key": report_key,
        "report_sha256": report_hash,
        "notification_sha256": _hash(notification),
        "part_key": {"PK": part["PK"], "SK": part["SK"]},
        "part_completed_at": part["completed_at"],
        "part_s3_key": part["payload_s3_key"],
        "part_sha256": part["payload_sha256"],
        "runbook_digest": part["runbook_digest"],
        "private_playbook_id": approved["playbook_id"],
        "attempt": attempt,
        "parent_attempt_present": "attempt" in parent,
        "terminal_parts": {
            name: {
                field: record[field]
                for field in (
                    "PK",
                    "SK",
                    "rca_id",
                    "engine",
                    "part",
                    "status",
                    "revision",
                    "payload_sha256",
                    "payload_s3_key",
                    "completed_at",
                    "attempt",
                )
            }
            for name, record in terminal_parts.items()
        },
    }
    binding = {
        "schema_version": 1,
        "rca_id": rca,
        "engine": engine,
        "recovery_revision": row["source_part_revision"],
        "recovery_playbook_sha256": _hash(approved),
        "public_playbook_id": public_id,
        "public_revision": revision,
        "public_body_sha256": _hash(json.loads(head["playbook_json"])),
        "source_rca_id": rca,
        "source_engine": head["engine"],
        "association_mode": MODE,
        "legacy_association": generation,
        "created_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "ttl": min(
            int(parent["ttl"]),
            int(snapshot["ttl"]),
            *(int(record["body_expires_at"]) for record in terminal_parts.values()),
            part_expiry,
            report_expiry,
            int(row["body_expires_at"]),
        ),
    }
    body_field = "completion_playbook" if "completion_playbook" in parent else "playbook"
    guards = [
        _guard(
            worker.table,
            parent,
            {
                "state": "COMPLETED",
                "analysis_parts_finalized": True,
                "claim_token": parent["claim_token"],
                "completed_at": parent["completed_at"],
                "report_s3_key": report_key,
                "completion_notification": notification_raw,
                body_field: parent[body_field],
            },
            now=now,
        ),
        _guard(
            worker.table,
            part,
            {
                "status": "COMPLETED",
                "approval_status": "READY",
                "claim_token": part["claim_token"],
                "revision": part["revision"],
                "payload_sha256": part["payload_sha256"],
                "payload_s3_key": part["payload_s3_key"],
                "runbook_digest": row["playbook_digest"],
                "completed_at": part["completed_at"],
            },
            now=now,
            body=True,
        ),
        _guard(
            worker.table,
            head,
            {
                "revision": revision,
                "playbook_json": head["playbook_json"],
                "publication_status": "PUBLISHED",
                "source_rca_id": rca,
                "engine": head["engine"],
            },
            now=now,
        ),
        _guard(
            worker.table,
            {"PK": f"PLAYBOOK#{public_id}", "SK": revision},
            {
                "revision": revision,
                "playbook_json": snapshot["playbook_json"],
                "source_rca_id": rca,
                "engine": snapshot["engine"],
            },
            now=now,
        ),
    ]
    parent_guard = guards[0]["ConditionCheck"]
    parent_guard["ExpressionAttributeNames"]["#attempt"] = "attempt"
    if "attempt" in parent:
        parent_guard["ConditionExpression"] += " AND #attempt = :attempt"
        parent_guard["ExpressionAttributeValues"].update(_pack({":attempt": parent["attempt"]}))
    else:
        parent_guard["ConditionExpression"] += " AND attribute_not_exists(#attempt)"
    recovery_guard = guards[1]["ConditionCheck"]
    recovery_guard["ConditionExpression"] += " AND #attempt = :attempt"
    recovery_guard["ExpressionAttributeNames"]["#attempt"] = "attempt"
    recovery_guard["ExpressionAttributeValues"].update(_pack({":attempt": attempt}))
    for name in ("root_cause", "operations"):
        record = terminal_parts[name]
        guards.append(
            _guard(
                worker.table,
                record,
                {
                    **generation["terminal_parts"][name],
                    "claim_token": parent["claim_token"],
                },
                now=now,
                body=True,
            )
        )
    return binding, completed, guards
