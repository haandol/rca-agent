"""Exercise actual container wiring and publication transactions, mocking only external model/vector transport."""

import io
import json
from contextlib import suppress
from unittest.mock import Mock

import test_playbook_library as fixtures

from headless_codex.adapters.secondary.execution import dynamodb_execution_store
from headless_codex.adapters.secondary.playbook.s3_vectors_playbook_store import S3VectorsPlaybookStore
from headless_codex.di.execution_container import AppExecutionContainer


def test_execution_container_can_publish_and_finalize_retrospective_with_actual_store(monkeypatch):
    """Missing DynamoDB injection must fail this full container-to-store publication path."""
    fixture = fixtures.storage.__wrapped__(monkeypatch)
    ddb, vectors, _ = next(fixture)
    try:
        monkeypatch.setattr(dynamodb_execution_store, "DYNAMODB_TABLE_NAME", "sessions")
        model = Mock()
        model.invoke_model.side_effect = lambda **kwargs: {
            "body": io.BytesIO(json.dumps({"embeddings": {"float": [[0.1, 0.2]]}}).encode())
        }
        container = AppExecutionContainer()
        container._dynamodb_client = ddb
        container._s3_vectors_client = vectors
        container._bedrock_client = model
        store = container.playbook_store
        assert isinstance(store, S3VectorsPlaybookStore)
        assert store._ddb is ddb and store._enabled
        assert container.playbook_store is store
        original = fixtures.document()
        fixtures.source(ddb, original)
        assert store.save_to_s3_vectors(original, "rca-1", source_engine=fixtures.ENGINE)
        baseline = store._library.retrospective_baseline(original, "rca-1")
        assert baseline["library_revision"] == "analysis:rca-1"
        revised = {**original, "verification_status": "VERIFIED"}
        ddb.put_item(
            TableName="sessions",
            Item=fixtures._pack(
                {
                    "PK": "RCA#rca-1",
                    "SK": "EXEC#exec-1",
                    "execution_state": "RESOLVED",
                    "retrospective_status": "RUNNING",
                    "claim_token": "offline-fixture-owner",
                }
            ),
        )
        container.execution_store.save_playbook_revision("rca-1", fixtures.ENGINE, revised, execution_id="exec-1")
        assert store.save_to_s3_vectors(
            revised,
            "rca-1",
            publication_id="exec-1",
            baseline_playbook=original,
            source_engine=fixtures.ENGINE,
            publication_result={
                "status": "NO_CHANGE",
                "summary": "Existing procedure verified",
                "playbook_snapshot_s3_key": "approvals/rca-1/exec-1/playbook.json",
                "diff_s3_key": "executions/rca-1/exec-1/retrospective-diff.json",
            },
        )
        assert store._library.head("pb-1")["revision"] == "analysis:rca-1"
        assert not store.finalize_publication("pb-1", "rca-1", publication_id="exec-1")
        container.execution_store.publish_playbook_revision("rca-1", fixtures.ENGINE, revised, execution_id="exec-1")
        assert store.finalize_publication("pb-1", "rca-1", publication_id="exec-1")
        head = store._library.head("pb-1")
        assert head["revision"] == "retrospective:exec-1" and head["publication_status"] == "PUBLISHED"
        execution = ddb.get_item(
            TableName="sessions",
            Key=fixtures._pack({"PK": "RCA#rca-1", "SK": "EXEC#exec-1"}),
        )["Item"]
        assert execution["execution_state"]["S"] == "RESOLVED"
        assert execution["retrospective_status"]["S"] == "NO_CHANGE"
        assert vectors.put_vectors.call_count == 2
        assert model.invoke_model.call_count == 2
    finally:
        with suppress(StopIteration):
            next(fixture)
