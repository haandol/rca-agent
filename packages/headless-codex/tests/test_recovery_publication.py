"""Headless completion publishes a server-attached recovery and immutable binding before index completion."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from test_playbook_library import document, source

from headless_codex.adapters.secondary.playbook.library import _pack
from headless_codex.ports.interfaces.session_store import CompletionHandoff
from headless_codex.services.analysis_parts import AnalysisPartStore, approval_digest
from headless_codex.services.pipeline import PipelineOrchestrator
from headless_codex.services.recovery_publication import preserve_recovery_procedure

pytest_plugins = ["test_playbook_library"]


@pytest.mark.parametrize("binding_fails", [False, True])
def test_real_completion_publisher_requires_binding_before_marking_indexed(storage, binding_fails, monkeypatch):
    """A failed binding keeps the same handoff retryable; successful publication references exact recovery."""
    ddb, _, publisher = storage
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="recovery-parts")
    public = {**document(), "execution_steps": [], "rollback_context": None}
    source(ddb, public, state="SCOPING")
    ddb.update_item(
        TableName="sessions",
        Key=_pack({"PK": "RCA#rca-1", "SK": "ANALYSIS#SESSION"}),
        UpdateExpression="SET claim_token = :claim",
        ExpressionAttributeValues=_pack({":claim": "claim"}),
    )
    parts = AnalysisPartStore(ddb, s3, table_name="sessions", bucket="recovery-parts", engine="headless-codex")
    parts.freeze_incident("rca-1", "claim", 1, {"alarm": {}, "scoping": {}, "observations": {}, "source_artifacts": []})
    parts.start_part("rca-1", "recovery", "claim", 1)
    fixture = Path(__file__).resolve().parents[2] / "agent/tests/fixtures/analysis-part-approval-playbook.json"
    private = json.loads(fixture.read_text())
    private["rca_id"] = "rca-1"
    part = parts.publish_part(
        "rca-1",
        "recovery",
        "claim",
        1,
        result={"recommendation": "ROLLBACK", "playbook": private, "verification": {"valid": True}},
        approval_status="READY",
        runbook_digest=approval_digest(private),
    )
    attached = preserve_recovery_procedure(public, part, "rca-1")
    assert public["execution_steps"] == []
    assert attached["execution_steps"] == private["execution_steps"]
    source(ddb, attached)
    ddb.update_item(
        TableName="sessions",
        Key=_pack({"PK": "RCA#rca-1", "SK": "ANALYSIS#SESSION"}),
        UpdateExpression="SET claim_token = :claim, playbook_index_status = :pending",
        ExpressionAttributeValues=_pack({":claim": "claim", ":pending": "PENDING"}),
    )
    if binding_fails:
        monkeypatch.setattr(publisher, "bind_recovery_publication", Mock(return_value=False))
    session = Mock()
    container = SimpleNamespace(analysis_part_store=parts, playbook_store=publisher, session_store=session)
    handoff = CompletionHandoff(
        rca_id="rca-1",
        state="COMPLETED",
        workflow="recovery-first-v1",
        playbook_index_status="PENDING",
        playbook=attached,
    )
    assert (
        PipelineOrchestrator(container)._flush_completion_handoff(
            "rca-1", claim_token="claim", log=Mock(), handoff=handoff
        )
        is not binding_fails
    )
    if binding_fails:
        session.mark_playbook_indexed.assert_not_called()
        return
    session.mark_playbook_indexed.assert_called_once_with("rca-1", claim_token="claim")
    binding = parts._get("rca-1", f"RECOVERY_PUBLICATION#{part['record']['revision']}")
    assert binding["public_revision"] == "analysis:rca-1"
    assert binding["engine"] == binding["source_engine"] == "headless-codex"
    assert int(binding["ttl"]) > time.time()
    assert parts.read_part("rca-1", "recovery") == part
