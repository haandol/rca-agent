from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from rca_agent.adapters.secondary.evidence.incident_observation import AwsIncidentObservation, _canonical
from rca_agent.ports.dto.models import (
    AlarmPayload,
    AlarmTrigger,
    Hypothesis,
    HypothesisCategory,
    ScopingResult,
)
from rca_agent.services.evidence import _build_user_prompt as evidence_prompt
from rca_agent.services.hypothesis import _build_user_prompt as hypothesis_prompt
from rca_agent.services.playbook_gen import _render_current_observations
from rca_agent.services.scoping import run_scoping
from rca_agent.services.validation import _build_user_prompt as validation_prompt


@pytest.fixture
def setup_observations():
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    alarm_time = now - timedelta(minutes=1)
    normal_time = now - timedelta(minutes=5)
    account, region = "123456789012", "us-east-1"
    cluster = f"arn:aws:ecs:{region}:{account}:cluster/test"
    service = f"arn:aws:ecs:{region}:{account}:service/test/sensor"
    normal_definition = f"arn:aws:ecs:{region}:{account}:task-definition/sensor:1"
    bad_definition = f"arn:aws:ecs:{region}:{account}:task-definition/sensor:2"
    scope = {
        "account_id": account,
        "region": region,
        "cluster_arn": cluster,
        "service_arn": service,
        "service_name": "sensor",
        "container_name": "app",
        "log_group": "/ecs/sensor",
        "desired_count": 1,
    }
    normal_digest, bad_digest = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    metrics = {
        name: {"namespace": "Sensor", "metric_name": name, "dimensions": {"ServiceName": "sensor"}}
        for name in ("attempts", "failures")
    }

    def event(kind, task_id, stamp, **data):
        return {
            "timestamp": int(stamp.timestamp() * 1000),
            "event_id": kind + "-" + task_id,
            "log_group": "/ecs/sensor",
            "log_stream": "ecs/app/" + task_id,
            "message": {"event": kind, "observed_at": stamp.isoformat(), **data},
        }

    normal_events = [
        event("source_manifest", "normal-task", normal_time, fingerprint="c" * 64, verified=True),
        event(
            "schema_snapshot",
            "normal-task",
            normal_time,
            schema_name="public",
            table_name="readings",
            column_names=["id", "reading_value"],
        ),
        event(
            "write_completed",
            "normal-task",
            normal_time,
            count=2,
            completion_semantics="committed_rows",
            sql_hash="d" * 64,
            table_name="readings",
            column_names=["id", "reading_value"],
        ),
    ]
    baseline = {
        "schema_version": 1,
        "run_id": "run-1",
        "observed_at": (normal_time + timedelta(minutes=1)).isoformat(),
        "scope": scope,
        "normal": {"task_definition_arn": normal_definition, "image_digest": normal_digest},
        "service_settings": {"desiredCount": 1},
        "metrics": metrics,
        "metric_observations": {
            "start": normal_time.isoformat(),
            "end": (normal_time + timedelta(minutes=1)).isoformat(),
            "attempts": [{"Timestamp": normal_time.isoformat(), "Sum": 2, "Unit": "Count"}],
            "failures": [{"Timestamp": normal_time.isoformat(), "Sum": 0, "Unit": "Count"}],
        },
        "observations": normal_events,
    }
    alarm = AlarmPayload(
        alarm_name="WriteFailures",
        alarm_arn=f"arn:aws:cloudwatch:{region}:{account}:alarm:WriteFailures",
        state_change_time=alarm_time,
        region=region,
        trigger=AlarmTrigger(metric_name="failures", namespace="Sensor", dimensions={"ServiceName": "sensor"}),
    )
    s3, logs, ecs = MagicMock(), MagicMock(), MagicMock()

    def task(task_id, definition, digest):
        return {
            "taskArn": f"arn:aws:ecs:{region}:{account}:task/test/{task_id}",
            "clusterArn": cluster,
            "group": "service:sensor",
            "taskDefinitionArn": definition,
            "lastStatus": "RUNNING",
            "containers": [{"name": "app", "imageDigest": digest}],
        }

    normal_task = task("normal-task", normal_definition, normal_digest)
    bad_task = task("bad-task", bad_definition, bad_digest)
    ecs.describe_tasks.side_effect = lambda **kwargs: {
        "tasks": [normal_task if "normal-task" in item else bad_task for item in kwargs["tasks"]],
    }
    ecs.describe_services.return_value = {
        "services": [
            {
                "serviceArn": service,
                "clusterArn": cluster,
                "taskDefinition": bad_definition,
                "desiredCount": 1,
                "deployments": [
                    {
                        "id": "ecs-svc/2",
                        "taskDefinition": bad_definition,
                        "status": "PRIMARY",
                        "rolloutState": "COMPLETED",
                        "createdAt": alarm_time - timedelta(minutes=1),
                    }
                ],
            }
        ],
    }
    ecs.list_tasks.return_value = {"taskArns": [bad_task["taskArn"]]}
    ecs.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [
                {
                    "name": "app",
                    "logConfiguration": {
                        "options": {
                            "awslogs-group": "/ecs/sensor",
                            "awslogs-region": region,
                            "awslogs-stream-prefix": "ecs",
                        }
                    },
                }
            ]
        }
    }
    current = [
        event(
            "db_write_error",
            "bad-task",
            alarm_time,
            sqlstate="42703",
            table_name="readings",
            column_names=["id", "missing_value"],
            sql_hash="e" * 64,
            parameters="secret patient",
        ),
        event(
            "schema_snapshot",
            "bad-task",
            alarm_time,
            schema_name="public",
            table_name="readings",
            column_names=["id", "reading_value"],
        ),
        event("source_manifest", "bad-task", alarm_time, fingerprint="f" * 64, verified=True),
    ]
    logs.filter_log_events.return_value = {
        "events": [
            {
                "timestamp": e["timestamp"],
                "eventId": e["event_id"],
                "logStreamName": e["log_stream"],
                "message": json.dumps(e["message"]),
            }
            for e in current
        ]
    }

    def publish():
        raw = _canonical(baseline)
        s3.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(raw)}
        alarm.alarm_description = json.dumps(
            {
                "summary": "write failures",
                "run_id": "run-1",
                "service": "sensor",
                "baseline_ref": {
                    "bucket": "evidence",
                    "key": "baselines/run-1/normal.json",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                },
            }
        )

    publish()
    reader = AwsIncidentObservation(
        s3_client=s3,
        logs_client_for_region=lambda region: logs,
        ecs_client_for_region=lambda region: ecs,
        evidence_bucket="evidence",
    )
    return reader, alarm, baseline, publish, s3, logs, ecs


def test_actual_normal_and_current_are_separate_with_safe_critical_facts(setup_observations):
    reader, alarm, _, _, s3, logs, _ = setup_observations
    result = reader.observe(alarm, timeout_seconds=90)
    assert result.diagnostics == []
    assert result.baseline_verified
    assert result.critical_facts[0].sqlstate == "42703"
    assert result.critical_facts[0].sql_fingerprint == "e" * 64
    assert result.critical_facts[0].image_digest == "sha256:" + "b" * 64
    assert result.baseline["normal"]["image_digest"] == "sha256:" + "a" * 64
    assert "secret patient" not in result.model_dump_json()
    assert logs.filter_log_events.call_args.kwargs["limit"] == 100
    assert s3.get_object.call_args.kwargs["ExpectedBucketOwner"] == "123456789012"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.update(run_id="other"),
        lambda b: b["scope"].update(account_id="000000000000"),
        lambda b: b["scope"].update(region="us-west-2"),
        lambda b: b["scope"].update(service_name="different", service_arn="different"),
        lambda b: b["metric_observations"]["failures"][0].update(Sum=1),
        lambda b: b["metric_observations"]["attempts"][0].update(Sum=0),
        lambda b: b["observations"][0]["message"].update(verified=False),
        lambda b: b["observations"][1]["message"].update(column_names=[]),
        lambda b: b["observations"][2]["message"].update(completion_semantics="attempted_rows"),
        lambda b: b["normal"].update(image_digest="sha256:" + "0" * 64),
        lambda b: b["observations"][0].update(event_id=""),
        lambda b: b["observations"][0].update(log_group="/wrong"),
    ],
)
def test_invalid_normal_proof_never_becomes_approval_context(setup_observations, mutation):
    reader, alarm, baseline, publish, *_ = setup_observations
    mutation(baseline)
    publish()
    result = reader.observe(alarm, timeout_seconds=90)
    assert not result.baseline_verified
    assert result.baseline == {}
    assert result.diagnostics


@pytest.mark.parametrize("field,value", [("bucket", "other"), ("key", "baselines/../normal.json"), ("sha256", "wrong")])
def test_invalid_reference_causes_no_external_request(setup_observations, field, value):
    reader, alarm, _, _, s3, _, ecs = setup_observations
    metadata = json.loads(alarm.alarm_description)
    metadata["baseline_ref"][field] = value
    alarm.alarm_description = json.dumps(metadata)
    assert not reader.observe(alarm, timeout_seconds=90).baseline_verified
    s3.get_object.assert_not_called()
    ecs.describe_tasks.assert_not_called()


def test_tampered_content_fails_before_any_scope_query(setup_observations):
    reader, alarm, _, _, s3, _, ecs = setup_observations
    s3.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(b"{}")}
    assert not reader.observe(alarm, timeout_seconds=90).baseline_verified
    ecs.describe_tasks.assert_not_called()


def test_critical_facts_survive_long_prose_all_the_way_to_runbook(setup_observations):
    reader, alarm, *_ = setup_observations
    observations = reader.observe(alarm, timeout_seconds=90)
    scoping = ScopingResult(alarm_summary="x" * 500, incident_observations=observations, raw_alarm=alarm)
    h = Hypothesis(description="candidate", category=HypothesisCategory.DEPLOYMENT, confidence_score=0.5)
    for prompt in (
        hypothesis_prompt(scoping),
        evidence_prompt(h, scoping),
        validation_prompt(h, "x" * 500, scoping),
        _render_current_observations(scoping),
    ):
        assert "42703" in prompt and "missing_value" in prompt and "reading_value" in prompt
        assert "e" * 64 in prompt and "f" * 64 in prompt
        assert "cloudwatch-logs://" in prompt


def test_scoping_timeout_keeps_deterministic_observations(setup_observations):
    reader, alarm, *_ = setup_observations
    report_store = MagicMock()
    report_store.search_similar.return_value = []
    scoping = run_scoping(
        alarm,
        MagicMock(side_effect=TimeoutError),
        report_store=report_store,
        incident_observer=reader,
    )
    assert scoping.incident_observations.baseline_verified
    assert scoping.incident_observations.critical_facts[0].sqlstate == "42703"


def test_historical_eval_does_not_read_current_aws(setup_observations):
    reader, alarm, _, _, s3, logs, ecs = setup_observations
    alarm.eval_source_metadata = {"stateChangeTime": "2026-01-01T01:00:00+09:00"}
    assert reader.observe(alarm, timeout_seconds=90).critical_facts == []
    s3.get_object.assert_not_called()
    logs.filter_log_events.assert_not_called()
    ecs.describe_services.assert_not_called()


def test_actual_accounting_wire_is_preserved_without_inventing_a_descriptor(setup_observations):
    reader, alarm, _, _, _, logs, _ = setup_observations
    event = dict(logs.filter_log_events.return_value["events"][0])
    event["eventId"] = "accounting-bad-task"
    policy = {
        "metric_namespace": "Healthcare/Sensor",
        "service_name": "healthcare-sensor-app",
        "attempt_metric": "VitalIngestAttempts",
        "failure_metric": "VitalIngestFailures",
        "attempt_semantics": "completed_successful_rows_plus_failed_rows",
        "failure_semantics": "failed_rows",
        "cancellation_semantics": "excluded_from_completed_counters",
        "success_evidence_event": "write_completed",
        "success_count_field": "count",
        "success_semantics": "committed_rows",
    }
    event["message"] = json.dumps(
        {
            "event": "write_accounting",
            "observed_at": alarm.state_change_time.isoformat(),
            **policy,
        }
    )
    logs.filter_log_events.return_value["events"].append(event)
    output = reader.observe(alarm, timeout_seconds=90)
    accounting = next(f.write_accounting for f in output.critical_facts if f.write_accounting)
    assert accounting == policy
    assert "accounting" not in accounting
    assert output.current["deployment_id"] == "ecs-svc/2"


def test_schema_chatter_cannot_crowd_errors_and_pages_preserve_distinct_facts(setup_observations):
    """Return 100 schema rows before a second-page error and ignore a foreign task stream."""
    from copy import deepcopy

    reader, alarm, _, _, _, logs, ecs = setup_observations
    originals = deepcopy(logs.filter_log_events.return_value["events"])
    ecs.list_tasks.return_value["taskArns"].append("normal-task")

    def query(**kwargs):
        assert kwargs["logStreamNames"] == ["ecs/app/bad-task"]
        kind = kwargs["filterPattern"].split('"')[1]
        if kind == "db_write_error":
            if not kwargs.get("nextToken"):
                return {"events": [], "nextToken": "error-page-2"}
            second = deepcopy(originals[0])
            second["eventId"] = "distinct-error"
            message = json.loads(second["message"])
            message["sqlstate"] = "23502"
            second["message"] = json.dumps(message)
            return {"events": [originals[0], second]}
        if kind == "schema_snapshot":
            return {
                "events": [{**originals[1], "eventId": f"schema-{i}"} for i in range(100)],
                "nextToken": "schema-more",
            }
        return {"events": [e for e in originals if json.loads(e["message"])["event"] == kind]}

    logs.filter_log_events.side_effect = query
    output = reader.observe(alarm, timeout_seconds=90)
    assert {f.sqlstate for f in output.critical_facts} >= {"42703", "23502"}
    assert sum(bool(f.actual_schema) for f in output.critical_facts) == 1
    assert any(f.source_revision == "f" * 64 for f in output.critical_facts)
    assert output.current["log_window"]["coverage"]["schema_snapshot"] == "pagination_incomplete"
    assert output.current["log_window"]["coverage"]["db_write_error"] == "complete"
    assert "distinct-error" in json.dumps(output.current["observations"])
    assert "secret patient" not in output.model_dump_json()


def test_observation_budget_exhaustion_keeps_already_observed_facts(setup_observations):
    """A remaining page is explicitly incomplete when no bounded SDK request can start."""
    from unittest.mock import patch

    reader, alarm, _, _, _, logs, _ = setup_observations
    original = logs.filter_log_events.return_value
    clock = [0.0]

    def query(**kwargs):
        clock[0] = 80.0
        return {**original, "nextToken": "more"}

    logs.filter_log_events.side_effect = query
    with patch(
        "rca_agent.adapters.secondary.evidence.incident_observation.time.monotonic", side_effect=lambda: clock[0]
    ):
        output = reader.observe(alarm, timeout_seconds=90)
    assert output.critical_facts[0].sqlstate == "42703"
    assert output.current["log_window"]["coverage"]["db_write_error"] == "budget_exhausted"
    assert logs.filter_log_events.call_count == 1


def test_observation_offset_preserves_instant():
    """Aware logger timestamps keep their original instant during source validation."""
    from rca_agent.adapters.secondary.evidence.incident_observation import _utc

    assert _utc("2026-09-15T12:00:00+09:00") == datetime(2026, 9, 15, 3, tzinfo=UTC)
