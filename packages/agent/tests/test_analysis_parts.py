"""Exercise real local storage transactions without AWS or model calls."""

from copy import deepcopy
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from rca_agent.services.analysis_parts import AnalysisPartStore


@pytest.fixture
def part_store():
    """Create isolated emulated stores with an active parent claim for every test."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        s3 = boto3.client("s3", region_name="us-east-1")
        ddb.create_table(
            TableName="parts",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3.create_bucket(Bucket="part-evidence")
        ddb.put_item(
            TableName="parts",
            Item={
                "PK": {"S": "RCA#rca-1"},
                "SK": {"S": "ANALYSIS#SESSION"},
                "state": {"S": "SCOPING"},
                "engine": {"S": "strands"},
                "claim_token": {"S": "claim"},
                "ttl": {"N": "9999999999"},
            },
        )
        yield AnalysisPartStore(
            ddb, s3, table_name="parts", bucket="part-evidence", engine="strands", clock=lambda: 1000
        )


def incident():
    """Represent only frozen source inputs, without model conclusions or approval results."""
    return {"alarm": {"name": "alarm"}, "scoping": {}, "observations": {"current": "fault"}, "source_artifacts": []}


def freeze(store):
    """Freeze the input required before any logical part can start."""
    return store.freeze_incident("rca-1", "claim", 1, incident())


def publish(store, part="recovery", **kwargs):
    """Publish a completed unavailable result without implying a verified rollback."""
    store.start_part("rca-1", part, "claim", 1)
    return store.publish_part("rca-1", part, "claim", 1, result={"summary": part}, **kwargs)


def test_ordered_publication_preserves_parent_and_immutable_bodies(part_store):
    """All three results can publish while global analysis remains active and approval unavailable."""
    frozen = freeze(part_store)
    for part in ("recovery", "root_cause", "operations"):
        result = publish(part_store, part)
        assert part_store.read_part("rca-1", part) == result
        assert result["payload"]["incident_ref"]["sha256"] == frozen["record"]["payload_sha256"]
    parent = part_store._get("rca-1", "ANALYSIS#SESSION")
    assert parent["state"] == "SCOPING" and parent["workflow"] == "recovery-first-v1"
    assert part_store.read_part("rca-1", "recovery")["record"]["approval_status"] == "UNAVAILABLE"


def test_redelivery_reuses_snapshot_and_result_without_extending_retention(part_store):
    """A rollback cannot replace the original incident or regenerate an already completed recovery."""
    frozen = freeze(part_store)
    result = publish(part_store)
    part_store.clock = lambda: 1010
    assert freeze(part_store) == frozen
    assert part_store.start_part("rca-1", "recovery", "claim", 2) == result
    assert part_store.publish_part("rca-1", "recovery", "claim", 2, result={"summary": "recovery"}) == result
    changed = incident()
    changed["observations"] = {"current": "restored"}
    with pytest.raises(ValueError, match="already frozen"):
        part_store.freeze_incident("rca-1", "claim", 2, changed)
    with pytest.raises(ValueError, match="expected current"):
        part_store.publish_part("rca-1", "recovery", "claim", 2, result={"summary": "different"})


def test_previous_result_required_and_failure_allows_following_part(part_store):
    """A root part cannot race recovery, but an explicit recovery failure permits root work."""
    freeze(part_store)
    with pytest.raises(ValueError, match="previous"):
        part_store.start_part("rca-1", "root_cause", "claim", 1)
    publish(part_store, status="FAILED", error="source unavailable")
    assert part_store.start_part("rca-1", "root_cause", "claim", 1)["record"]["status"] == "RUNNING"


@pytest.mark.parametrize(
    "field,value",
    [
        ("claim_token", "other"),
        ("state", "CANCELLED"),
        ("state", "FAILED"),
        ("state", "COMPLETED"),
        ("state", "OUTDATED"),
        ("engine", "headless-codex"),
    ],
)
def test_stale_or_terminal_parent_cannot_publish(part_store, field, value):
    """Late results never become authoritative after claim replacement, cancellation or termination."""
    freeze(part_store)
    part_store.start_part("rca-1", "recovery", "claim", 1)
    part_store.ddb.update_item(
        TableName="parts",
        Key={"PK": {"S": "RCA#rca-1"}, "SK": {"S": "ANALYSIS#SESSION"}},
        UpdateExpression="SET #field = :value",
        ExpressionAttributeNames={"#field": field},
        ExpressionAttributeValues={":value": {"S": value}},
    )
    with pytest.raises(ClientError):
        part_store.publish_part("rca-1", "recovery", "claim", 1, result={"summary": "late"})
    assert part_store.read_part("rca-1", "recovery")["payload"] is None


def test_failed_object_write_does_not_publish_and_tampering_is_rejected(part_store, monkeypatch):
    """S3 failure leaves only RUNNING; subsequent corruption cannot be read as a valid part."""
    freeze(part_store)
    part_store.start_part("rca-1", "recovery", "claim", 1)
    original = part_store._put_object

    def fail(*args):
        """Simulate object persistence failure before the metadata transaction."""
        raise RuntimeError("object unavailable")

    monkeypatch.setattr(part_store, "_put_object", fail)
    with pytest.raises(RuntimeError):
        part_store.publish_part("rca-1", "recovery", "claim", 1, result={})
    assert part_store.read_part("rca-1", "recovery")["payload"] is None
    monkeypatch.setattr(part_store, "_put_object", original)
    result = part_store.publish_part("rca-1", "recovery", "claim", 1, result={})
    part_store.s3.put_object(Bucket=part_store.bucket, Key=result["record"]["payload_s3_key"], Body=b"{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        part_store.read_part("rca-1", "recovery")


def test_explicit_revision_preserves_old_payload_and_rejects_wrong_compare_value(part_store):
    """An intentional new revision needs the expected previous hash and preserves the older approval source."""
    freeze(part_store)
    old = publish(part_store)
    with pytest.raises(ValueError):
        part_store.publish_part("rca-1", "recovery", "claim", 1, result={"summary": "new"}, expected_revision="wrong")
    new = part_store.publish_part(
        "rca-1", "recovery", "claim", 1, result={"summary": "new"}, expected_revision=old["record"]["revision"]
    )
    assert old["record"]["revision"] != new["record"]["revision"]
    assert part_store._body(deepcopy(old["record"]), "recovery") == old["payload"]


def test_shared_store_is_byte_identical():
    """Both runtimes use the exact same persistence and read validation contract."""
    root = Path(__file__).resolve().parents[3]
    assert (root / "packages/agent/src/rca_agent/services/analysis_parts.py").read_bytes() == (
        root / "packages/headless-codex/src/headless_codex/services/analysis_parts.py"
    ).read_bytes()


def test_cross_engine_takeover_reads_original_incident_after_rollback(part_store):
    """A shared-queue takeover cannot reinterpret restored state as the original incident."""
    original = freeze(part_store)
    part_store.ddb.update_item(
        TableName="parts",
        Key={"PK": {"S": "RCA#rca-1"}, "SK": {"S": "ANALYSIS#SESSION"}},
        UpdateExpression="SET engine = :engine, claim_token = :claim",
        ExpressionAttributeValues={":engine": {"S": "headless-codex"}, ":claim": {"S": "new"}},
    )
    new = AnalysisPartStore(
        part_store.ddb,
        part_store.s3,
        table_name="parts",
        bucket=part_store.bucket,
        engine="headless-codex",
        clock=part_store.clock,
    )
    assert new.read_incident("rca-1") == original
    restored = incident()
    restored["observations"] = {"current": "restored"}
    assert new.freeze_incident("rca-1", "new", 2, restored) == original
    new.start_part("rca-1", "recovery", "new", 2)
    result = new.publish_part("rca-1", "recovery", "new", 2, result={"summary": "not eligible now"})
    assert result["record"]["engine"] == "headless-codex"
    assert result["payload"]["incident_ref"]["key"].startswith("analysis-parts/strands/")
    assert new.read_incident("rca-1")["payload"]["observations"] == {"current": "fault"}


def test_parent_completion_requires_all_parts_and_does_not_publish_library(part_store):
    """The global terminal state is separate from early availability and public knowledge."""
    freeze(part_store)
    publish(part_store)
    with pytest.raises(ValueError, match="all analysis parts"):
        part_store.complete_analysis("rca-1", "claim", notification={"rca_id": "rca-1"})
    publish(part_store, "root_cause")
    publish(part_store, "operations")
    part_store.complete_analysis("rca-1", "claim", notification={"rca_id": "rca-1"})
    parent = part_store._get("rca-1", "ANALYSIS#SESSION")
    assert parent["state"] == "COMPLETED"
    assert "playbook_index_status" not in parent


def test_unreadable_original_snapshot_cannot_fall_back_to_new_observations(part_store):
    """Loss of the original source blocks resume instead of observing post-rollback state."""
    original = freeze(part_store)
    part_store.s3.delete_object(Bucket=part_store.bucket, Key=original["record"]["payload_s3_key"])
    with pytest.raises(ClientError):
        part_store.read_incident("rca-1")


def test_real_approval_playbook_digest_matches_dashboard_and_payload_hash_is_distinct(part_store):
    """A captured complete wire playbook uses the existing JS approval bytes, including unknown numeric fields."""
    import hashlib
    import json
    import subprocess

    from rca_agent.services.analysis_parts import approval_digest

    freeze(part_store)
    book = json.loads((Path(__file__).parent / "fixtures/analysis-part-approval-playbook.json").read_text())
    book["rca_id"] = "rca-1"
    book["test_numeric_extension"] = {"count": 1.0, "ratio": 0.25}
    script = """
const crypto=require('crypto');

function c(v){return Array.isArray(v)?v.map(c):v&&typeof v==='object'
?Object.fromEntries(Object.keys(v).sort().map(k=>[k,c(v[k])])):v};
process.stdout.write(crypto.createHash('sha256').update(JSON.stringify(c(JSON.parse(process.argv[1])))).digest('hex'))
"""
    expected = subprocess.check_output(["node", "-e", script, json.dumps(book)], text=True)
    assert approval_digest(book) == expected
    part_store.start_part("rca-1", "recovery", "claim", 1)
    result = {"summary": "ready", "recommendation": "ROLLBACK", "playbook": book, "verification": {"valid": True}}
    for valid in ("true", 1, False):
        bad = {**result, "verification": {"valid": valid}}
        with pytest.raises(ValueError, match="READY requires"):
            part_store.publish_part(
                "rca-1", "recovery", "claim", 1, result=bad, approval_status="READY", runbook_digest=expected
            )
    with pytest.raises(ValueError, match="READY requires"):
        part_store.publish_part(
            "rca-1",
            "recovery",
            "claim",
            1,
            result=result,
            approval_status="READY",
            runbook_digest=hashlib.sha256(b"other").hexdigest(),
        )
    published = part_store.publish_part(
        "rca-1", "recovery", "claim", 1, result=result, approval_status="READY", runbook_digest=expected
    )
    assert published["record"]["runbook_digest"] == expected
    assert published["record"]["payload_sha256"] != expected


def test_actual_dashboard_module_numeric_property_order_matches_at_every_depth():
    """Use the production TypeScript serializer, including integer keys which JSON.stringify reorders."""
    import json
    import subprocess

    from rca_agent.services.analysis_parts import approval_digest

    module = Path(__file__).resolve().parents[3] / "packages/dashboard/server/utils/executionApproval.ts"
    script = """import { serializePlaybookSnapshot, sha256Hex } from MODULE;
process.stdout.write(sha256Hex(serializePlaybookSnapshot(JSON.parse(process.argv[1]))));""".replace(
        "MODULE", json.dumps(module.as_uri())
    )
    value = {
        "2": "two",
        "10": "ten",
        "1": "one",
        "nested": {"4294967294": 1.0, "4294967295": 2, "01": -0.0, "0": [1e-7, {"10": 1, "2": 2}], "𐀀": "value"},
    }
    expected = subprocess.check_output(["node", "--input-type=module", "-e", script, json.dumps(value)], text=True)
    assert approval_digest(value) == expected


@pytest.mark.parametrize("mutation", ["stop_task", "second_write", "no_rollback"])
def test_early_operation_gate_rejects_other_writes_before_ready_publication(part_store, mutation):
    """Even validly hashed, otherwise well-formed early plans cannot add a non-rollback mutation."""
    import json

    from rca_agent.services.analysis_parts import approval_digest, validate_recovery_operations
    from rca_agent.services.runbook_contract import validate_runbook

    freeze(part_store)
    book = json.loads((Path(__file__).parent / "fixtures/analysis-part-approval-playbook.json").read_text())
    book["rca_id"] = "rca-1"
    scope = book["rollback_context"]["scope"]
    if mutation == "stop_task":
        task = (
            f"arn:aws:ecs:{scope['region']}:{scope['account_id']}:task/"
            + scope["cluster_arn"].rsplit("/", 1)[-1]
            + "/"
            + "a" * 32
        )
        book["execution_steps"].insert(
            1,
            {
                "step_id": "unrelated",
                "action": "unrelated task",
                "success_criteria": "stopped",
                "commands": [
                    f"aws ecs stop-task --cluster {scope['cluster_arn']} --task {task} --region {scope['region']}"
                ],
            },
        )
        validate_runbook(book["execution_steps"])
    elif mutation == "second_write":
        book["execution_steps"][0]["commands"].append(book["execution_steps"][1]["commands"][0])
    else:
        book["execution_steps"] = [book["execution_steps"][0]]
    with pytest.raises(ValueError):
        validate_recovery_operations(book)
    part_store.start_part("rca-1", "recovery", "claim", 1)
    with pytest.raises(ValueError):
        part_store.publish_part(
            "rca-1",
            "recovery",
            "claim",
            1,
            result={"recommendation": "ROLLBACK", "playbook": book, "verification": {"valid": True}},
            approval_status="READY",
            runbook_digest=approval_digest(book),
        )
    assert part_store.read_part("rca-1", "recovery")["payload"] is None


@pytest.mark.parametrize(
    "provided,expiry,lease_claim,allowed",
    [
        ("owned", 2000, "claim", True),
        ("owned", 2000, None, True),
        ("foreign", 2000, "claim", False),
        (None, 2000, "claim", False),
        ("owned", 999, "claim", False),
        ("owned", 2000, "other", False),
    ],
)
def test_completion_checks_and_atomically_consumes_publication_lease(
    part_store, provided, expiry, lease_claim, allowed
):
    """Both engine lease shapes preserve ownership and expiry at the terminal commit, without a later release."""
    freeze(part_store)
    for part in ("recovery", "root_cause", "operations"):
        publish(part_store, part)
    key = {"PK": {"S": "RCA#rca-1"}, "SK": {"S": "ANALYSIS#SESSION"}}
    values = {":token": {"S": "owned"}, ":expires": {"N": str(expiry)}}
    expression = "SET side_effect_lease_token = :token, side_effect_lease_expires_at = :expires"
    if lease_claim is not None:
        expression += ", side_effect_lease_claim_token = :owner"
        values[":owner"] = {"S": lease_claim}
    part_store.ddb.update_item(
        TableName="parts", Key=key, UpdateExpression=expression, ExpressionAttributeValues=values
    )
    if allowed:
        assert part_store.complete_analysis(
            "rca-1", "claim", notification={"rca_id": "rca-1"}, side_effect_lease_token=provided
        )
        parent = part_store._get("rca-1", "ANALYSIS#SESSION")
        assert parent["state"] == "COMPLETED" and parent["analysis_parts_finalized"] is True
        assert not any(name.startswith("side_effect_lease_") for name in parent)
    else:
        with pytest.raises(ClientError):
            part_store.complete_analysis(
                "rca-1", "claim", notification={"rca_id": "rca-1"}, side_effect_lease_token=provided
            )
        assert part_store._get("rca-1", "ANALYSIS#SESSION")["state"] == "SCOPING"
