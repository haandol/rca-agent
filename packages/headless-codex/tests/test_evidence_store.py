import hashlib
import json

import boto3
import pytest
from moto import mock_aws

from headless_codex.adapters.secondary.evidence import s3_evidence_store
from headless_codex.adapters.secondary.evidence.s3_evidence_store import S3EvidenceStore


def test_execution_evidence_is_persisted_with_an_injected_client(monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "rca-evidence"
        s3.create_bucket(Bucket=bucket)
        monkeypatch.setattr(s3_evidence_store, "S3_EVIDENCE_BUCKET", bucket)

        key = S3EvidenceStore(s3).save_execution_evidence(
            "exec-1",
            rca_id="rca-1",
            evidence={"final_state": "RESOLVED"},
        )

        stored = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert json.loads(stored)["final_state"] == "RESOLVED"


def test_approved_playbook_is_loaded_from_exact_bytes_after_digest_verification(monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "rca-evidence"
        key = "approved/rca-1/exec-1/playbook.json"
        s3.create_bucket(Bucket=bucket)
        raw = json.dumps(
            {
                "playbook_id": "pb-1",
                "execution_steps": [{"step_id": "step-1"}],
            },
            separators=(",", ":"),
        ).encode()
        s3.put_object(Bucket=bucket, Key=key, Body=raw)
        monkeypatch.setattr(s3_evidence_store, "S3_EVIDENCE_BUCKET", bucket)

        playbook = S3EvidenceStore(s3).load_approved_playbook(
            key,
            playbook_digest=hashlib.sha256(raw).hexdigest(),
        )

        assert playbook["playbook_id"] == "pb-1"


def test_approved_playbook_digest_is_over_stored_bytes(monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "rca-evidence"
        key = "approved/rca-1/exec-1/playbook.json"
        s3.create_bucket(Bucket=bucket)
        s3.put_object(Bucket=bucket, Key=key, Body=b'{"playbook_id":"pb-current"}')
        monkeypatch.setattr(s3_evidence_store, "S3_EVIDENCE_BUCKET", bucket)

        with pytest.raises(RuntimeError, match="digest"):
            S3EvidenceStore(s3).load_approved_playbook(key, playbook_digest="0" * 64)


@pytest.mark.parametrize(
    ("bucket", "client", "message"),
    [
        ("", object(), "bucket"),
        ("rca-evidence", None, "client"),
    ],
)
def test_execution_evidence_save_fails_when_the_durable_store_is_unavailable(
    monkeypatch,
    bucket,
    client,
    message,
):
    monkeypatch.setattr(s3_evidence_store, "S3_EVIDENCE_BUCKET", bucket)

    with pytest.raises(RuntimeError, match=message):
        S3EvidenceStore(client).save_execution_evidence(
            "exec-1",
            rca_id="rca-1",
            evidence={"final_state": "RESOLVED"},
        )
