"""Real table boundaries for sixty-day originals, ninety-day state and archived comparisons."""

from __future__ import annotations

import importlib
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from rca_agent.adapters.secondary.playbook.library import ORIGINAL_SECONDS, STATE_SECONDS, _pack, state_record
from rca_agent.ports.interfaces.playbook_store import PlaybookArchiveUnavailable
from tests.test_playbook_library import ENGINE, completed, document, hit, publish, source
from tests.test_playbook_library import storage as storage


def clock_for(store, monkeypatch):
    """Share one clock between source fixtures and eligibility checks without replacing the SDK clock."""
    now = [int(time.time())]
    clock = SimpleNamespace(time=lambda: now[0])
    module = importlib.import_module(type(store._library).__module__)
    fixture_module = importlib.import_module(source.__module__)
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(fixture_module, "time", clock)
    return now


def archive(store, value, rca_id="rca-1"):
    """Exercise the positional generator contract through each actual adapter."""
    if ENGINE == "strands":
        from rca_agent.ports.dto.models import Playbook

        return store.archive_comparison(Playbook.model_validate(value), rca_id, ENGINE).model_dump(mode="json")
    return store.archive_comparison(value, rca_id, ENGINE)


@pytest.mark.parametrize("age", [59, 60, 61])
def test_canonical_original_expires_at_sixty_while_body_free_state_lives_ninety(storage, monkeypatch, age):
    ddb, vectors, store = storage
    clock = clock_for(store, monkeypatch)
    initial = clock[0]
    value, _ = completed(storage)
    assert publish(store, value)
    head = store._library.head("pb-1")
    assert int(head["ttl"]) == initial + ORIGINAL_SECONDS
    state = store._library._get("PLAYBOOK_LIBRARY_STATE", "pb-1")
    assert int(state["ttl"]) == initial + STATE_SECONDS
    assert state["publication_status"] == head["publication_status"] == "PUBLISHED"
    assert set(state) == {
        "PK",
        "SK",
        "source_rca_id",
        "engine",
        "revision",
        "publication_status",
        "vector_key",
        "metric_name",
        "updated_at",
        "failure_type",
        "symptom_pattern",
        "tags",
        "verification_status",
        "ttl",
    }
    clock[0] = initial + age * 86400
    assert store._library.source("rca-1", "pb-1") is not None  # Source state still has ninety-day authority.
    detail = store._library.load("pb-1", "rca-1", ENGINE, head["revision"])
    assert (detail is not None) is (age == 59)
    assert (store._library.snapshot("pb-1", head["revision"]) is not None) is (age == 59)
    if age >= 60:
        assert "playbook_json" not in store._library.head("pb-1")
        # TTL removal of the body cannot resurrect a legacy indexed original.
        ddb.delete_item(TableName="sessions", Key=_pack({"PK": "PLAYBOOK_LIBRARY", "SK": "pb-1"}))
        assert store._library.head("pb-1")["_body_unavailable"]
        assert store._library.load("pb-1", "rca-1") is None
        assert store._library._get("PLAYBOOK_LIBRARY_STATE", "pb-1") == state
    vectors.delete_vectors.assert_not_called()


@pytest.mark.parametrize("age", [59, 60, 61])
@pytest.mark.parametrize("lineage", ["created_at", "completed_at", "ttl"])
def test_legacy_original_retention_uses_its_recorded_lineage(storage, monkeypatch, age, lineage):
    ddb, _, store = storage
    clock = clock_for(store, monkeypatch)
    initial = clock[0]
    item = source(ddb, document(), ttl=initial + STATE_SECONDS)
    if lineage != "created_at":
        item.pop("created_at")
    if lineage == "completed_at":
        item["completed_at"] = datetime.fromtimestamp(initial, UTC).isoformat()
    ddb.put_item(TableName="sessions", Item=_pack(item))
    clock[0] += age * 86400
    assert store._library.source("rca-1", "pb-1") is not None
    assert (store._library.load("pb-1", "rca-1") is not None) is (age == 59)


def test_fresh_canonical_and_legacy_retrospective_can_use_retained_sixty_one_day_source(storage, monkeypatch):
    ddb, _, store = storage
    clock = clock_for(store, monkeypatch)
    initial = clock[0]
    value = document()
    source(ddb, value, ttl=initial + STATE_SECONDS)
    clock[0] += 61 * 86400
    fresh = datetime.fromtimestamp(clock[0], UTC).isoformat()
    ddb.put_item(
        TableName="sessions",
        Item=_pack(
            {
                "PK": "RCA#rca-1",
                "SK": f"{ENGINE}#PLAYBOOK_REVISION",
                "playbook_id": "pb-1",
                "playbook": json.dumps(value),
                "updated_at": fresh,
                "publication_status": "PUBLISHED",
                "revised_by_execution_id": "recent",
                "ttl": clock[0] + STATE_SECONDS,
            }
        ),
    )
    assert store._library.load("pb-1", "rca-1", ENGINE, publication_id="recent") is not None
    head = {
        "PK": "PLAYBOOK_LIBRARY",
        "SK": "pb-1",
        "playbook_json": json.dumps(value),
        "revision": "proposal:fresh",
        "source_rca_id": "rca-1",
        "engine": ENGINE,
        "publication_status": "PUBLISHED",
        "vector_key": "pb-1@proposal:fresh",
        "metric_name": "",
        "updated_at": fresh,
        "original_created_at": fresh,
        "ttl": clock[0] + ORIGINAL_SECONDS,
    }
    for row in [head, state_record(head), {**head, "PK": "PLAYBOOK#pb-1", "SK": head["revision"]}]:
        ddb.put_item(TableName="sessions", Item=_pack(row))
    assert store._library.load("pb-1", "rca-1", ENGINE, head["revision"]) is not None
    baseline = store._library.retrospective_baseline(dict(value, library_revision=head["revision"]), "rca-1")
    staged = store._library.stage(value, "rca-1", "retrospective:new", engine=ENGINE, baseline=baseline)
    assert int(staged["ttl"]) == clock[0] + ORIGINAL_SECONDS
    assert store._library.head("pb-1") == head
    assert store._library._get("PLAYBOOK_LIBRARY_STATE", "pb-1") == state_record(head)


def full_comparison():
    """Real cited values and the initial causal chain must survive, not just file-name references."""
    before = {"playbook_id": "old", "execution_steps": [{"step_id": "old", "commands": ["old-command"]}]}
    return {
        "status": "UPDATE_PROPOSED",
        "query": "pool pressure",
        "selected_playbook_id": "old",
        "candidates": [{"playbook_id": "old", "similarity": 0.9, "rationale": "actual match"}],
        "baseline": before,
        "actual_evidence": [
            {
                "ref": "scoping.json#/metric_observations/0",
                "value": {"connection_count": 42, "source": "actual-observation-sentinel"},
            }
        ],
        "initial_five_whys": ["original-causal-chain-sentinel"],
        "proposal": {
            "proposal_id": "proposal-1",
            "playbook_id": "old",
            "state": "PENDING",
            "before": before,
            "after": {**before, "tags": ["pool"]},
            "changes": [{"field": "tags", "before": [], "after": ["pool"]}],
            "evidence": ["scoping.json#/metric_observations/0"],
            "rationale": "observed pool pressure",
        },
    }


@pytest.mark.parametrize("age", [59, 60, 61])
@pytest.mark.parametrize("status", ["UPDATE_PROPOSED", "NO_CHANGE"])
def test_full_comparison_is_immutable_sixty_day_original_with_only_thin_state(storage, monkeypatch, age, status):
    ddb, _, store = storage
    clock = clock_for(store, monkeypatch)
    initial = clock[0]
    full = dict(document(), comparison=full_comparison())
    full["comparison"]["status"] = status
    if status == "NO_CHANGE":
        full["comparison"].pop("proposal")
    source(ddb, full, state="REPORT_GENERATION", ttl=initial + STATE_SECONDS)
    ddb.update_item(
        TableName="sessions",
        Key=_pack({"PK": "RCA#rca-1", "SK": "ANALYSIS#SESSION"}),
        UpdateExpression="REMOVE playbook",
    )
    thin = archive(store, full)
    ref = thin["comparison"]
    assert set(ref) == {"status", "selected_playbook_id", "original_sk", "original_expires_at"} | (
        {"proposal"} if status == "UPDATE_PROPOSED" else set()
    )
    if status == "UPDATE_PROPOSED":
        assert ref["proposal"] == {"proposal_id": "proposal-1", "state": "PENDING"}
    original = store._library._get("RCA#rca-1", ref["original_sk"])
    assert json.loads(original["comparison_json"]) == full["comparison"]
    assert int(original["ttl"]) == ref["original_expires_at"] == initial + ORIGINAL_SECONDS
    assert store._library.source("rca-1", "pb-1") is None  # Archived, but analysis is not public yet.
    source(ddb, thin, ttl=initial + STATE_SECONDS)
    assert publish(store, thin)
    records = [
        store._library._get("RCA#rca-1", "ANALYSIS#SESSION"),
        store._library._get("PLAYBOOK_LIBRARY_STATE", "pb-1"),
    ]
    for row in records:
        assert "comparison_json" not in row
        assert "actual-observation-sentinel" not in str(row)
        assert "original-causal-chain-sentinel" not in str(row)
    clock[0] += age * 86400
    if age == 59:
        assert archive(store, full)["comparison"] == ref
        assert store._library._get("RCA#rca-1", ref["original_sk"]) == original
    else:
        with pytest.raises(PlaybookArchiveUnavailable):
            archive(store, full)
        with pytest.raises(PlaybookArchiveUnavailable):
            archive(store, thin)
    assert full["comparison"]["actual_evidence"][0]["value"]["connection_count"] == 42


def test_archive_failure_is_typed_and_does_not_mutate_incident_plan(storage):
    ddb, _, store = storage
    full = dict(document(), comparison=full_comparison())
    source(ddb, full, state="REPORT_GENERATION")
    ddb.transact_write_items = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("write unavailable"))
    with pytest.raises(PlaybookArchiveUnavailable):
        archive(store, full)
    assert full["comparison"] == full_comparison()
    assert store._library.head("pb-1") is None


def test_unavailable_relevant_hit_is_preserved_beside_valid_hit_without_body_exposure(storage):
    _, vectors, store = storage
    value, _ = completed(storage)
    assert publish(store, value)
    valid = hit(store._library.head("pb-1"), 0.1)
    unavailable = {
        "key": "lost@analysis:old",
        "distance": 0.01,
        "metadata": {
            "playbook_id": "lost",
            "library_revision": "analysis:old",
            "rca_id": "old",
            "engine": ENGINE,
            "verification_status": "VERIFIED",
            "failure_type": "untrusted-body",
        },
    }
    vectors.query_vectors.return_value = {"vectors": [unavailable, valid]}
    matches = store.search_similar("pool", threshold=0.8)
    assert [match.playbook_id for match in matches] == ["lost", "pb-1"]
    assert matches[0].unavailable_reason and matches[0].verification_status == "DRAFT"
    assert matches[0].failure_type == "" and matches[0].library_revision == "analysis:old"
    assert store.load_detail(matches[0]) is None
    assert store.load_detail(matches[1]) is not None
    vectors.query_vectors.return_value = {"vectors": []}
    assert store.search_similar("pool", threshold=0.8) == []


def first_version_head(ddb, value, initial):
    """Represent a locally published first-version head/snapshot with update time and ninety-day TTL."""
    head = {
        "PK": "PLAYBOOK_LIBRARY",
        "SK": value["playbook_id"],
        "playbook_json": json.dumps(value),
        "revision": "analysis:rca-1",
        "source_rca_id": "rca-1",
        "engine": ENGINE,
        "publication_status": "PUBLISHED",
        "vector_key": "pb-1@analysis:rca-1",
        "metric_name": "",
        "updated_at": datetime.fromtimestamp(initial, UTC).isoformat(),
        "ttl": initial + STATE_SECONDS,
    }
    ddb.put_item(TableName="sessions", Item=_pack(head))
    ddb.put_item(TableName="sessions", Item=_pack({**head, "PK": "PLAYBOOK#pb-1", "SK": head["revision"]}))
    return head


@pytest.mark.parametrize("age", [59, 60, 61])
def test_first_version_updated_at_controls_sixty_day_read_without_migration(storage, monkeypatch, age):
    """Existing local data is readable until sixty days; reads never rewrite its old ninety-day rows."""
    ddb, _, store = storage
    clock = clock_for(store, monkeypatch)
    initial = clock[0]
    value = document()
    source(ddb, value, ttl=initial + STATE_SECONDS)
    original = first_version_head(ddb, value, initial)
    snapshot = store._library._get("PLAYBOOK#pb-1", original["revision"])
    clock[0] += age * 86400
    assert (store._library.load("pb-1", "rca-1", ENGINE, original["revision"]) is not None) is (age == 59)
    assert (store._library.snapshot("pb-1", original["revision"]) is not None) is (age == 59)
    assert store._library._get("PLAYBOOK_LIBRARY", "pb-1") == original
    assert store._library._get("PLAYBOOK#pb-1", original["revision"]) == snapshot
    assert store._library._get("PLAYBOOK_LIBRARY_STATE", "pb-1") is None


def test_recorded_birth_takes_precedence_over_a_recent_updated_at(storage, monkeypatch):
    """A recent state update cannot reset the sixty-day lifetime of an explicitly dated original."""
    ddb, _, store = storage
    clock = clock_for(store, monkeypatch)
    initial = clock[0]
    value = document()
    source(ddb, value, ttl=initial + STATE_SECONDS)
    head = first_version_head(ddb, value, initial)
    clock[0] += 61 * 86400
    head.update(
        original_created_at=datetime.fromtimestamp(initial, UTC).isoformat(),
        updated_at=datetime.fromtimestamp(clock[0], UTC).isoformat(),
    )
    ddb.put_item(TableName="sessions", Item=_pack(head))
    assert store._library.load("pb-1", "rca-1", ENGINE, head["revision"]) is None


def test_normal_revision_write_adopts_split_retention_after_reading_first_version_head(storage, monkeypatch):
    """A real publication creates the new sixty/ninety-day shape; compatibility reads do not migrate data."""
    ddb, _, store = storage
    clock = clock_for(store, monkeypatch)
    initial = clock[0]
    value = document()
    source(ddb, value, ttl=initial + STATE_SECONDS)
    old = first_version_head(ddb, value, initial)
    clock[0] += 59 * 86400
    baseline = store._library.retrospective_baseline(dict(value, library_revision=old["revision"]), "rca-1")
    revised = dict(value, temporary_mitigation="new observed knowledge")
    work = store._library.stage(revised, "rca-1", "retrospective:compatible", engine=ENGINE, baseline=baseline)
    assert store._library.head("pb-1") == old
    assert store._library._get("PLAYBOOK_LIBRARY_STATE", "pb-1") is None
    ddb.put_item(
        TableName="sessions",
        Item=_pack(
            {
                "PK": "RCA#rca-1",
                "SK": f"{ENGINE}#PLAYBOOK_REVISION",
                "playbook_id": "pb-1",
                "playbook": json.dumps(revised),
                "publication_status": "PUBLISHED",
                "revised_by_execution_id": "compatible",
            }
        ),
    )
    store._library.finalize(work)
    head = store._library.head("pb-1")
    state = store._library._get("PLAYBOOK_LIBRARY_STATE", "pb-1")
    assert head["original_created_at"] == datetime.fromtimestamp(clock[0], UTC).isoformat()
    assert int(head["ttl"]) == clock[0] + ORIGINAL_SECONDS
    assert int(state["ttl"]) == clock[0] + STATE_SECONDS
    assert "playbook_json" not in state
    assert head["publication_status"] == state["publication_status"] == "PUBLISHED"
    assert store._library._get("PLAYBOOK#pb-1", old["revision"])["ttl"] == old["ttl"]


def test_first_version_pending_finalize_adopts_sixty_day_body_without_extending_origin(storage, monkeypatch):
    """Normal publication updates retention metadata atomically while preserving old snapshot content."""
    ddb, _, store = storage
    clock = clock_for(store, monkeypatch)
    initial = clock[0]
    value = document()
    source(ddb, value, ttl=initial + STATE_SECONDS)
    old = first_version_head(ddb, value, initial)
    pending = {**old, "publication_status": "PENDING"}
    ddb.put_item(TableName="sessions", Item=_pack(pending))
    ddb.put_item(TableName="sessions", Item=_pack({**pending, "PK": "PLAYBOOK#pb-1", "SK": pending["revision"]}))
    clock[0] += 59 * 86400
    store._library.finalize(pending)
    head = store._library.head("pb-1")
    snapshot = store._library._get("PLAYBOOK#pb-1", pending["revision"])
    state = store._library._get("PLAYBOOK_LIBRARY_STATE", "pb-1")
    assert head["ttl"] == snapshot["ttl"] == initial + ORIGINAL_SECONDS
    assert head["original_created_at"] == snapshot["original_created_at"] == old["updated_at"]
    assert snapshot["playbook_json"] == head["playbook_json"] == old["playbook_json"]
    assert state["ttl"] == initial + STATE_SECONDS
    assert state["publication_status"] == head["publication_status"] == "PUBLISHED"


def test_source_fixture_shares_frozen_instant_after_wall_clock_crosses_a_second(storage, monkeypatch):
    """A moving wall clock cannot create future fixtures; genuinely future originals remain rejected."""
    ddb, _, store = storage
    wall = [int(time.time())]
    test_module = importlib.import_module(__name__)
    monkeypatch.setattr(test_module, "time", SimpleNamespace(time=lambda: wall[0]))
    clock = clock_for(store, monkeypatch)
    wall[0] += 1
    recorded = source(ddb, document())
    assert recorded["created_at"] == clock[0]
    assert recorded["ttl"] == clock[0] + STATE_SECONDS
    assert store._library.load("pb-1", "rca-1") is not None
    recorded["created_at"] = clock[0] + 1
    ddb.put_item(TableName="sessions", Item=_pack(recorded))
    library_module = importlib.import_module(type(store._library).__module__)
    with pytest.raises(ValueError, match="future"):
        library_module.legacy_origin(recorded)
    assert store._library.load("pb-1", "rca-1") is None
