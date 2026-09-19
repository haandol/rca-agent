"""Local native-store compatibility replay; no prose-only authority, model, or infrastructure execution."""

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_playbook_library import source

from headless_codex.adapters.secondary.playbook.library import _pack
from headless_codex.services.legacy_recovery_association import notification_book

pytest_plugins = ["test_deferred_publication"]


@pytest.fixture
def legacy(setup):
    """Create the historical typed generation and exact server report shape with actual local S3/DDB storage."""
    worker, execution, _, public, _, _, read, s3, vectors = setup
    private = json.loads(
        (Path(__file__).resolve().parents[2] / "agent/tests/fixtures/analysis-part-approval-playbook.json").read_text()
    )
    private.update(rca_id="rca-1", playbook_id="private-pb")
    raw_approved = json.dumps(private, ensure_ascii=False).encode()
    execution["playbook_digest"] = hashlib.sha256(raw_approved).hexdigest()
    now = datetime.now(UTC)
    payload = {
        "schema_version": 1,
        "rca_id": "rca-1",
        "engine": "headless-codex",
        "part": "recovery",
        "result": {"playbook": private, "verification": {"valid": True}},
    }
    raw_part = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    revision = hashlib.sha256(raw_part).hexdigest()
    part_key = f"analysis-parts/headless-codex/rca-1/recovery/{revision}.json"
    execution.update(source_part_revision=revision, source_part_payload_sha256=revision)
    worker.ddb.put_item(TableName="sessions", Item=_pack(execution))
    s3.put_object(Bucket="evidence", Key=execution["approved_playbook_s3_key"], Body=raw_approved)
    s3.put_object(Bucket="evidence", Key=part_key, Body=raw_part)
    part = {
        "PK": "RCA#rca-1",
        "SK": "headless-codex#ANALYSIS_PART#recovery",
        "rca_id": "rca-1",
        "engine": "headless-codex",
        "part": "recovery",
        "status": "COMPLETED",
        "attempt": 1,
        "approval_status": "READY",
        "revision": revision,
        "payload_sha256": revision,
        "payload_s3_key": part_key,
        "claim_token": "same-generation",
        "runbook_digest": execution["playbook_digest"],
        "completed_at": (now - timedelta(seconds=30)).isoformat(),
        "ttl": int(time.time()) + 86400,
        "body_expires_at": int(time.time()) + 80000,
    }
    worker.ddb.put_item(TableName="sessions", Item=_pack(part))
    for name, digest in (("root_cause", "b" * 64), ("operations", "c" * 64)):
        record = {
            **part,
            "part": name,
            "SK": f"headless-codex#ANALYSIS_PART#{name}",
            "revision": digest,
            "payload_sha256": digest,
            "payload_s3_key": f"analysis-parts/headless-codex/rca-1/{name}/{digest}.json",
        }
        worker.ddb.put_item(TableName="sessions", Item=_pack(record))
    report_key = "reports/headless-codex/rca-1/attempt-1-same-generation/report.md"
    report = (
        '# RCA Report: rca-1\n\n<!-- rca-summary:v1\n{"playbook_id":"private-pb"}\n-->\n'
        "\n## 최종 공개 지식 — 실행 권한과 별개\n\n- **플레이북 ID**: pb-1\n"
        "\n## 산출물 manifest\n\n"
        f"- recovery: `{part_key}`; SHA-256 `{revision}`\n"
        f"- root_cause: `analysis-parts/headless-codex/rca-1/root_cause/{'b' * 64}.json`; SHA-256 `{'b' * 64}`\n"
        f"- operations: `analysis-parts/headless-codex/rca-1/operations/{'c' * 64}.json`; SHA-256 `{'c' * 64}`"
    )
    s3.put_object(Bucket="evidence", Key=report_key, Body=report.encode())
    parent = source(worker.ddb, public)
    parent.update(
        claim_token="same-generation",
        completed_at=now.isoformat(),
        analysis_parts_finalized=True,
        report_s3_key=report_key,
        completion_notification=json.dumps(
            {"rca_id": "rca-1", "report_s3_key": report_key, "playbook": notification_book(public)}
        ),
    )
    worker.ddb.put_item(TableName="sessions", Item=_pack(parent))
    assert worker.playbook_store.save_to_s3_vectors(public, "rca-1")
    return worker, execution, parent, part, public, private, report, read, report_key


def test_legacy_typed_association_publishes_new_revision_preserving_all_originals(legacy):
    """A separate approved runbook is attached only to the new review revision; all old authority stays intact."""
    worker, execution, parent, part, public, private, _, read, _ = legacy
    original_snapshot = worker.playbook_store._library.snapshot("pb-1", "analysis:rca-1")
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "PUBLISHED", read(child)["reason"]
    binding = worker._get({"PK": parent["PK"], "SK": "RECOVERY_PUBLICATION#" + part["revision"]})
    assert binding["association_mode"] == "LEGACY_SAME_GENERATION"
    assert "claim_token" not in binding["legacy_association"]
    assert all("claim_token" not in record for record in binding["legacy_association"]["terminal_parts"].values())
    assert binding["legacy_association"]["claim_sha256"] == hashlib.sha256(b"same-generation").hexdigest()
    assert read(execution) == execution and read(parent) == parent and read(part) == part
    assert worker.playbook_store._library.snapshot("pb-1", "analysis:rca-1") == original_snapshot
    head = worker.playbook_store._library.head("pb-1")
    result = json.loads(head["playbook_json"])
    assert head["revision"] == "retrospective:exec-1"
    assert result["execution_steps"] == private["execution_steps"]
    assert result["rollback_context"] == private["rollback_context"]
    assert result["verification_status"] == "VERIFIED"
    assert result["permanent_remediation"] == public["permanent_remediation"]
    worker.resume(child)
    assert read(child)["status"] == "PUBLISHED"


@pytest.mark.parametrize(
    "fault",
    [
        "different_claim",
        "late_part",
        "not_ready",
        "raw_part",
        "notification",
        "duplicate_manifest",
        "duplicate_summary",
        "foreign_manifest",
        "trailing_prose",
        "private_id",
        "public_id",
    ],
)
def test_invalid_legacy_chain_is_blocked_without_binding_or_publication(legacy, fault):
    """Missing/ambiguous/foreign evidence cannot be repaired by a same-RCA guess."""
    worker, execution, parent, part, _, _, report, read, report_key = legacy
    if fault == "different_claim":
        part["claim_token"] = "other-generation"
    elif fault == "late_part":
        part["completed_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    elif fault == "not_ready":
        part["approval_status"] = "UNAVAILABLE"
    elif fault == "raw_part":
        worker.s3.put_object(Bucket="evidence", Key=part["payload_s3_key"], Body=b'{"foreign":true}')
    elif fault == "notification":
        parent["completion_notification"] = json.dumps({"rca_id": "rca-1", "report_s3_key": "other"})
    elif fault == "duplicate_manifest":
        report += "\n## 산출물 manifest\n\n"
    elif fault == "duplicate_summary":
        report = report.replace("<!-- rca-summary:v1", "<!-- rca-summary:v1\n{}\n-->\n<!-- rca-summary:v1")
    elif fault == "foreign_manifest":
        report = report.replace("/rca-1/recovery/", "/other/recovery/")
    elif fault == "trailing_prose":
        report += "\nplausible recovery reference elsewhere"
    elif fault == "private_id":
        report = report.replace('"private-pb"', '"foreign"')
    elif fault == "public_id":
        report = report.replace("**플레이북 ID**: pb-1", "**플레이북 ID**: foreign")
    worker.ddb.put_item(TableName="sessions", Item=_pack(parent))
    worker.ddb.put_item(TableName="sessions", Item=_pack(part))
    worker.s3.put_object(Bucket="evidence", Key=report_key, Body=report.encode())
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "BLOCKED", read(child)["reason"]
    assert worker._get({"PK": parent["PK"], "SK": "RECOVERY_PUBLICATION#" + part["revision"]}) is None
    assert worker.playbook_store._library.head("pb-1")["revision"] == "analysis:rca-1"


def test_report_mutation_during_vector_io_cannot_complete_publication(legacy):
    """A changed report after association creation must fail the post-IO source recheck."""
    worker, _, _, _, _, _, report, read, report_key = legacy
    worker.playbook_store._s3v.put_vectors.side_effect = lambda **kwargs: worker.s3.put_object(
        Bucket="evidence", Key=report_key, Body=report.replace("pb-1", "foreign").encode()
    )
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "BLOCKED"
    assert worker.playbook_store._library.head("pb-1")["revision"] == "analysis:rca-1"


@pytest.mark.parametrize("name,status", [("root_cause", "FAILED"), ("operations", "FAILED"), ("operations", "SKIPPED")])
def test_all_terminal_part_statuses_remain_valid_when_exact_generation_matches(legacy, name, status):
    """Failure/skipping is a preserved terminal outcome, never grounds to substitute another part's hash."""
    worker, _, _, _, _, _, _, read, _ = legacy
    record = worker._get({"PK": "RCA#rca-1", "SK": f"headless-codex#ANALYSIS_PART#{name}"})
    record["status"] = status
    worker.ddb.put_item(TableName="sessions", Item=_pack(record))
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "PUBLISHED", read(child)["reason"]


@pytest.mark.parametrize("fault", ["part_hash", "part_claim", "part_attempt", "parent_attempt", "report_path"])
def test_terminal_part_and_attempt_identity_cannot_be_guessed(legacy, fault):
    """Manifest syntax alone cannot authenticate a different terminal part or report generation."""
    worker, _, parent, _, _, _, report, read, report_key = legacy
    record = worker._get({"PK": "RCA#rca-1", "SK": "headless-codex#ANALYSIS_PART#operations"})
    if fault == "part_hash":
        record.update(revision="f" * 64, payload_sha256="f" * 64)
    elif fault == "part_claim":
        record["claim_token"] = "other"
    elif fault == "part_attempt":
        record["attempt"] = 2
    elif fault == "parent_attempt":
        parent["attempt"] = 2
    else:
        other = report_key.replace("attempt-1-", "attempt-2-")
        parent["report_s3_key"] = other
        notice = json.loads(parent["completion_notification"])
        notice["report_s3_key"] = other
        parent["completion_notification"] = json.dumps(notice)
        worker.s3.put_object(Bucket="evidence", Key=other, Body=report.encode())
    worker.ddb.put_item(TableName="sessions", Item=_pack(parent))
    worker.ddb.put_item(TableName="sessions", Item=_pack(record))
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "BLOCKED"


def test_metadata_length_mismatch_cannot_create_legacy_binding(legacy, monkeypatch):
    """A transport/body metadata mismatch must not be treated as a complete server report."""
    worker, _, _, part, _, _, _, read, report_key = legacy
    original = worker.s3.get_object

    def wrong_length(**kwargs):
        response = original(**kwargs)
        if kwargs["Key"] == report_key:
            response["ContentLength"] += 1
        return response

    monkeypatch.setattr(worker.s3, "get_object", wrong_length)
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "BLOCKED"
    assert worker._get({"PK": "RCA#rca-1", "SK": "RECOVERY_PUBLICATION#" + part["revision"]}) is None


def test_existing_association_retries_with_new_clock_without_changing_sources(legacy, monkeypatch):
    """Creation timestamps are not replay identity; all immutable hashes and source expiry stay fixed."""
    worker, _, _, _, _, _, _, read, _ = legacy
    original = worker.playbook_store.finalize_publication
    monkeypatch.setattr(worker.playbook_store, "finalize_publication", lambda *a, **kw: False)
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "FAILED"
    worker.clock = lambda: time.time() + 1
    monkeypatch.setattr(worker.playbook_store, "finalize_publication", original)
    worker.resume(child)
    assert read(child)["status"] == "PUBLISHED", read(child)["reason"]


def test_final_cas_rejects_terminal_part_change_after_post_io_read(legacy, monkeypatch):
    """Every manifest part remains conditionally pinned through the final public-head transaction."""
    worker, _, _, _, _, _, _, read, _ = legacy
    original = worker.playbook_store.finalize_publication

    def changed(*args, **kwargs):
        record = worker._get({"PK": "RCA#rca-1", "SK": "headless-codex#ANALYSIS_PART#operations"})
        record["status"] = "RUNNING"
        worker.ddb.put_item(TableName="sessions", Item=_pack(record))
        return original(*args, **kwargs)

    monkeypatch.setattr(worker.playbook_store, "finalize_publication", changed)
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "FAILED"
    assert worker.playbook_store._library.head("pb-1")["revision"] == "analysis:rca-1"
