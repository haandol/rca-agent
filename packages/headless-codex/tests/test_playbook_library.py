"""Real DynamoDB transactions guard shared playbook publication, including races."""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from headless_codex.adapters.secondary.playbook.library import _pack, encoded
from headless_codex.adapters.secondary.playbook.s3_vectors_playbook_store import S3VectorsPlaybookStore
from headless_codex.ports.interfaces.playbook_store import PlaybookSearchUnavailable

MODULE = "headless_codex.adapters.secondary.playbook.s3_vectors_playbook_store"
ENGINE = "headless-codex"


@pytest.fixture
def storage(monkeypatch):
    """Create the actual existing-table wire; mock only embedding and S3 Vectors."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="sessions",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(f"{MODULE}.DYNAMODB_TABLE_NAME", "sessions")
        monkeypatch.setattr(f"{MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
        vectors, embedding = MagicMock(), MagicMock()
        embedding.embed_query.return_value = embedding.embed_document.return_value = [0.1, 0.2]
        vectors.query_vectors.return_value = {"vectors": []}
        yield ddb, vectors, S3VectorsPlaybookStore(vectors, embedding, ddb)


def document():
    """Include comparison evidence to expose accidental manual field reconstruction."""
    return {
        "playbook_id": "pb-1",
        "failure_type": "Pool pressure",
        "symptom_pattern": "Connections rise",
        "rca_id": "rca-1",
        "verification_status": "DRAFT",
        "comparison": {"status": "NO_SIMILAR_CANDIDATE", "query": "pool pressure", "candidates": []},
    }


def source(ddb, value, *, engine=ENGINE, state="COMPLETED", ttl=None, rca_id="rca-1"):
    """Persist source birth and expiry from one fixture instant, independent of publication state."""
    now = int(time.time())
    item = {
        "PK": f"RCA#{rca_id}",
        "SK": "ANALYSIS#SESSION",
        "engine": engine,
        "state": state,
        "playbook_id": value["playbook_id"],
        "playbook": json.dumps(value),
        "playbook_index_status": "PUBLISHED",
        "created_at": now,
        "ttl": ttl if ttl is not None else now + 90 * 86400,
    }
    ddb.put_item(TableName="sessions", Item=_pack(item))
    return item


def publish(store, value, **kwargs):
    """Exercise the complete public adapter API in each package."""
    if ENGINE == "strands":
        from rca_agent.ports.dto.models import Playbook

        # The real DTO includes defaults; completion must contain that identical full payload.
        return store.save(Playbook.model_validate(value), **kwargs)
    return store.save_to_s3_vectors(value, value.get("rca_id", "rca-1"), **kwargs)


def completed(storage):
    """Keep source and incident payload byte-for-value equivalent, as completion handoff does."""
    ddb, _, store = storage
    value = document()
    if ENGINE == "strands":
        from rca_agent.ports.dto.models import Playbook

        value = Playbook.model_validate(value).model_dump(mode="json")
    source(ddb, value)
    return value, store


def hit(head, distance=0.1):
    """Build an immutable vector pointer from the shared published wire."""
    return {
        "key": head["vector_key"],
        "distance": distance,
        "metadata": {
            "playbook_id": head["SK"],
            "library_revision": head["revision"],
            "engine": head["engine"],
            "rca_id": head["source_rca_id"],
            "verification_status": "VERIFIED",
        },
    }


def test_full_source_snapshot_and_exact_retry(storage):
    ddb, vectors, _ = storage
    value, store = completed(storage)
    assert publish(store, value, metric_name="DatabaseConnections")
    head = store._library.head("pb-1")
    assert head["publication_status"] == "PUBLISHED"
    assert head["revision"] == "analysis:rca-1"
    assert json.loads(head["playbook_json"]) == json.loads(encoded(value))
    snapshot = ddb.get_item(TableName="sessions", Key=_pack({"PK": "PLAYBOOK#pb-1", "SK": head["revision"]}))["Item"]
    assert snapshot["playbook_json"]["S"] == head["playbook_json"]
    assert publish(store, value)
    assert vectors.put_vectors.call_count == 1
    assert (
        snapshot
        == ddb.get_item(TableName="sessions", Key=_pack({"PK": "PLAYBOOK#pb-1", "SK": head["revision"]}))["Item"]
    )
    vectors.query_vectors.return_value = {"vectors": [hit(head)]}
    matches = store.search_similar("pool pressure", threshold=0.8)
    assert matches[0].playbook_id == "pb-1"
    assert matches[0].engine == ENGINE
    assert matches[0].library_revision == head["revision"]
    assert matches[0].verification_status == "DRAFT"
    detail = store.load_detail(matches[0])
    if not isinstance(detail, dict):
        detail = detail.model_dump(mode="json")
    assert detail["library_revision"] == head["revision"]
    assert detail["source_engine"] == ENGINE
    assert detail["comparison"] == value["comparison"]


def test_pending_failure_retries_same_immutable_vector(storage):
    _, vectors, _ = storage
    value, store = completed(storage)
    vectors.put_vectors.side_effect = RuntimeError("index offline")
    assert not publish(store, value)
    head = store._library.head("pb-1")
    assert head["publication_status"] == "PENDING"
    vectors.query_vectors.return_value = {"vectors": [hit(head)]}
    assert_unavailable(store, store.search_similar("pool", threshold=0.8))
    vectors.put_vectors.side_effect = None
    assert publish(store, value)
    assert {call.kwargs["vectors"][0]["key"] for call in vectors.put_vectors.call_args_list} == {head["vector_key"]}


@pytest.mark.parametrize("fault", ["missing", "expired", "incomplete", "wrong_id", "wrong_engine"])
def test_source_guard_rejects_unusable_completion(storage, fault):
    ddb, vectors, store = storage
    value = document()
    if fault != "missing":
        recorded = dict(value, playbook_id="other") if fault == "wrong_id" else value
        source(
            ddb,
            recorded,
            state="ANALYZING" if fault == "incomplete" else "COMPLETED",
            ttl=1 if fault == "expired" else None,
            engine=("headless-codex" if ENGINE == "strands" else "strands") if fault == "wrong_engine" else ENGINE,
        )
    assert not publish(store, value)
    vectors.put_vectors.assert_not_called()


def test_new_source_and_changed_replay_do_not_overwrite(storage):
    ddb, vectors, _ = storage
    value, store = completed(storage)
    assert publish(store, value)
    original = store._library.head("pb-1")
    changed = dict(value, temporary_mitigation="different")
    assert not publish(store, changed)
    other = dict(value, rca_id="rca-2")
    source(ddb, other, rca_id="rca-2")
    assert not publish(store, other)
    assert store._library.head("pb-1") == original
    assert vectors.put_vectors.call_count == 1


@pytest.mark.parametrize("status", ["UPDATE_PROPOSED", "NO_CHANGE"])
def test_matched_comparison_never_replaces_public_knowledge(storage, status):
    _, vectors, _ = storage
    value, store = completed(storage)
    assert publish(store, value)
    before = store._library.head("pb-1")
    incident = dict(value, comparison={"status": status, "selected_playbook_id": "pb-1"})
    assert publish(store, incident)
    assert store._library.head("pb-1") == before
    assert vectors.put_vectors.call_count == 1


def test_late_vector_publisher_cannot_finalize_or_select_old_key(storage):
    _, vectors, _ = storage
    value, store = completed(storage)

    def replace_head(**kwargs):
        head = store._library.head("pb-1")
        # Simulate the dashboard applying a newer revision while vector I/O was in flight.
        newer = dict(head, revision="proposal:p-2", vector_key="pb-1@proposal:p-2", publication_status="PUBLISHED")
        store._ddb.put_item(TableName="sessions", Item=_pack(newer))

    vectors.put_vectors.side_effect = replace_head
    assert not publish(store, value)
    current = store._library.head("pb-1")
    stale = dict(current, revision="analysis:rca-1", vector_key="pb-1@analysis:rca-1")
    vectors.query_vectors.return_value = {"vectors": [hit(stale, 0.01), hit(current, 0.1)]}
    matches = store.search_similar("pool", threshold=0.8)
    assert [item.library_revision for item in matches if not item.unavailable_reason] == ["proposal:p-2"]


@pytest.mark.parametrize("fault", ["malformed", "expired_source", "missing_source", "pending"])
def test_bad_latest_never_resurrects_original(storage, fault):
    ddb, vectors, _ = storage
    value, store = completed(storage)
    assert publish(store, value)
    head = store._library.head("pb-1")
    if fault == "malformed":
        head["playbook_json"] = "{broken"
    elif fault == "pending":
        head["publication_status"] = "PENDING"
    elif fault == "expired_source":
        source(ddb, value, ttl=1)
    else:
        ddb.delete_item(TableName="sessions", Key=_pack({"PK": "RCA#rca-1", "SK": "ANALYSIS#SESSION"}))
    ddb.put_item(TableName="sessions", Item=_pack(head))
    vectors.query_vectors.return_value = {
        "vectors": [hit(head), {"key": "pb-1", "distance": 0.01, "metadata": {"rca_id": "rca-1"}}]
    }
    assert_unavailable(store, store.search_similar("pool", threshold=0.8))


def test_source_expiration_during_vector_io_prevents_publication(storage):
    ddb, vectors, _ = storage
    value, store = completed(storage)
    vectors.put_vectors.side_effect = lambda **kwargs: source(ddb, value, ttl=1)
    assert not publish(store, value)
    assert store._library.head("pb-1")["publication_status"] == "PENDING"


def test_source_revoked_between_read_and_transaction(storage):
    ddb, vectors, _ = storage
    value, store = completed(storage)
    original = ddb.transact_write_items

    def revoke(**kwargs):
        source(ddb, value, state="CANCELLED")
        return original(**kwargs)

    ddb.transact_write_items = revoke
    assert not publish(store, value)
    assert store._library.head("pb-1") is None
    vectors.put_vectors.assert_not_called()


def test_legacy_pagination_and_actual_source_engine(storage):
    ddb, _, store = storage
    value = document()
    source(ddb, value, engine="cc-headless")
    revision = {
        "PK": "RCA#rca-1",
        "SK": "cc-headless#PLAYBOOK_REVISION",
        "playbook_id": "pb-1",
        "publication_status": "PUBLISHED",
        "revised_by_execution_id": "exec-0",
        "playbook": json.dumps(dict(value, verification_status="VERIFIED")),
    }
    ddb.put_item(TableName="sessions", Item=_pack(revision))
    query = ddb.query
    calls = []

    def paginated(**kwargs):
        calls.append(kwargs)
        return query(**kwargs, Limit=1)

    ddb.query = paginated
    detail = store._library.load("pb-1", "rca-1", publication_id="exec-0")
    assert detail["source_engine"] == "cc-headless"
    assert detail["library_revision"] == "legacy"
    assert any("ExclusiveStartKey" in call for call in calls)
    revision["playbook"] = "{broken"
    ddb.put_item(TableName="sessions", Item=_pack(revision))
    assert store._library.load("pb-1", "rca-1", publication_id="exec-0") is None


def test_retrospective_requires_matching_baseline_and_original_commit(storage):
    ddb, vectors, _ = storage
    value, store = completed(storage)
    assert publish(store, value)
    head = store._library.head("pb-1")
    baseline = store._library.load("pb-1", "rca-1", ENGINE, head["revision"])
    revised = dict(value, verification_status="VERIFIED")
    assert not store._publish(
        revised, "rca-1", publication_id="exec-1", baseline_playbook=dict(baseline, library_revision="proposal:stale")
    )
    assert store._library.head("pb-1") == head
    assert store._publish(revised, "rca-1", publication_id="exec-1", baseline_playbook=baseline)
    pending = store._library.snapshot("pb-1", "retrospective:exec-1")
    assert pending["publication_status"] == "PENDING"
    assert store._library.head("pb-1") == head
    assert not store.finalize_publication("pb-1", "rca-1", publication_id="exec-1")
    vectors.query_vectors.return_value = {"vectors": [hit(pending), hit(head)]}
    assert [m.library_revision for m in store.search_similar("pool", threshold=0.8) if not m.unavailable_reason] == [
        head["revision"]
    ]
    ddb.put_item(
        TableName="sessions",
        Item=_pack(
            {
                "PK": "RCA#rca-1",
                "SK": f"{ENGINE}#PLAYBOOK_REVISION",
                "playbook_id": "pb-1",
                "playbook": json.dumps(revised),
                "publication_status": "PUBLISHED",
                "revised_by_execution_id": "exec-1",
            }
        ),
    )
    assert store.finalize_publication("pb-1", "rca-1", publication_id="exec-1")
    assert store.search_similar("pool", threshold=0.8)[0].verification_status == "VERIFIED"
    assert vectors.delete_vectors.call_args.kwargs["keys"] == [head["vector_key"]]


@pytest.mark.parametrize("fault", ["disabled", "embed", "query", "state"])
def test_search_unavailable_is_distinct_from_empty(storage, monkeypatch, fault):
    _, vectors, store = storage
    if fault == "disabled":
        monkeypatch.setattr(f"{MODULE}.S3_VECTOR_BUCKET_NAME", "")
    elif fault == "embed":
        store._embedding.embed_query.side_effect = RuntimeError("embed down")
    elif fault == "query":
        vectors.query_vectors.side_effect = RuntimeError("query down")
    else:
        vectors.query_vectors.return_value = {"vectors": [{"key": "pb-1", "distance": 0.1, "metadata": {}}]}
        store._ddb.get_item = MagicMock(side_effect=RuntimeError("state down"))
    if fault == "state":
        matches = store.search_similar("pool", threshold=0.8)
        assert len(matches) == 1 and matches[0].unavailable_reason
        assert store.load_detail(matches[0]) is None
    else:
        with pytest.raises(PlaybookSearchUnavailable):
            store.search_similar("pool", threshold=0.8)


def test_wire_fixture_and_cross_engine_helper_parity():
    root = Path(__file__).resolve().parents[3]
    paths = [
        root / "packages" / package / "src" / module / "adapters/secondary/playbook/library.py"
        for package, module in [("agent", "rca_agent"), ("headless-codex", "headless_codex")]
    ]
    assert paths[0].read_text() == paths[1].read_text()
    wire = json.loads((Path(__file__).parent / "fixtures/playbook-library-wire.json").read_text())
    assert wire["playbook_json"] == encoded(document())
    assert wire["vector_key"] == f"{wire['SK']}@{wire['revision']}"
    for path in paths:
        spec = importlib.util.spec_from_file_location("parity_library", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.encoded(document()) == wire["playbook_json"]


@pytest.mark.parametrize("prior_revision", [False, True])
def test_legacy_retrospective_baseline_race_is_atomic(storage, prior_revision):
    ddb, vectors, store = storage
    value = document()
    source(ddb, value)
    original_revision = {
        "PK": "RCA#rca-1",
        "SK": f"{ENGINE}#PLAYBOOK_REVISION",
        "playbook_id": "pb-1",
        "playbook": json.dumps(value),
        "publication_status": "PUBLISHED",
        "revised_by_execution_id": "exec-old",
    }
    if prior_revision:
        ddb.put_item(TableName="sessions", Item=_pack(original_revision))
    baseline = store._library.load("pb-1", "rca-1")
    transaction = ddb.transact_write_items

    def race(**kwargs):
        changed = dict(
            original_revision,
            playbook=json.dumps(dict(value, failure_type="changed")),
            revised_by_execution_id="exec-other",
        )
        ddb.put_item(TableName="sessions", Item=_pack(changed))
        return transaction(**kwargs)

    ddb.transact_write_items = race
    assert not store._publish(
        dict(value, verification_status="VERIFIED"), "rca-1", publication_id="exec-new", baseline_playbook=baseline
    )
    assert store._library.head("pb-1") is None
    vectors.put_vectors.assert_not_called()


def test_legacy_strands_session_key_preserves_owner_without_engine_attribute(storage):
    ddb, _, store = storage
    value = document()
    ddb.put_item(
        TableName="sessions",
        Item=_pack(
            {
                "PK": "RCA#rca-1",
                "SK": "SESSION",
                "state": "COMPLETED",
                "created_at": int(time.time()),
                "playbook_id": "pb-1",
                "playbook": json.dumps(value),
                "ttl": int(time.time()) + 86400,
            }
        ),
    )
    assert store._library.load("pb-1", "rca-1")["source_engine"] == "strands"


def test_cleanup_failure_does_not_unpublish_current_retrospective(storage):
    ddb, vectors, store = storage
    value = document()
    source(ddb, value)
    baseline = store._library.load("pb-1", "rca-1")
    revised = dict(value, verification_status="VERIFIED")
    assert store._publish(revised, "rca-1", publication_id="exec-9", baseline_playbook=baseline)
    ddb.put_item(
        TableName="sessions",
        Item=_pack(
            {
                "PK": "RCA#rca-1",
                "SK": f"{ENGINE}#PLAYBOOK_REVISION",
                "playbook_id": "pb-1",
                "playbook": json.dumps(revised),
                "publication_status": "PUBLISHED",
                "revised_by_execution_id": "exec-9",
            }
        ),
    )
    vectors.delete_vectors.side_effect = RuntimeError("cleanup unavailable")
    assert store.finalize_publication("pb-1", "rca-1", publication_id="exec-9")
    assert store._library.head("pb-1")["publication_status"] == "PUBLISHED"
    assert vectors.delete_vectors.call_args.kwargs["keys"] == ["pb-1"]


def test_retrospective_accepts_dashboard_json_order_but_compares_entire_domain(storage):
    ddb, _, store = storage
    value = document()
    source(ddb, value)
    baseline = {key: item for key, item in value.items() if key != "comparison"}
    current = {
        "PK": "PLAYBOOK_LIBRARY",
        "SK": "pb-1",
        "revision": "proposal:p-1",
        "playbook_json": json.dumps(baseline, indent=2),
        "engine": ENGINE,
        "source_rca_id": "rca-1",
        "publication_status": "PUBLISHED",
        "vector_key": "pb-1@proposal:p-1",
        "ttl": int(time.time()) + 86400,
    }
    ddb.put_item(TableName="sessions", Item=_pack(current))
    ddb.put_item(TableName="sessions", Item=_pack({**current, "PK": "PLAYBOOK#pb-1", "SK": current["revision"]}))
    baseline = {**baseline, "library_revision": "proposal:p-1", "source_engine": ENGINE, "source_rca_id": "rca-1"}
    revised = dict(baseline, verification_status="VERIFIED")
    assert store._publish(revised, "rca-1", publication_id="exec-1", baseline_playbook=baseline)


def test_canonical_reader_preserves_cross_engine_source_identity(storage):
    ddb, _, store = storage
    value = document()
    other = "headless-codex" if ENGINE == "strands" else "strands"
    source(ddb, value, engine=other)
    head = store._library.stage(value, "rca-1", "analysis:rca-1", engine=other)
    store._library.finalize(head)
    result = store._library.load("pb-1", "rca-1", other, "analysis:rca-1")
    assert result["source_engine"] == other
    assert result["library_revision"] == "analysis:rca-1"


def matched_incident(storage):
    """A newly approved incident keeps the public revision but owns a different runbook."""
    ddb, _, _ = storage
    original, store = completed(storage)
    assert publish(store, original)
    public = store._library.head("pb-1")
    target = dict(
        original,
        rca_id="rca-2",
        library_revision=public["revision"],
        source_rca_id="rca-1",
        source_engine=ENGINE,
        execution_steps=[
            {
                "step_id": "new-step",
                "commands": ["aws ecs list-clusters --region us-west-2"],
                "action": "observe new incident",
                "success_criteria": "new target healthy",
            }
        ],
        comparison={"status": "NO_CHANGE", "selected_playbook_id": "pb-1"},
    )
    source(ddb, target, rca_id="rca-2")
    revised = dict(target, verification_status="VERIFIED")
    return store, public, target, revised


def commit_new_incident_retrospective(ddb, revised):
    """Represent the existing execution store's successful original revision transaction."""
    ddb.put_item(
        TableName="sessions",
        Item=_pack(
            {
                "PK": "RCA#rca-2",
                "SK": f"{ENGINE}#PLAYBOOK_REVISION",
                "playbook_id": "pb-1",
                "playbook": json.dumps(revised),
                "publication_status": "PUBLISHED",
                "revised_by_execution_id": "exec-new",
            }
        ),
    )


def test_matched_new_incident_retrospective_uses_immutable_public_baseline(storage):
    ddb, vectors, _ = storage
    store, old, target, revised = matched_incident(storage)
    assert store._publish(revised, "rca-2", publication_id="exec-new", baseline_playbook=target)
    assert store._library.head("pb-1") == old
    staged = store._library.snapshot("pb-1", "retrospective:exec-new")
    assert staged["baseline_revision"] == old["revision"]
    assert staged["baseline_playbook_json"] == old["playbook_json"]
    assert json.loads(staged["playbook_json"])["execution_steps"] == target["execution_steps"]
    commit_new_incident_retrospective(ddb, revised)
    assert store.finalize_publication("pb-1", "rca-2", publication_id="exec-new")
    published = store._library.head("pb-1")
    assert published["source_rca_id"] == "rca-2"
    assert published["revision"] == "retrospective:exec-new"
    assert json.loads(published["playbook_json"])["rca_id"] == "rca-2"
    assert store._library.snapshot("pb-1", old["revision"])["playbook_json"] == old["playbook_json"]
    vectors.query_vectors.return_value = {"vectors": [hit(old, 0.01), hit(published, 0.1)]}
    assert [m.rca_id for m in store.search_similar("pool", threshold=0.8) if not m.unavailable_reason] == ["rca-2"]


def test_applied_proposal_invalidates_new_incident_retrospective_baseline(storage):
    ddb, vectors, _ = storage
    store, old, target, revised = matched_incident(storage)
    newer = dict(
        old,
        revision="proposal:human-change",
        vector_key="pb-1@proposal:human-change",
        playbook_json=json.dumps(dict(json.loads(old["playbook_json"]), temporary_mitigation="human change")),
    )
    ddb.put_item(TableName="sessions", Item=_pack(newer))
    assert not store._publish(revised, "rca-2", publication_id="exec-new", baseline_playbook=target)
    assert store._library.head("pb-1") == newer
    assert vectors.put_vectors.call_count == 1  # Only the original analysis publication.


@pytest.mark.parametrize("phase", ["before_vector", "after_vector_before_commit"])
def test_failed_retrospective_leaves_prior_public_head_unchanged(storage, phase):
    _, vectors, _ = storage
    store, old, target, revised = matched_incident(storage)
    if phase == "before_vector":
        store._embedding.embed_document.side_effect = RuntimeError("embedding offline")
    staged = store._publish(revised, "rca-2", publication_id="exec-new", baseline_playbook=target)
    assert staged is (phase != "before_vector")
    assert not store.finalize_publication("pb-1", "rca-2", publication_id="exec-new")
    assert store._library.head("pb-1") == old
    vectors.query_vectors.return_value = {"vectors": [hit(old)]}
    assert store.search_similar("pool", threshold=0.8)[0].library_revision == old["revision"]


def test_committed_retrospective_recovers_head_and_result_from_durable_snapshot(storage):
    ddb, vectors, _ = storage
    store, old, target, revised = matched_incident(storage)
    outcome = {
        "status": "UPDATED",
        "summary": "new incident execution observed",
        "playbook_snapshot_s3_key": "approved",
        "diff_s3_key": "retrospective-diff",
    }
    ddb.put_item(
        TableName="sessions",
        Item=_pack(
            {
                "PK": "RCA#rca-2",
                "SK": "EXEC#exec-new",
                "claim_token": "owner",
                "execution_state": "RESOLVED",
                "retrospective_status": "RUNNING",
            }
        ),
    )
    assert store._publish(
        revised, "rca-2", publication_id="exec-new", baseline_playbook=target, publication_result=outcome
    )
    commit_new_incident_retrospective(ddb, revised)
    original_transaction = ddb.transact_write_items
    ddb.transact_write_items = MagicMock(side_effect=RuntimeError("finalization unavailable"))
    assert not store.finalize_publication("pb-1", "rca-2", publication_id="exec-new")
    assert store._library.head("pb-1") == old
    ddb.transact_write_items = original_transaction
    assert store.recover_publication("rca-2", publication_id="exec-new", source_engine=ENGINE)
    assert store._library.head("pb-1")["revision"] == "retrospective:exec-new"
    execution = ddb.get_item(TableName="sessions", Key=_pack({"PK": "RCA#rca-2", "SK": "EXEC#exec-new"}))["Item"]
    assert execution["execution_state"]["S"] == "RESOLVED"
    assert execution["retrospective_status"]["S"] == "UPDATED"
    assert execution["retrospective_diff_s3_key"]["S"] == "retrospective-diff"
    assert store.recover_publication("rca-2", publication_id="exec-new", source_engine=ENGINE)
    assert vectors.put_vectors.call_count == 2  # No embedding or vector replay during finalization recovery.


def test_superseded_post_commit_publication_records_failure_without_overwriting_new_head(storage):
    ddb, _, _ = storage
    store, old, target, revised = matched_incident(storage)
    outcome = {
        "status": "UPDATED",
        "summary": "observed",
        "playbook_snapshot_s3_key": "approved",
        "diff_s3_key": "diff",
    }
    ddb.put_item(
        TableName="sessions",
        Item=_pack(
            {
                "PK": "RCA#rca-2",
                "SK": "EXEC#exec-new",
                "claim_token": "owner",
                "execution_state": "RESOLVED",
                "retrospective_status": "RUNNING",
            }
        ),
    )
    assert store._publish(
        revised, "rca-2", publication_id="exec-new", baseline_playbook=target, publication_result=outcome
    )
    commit_new_incident_retrospective(ddb, revised)
    newer = dict(old, revision="proposal:later", vector_key="pb-1@proposal:later")
    ddb.put_item(TableName="sessions", Item=_pack(newer))
    assert store.recover_publication("rca-2", publication_id="exec-new", source_engine=ENGINE)
    assert store._library.head("pb-1") == newer
    item = ddb.get_item(TableName="sessions", Key=_pack({"PK": "RCA#rca-2", "SK": "EXEC#exec-new"}))["Item"]
    assert item["retrospective_status"]["S"] == "FAILED"
    assert item["execution_state"]["S"] == "RESOLVED"


def claim_source_deletion(ddb, rca_id, marker="2026-09-15T00:00:00Z"):
    """Fence a retained completed source without changing its analysis state or payload."""
    ddb.update_item(
        TableName="sessions",
        Key=_pack({"PK": f"RCA#{rca_id}", "SK": "ANALYSIS#SESSION"}),
        UpdateExpression="SET deleting_at = :marker",
        ExpressionAttributeValues=_pack({":marker": marker}),
    )


@pytest.mark.parametrize("marker", ["2026-09-15T00:00:00Z", "", None])
def test_deletion_claim_hides_source_and_prevents_initial_stage(storage, marker):
    """Attribute presence, including empty/null markers, fences reads and initial publication."""
    ddb, vectors, _ = storage
    value, store = completed(storage)
    claim_source_deletion(ddb, "rca-1", marker)
    assert store._library.source("rca-1", "pb-1") is None
    assert store._library.load("pb-1", "rca-1") is None
    assert not publish(store, value)
    assert store._library.head("pb-1") is None
    assert store._library.snapshot("pb-1", "analysis:rca-1") is None
    store._embedding.embed_document.assert_not_called()
    vectors.put_vectors.assert_not_called()


def test_published_head_is_unreadable_after_source_deletion_claim(storage):
    """A canonical head cannot keep a deletion-fenced source searchable through stale vectors."""
    ddb, vectors, _ = storage
    value, store = completed(storage)
    assert publish(store, value)
    head = store._library.head("pb-1")
    claim_source_deletion(ddb, "rca-1")
    assert store._library.load("pb-1", "rca-1", ENGINE, head["revision"]) is None
    vectors.query_vectors.return_value = {"vectors": [hit(head)]}
    assert_unavailable(store, store.search_similar("pool", threshold=0.8))
    assert store._library.head("pb-1") == head


def test_stage_transaction_rejects_deletion_claim_after_source_read(storage):
    """Deletion winning the race must prevent the head/snapshot transaction and any vector write."""
    ddb, vectors, _ = storage
    value, store = completed(storage)
    transaction = ddb.transact_write_items

    def deletion_first(**kwargs):
        claim_source_deletion(ddb, "rca-1")
        return transaction(**kwargs)

    ddb.transact_write_items = deletion_first
    assert not publish(store, value)
    assert store._library.head("pb-1") is None
    assert store._library.snapshot("pb-1", "analysis:rca-1") is None
    vectors.put_vectors.assert_not_called()


@pytest.mark.parametrize("kind", ["analysis", "retrospective"])
@pytest.mark.parametrize("after_read", [False, True], ids=["deletion-before-read", "deletion-before-transaction"])
def test_finalization_rejects_source_deletion_without_late_canonical_publication(storage, kind, after_read):
    """Real DynamoDB conditions fence finalization even when the source still says COMPLETED."""
    from botocore.exceptions import ClientError

    ddb, vectors, _ = storage
    if kind == "analysis":
        value, store = completed(storage)
        source_rca = "rca-1"
        work = store._library.stage(value, source_rca, "analysis:rca-1", engine=ENGINE)
    else:
        store, _, target, revised = matched_incident(storage)
        source_rca = "rca-2"
        baseline = store._library.retrospective_baseline(target, source_rca)
        work = store._library.stage(revised, source_rca, "retrospective:exec-new", engine=ENGINE, baseline=baseline)
        commit_new_incident_retrospective(ddb, revised)
    previous_head = store._library.head("pb-1")
    snapshot = store._library.snapshot("pb-1", work["revision"])
    vector_writes = vectors.put_vectors.call_count
    if after_read:
        transaction = ddb.transact_write_items

        def deletion_first(**kwargs):
            claim_source_deletion(ddb, source_rca)
            return transaction(**kwargs)

        ddb.transact_write_items = deletion_first
    else:
        claim_source_deletion(ddb, source_rca)
    with pytest.raises(ClientError if after_read else ValueError) as failure:
        store._library.finalize(work)
    if after_read:
        assert failure.value.response["Error"]["Code"] == "TransactionCanceledException"
    assert store._library.head("pb-1") == previous_head
    assert store._library.snapshot("pb-1", work["revision"]) == snapshot
    assert vectors.put_vectors.call_count == vector_writes
    vectors.delete_vectors.assert_not_called()
    recorded = ddb.get_item(TableName="sessions", Key=_pack({"PK": f"RCA#{source_rca}", "SK": "ANALYSIS#SESSION"}))[
        "Item"
    ]
    assert recorded["state"]["S"] == "COMPLETED"
    assert "deleting_at" in recorded


def assert_unavailable(store, matches):
    """Relevant identities survive source failure, but no historical body becomes usable."""
    assert matches
    assert all(match.unavailable_reason for match in matches)
    assert all(store.load_detail(match) is None for match in matches)
