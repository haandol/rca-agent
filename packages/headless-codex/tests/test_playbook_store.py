"""Storage regressions use retained completed sources, not index-only mock authority."""

from __future__ import annotations

import json

import pytest
from test_playbook_library import completed, document, hit, publish, source
from test_playbook_library import storage as storage

from headless_codex.adapters.secondary.playbook.library import _pack
from headless_codex.ports.interfaces.playbook_store import PlaybookMatch, PlaybookSearchUnavailable


def test_original_and_published_retrospective_are_readable(storage):
    ddb, _, store = storage
    value = document()
    source(ddb, value)
    match = PlaybookMatch(playbook_id="pb-1", rca_id="rca-1", similarity=0.9)
    assert store.load_detail(match)["verification_status"] == "DRAFT"
    revised = dict(value, verification_status="VERIFIED", temporary_mitigation="corrected")
    ddb.put_item(
        TableName="sessions",
        Item=_pack(
            {
                "PK": "RCA#rca-1",
                "SK": "headless-codex#PLAYBOOK_REVISION",
                "playbook_id": "pb-1",
                "playbook": json.dumps(revised),
                "publication_status": "PUBLISHED",
                "revised_by_execution_id": "exec-9",
            }
        ),
    )
    match = PlaybookMatch(playbook_id="pb-1", rca_id="rca-1", similarity=0.9, publication_id="exec-9")
    detail = store.load_detail(match)
    assert detail["verification_status"] == "VERIFIED"
    assert detail["temporary_mitigation"] == "corrected"
    assert detail["source_engine"] == "headless-codex"


@pytest.mark.parametrize("fault", ["pending", "malformed", "wrong_id", "wrong_publication"])
def test_latest_revision_failure_never_falls_back_to_original(storage, fault):
    ddb, _, store = storage
    value = document()
    source(ddb, value)
    record = {
        "PK": "RCA#rca-1",
        "SK": "headless-codex#PLAYBOOK_REVISION",
        "playbook_id": "pb-1",
        "playbook": json.dumps(value),
        "publication_status": "PUBLISHED",
        "revised_by_execution_id": "exec-9",
    }
    if fault == "pending":
        record["publication_status"] = "PENDING"
    elif fault == "malformed":
        record["playbook"] = "{broken"
    elif fault == "wrong_id":
        record["playbook"] = json.dumps(dict(value, playbook_id="other"))
    else:
        record["revised_by_execution_id"] = "other"
    ddb.put_item(TableName="sessions", Item=_pack(record))
    assert (
        store.load_detail(PlaybookMatch(playbook_id="pb-1", rca_id="rca-1", similarity=0.9, publication_id="exec-9"))
        is None
    )


def test_staged_retrospective_vector_does_not_fall_back_to_original(storage):
    ddb, vectors, store = storage
    source(ddb, document())
    vectors.query_vectors.return_value = {
        "vectors": [
            {
                "key": "pb-1",
                "distance": 0.01,
                "metadata": {"rca_id": "rca-1", "publication_id": "uncommitted", "verification_status": "VERIFIED"},
            }
        ]
    }
    matches = store.search_similar("pool", threshold=0.8)
    assert len(matches) == 1 and matches[0].unavailable_reason
    assert matches[0].publication_id == "uncommitted"
    assert store.load_detail(matches[0]) is None


def test_query_keeps_embedding_type_distance_and_threshold_contract(storage):
    _, vectors, _ = storage
    value, store = completed(storage)
    assert publish(store, value)
    head = store._library.head("pb-1")
    vectors.query_vectors.return_value = {"vectors": [hit(head, 0.05), hit(head, 0.5)]}
    matches = store.search_similar("query", threshold=0.8)
    assert [match.similarity for match in matches] == [0.95]
    assert vectors.query_vectors.call_args.kwargs["returnDistance"] is True
    assert vectors.query_vectors.call_args.kwargs["returnMetadata"] is True
    store._embedding.embed_query.assert_called_once_with("query")


@pytest.mark.parametrize("distance", [None, float("nan"), float("inf"), "0.1"])
def test_unreadable_distance_is_not_ranked(storage, distance):
    _, vectors, store = storage
    vectors.query_vectors.return_value = {"vectors": [{"key": "pb-1", "distance": distance}]}
    assert store.search_similar("query", threshold=0.8) == []


def test_index_metadata_contains_explicit_identity_revision_engine_and_status(storage):
    _, vectors, _ = storage
    value, store = completed(storage)
    assert publish(store, value)
    vector = vectors.put_vectors.call_args.kwargs["vectors"][0]
    assert vector["key"] == "pb-1@analysis:rca-1"
    assert vector["metadata"]["playbook_id"] == "pb-1"
    assert vector["metadata"]["library_revision"] == "analysis:rca-1"
    assert vector["metadata"]["engine"] == "headless-codex"
    assert vector["metadata"]["verification_status"] == "DRAFT"
    assert "comparison" not in vector["metadata"]


def test_retrospective_metadata_keeps_publication_id(storage):
    _, vectors, _ = storage
    value, store = completed(storage)
    assert publish(store, value)
    baseline = store._library.load("pb-1", "rca-1", "headless-codex", "analysis:rca-1")
    assert store.save_to_s3_vectors(
        dict(value, verification_status="VERIFIED"), "rca-1", publication_id="exec-9", baseline_playbook=baseline
    )
    assert vectors.put_vectors.call_args.kwargs["vectors"][0]["metadata"]["publication_id"] == "exec-9"
    assert store._library.head("pb-1")["revision"] == "analysis:rca-1"
    assert store._library.snapshot("pb-1", "retrospective:exec-9")["publication_status"] == "PENDING"


def test_unknown_recorded_status_is_unavailable_not_verified(storage):
    ddb, vectors, store = storage
    value = dict(document(), verification_status="PROBABLY_FINE")
    source(ddb, value)
    assert not publish(store, value)
    vectors.put_vectors.assert_not_called()


def test_disabled_search_is_typed_failure(storage, monkeypatch):
    _, _, store = storage
    monkeypatch.setattr(
        "headless_codex.adapters.secondary.playbook.s3_vectors_playbook_store.S3_VECTOR_BUCKET_NAME", ""
    )
    with pytest.raises(PlaybookSearchUnavailable):
        store.search_similar("query", threshold=0.8)
