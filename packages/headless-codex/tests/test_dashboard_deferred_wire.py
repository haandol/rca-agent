"""Replay exact exported Dashboard apply rows; only execution/S3 review setup is local synthetic data."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from headless_codex.adapters.secondary.playbook.library import _pack

pytest_plugins = ["test_deferred_publication"]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("pending", "WAITING_FOR_PUBLICATION"),
        ("rejected", "BLOCKED"),
        ("applied_matching", "PUBLISHED"),
        ("applied_mismatch", "BLOCKED"),
        ("binding_pending", "WAITING_FOR_PUBLICATION"),
    ],
)
def test_dashboard_apply_export_to_actual_worker(setup, monkeypatch, mode, expected):
    """User disposition/binding comes from the real Dashboard harness output, never a fabricated positive link."""
    worker, *_ = setup
    raw_export = (Path(__file__).parent / "fixtures/actual-dashboard-apply.json").read_bytes()
    assert hashlib.sha256(raw_export).hexdigest() == "96902eb0a92825cbd605bdc67ed8622fae6605f190409011d1a31dc7d1db750d"
    export = json.loads(raw_export)
    case = deepcopy(export["applied_matching" if mode == "binding_pending" else mode])
    observed = datetime.fromisoformat(case["rows"][0]["created_at"].replace("Z", "+00:00"))
    epoch = int(observed.timestamp())

    class ExportClock(datetime):
        """Keep new local publication timestamps at the real Dashboard export's observation clock."""

        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(epoch, tz or UTC)

    from headless_codex.adapters.secondary.execution import dynamodb_execution_store
    from headless_codex.adapters.secondary.playbook import library, s3_vectors_playbook_store

    worker.clock = lambda: epoch
    for module in (library, s3_vectors_playbook_store, dynamodb_execution_store):
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: epoch))
    for module in (library, dynamodb_execution_store):
        monkeypatch.setattr(module, "datetime", ExportClock)
    original_get = worker.s3.get_object

    def observed_s3(**kwargs):
        """Only synthetic execution objects use this clock; exported Dashboard rows remain byte-for-value unchanged."""
        response = original_get(**kwargs)
        response["LastModified"] = observed
        return response

    monkeypatch.setattr(worker.s3, "get_object", observed_s3)
    for row in case["rows"]:
        # Deliberately withhold the actual link for the post-apply/binding-lag case.
        if mode == "binding_pending" and row["SK"].startswith("RECOVERY_PUBLICATION#"):
            continue
        worker.ddb.put_item(TableName="sessions", Item=_pack(row))
    private = case["privateBook"]
    approved = json.dumps(private, ensure_ascii=False).encode()
    execution = {
        "PK": "RCA#new",
        "SK": "EXEC#exec-apply",
        "rca_id": "new",
        "execution_id": "exec-apply",
        "engine": "headless-codex",
        "approval_id": "approval-apply",
        "source_part": "recovery",
        "source_part_revision": "c" * 64,
        "source_part_payload_sha256": "c" * 64,
        "playbook_digest": hashlib.sha256(approved).hexdigest(),
        "approved_playbook_s3_key": "approvals/new/approval-apply/playbook.json",
        "evidence_s3_key": "executions/new/exec-apply/evidence.json",
        "retrospective_diff_s3_key": "executions/new/exec-apply/retrospective-diff.json",
        "execution_state": "RESOLVED",
        "retrospective_status": "FAILED",
        "retrospective_summary": "historical private publication unavailable",
        "started_at": observed.isoformat(),
        "ttl": epoch + 86400,
    }
    worker.ddb.put_item(TableName="sessions", Item=_pack(execution))
    evidence = {
        "rca_id": "new",
        "execution_id": "exec-apply",
        "playbook_id": private["playbook_id"],
        "final_state": "RESOLVED",
        "resolution_confirmed": True,
    }
    for key, body in (
        (execution["approved_playbook_s3_key"], approved),
        (execution["evidence_s3_key"], json.dumps(evidence).encode()),
        (
            execution["retrospective_diff_s3_key"],
            json.dumps({"rationale": "retained local review", "update": {}}).encode(),
        ),
    ):
        worker.s3.put_object(Bucket="evidence", Key=key, Body=body)
    child = worker.enroll("new", "exec-apply")
    worker.resume(child)
    result = worker._get({"PK": child["PK"], "SK": child["SK"]})
    assert result["status"] == expected, result["reason"]
    assert worker._get({"PK": execution["PK"], "SK": execution["SK"]}) == execution
    if expected == "PUBLISHED":
        assert worker.playbook_store._library.head("historical")["revision"] == "retrospective:exec-apply"
