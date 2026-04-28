import boto3 as _boto3
from botocore.config import Config as _Config

from cc_headless.adapters.secondary.report.s3_report_store import S3ReportStore  # noqa: F401
from cc_headless.config.settings import (  # noqa: F401
    ENGINE,
    PRESIGNED_URL_EXPIRY,
    S3_REPORT_BUCKET,
    SNS_NOTIFICATION_TOPIC_ARN,
)

_s3 = _boto3.client("s3", config=_Config(signature_version="s3v4"))
_sns = _boto3.client("sns")
_default_store = S3ReportStore(_s3, _sns)


def save_report(rca_id, report_markdown):
    return _default_store.save_report(rca_id, report_markdown)


def send_notification(
    rca_id,
    alarm_name,
    root_cause,
    report_s3_key,
    elapsed_seconds,
    *,
    playbook=None,
):
    return _default_store.send_notification(
        rca_id,
        alarm_name,
        root_cause,
        report_s3_key,
        elapsed_seconds,
        playbook=playbook,
    )
