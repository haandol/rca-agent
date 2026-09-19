"""A real local publisher binds server-attached recovery independently of model knowledge."""

import hashlib
import json
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from rca_agent.adapters.secondary.playbook.library import _pack
from rca_agent.adapters.secondary.playbook.library import payload as domain_payload
from rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store import S3VectorsPlaybookStore
from rca_agent.services import playbook_gen
from rca_agent.services.analysis_parts import approval_digest
from rca_agent.services.recovery_context import _digest
from rca_agent.services.recovery_publication import bind_recovery_publication, preserve_recovery_procedure
from tests.test_analysis_parts import freeze
from tests.test_playbook_gen import _make_report
from tests.test_playbook_library import MODULE

pytest_plugins = ["tests.test_analysis_parts"]


@pytest.fixture
def publication(part_store, monkeypatch, request):
    """Publish actual validated recovery bytes before staging a distinct knowledge document."""
    part_store.clock = time.time
    part_store.engine = getattr(request, "param", "strands")
    part_store.ddb.update_item(
        TableName="parts",
        Key=_pack({"PK": "RCA#rca-1", "SK": "ANALYSIS#SESSION"}),
        UpdateExpression="SET engine = :engine",
        ExpressionAttributeValues=_pack({":engine": part_store.engine}),
    )
    freeze(part_store)
    private = json.loads((Path(__file__).parent / "fixtures/analysis-part-approval-playbook.json").read_text())
    private["rca_id"] = "rca-1"
    part_store.start_part("rca-1", "recovery", "claim", 1)
    part = part_store.publish_part(
        "rca-1",
        "recovery",
        "claim",
        1,
        result={
            "recommendation": "ROLLBACK",
            "playbook": private,
            "verification": {"valid": True, "rollback_context": deepcopy(private["rollback_context"])},
        },
        approval_status="READY",
        runbook_digest=approval_digest(private),
    )

    def model(agent, prompt, output_model, timeout, **kwargs):
        """Only the provider call is replaced: it returns knowledge without an executable procedure."""
        return output_model.model_validate(
            {
                "failure_type": "observed physical column mismatch",
                "symptom_pattern": "failed writes",
                "execution_steps": [],
            }
        )

    monkeypatch.setattr(playbook_gen, "invoke_agent", model)
    public = playbook_gen._generate_draft(_make_report(), object(), time.monotonic() + 900)
    assert public.execution_steps == [] and public.rollback_context is None
    public = preserve_recovery_procedure(public, part, "rca-1")
    monkeypatch.setattr(f"{MODULE}.DYNAMODB_TABLE_NAME", "parts")
    monkeypatch.setattr(f"{MODULE}.S3_VECTOR_BUCKET_NAME", "vectors")
    monkeypatch.setattr(f"{MODULE}.ENGINE", part_store.engine)
    vectors, embedding = Mock(), Mock()
    embedding.embed_document.return_value = [0.1, 0.2]
    publisher = S3VectorsPlaybookStore(vectors, embedding, part_store.ddb)
    part_store.ddb.put_item(
        TableName="parts",
        Item=_pack(
            {
                "PK": "RCA#rca-1",
                "SK": "ANALYSIS#SESSION",
                "engine": part_store.engine,
                "state": "COMPLETED",
                "playbook_id": public.playbook_id,
                "playbook": json.dumps(public.model_dump(mode="json")),
                "playbook_index_status": "PENDING",
                "created_at": int(time.time()),
                "ttl": int(time.time()) + 90 * 86400,
            }
        ),
    )
    part_store.ddb.update_item(
        TableName="parts",
        Key=_pack({"PK": "RCA#rca-1", "SK": "ANALYSIS#SESSION"}),
        UpdateExpression="SET claim_token = :claim",
        ExpressionAttributeValues=_pack({":claim": "claim"}),
    )
    return part_store, publisher, public, part


def test_real_publisher_binds_server_attached_runbook_without_rewriting_knowledge(publication):
    """The server attaches exact recovery to knowledge before actual publication and binding."""
    store, publisher, public, part = publication
    before = deepcopy((public.model_dump(mode="json"), part))
    assert publisher.save(public)
    assert bind_recovery_publication(publisher._library, public, part, "claim")
    row = store._get("rca-1", f"RECOVERY_PUBLICATION#{part['record']['revision']}")
    assert not any(key.startswith("bound_runbook") for key in row)
    assert row["recovery_playbook_sha256"] == _digest(part["payload"]["result"]["playbook"])
    snapshot = publisher._library.snapshot(public.playbook_id, row["public_revision"])
    assert (
        json.loads(snapshot["playbook_json"])["execution_steps"]
        == part["payload"]["result"]["playbook"]["execution_steps"]
    )
    assert (
        json.loads(snapshot["playbook_json"])["rollback_context"]
        == part["payload"]["result"]["playbook"]["rollback_context"]
    )
    assert row["public_body_sha256"] == _digest(domain_payload(public.model_dump(mode="json")))
    assert bind_recovery_publication(publisher._library, public, part, "claim")
    assert store._get("rca-1", row["SK"]) == row
    assert store.read_part("rca-1", "recovery") == part
    assert (public.model_dump(mode="json"), part) == before


@pytest.mark.parametrize(
    "fault", [None, "wrong_rca", "changed_bytes", "not_ready", "truthy_valid", "different_context"]
)
def test_model_recovery_summary_requires_retained_verified_authority(publication, fault):
    """Frozen null context cannot hide READY, while tampered or unverified role data cannot assert READY."""
    from rca_agent.services.public_playbook_context import recovery_assessment

    _, _, _, part = publication
    report = _make_report()
    report.analysis_parts = {"recovery": deepcopy(part)}
    supplied = report.analysis_parts["recovery"]
    if fault == "wrong_rca":
        report.rca_id = "other"
    elif fault == "changed_bytes":
        supplied["payload"]["result"]["playbook"]["rollback_context"]["current"]["deployment_id"] = "other"
    elif fault == "not_ready":
        supplied["record"]["approval_status"] = "UNAVAILABLE"
    elif fault in {"truthy_valid", "different_context"}:
        verification = supplied["payload"]["result"]["verification"]
        if fault == "truthy_valid":
            verification["valid"] = "true"
        else:
            verification["rollback_context"] = {"unverified": "other"}
        digest = _digest(supplied["payload"])
        supplied["record"]["payload_sha256"] = digest
        supplied["record"]["payload_s3_key"] = f"analysis-parts/strands/rca-1/recovery/{digest}.json"
    before = deepcopy(supplied)
    summary = recovery_assessment(report)
    assert summary["status"] == ("VERIFIED_READY" if fault is None else "NOT_VERIFIED")
    if fault is None:
        assert summary["rollback_context"] == supplied["payload"]["result"]["playbook"]["rollback_context"]
        assert "not that it was approved, executed or resolved" in summary["meaning"]
    else:
        assert "rollback_context" not in summary
    assert supplied == before


@pytest.mark.parametrize("fault", ["pending", "stale_claim", "changed_public", "changed_part", "revoked_part"])
def test_binding_rejects_unpublished_or_changed_authority(publication, fault):
    """Independent hashes and owner/READY checks cannot be bypassed by an equal RCA identifier."""
    store, publisher, public, part = publication
    if fault == "pending":
        publisher._library.stage(public.model_dump(mode="json"), "rca-1", "analysis:rca-1", engine="strands")
    else:
        assert publisher.save(public)
    if fault == "changed_public":
        public.symptom_pattern = "different public body"
    if fault == "changed_part":
        part["payload"]["result"]["playbook"]["execution_steps"][0]["success_criteria"] = "changed"
    if fault == "revoked_part":
        store.ddb.update_item(
            TableName="parts",
            Key=_pack({"PK": "RCA#rca-1", "SK": "strands#ANALYSIS_PART#recovery"}),
            UpdateExpression="SET approval_status = :state",
            ExpressionAttributeValues=_pack({":state": "UNAVAILABLE"}),
        )
    with pytest.raises((ValueError, ClientError)):
        bind_recovery_publication(
            publisher._library, public, part, "other-claim" if fault == "stale_claim" else "claim"
        )
    assert store._get("rca-1", f"RECOVERY_PUBLICATION#{part['record']['revision']}") is None


@pytest.mark.parametrize("order", ["execution_first", "analysis_first"])
@pytest.mark.parametrize("publication", ["strands", "headless-codex"], indirect=True)
def test_actual_agent_publisher_binding_resumes_headless_worker(publication, monkeypatch, order):
    """Real producer/consumer adapters meet on their shared wire; only vectors and model are doubles."""
    store, publisher, public, part = publication
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "headless-codex/src"))
    from headless_codex.adapters.secondary.execution.dynamodb_execution_store import DynamoDbExecutionStore
    from headless_codex.adapters.secondary.playbook.s3_vectors_playbook_store import (
        S3VectorsPlaybookStore as HeadlessStore,
    )
    from headless_codex.services.deferred_publication import DeferredPublication

    monkeypatch.setattr(
        "headless_codex.adapters.secondary.execution.dynamodb_execution_store.DYNAMODB_TABLE_NAME", "parts"
    )
    monkeypatch.setattr(
        "headless_codex.adapters.secondary.playbook.s3_vectors_playbook_store.DYNAMODB_TABLE_NAME", "parts"
    )
    monkeypatch.setattr(
        "headless_codex.adapters.secondary.playbook.s3_vectors_playbook_store.S3_VECTOR_BUCKET_NAME", "vectors"
    )
    execution_store = DynamoDbExecutionStore(store.ddb, s3_client=store.s3)
    headless_publisher = HeadlessStore(publisher._s3v, publisher._embedding, store.ddb)
    worker = DeferredPublication(store.ddb, store.s3, "parts", store.bucket, execution_store, headless_publisher)
    approved = part["payload"]["result"]["playbook"]
    raw = json.dumps(approved).encode()
    execution = {
        "PK": "RCA#rca-1",
        "SK": "EXEC#exec-1",
        "rca_id": "rca-1",
        "execution_id": "exec-1",
        "approval_id": "approval-1",
        "engine": store.engine,
        "execution_state": "RESOLVED",
        "retrospective_status": "FAILED",
        "source_part": "recovery",
        "source_part_revision": part["record"]["revision"],
        "source_part_payload_sha256": part["record"]["payload_sha256"],
        "approved_playbook_s3_key": "approvals/rca-1/approval-1/playbook.json",
        "playbook_digest": hashlib.sha256(raw).hexdigest(),
        "evidence_s3_key": "executions/rca-1/exec-1/evidence.json",
        "retrospective_diff_s3_key": "executions/rca-1/exec-1/retrospective-diff.json",
        "ttl": int(time.time()) + 86400,
        "started_at": datetime.now(UTC).isoformat(),
    }
    evidence = {
        "rca_id": "rca-1",
        "execution_id": "exec-1",
        "playbook_id": approved["playbook_id"],
        "final_state": "RESOLVED",
        "resolution_confirmed": True,
    }
    for key, body in (
        (execution["approved_playbook_s3_key"], raw),
        (execution["evidence_s3_key"], json.dumps(evidence).encode()),
        (execution["retrospective_diff_s3_key"], json.dumps({"rationale": "retained review", "update": {}}).encode()),
    ):
        store.s3.put_object(Bucket=store.bucket, Key=key, Body=body)
    store.ddb.put_item(TableName="parts", Item=_pack(execution))

    def publish():
        """Use each production completion handoff and publisher, never a manual binding PutItem."""
        session_store = Mock()
        container = SimpleNamespace(
            analysis_part_store=store,
            session_store=session_store,
            playbook_store=publisher,
        )
        if store.engine == "strands":
            from rca_agent.ports.dto.models import CompletionHandoff, RcaSessionState
            from rca_agent.services.pipeline import PipelineOrchestrator

            handoff = CompletionHandoff(
                rca_id="rca-1",
                state=RcaSessionState.COMPLETED,
                playbook=public,
                playbook_index_status="PENDING",
                workflow="recovery-first-v1",
            )
            assert PipelineOrchestrator(container)._flush_completion_handoff(
                "rca-1", claim_token="claim", handoff=handoff
            )
        else:
            from headless_codex.services.recovery_publication import preserve_recovery_procedure as attach

            knowledge = {**public.model_dump(mode="json"), "execution_steps": [], "rollback_context": None}
            native_public = attach(knowledge, part, "rca-1")
            assert native_public == public.model_dump(mode="json")
            assert headless_publisher.save_to_s3_vectors(native_public, "rca-1")
            assert headless_publisher.bind_recovery_publication(native_public, part, claim_token="claim")
        if store.engine == "strands":
            session_store.mark_playbook_indexed.assert_called_once()

    if order == "analysis_first":
        publish()
    child = worker.enroll("rca-1", "exec-1")
    if order == "execution_first":
        worker.resume(child)
        assert store._get("rca-1", child["SK"])["status"] == "WAITING_FOR_PUBLICATION"
        publish()
    worker.resume(child)
    result = store._get("rca-1", child["SK"])
    assert result["status"] == "PUBLISHED", result
    assert store._get("rca-1", "EXEC#exec-1") == execution
    assert store.read_part("rca-1", "recovery") == part
    head = headless_publisher._library.head(public.playbook_id)
    assert head["revision"] == "retrospective:exec-1"
    assert json.loads(head["playbook_json"])["execution_steps"] == approved["execution_steps"]
    assert json.loads(head["playbook_json"])["verification_status"] == "VERIFIED"
    worker.resume(child)
    assert store._get("rca-1", child["SK"]) == result
