import json
from unittest.mock import Mock

import pytest

from headless_codex.adapters.secondary.report import s3_report_store
from headless_codex.adapters.secondary.report.s3_report_store import S3ReportStore
from headless_codex.ports.dto.models import AlarmContext


def test_report_key_is_isolated_by_attempt_and_claim(monkeypatch):
    monkeypatch.setattr(s3_report_store, "S3_REPORT_BUCKET", "rca-reports")
    s3 = Mock()

    key = S3ReportStore(s3_client=s3).save_report(
        "rca-1",
        "# report",
        claim_token="claim-abc",
        attempt=3,
    )

    assert key == "reports/headless-codex/rca-1/attempt-3-claim-abc/report.md"
    assert s3.put_object.call_args.kwargs["Key"] == key


def test_configured_report_bucket_without_a_client_fails_instead_of_returning_a_key(monkeypatch):
    monkeypatch.setattr(s3_report_store, "S3_REPORT_BUCKET", "rca-reports")

    with pytest.raises(RuntimeError, match="S3 client"):
        S3ReportStore().save_report("rca-1", "# report")


def test_completion_notification_does_not_trigger_external_remediation_worker(monkeypatch):
    monkeypatch.setattr(
        s3_report_store,
        "SNS_NOTIFICATION_TOPIC_ARN",
        "arn:aws:sns:us-east-1:123456789012:rca-notifications",
    )
    monkeypatch.setattr(s3_report_store, "S3_REPORT_BUCKET", "rca-reports")
    sns = Mock()
    alarm_context = AlarmContext(
        alarm_name="Healthcare-RdsHighConnections",
        namespace="AWS/RDS",
        metric_name="DatabaseConnections",
        threshold=30.0,
    )

    S3ReportStore(sns_client=sns).send_notification(
        "rca-1",
        alarm_context.alarm_name,
        "database connection pool exhausted",
        "reports/headless-codex/rca-1.md",
        42,
        confirmed=True,
        alarm_context=alarm_context,
    )

    published = sns.publish.call_args.kwargs
    assert published["MessageAttributes"]["event_type"] == {
        "DataType": "String",
        "StringValue": "headless_codex_complete",
    }
    assert published["MessageAttributes"]["notification_id"] == {
        "DataType": "String",
        "StringValue": "headless-codex:rca-1:complete",
    }
    body = json.loads(published["Message"])
    assert body["notification_id"] == "headless-codex:rca-1:complete"
    assert body["confirmed"] is True
    assert body["root_cause"] == "database connection pool exhausted"
    assert body["alarm_context"]["alarm_name"] == "Healthcare-RdsHighConnections"
    assert body["alarm_context"]["namespace"] == "AWS/RDS"
    assert body["alarm_context"]["metric_name"] == "DatabaseConnections"
    assert body["alarm_context"]["threshold"] == 30.0


def test_fifo_completion_notification_uses_stable_sns_deduplication_id(monkeypatch):
    monkeypatch.setattr(
        s3_report_store,
        "SNS_NOTIFICATION_TOPIC_ARN",
        "arn:aws:sns:us-east-1:123456789012:rca-notifications.fifo",
    )
    sns = Mock()
    store = S3ReportStore(sns_client=sns)

    for report_key in ("reports/attempt-1/report.md", "reports/attempt-2/report.md"):
        store.send_notification(
            "rca-1",
            "HighCPU",
            "high CPU",
            report_key,
            42,
        )

    first, second = (call.kwargs for call in sns.publish.call_args_list)
    assert first["MessageDeduplicationId"] == second["MessageDeduplicationId"] == "headless-codex:rca-1:complete"
    assert first["MessageGroupId"] == second["MessageGroupId"] == "headless-codex-completion"
