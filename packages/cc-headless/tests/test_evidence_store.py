import hashlib
import json

import boto3
import pytest
from moto import mock_aws

from cc_headless.adapters.secondary.evidence import s3_evidence_store
from cc_headless.adapters.secondary.evidence.s3_evidence_store import S3EvidenceStore


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
