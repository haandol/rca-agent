"""Bind an immutable private recovery to its completed canonical publication, never to a latest guess."""

import json
import time
from copy import deepcopy
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from rca_agent.adapters.secondary.playbook.library import _pack
from rca_agent.adapters.secondary.playbook.library import payload as domain_payload
from rca_agent.ports.dto.models import ExecutionStep, PlaybookVerificationStatus
from rca_agent.services.analysis_parts import validate_recovery_operations
from rca_agent.services.recovery_context import _digest
from rca_agent.services.runbook_contract import validate_runbook


def procedure_identity(book: dict) -> dict:
    """Compare the full related runbook independently from model-authored knowledge."""
    return {
        key: value
        for key, value in book.items()
        if key in {"execution_steps", "rollback_context", "region", "account_id"}
        or key.startswith(("target_", "execution_", "approval_"))
    }


def retained_recovery(part: dict | None, rca_id: str) -> dict | None:
    """Require verified content-addressed recovery bytes before associating its retained procedure."""
    if not part:
        return None
    record, payload = part.get("record", {}), part.get("payload", {})
    if record.get("status") != "COMPLETED" or record.get("approval_status") != "READY":
        return None
    if (
        payload.get("rca_id") != rca_id
        or payload.get("part") != "recovery"
        or payload.get("engine") != record.get("engine")
        or record.get("revision") != record.get("payload_sha256")
        or _digest(payload) != record.get("payload_sha256")
    ):
        raise ValueError("recovery publication source identity/hash differs")
    result = payload.get("result", {})
    book = result.get("playbook")
    if (
        result.get("verification", {}).get("valid") is not True
        or not isinstance(book, dict)
        or book.get("rca_id") != rca_id
    ):
        raise ValueError("recovery publication source is not verified")
    validate_runbook(book.get("execution_steps", []))
    validate_recovery_operations(book)
    return deepcopy(book)


def preserve_recovery_procedure(public, part: dict | None, rca_id: str):
    """Attach only the server-verified retained runbook; never treat model knowledge as execution authority."""
    private = retained_recovery(part, rca_id)
    result = public.model_copy(deep=True)
    result.execution_steps = (
        [ExecutionStep.model_validate(step) for step in private["execution_steps"]] if private else []
    )
    result.rollback_context = deepcopy(private["rollback_context"]) if private else None
    result.verification_status = PlaybookVerificationStatus.DRAFT
    if private and procedure_identity(result.model_dump(mode="json")) != procedure_identity(private):
        raise ValueError("related runbook lost a retained execution binding")
    return result


def bind_recovery_publication(library, public, part: dict, claim_token: str) -> bool:
    """CAS one immutable binding after published-head confirmation; existing idle discovery is the wake path."""
    private = retained_recovery(part, public.rca_id)
    if private is None:
        return True
    if procedure_identity(private) != procedure_identity(public.model_dump(mode="json")):
        raise ValueError("published related runbook differs from retained recovery")
    source = library.source(public.rca_id, public.playbook_id)
    if source is None or not claim_token or source[0].get("claim_token") != claim_token:
        raise ValueError("canonical publication owner unavailable")
    session = source[0]
    revision = f"analysis:{public.rca_id}"
    snapshot = library.snapshot(public.playbook_id, revision)
    if not snapshot or json.loads(snapshot["playbook_json"]) != domain_payload(public.model_dump(mode="json")):
        raise ValueError("exact canonical snapshot unavailable")
    now = int(time.time())
    record = part["record"]
    row = {
        "schema_version": 1,
        "rca_id": public.rca_id,
        "engine": record["engine"],
        "recovery_revision": record["revision"],
        "recovery_playbook_sha256": _digest(private),
        "public_playbook_id": public.playbook_id,
        "public_revision": revision,
        "public_body_sha256": _digest(json.loads(snapshot["playbook_json"])),
        "source_rca_id": snapshot["source_rca_id"],
        "source_engine": snapshot["engine"],
    }
    identity = dict(row)
    key = {"PK": f"RCA#{public.rca_id}", "SK": f"RECOVERY_PUBLICATION#{record['revision']}"}

    def existing_matches():
        """An idempotent replay may not overwrite a different binding or revive expired authority."""
        existing = library._get(key["PK"], key["SK"])
        if existing is None:
            return False
        if any(existing.get(k) != v for k, v in identity.items()) or int(existing.get("ttl", 0)) <= now:
            raise ValueError("immutable recovery publication binding differs or expired")
        return True

    if existing_matches():
        return True
    head = library.head(public.playbook_id)
    if (
        not head
        or head.get("publication_status") != "PUBLISHED"
        or head.get("revision") != revision
        or head.get("playbook_json") != snapshot["playbook_json"]
    ):
        raise ValueError("canonical publication is not the exact published revision")
    source_guard = library._source_condition(session)
    source_guard["ConditionCheck"]["ConditionExpression"] += " AND claim_token = :binding_claim"
    source_guard["ConditionCheck"]["ExpressionAttributeValues"].update(_pack({":binding_claim": claim_token}))
    expiry = min(int(session["ttl"]), int(snapshot["ttl"]), int(record.get("body_expires_at", record["ttl"])))
    if expiry <= now:
        raise ValueError("recovery publication source expired")
    guards = [source_guard]
    for check_key, expected in (
        (
            {"PK": key["PK"], "SK": f"{record['engine']}#ANALYSIS_PART#recovery"},
            {
                "revision": record["revision"],
                "payload_sha256": record["payload_sha256"],
                "payload_s3_key": record["payload_s3_key"],
                "status": "COMPLETED",
                "approval_status": "READY",
            },
        ),
        (
            {"PK": "PLAYBOOK_LIBRARY", "SK": public.playbook_id},
            {"revision": revision, "playbook_json": snapshot["playbook_json"], "publication_status": "PUBLISHED"},
        ),
    ):
        guards.append(
            {
                "ConditionCheck": {
                    "TableName": library.table,
                    "Key": _pack(check_key),
                    "ConditionExpression": " AND ".join(f"#f{i} = :v{i}" for i in range(len(expected)))
                    + " AND #ttl > :now",
                    "ExpressionAttributeNames": {**{f"#f{i}": name for i, name in enumerate(expected)}, "#ttl": "ttl"},
                    "ExpressionAttributeValues": _pack(
                        {**{f":v{i}": value for i, value in enumerate(expected.values())}, ":now": now}
                    ),
                }
            }
        )
    row.update(created_at=datetime.now(UTC).isoformat(), ttl=expiry)
    try:
        library.client.transact_write_items(
            TransactItems=[
                *guards,
                {
                    "Put": {
                        "TableName": library.table,
                        "Item": _pack({**key, **row}),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
            ]
        )
    except ClientError:
        if not existing_matches():
            raise
    return True
