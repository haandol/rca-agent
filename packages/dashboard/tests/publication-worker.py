"""Consume actual Dashboard apply output with real worker/adapters and local Moto storage only."""

import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

import boto3
from headless_codex.adapters.secondary.execution import (
    dynamodb_execution_store as execution_module,
)
from headless_codex.adapters.secondary.playbook import (
    s3_vectors_playbook_store as publication_module,
)
from headless_codex.adapters.secondary.playbook.library import _pack, _unpack
from headless_codex.services.deferred_publication import DeferredPublication
from moto import mock_aws


def exercise(data):
    """Seed only emitted rows; the positive RECOVERY_PUBLICATION must come from the actual TS apply hook."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="sessions",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="evidence")
        execution_module.DYNAMODB_TABLE_NAME = (
            publication_module.DYNAMODB_TABLE_NAME
        ) = "sessions"
        publication_module.S3_VECTOR_BUCKET_NAME = "vectors"
        vectors, embedding = MagicMock(), MagicMock()
        embedding.embed_document.return_value = [0.1, 0.2]
        store = execution_module.DynamoDbExecutionStore(ddb, s3_client=s3)
        publisher = publication_module.S3VectorsPlaybookStore(vectors, embedding, ddb)
        worker = DeferredPublication(ddb, s3, "sessions", "evidence", store, publisher)
        for row in data["rows"]:
            ddb.put_item(TableName="sessions", Item=_pack(row))
        approved = data["privateBook"]
        approved_bytes = json.dumps(
            approved, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        eid = "dashboard-user-apply"
        execution = {
            "PK": "RCA#new",
            "SK": f"EXEC#{eid}",
            "rca_id": "new",
            "execution_id": eid,
            "approval_id": eid,
            "engine": "headless-codex",
            "execution_state": "RESOLVED",
            "retrospective_status": "FAILED",
            "retrospective_summary": "historical failure preserved",
            "source_part": "recovery",
            "source_part_revision": "c" * 64,
            "source_part_payload_sha256": "c" * 64,
            "approved_playbook_s3_key": f"approvals/new/{eid}/playbook.json",
            "playbook_digest": hashlib.sha256(approved_bytes).hexdigest(),
            "evidence_s3_key": f"executions/new/{eid}/evidence.json",
            "retrospective_diff_s3_key": f"executions/new/{eid}/retrospective-diff.json",
            "started_at": datetime.now(UTC).isoformat(),
            "ttl": int(time.time()) + 86400,
        }
        ddb.put_item(TableName="sessions", Item=_pack(execution))
        evidence = {
            "rca_id": "new",
            "execution_id": eid,
            "playbook_id": approved["playbook_id"],
            "final_state": "RESOLVED",
            "resolution_confirmed": True,
        }
        for key, body in [
            (execution["approved_playbook_s3_key"], approved_bytes),
            (execution["evidence_s3_key"], json.dumps(evidence).encode()),
            (
                execution["retrospective_diff_s3_key"],
                json.dumps({"rationale": "preserved review", "update": {}}).encode(),
            ),
        ]:
            s3.put_object(Bucket="evidence", Key=key, Body=body)
        child = worker.enroll("new", eid)
        worker.resume(child)
        read = lambda row: _unpack(
            ddb.get_item(
                TableName="sessions", Key=_pack({"PK": row["PK"], "SK": row["SK"]})
            )["Item"]
        )
        final = read(child)
        assert read(execution) == execution
        puts = vectors.put_vectors.call_count
        worker.resume(final)
        assert vectors.put_vectors.call_count == puts
        return {
            "status": final["status"],
            "reason": final.get("reason"),
            "vector_puts": puts,
            "historical_execution_unchanged": True,
        }


if __name__ == "__main__":
    cases = json.load(sys.stdin)
    print(json.dumps({name: exercise(data) for name, data in cases.items()}))
