"""Actual local DynamoDB/S3 adapters; idle followup never receives a runner or model."""

import hashlib
import json
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from test_execution_pipeline import PLAYBOOK
from test_playbook_library import document, source

from headless_codex.adapters.secondary.execution.dynamodb_execution_store import DynamoDbExecutionStore
from headless_codex.adapters.secondary.playbook.library import _pack, _unpack
from headless_codex.services.deferred_publication import DeferredPublication, content_hash

pytest_plugins = ["test_playbook_library"]


@pytest.fixture
def setup(storage, monkeypatch):
    ddb, vectors, publisher = storage
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="evidence")
    monkeypatch.setattr(
        "headless_codex.adapters.secondary.execution.dynamodb_execution_store.DYNAMODB_TABLE_NAME", "sessions"
    )
    execution_store = DynamoDbExecutionStore(ddb, s3_client=s3)
    worker = DeferredPublication(ddb, s3, "sessions", "evidence", execution_store, publisher)
    approved = {
        **deepcopy(PLAYBOOK),
        "rca_id": "rca-1",
        "playbook_id": "private-pb",
        "rollback_context": {"target": "pinned"},
    }
    public = {
        **document(),
        "playbook_id": "pb-1",
        "execution_steps": [],
        "permanent_remediation": "retained public knowledge",
    }
    raw = json.dumps(approved).encode()
    now = int(time.time())
    execution = {
        "PK": "RCA#rca-1",
        "SK": "EXEC#exec-1",
        "rca_id": "rca-1",
        "execution_id": "exec-1",
        "approval_id": "approval-1",
        "engine": "headless-codex",
        "execution_state": "RESOLVED",
        "retrospective_status": "FAILED",
        "retrospective_summary": "historical private publication unavailable",
        "source_part": "recovery",
        "source_part_revision": "a" * 64,
        "source_part_payload_sha256": "a" * 64,
        "approved_playbook_s3_key": "approvals/rca-1/approval-1/playbook.json",
        "playbook_digest": hashlib.sha256(raw).hexdigest(),
        "evidence_s3_key": "executions/rca-1/exec-1/evidence.json",
        "retrospective_diff_s3_key": "executions/rca-1/exec-1/retrospective-diff.json",
        "ttl": now + 86400,
        "started_at": datetime.now(UTC).isoformat(),
    }
    evidence = {
        "rca_id": "rca-1",
        "execution_id": "exec-1",
        "playbook_id": "private-pb",
        "final_state": "RESOLVED",
        "resolution_confirmed": True,
    }
    review = {"rationale": "retained completed review", "update": {}}
    for key, body in (
        (execution["approved_playbook_s3_key"], raw),
        (execution["evidence_s3_key"], json.dumps(evidence).encode()),
        (execution["retrospective_diff_s3_key"], json.dumps(review).encode()),
    ):
        s3.put_object(Bucket="evidence", Key=key, Body=body)
    ddb.put_item(TableName="sessions", Item=_pack(execution))
    source(ddb, public, state="REPORTING")
    binding = {
        "PK": "RCA#rca-1",
        "SK": "RECOVERY_PUBLICATION#" + "a" * 64,
        "schema_version": 1,
        "rca_id": "rca-1",
        "engine": "headless-codex",
        "recovery_revision": "a" * 64,
        "recovery_playbook_sha256": content_hash(approved),
        "public_playbook_id": "pb-1",
        "public_revision": "analysis:rca-1",
        "public_body_sha256": content_hash(public),
        "source_rca_id": "rca-1",
        "source_engine": "headless-codex",
        "created_at": now,
        "ttl": now + 86400,
    }

    def canonical():
        # Production keeps model knowledge separate; the server attaches its
        # verified retained incident runbook through the existing related fields.
        public.update(
            execution_steps=deepcopy(approved["execution_steps"]),
            rollback_context=deepcopy(approved["rollback_context"]),
        )
        binding["public_body_sha256"] = content_hash(public)
        source(ddb, public, engine=binding["source_engine"])
        assert publisher.save_to_s3_vectors(public, "rca-1", source_engine=binding["source_engine"])
        ddb.put_item(TableName="sessions", Item=_pack(binding))

    def read(row):
        return _unpack(ddb.get_item(TableName="sessions", Key=_pack({"PK": row["PK"], "SK": row["SK"]}))["Item"])

    return worker, execution, approved, public, binding, canonical, read, s3, vectors


@pytest.mark.parametrize("order", ["execution_first", "analysis_first"])
def test_both_orderings_publish_once_without_rewriting_historical_failure(setup, order):
    worker, execution, _, public, _, canonical, read, _, vectors = setup
    if order == "analysis_first":
        canonical()
    child = worker.enroll("rca-1", "exec-1")
    if order == "execution_first":
        worker.resume(child)
        assert read(child)["status"] == "WAITING_FOR_PUBLICATION"
        canonical()
    worker.resume(child)
    assert read(child)["status"] == "PUBLISHED"
    assert read(execution) == execution
    assert worker.playbook_store._library.head(public["playbook_id"])["revision"] == "retrospective:exec-1"
    count = vectors.put_vectors.call_count
    worker.resume(child)
    assert worker.enroll("rca-1", "exec-1")["SK"] == child["SK"]
    assert vectors.put_vectors.call_count == count


@pytest.mark.parametrize(
    "mutation", ["digest", "revision", "rca", "engine", "public_hash", "expiry", "steps", "binding"]
)
def test_wrong_lineage_or_procedure_never_publishes(setup, mutation):
    worker, execution, approved, public, binding, canonical, read, _, vectors = setup
    if mutation == "steps":
        approved["execution_steps"][0]["success_criteria"] = "different full criterion"
    elif mutation == "binding":
        approved["rollback_context"]["target"] = "foreign target"
    canonical()
    child = worker.enroll("rca-1", "exec-1")
    if mutation in {"digest", "revision"}:
        execution["playbook_digest" if mutation == "digest" else "source_part_revision"] = "b" * 64
        worker.ddb.put_item(TableName="sessions", Item=_pack(execution))
    else:
        key = {"rca": "rca_id", "engine": "engine", "public_hash": "public_body_sha256", "expiry": "ttl"}.get(mutation)
        if key:
            binding[key] = 1 if mutation == "expiry" else "foreign"
            worker.ddb.put_item(TableName="sessions", Item=_pack(binding))
    count = vectors.put_vectors.call_count
    worker.resume(child)
    assert read(child)["status"] == "BLOCKED"
    assert vectors.put_vectors.call_count == count


def test_no_binding_keeps_user_apply_pending_and_does_not_choose_same_rca_head(setup):
    worker, _, _, _, binding, canonical, read, _, vectors = setup
    canonical()
    worker.ddb.delete_item(TableName="sessions", Key=_pack({"PK": binding["PK"], "SK": binding["SK"]}))
    child = worker.enroll("rca-1", "exec-1")
    count = vectors.put_vectors.call_count
    worker.resume(child)
    assert read(child)["status"] == "WAITING_FOR_PUBLICATION"
    assert vectors.put_vectors.call_count == count


def test_missing_attestation_is_not_enrolled_as_completed_review(setup):
    worker, execution, *_ = setup
    worker.s3.delete_object(Bucket="evidence", Key=execution["retrospective_diff_s3_key"])
    with pytest.raises(ValueError, match="no longer available"):
        worker.enroll("rca-1", "exec-1")
    assert not any(
        item["SK"]["S"].startswith("RETROSPECTIVE_FOLLOWUP") for item in worker.ddb.scan(TableName="sessions")["Items"]
    )


def test_original_review_mutation_blocks_reuse(setup):
    worker, execution, _, _, _, canonical, read, s3, _ = setup
    child = worker.enroll("rca-1", "exec-1")
    canonical()
    s3.put_object(
        Bucket="evidence",
        Key=execution["retrospective_diff_s3_key"],
        Body=json.dumps({"rationale": "changed", "update": {}}).encode(),
    )
    worker.resume(child)
    assert read(child)["status"] == "BLOCKED"


def test_idle_scan_enrolls_historical_failure_without_queue_or_runner(setup):
    worker, execution, _, _, _, canonical, read, _, _ = setup
    canonical()
    for _ in range(40):
        worker.tick()
    child = worker.enroll("rca-1", "exec-1")
    assert read(child)["status"] == "PUBLISHED"
    assert read(execution) == execution


@pytest.mark.parametrize("failure", ["vector", "finalize"])
def test_partial_publication_resumes_same_saved_review_and_hash(setup, monkeypatch, failure):
    worker, execution, _, public, _, canonical, read, _, vectors = setup
    canonical()
    child = worker.enroll("rca-1", "exec-1")
    if failure == "vector":
        vectors.put_vectors.side_effect = RuntimeError("transient vector error")
    else:
        original = worker.playbook_store.finalize_publication
        monkeypatch.setattr(worker.playbook_store, "finalize_publication", lambda *a, **kw: False)
    worker.resume(child)
    assert read(child)["status"] == "FAILED"
    assert worker.playbook_store._library.head(public["playbook_id"])["revision"] == "analysis:rca-1"
    if failure == "vector":
        vectors.put_vectors.side_effect = None
    else:
        monkeypatch.setattr(worker.playbook_store, "finalize_publication", original)
    worker.resume(child)
    assert read(child)["status"] == "PUBLISHED"
    assert read(execution) == execution


def test_cross_engine_public_source_uses_publisher_binding_not_approval_engine(setup):
    worker, execution, _, _, binding, canonical, read, _, _ = setup
    binding["source_engine"] = "strands"
    canonical()
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "PUBLISHED"
    assert read(execution)["engine"] == "headless-codex"
    assert worker._get({"PK": "RCA#rca-1", "SK": "strands#PLAYBOOK_REVISION"})["revised_by_execution_id"] == "exec-1"


@pytest.mark.parametrize("field,value", [("state", "FAILED"), ("deleting", True), ("deleting_at", "now"), ("ttl", 1)])
def test_invalid_parent_blocks_before_missing_binding_wait(setup, field, value):
    worker, *_ = setup
    child = worker.enroll("rca-1", "exec-1")
    parent = worker._get({"PK": "RCA#rca-1", "SK": "ANALYSIS#SESSION"})
    parent[field] = value
    worker.ddb.put_item(TableName="sessions", Item=_pack(parent))
    worker.resume(child)
    assert worker._get({"PK": child["PK"], "SK": child["SK"]})["status"] == "BLOCKED"


def test_sixty_day_source_expiry_is_independent_of_ninety_day_state(setup):
    worker, execution, *_ = setup
    execution["started_at"] = datetime.fromtimestamp(time.time() - 61 * 86400, UTC).isoformat()
    execution["ttl"] = int(time.time()) + 29 * 86400
    worker.ddb.put_item(TableName="sessions", Item=_pack(execution))
    with pytest.raises(ValueError, match="expired"):
        worker.enroll("rca-1", "exec-1")


@pytest.mark.parametrize(
    "body", [b'{"rationale":"a","rationale":"b","update":{}}', b'{"rationale":"a","update":{"value":NaN}}']
)
def test_ambiguous_retained_json_never_enrolls(setup, body):
    worker, execution, *_ = setup
    worker.s3.put_object(Bucket="evidence", Key=execution["retrospective_diff_s3_key"], Body=body)
    with pytest.raises(ValueError):
        worker.enroll("rca-1", "exec-1")


def test_large_retained_json_is_explicitly_rejected_not_silently_cut(setup, monkeypatch):
    worker, *_ = setup
    monkeypatch.setattr("headless_codex.services.deferred_publication._MAX_SOURCE_BYTES", 10)
    with pytest.raises(ValueError, match="bounded"):
        worker.enroll("rca-1", "exec-1")


def test_pending_query_bypasses_unrelated_trace_scan(setup, monkeypatch):
    worker, _, _, _, _, canonical, read, _, _ = setup
    canonical()
    child = worker.enroll("rca-1", "exec-1")
    monkeypatch.setattr(worker.ddb, "scan", lambda **kwargs: pytest.fail("pending work must not need table scan"))
    worker.tick()
    assert read(child)["status"] == "PUBLISHED"


@pytest.mark.parametrize("phase", ["embedding", "finalize"])
def test_lost_lease_cannot_publish_or_overwrite_new_owner(setup, monkeypatch, phase):
    worker, _, _, public, _, canonical, read, _, vectors = setup
    canonical()
    child = worker.enroll("rca-1", "exec-1")

    def steal():
        worker.ddb.update_item(
            TableName="sessions",
            Key=_pack({"PK": child["PK"], "SK": child["SK"]}),
            UpdateExpression="SET lease_token = :token",
            ExpressionAttributeValues=_pack({":token": "other-owner"}),
        )

    if phase == "embedding":
        worker.playbook_store._embedding.embed_document.side_effect = lambda *a, **kw: steal() or [0.1, 0.2]
    else:
        original = worker.playbook_store.finalize_publication

        def final(*a, **kw):
            steal()
            return original(*a, **kw)

        monkeypatch.setattr(worker.playbook_store, "finalize_publication", final)
    prior_writes = vectors.put_vectors.call_count
    with pytest.raises(ClientError):
        worker.resume(child)
    assert read(child)["lease_token"] == "other-owner"
    assert worker.playbook_store._library.head(public["playbook_id"])["revision"] == "analysis:rca-1"
    if phase == "embedding":
        assert vectors.put_vectors.call_count == prior_writes


def test_changed_rollback_binding_in_review_stays_draft(setup):
    worker, execution, _, public, _, canonical, read, s3, _ = setup
    canonical()
    s3.put_object(
        Bucket="evidence",
        Key=execution["retrospective_diff_s3_key"],
        Body=json.dumps(
            {"rationale": "new unexecuted binding", "update": {"rollback_context": {"target": "new"}}}
        ).encode(),
    )
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "PUBLISHED"
    head = worker.playbook_store._library.head(public["playbook_id"])
    result = json.loads(head["playbook_json"])
    assert result["verification_status"] == "DRAFT"
    assert result["execution_steps"] == public["execution_steps"]
    assert result["rollback_context"]["target"] == "new"
    assert result["permanent_remediation"] == public["permanent_remediation"]


@pytest.mark.parametrize("recovery_engine", ["strands", "headless-codex"])
def test_actual_agent_publisher_binding_drives_headless_followup(setup, monkeypatch, recovery_engine):
    """Exercise server attachment + Agent publication/binding + Headless resume with real local stores."""
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "packages/agent/src"))
    from rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store import S3VectorsPlaybookStore
    from rca_agent.ports.dto.models import Playbook
    from rca_agent.services.recovery_publication import bind_recovery_publication, preserve_recovery_procedure

    worker, execution, _, _, _, _, read, s3, vectors = setup
    private = json.loads((root / "packages/agent/tests/fixtures/analysis-part-approval-playbook.json").read_text())
    private["rca_id"] = "rca-1"
    private = Playbook.model_validate(private).model_dump(mode="json")
    part_payload = {
        "schema_version": 1,
        "rca_id": "rca-1",
        "engine": recovery_engine,
        "part": "recovery",
        "result": {"playbook": private, "verification": {"valid": True}},
    }
    revision = content_hash(part_payload)
    now = int(time.time())
    part_record = {
        "PK": "RCA#rca-1",
        "SK": f"{recovery_engine}#ANALYSIS_PART#recovery",
        "engine": recovery_engine,
        "revision": revision,
        "payload_sha256": revision,
        "payload_s3_key": f"analysis-parts/{recovery_engine}/rca-1/recovery/{revision}.json",
        "status": "COMPLETED",
        "approval_status": "READY",
        "ttl": now + 86400,
        "body_expires_at": now + 86400,
    }
    part = {"record": part_record, "payload": part_payload}
    model_knowledge = Playbook(
        playbook_id="public-knowledge",
        rca_id="rca-1",
        failure_type="actual source mismatch",
        symptom_pattern="write failures",
        execution_steps=[],
    )
    public = preserve_recovery_procedure(model_knowledge, part, "rca-1")
    assert model_knowledge.execution_steps == []  # Only server attachment adds related procedure.
    execution.update(
        engine=recovery_engine,
        source_part_revision=revision,
        source_part_payload_sha256=revision,
        playbook_digest=hashlib.sha256(json.dumps(private).encode()).hexdigest(),
    )
    s3.put_object(Bucket="evidence", Key=execution["approved_playbook_s3_key"], Body=json.dumps(private).encode())
    s3.put_object(
        Bucket="evidence",
        Key=execution["evidence_s3_key"],
        Body=json.dumps(
            {
                "rca_id": "rca-1",
                "execution_id": "exec-1",
                "playbook_id": private["playbook_id"],
                "final_state": "RESOLVED",
                "resolution_confirmed": True,
            }
        ).encode(),
    )
    worker.ddb.put_item(TableName="sessions", Item=_pack(execution))
    worker.ddb.put_item(TableName="sessions", Item=_pack(part_record))
    parent = source(worker.ddb, public.model_dump(mode="json"), engine="strands")
    parent["claim_token"] = "publication-claim"
    worker.ddb.put_item(TableName="sessions", Item=_pack(parent))
    module = "rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store"
    monkeypatch.setattr(f"{module}.DYNAMODB_TABLE_NAME", "sessions")
    monkeypatch.setattr(f"{module}.S3_VECTOR_BUCKET_NAME", "vectors")
    publisher = S3VectorsPlaybookStore(vectors, worker.playbook_store._embedding, worker.ddb)
    assert publisher.save(public)
    assert bind_recovery_publication(publisher._library, public, part, "publication-claim")
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "PUBLISHED"
    assert read(execution) == execution


def test_retained_publication_body_cannot_replace_review_result_on_retry(setup, monkeypatch):
    worker, _, _, public, _, canonical, read, _, _ = setup
    canonical()
    child = worker.enroll("rca-1", "exec-1")
    original = worker.playbook_store.finalize_publication
    monkeypatch.setattr(worker.playbook_store, "finalize_publication", lambda *a, **kw: False)
    worker.resume(child)
    work_key = {"PK": f"PLAYBOOK#{public['playbook_id']}", "SK": "retrospective:exec-1"}
    work = worker._get(work_key)
    body = json.loads(work["playbook_json"])
    body["rollback_context"] = {"target": "unreviewed"}
    work["playbook_json"] = json.dumps(body)
    worker.ddb.put_item(TableName="sessions", Item=_pack(work))
    monkeypatch.setattr(worker.playbook_store, "finalize_publication", original)
    worker.resume(child)
    assert read(child)["status"] == "BLOCKED"
    assert worker.playbook_store._library.head(public["playbook_id"])["revision"] == "analysis:rca-1"


def test_binding_expiry_is_checked_at_final_cas_even_without_content_change(setup, monkeypatch):
    from types import SimpleNamespace

    from headless_codex.adapters.secondary.playbook import library

    worker, _, _, public, binding, canonical, read, _, _ = setup
    binding["ttl"] = int(time.time()) + 5
    canonical()
    child = worker.enroll("rca-1", "exec-1")
    original = worker.playbook_store.finalize_publication

    def after_expiry(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(library, "time", SimpleNamespace(time=lambda: binding["ttl"] + 1))
            return original(*args, **kwargs)

    monkeypatch.setattr(worker.playbook_store, "finalize_publication", after_expiry)
    worker.resume(child)
    assert read(child)["status"] == "FAILED"
    assert worker.playbook_store._library.head(public["playbook_id"])["revision"] == "analysis:rca-1"


def test_approved_wrong_rca_rejected_even_with_matching_raw_digest(setup):
    worker, execution, approved, *_ = setup
    approved["rca_id"] = "other"
    raw = json.dumps(approved).encode()
    execution["playbook_digest"] = hashlib.sha256(raw).hexdigest()
    worker.ddb.put_item(TableName="sessions", Item=_pack(execution))
    worker.s3.put_object(Bucket="evidence", Key=execution["approved_playbook_s3_key"], Body=raw)
    with pytest.raises(ValueError, match="retained review"):
        worker.enroll("rca-1", "exec-1")


def test_original_object_lifetime_checked_even_when_s3_deletion_lags(setup, monkeypatch):
    worker, *_ = setup
    original = worker.s3.get_object

    def stale(**kwargs):
        result = original(**kwargs)
        result["LastModified"] = datetime.fromtimestamp(time.time() - 61 * 86400, UTC)
        return result

    monkeypatch.setattr(worker.s3, "get_object", stale)
    with pytest.raises(ValueError, match="sixty-day"):
        worker.enroll("rca-1", "exec-1")


@pytest.mark.parametrize("mode", ["missing_draft", "failed_handoff", "pending_index"])
def test_completed_draft_failure_is_not_confused_with_index_wait(setup, mode):
    worker, *_ = setup
    child = worker.enroll("rca-1", "exec-1")
    parent = worker._get({"PK": "RCA#rca-1", "SK": "ANALYSIS#SESSION"})
    parent["state"] = "COMPLETED"
    parent["playbook_index_status"] = "PENDING"
    if mode == "missing_draft":
        parent.pop("playbook", None)
    elif mode == "failed_handoff":
        parent["playbook_index_status"] = "FAILED"
    worker.ddb.put_item(TableName="sessions", Item=_pack(parent))
    worker.resume(child)
    result = worker._get({"PK": child["PK"], "SK": child["SK"]})
    assert result["status"] == ("WAITING_FOR_PUBLICATION" if mode == "pending_index" else "BLOCKED")
    if mode == "pending_index":
        assert "index publication" in result["reason"]


def test_earliest_original_source_expiry_fences_vector_completion(setup, monkeypatch):
    """An old approval object can expire before execution state or the freshly saved review."""
    from types import SimpleNamespace

    from headless_codex.adapters.secondary.playbook import library, s3_vectors_playbook_store

    worker, execution, _, public, _, canonical, read, _, vectors = setup
    canonical()
    expiry = int(time.time()) + 60
    original = worker.s3.get_object

    def old_approval(**kwargs):
        result = original(**kwargs)
        if kwargs["Key"] == execution["approved_playbook_s3_key"]:
            result["LastModified"] = datetime.fromtimestamp(expiry - 60 * 86400, UTC)
        return result

    monkeypatch.setattr(worker.s3, "get_object", old_approval)
    child = worker.enroll("rca-1", "exec-1")
    assert child["body_expires_at"] == expiry

    def finishes_after_expiry(**kwargs):
        later = SimpleNamespace(time=lambda: expiry + 1)
        monkeypatch.setattr(library, "time", later)
        monkeypatch.setattr(s3_vectors_playbook_store, "time", later)
        worker.clock = later.time

    vectors.put_vectors.side_effect = finishes_after_expiry
    worker.resume(child)
    assert read(child)["status"] == "FAILED"
    assert read(child)["updated_at"] == expiry + 1
    assert worker.playbook_store._library.head(public["playbook_id"])["revision"] == "analysis:rca-1"
    assert worker._get({"PK": "RCA#rca-1", "SK": "headless-codex#PLAYBOOK_REVISION"}) is None


def test_deferred_review_cannot_relabel_canonical_identity(setup):
    """Saved model metadata cannot replace the canonical incident/revision authority during deferred publication."""
    worker, execution, _, public, _, canonical, read, s3, _ = setup
    canonical()
    s3.put_object(
        Bucket="evidence",
        Key=execution["retrospective_diff_s3_key"],
        Body=json.dumps(
            {
                "rationale": "Retained review with untrusted identity fields",
                "update": {
                    "rca_id": "forged",
                    "source_rca_id": "forged",
                    "source_engine": "forged",
                    "library_revision": "forged",
                    "comparison": {"status": "APPLIED"},
                },
            }
        ).encode(),
    )
    child = worker.enroll("rca-1", "exec-1")
    worker.resume(child)
    assert read(child)["status"] == "PUBLISHED"
    head = worker.playbook_store._library.head(public["playbook_id"])
    published = json.loads(head["playbook_json"])
    assert published["rca_id"] == public["rca_id"]
    assert published["comparison"] == public["comparison"]
    assert head["source_rca_id"] == "rca-1"
    assert head["engine"] == "headless-codex"
    assert head["revision"] == "retrospective:exec-1"
    assert not any(key in published for key in ("source_rca_id", "source_engine", "library_revision"))
    assert read(execution) == execution
