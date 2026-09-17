"""Local fake SDK fixture shared with Strands reader contract tests."""

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from headless_codex.services.recovery_observation import AwsIncidentObservation, _canonical

AlarmPayload = SimpleNamespace
AlarmTrigger = SimpleNamespace


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
        eval_source_metadata=None,
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
