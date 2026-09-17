"""Early execution uses only immutable reservation/snapshot authority, never later analysis state."""

import copy
import hashlib
import io
import json
from unittest.mock import Mock

import pytest
from test_service_deployment import plan as plan

from headless_codex.adapters.secondary.execution import dynamodb_execution_store as module
from headless_codex.ports.interfaces.execution_store import ExecutionTargetUnavailableError


@pytest.fixture
def reservation(monkeypatch):
    monkeypatch.setattr(module, "DYNAMODB_TABLE_NAME", "offline")
    monkeypatch.setattr(module, "S3_EVIDENCE_BUCKET", "configured-evidence")
    raw = {
        "AlarmName": "HighFailure",
        "AlarmDescription": "original description " + ("x" * 450000),
        "AWSAccountId": "123456789012",
        "unknown": {"preserve": [1, True, None]},
        "Trigger": {
            "MetricName": "failures",
            "Dimensions": [{"name": "ServiceName", "value": "sensor"}],
            "ExtendedStatistic": "p99.9",
        },
    }
    item = {
        key: {"S": value}
        for key, value in {
            "rca_id": "rca",
            "approval_id": "approval",
            "approved_playbook_s3_key": "approvals/rca/approval/playbook.json",
            "engine": "headless-codex",
            "claim_token": "claim",
            "execution_state": "EXECUTING",
            "source_part": "recovery",
            "source_part_revision": "a" * 64,
            "source_part_payload_sha256": "a" * 64,
            "report_s3_key": "analysis-parts/headless-codex/rca/recovery/" + ("a" * 64) + ".json",
            "source_alarm_name": raw["AlarmName"],
        }.items()
    }
    pin(item, raw)
    return item, raw


def pin(item, original_alarm, **overrides):
    document = {"schema_version": 1, "rca_id": "rca", "engine": "strands", "alarm": original_alarm, **overrides}
    raw = json.dumps(document).encode()
    digest = hashlib.sha256(raw).hexdigest()
    item.update(
        {
            "source_incident_engine": {"S": "strands"},
            "source_incident_sha256": {"S": digest},
            "source_incident_s3_key": {"S": "approvals/rca/approval/incident.json"},
            "original_incident_s3_key": {"S": f"analysis-parts/strands/rca/incident/{digest}.json"},
        }
    )
    OBJECTS[item["source_incident_s3_key"]["S"]] = raw
    return raw


OBJECTS = {}


def load(item, book, *, replacement=None, unavailable=False, after=None):
    client = Mock()
    client.get_item.side_effect = AssertionError("must not read latest analysis/part")
    s3 = Mock()

    def get_object(**request):
        assert request["Bucket"] == "configured-evidence"
        assert request["Key"] == item["source_incident_s3_key"]["S"]
        if unavailable:
            raise OSError("offline missing object")
        return {"Body": io.BytesIO(replacement if replacement is not None else OBJECTS[request["Key"]])}

    s3.get_object.side_effect = get_object
    store = module.DynamoDbExecutionStore(client, s3_client=s3)
    store._get_execution = Mock(side_effect=[item, after or item])
    target = store.load_target(
        "rca",
        "headless-codex",
        report_s3_key=item["report_s3_key"]["S"],
        playbook=book,
        execution_id="exec",
        claim_token="claim",
    )
    client.get_item.assert_not_called()
    return target


def test_early_target_preserves_full_original_alarm_and_snapshot(reservation, plan):
    item, raw = reservation
    alarm = next(step["metric_wait"]["failure_alarm_name"] for step in plan["execution_steps"] if "metric_wait" in step)
    raw["AlarmName"] = alarm
    item["source_alarm_name"]["S"] = alarm
    pin(item, raw)
    before = copy.deepcopy(plan)
    target = load(item, plan)
    assert len(json.dumps(item)) < 4000
    assert "source_alarm_data_json" not in item
    assert target.source_part == "recovery"
    assert target.alarm_data == raw
    assert len(target.alarm_data["AlarmDescription"]) > 400000
    assert target.playbook is plan and plan == before


@pytest.mark.parametrize(
    "field",
    [
        "source_incident_s3_key",
        "source_incident_sha256",
        "source_incident_engine",
        "approval_id",
        "approved_playbook_s3_key",
        "source_alarm_name",
        "source_part_revision",
        "source_part_payload_sha256",
        "claim_token",
        "engine",
        "rca_id",
    ],
)
def test_early_target_rejects_missing_or_changed_reserved_authority(reservation, plan, field):
    item, raw = reservation
    item[field] = {"S": "wrong"}
    with pytest.raises((ValueError, ExecutionTargetUnavailableError)):
        load(item, plan)


def test_early_target_rejects_wait_alarm_different_from_frozen_alarm(reservation, plan):
    item, _ = reservation
    with pytest.raises(ExecutionTargetUnavailableError, match="original alarm"):
        load(item, plan)


@pytest.mark.parametrize("change", ["extra_stop", "other_write", "no_update"])
def test_early_reservation_cannot_authorize_nonrollback_writes(reservation, plan, change):
    from headless_codex.services.execution_contract import validate_steps

    item, raw = reservation
    alarm = next(step["metric_wait"]["failure_alarm_name"] for step in plan["execution_steps"] if "metric_wait" in step)
    raw["AlarmName"] = alarm
    item["source_alarm_name"]["S"] = alarm
    pin(item, raw)
    plan = copy.deepcopy(plan)
    if change == "no_update":
        plan["execution_steps"] = plan["execution_steps"][1:]
    else:
        scope = plan["rollback_context"]["scope"]
        command = (
            f"aws ecs stop-task --cluster {scope['cluster_arn']} "
            "--task arn:aws:ecs:us-east-1:123456789012:task/cluster/other --region us-east-1"
            if change == "extra_stop"
            else "aws rds reboot-db-instance --db-instance-identifier demo --region us-east-1"
        )
        plan["execution_steps"].append(
            {
                "step_id": "extra",
                "action": "other action",
                "intent": "other write",
                "success_criteria": "done",
                "commands": [command],
            }
        )
        # The ordinary confirmed-plan contract still permits these approved operations.
        assert validate_steps(plan)
    with pytest.raises(ValueError):
        load(item, plan)


@pytest.mark.parametrize("change", ["hash", "missing", "rca", "engine", "schema", "alarm", "claim", "json_legacy"])
def test_pinned_source_failure_cannot_load_target(reservation, plan, change):
    item, alarm = reservation
    name = next(step["metric_wait"]["failure_alarm_name"] for step in plan["execution_steps"] if "metric_wait" in step)
    alarm["AlarmName"] = name
    item["source_alarm_name"]["S"] = name
    pin(item, alarm)
    kwargs = {}
    if change == "hash":
        kwargs["replacement"] = b"{}"
    elif change == "missing":
        kwargs["unavailable"] = True
    elif change in {"rca", "engine", "schema", "alarm"}:
        field = {"rca": "rca_id", "engine": "engine", "schema": "schema_version", "alarm": "alarm"}[change]
        pin(item, alarm, **{field: True if change == "schema" else "wrong"})
    elif change == "claim":
        kwargs["after"] = {**item, "claim_token": {"S": "other"}}
    else:
        item.pop("source_incident_s3_key")
        item["source_alarm_data_json"] = {"S": json.dumps(alarm)}
    with pytest.raises(ExecutionTargetUnavailableError):
        load(item, plan, **kwargs)


def test_approval_copy_survives_analysis_object_deletion(reservation, plan):
    item, alarm = reservation
    alarm["AlarmName"] = next(
        step["metric_wait"]["failure_alarm_name"] for step in plan["execution_steps"] if "metric_wait" in step
    )
    item["source_alarm_name"]["S"] = alarm["AlarmName"]
    original_bytes = pin(item, alarm)
    original_key = item["original_incident_s3_key"]["S"]
    OBJECTS[original_key] = original_bytes
    del OBJECTS[original_key]
    target = load(item, plan)
    assert target.alarm_data == alarm
    assert OBJECTS[item["source_incident_s3_key"]["S"]] == original_bytes


@pytest.mark.parametrize(
    "path",
    [
        "approvals/other/approval/incident.json",
        "approvals/rca/other/incident.json",
        "analysis-parts/strands/rca/incident/{digest}.json",
    ],
)
def test_worker_rejects_copy_from_other_approval_or_analysis_path(reservation, plan, path):
    item, _ = reservation
    item["source_incident_s3_key"]["S"] = path.format(digest=item["source_incident_sha256"]["S"])
    with pytest.raises(ExecutionTargetUnavailableError, match="reference"):
        load(item, plan)


def test_missing_approval_copy_does_not_fall_back_to_existing_analysis_object(reservation, plan):
    item, alarm = reservation
    original_bytes = pin(item, alarm)
    OBJECTS[item["original_incident_s3_key"]["S"]] = original_bytes
    with pytest.raises(ExecutionTargetUnavailableError, match="could not be verified"):
        load(item, plan, unavailable=True)
