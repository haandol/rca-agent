from __future__ import annotations

import json
import logging

import boto3
from botocore.config import Config

from cc_headless.config import ENGINE, PRESIGNED_URL_EXPIRY, S3_REPORT_BUCKET, SNS_NOTIFICATION_TOPIC_ARN

logger = logging.getLogger(__name__)

_s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
_sns = boto3.client("sns")


def save_report(rca_id: str, report_markdown: str) -> str:
    key = f"reports/{rca_id}.md"
    if not S3_REPORT_BUCKET:
        return key
    _s3.put_object(Bucket=S3_REPORT_BUCKET, Key=key, Body=report_markdown.encode(), ContentType="text/markdown")
    return key


def send_notification(
    rca_id: str,
    alarm_name: str,
    root_cause: str,
    report_s3_key: str,
    elapsed_seconds: int,
) -> None:
    if not SNS_NOTIFICATION_TOPIC_ARN:
        return

    report_url = f"s3://{S3_REPORT_BUCKET}/{report_s3_key}"
    if S3_REPORT_BUCKET:
        try:
            report_url = _s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_REPORT_BUCKET, "Key": report_s3_key},
                ExpiresIn=PRESIGNED_URL_EXPIRY,
            )
        except Exception:
            logger.warning("Failed to generate presigned URL, falling back to S3 URI")

    message = json.dumps(
        {
            "rca_id": rca_id,
            "alarm_name": alarm_name,
            "root_cause": root_cause,
            "report_url": report_url,
            "engine": ENGINE,
            "elapsed_seconds": elapsed_seconds,
        }
    )

    _sns.publish(
        TopicArn=SNS_NOTIFICATION_TOPIC_ARN,
        Subject=f"[RCA] {alarm_name} — Analysis Complete ({ENGINE})",
        Message=message,
    )
