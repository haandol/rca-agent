from __future__ import annotations

import json

import structlog

from cc_headless.config.settings import ENGINE, PRESIGNED_URL_EXPIRY, S3_REPORT_BUCKET, SNS_NOTIFICATION_TOPIC_ARN
from cc_headless.ports.dto.models import AlarmContext
from cc_headless.ports.interfaces.report_store import ReportStorePort

logger = structlog.get_logger()


class S3ReportStore(ReportStorePort):
    def __init__(self, s3_client=None, sns_client=None):
        self._s3 = s3_client
        self._sns = sns_client

    def save_report(
        self,
        rca_id: str,
        report_markdown: str,
        *,
        claim_token: str | None = None,
        attempt: int | None = None,
    ) -> str:
        if claim_token:
            attempt_segment = f"attempt-{attempt or 1}-{claim_token}"
            key = f"reports/{ENGINE}/{rca_id}/{attempt_segment}/report.md"
        else:
            key = f"reports/{ENGINE}/{rca_id}.md"
        if not S3_REPORT_BUCKET:
            return key
        if not self._s3:
            raise RuntimeError("report bucket is configured but the S3 client is unavailable")
        self._s3.put_object(
            Bucket=S3_REPORT_BUCKET,
            Key=key,
            Body=report_markdown.encode(),
            ContentType="text/markdown",
        )
        return key

    def send_notification(
        self,
        rca_id: str,
        alarm_name: str,
        root_cause: str,
        report_s3_key: str,
        elapsed_seconds: int,
        *,
        playbook: dict | None = None,
        confirmed: bool = False,
        alarm_context: AlarmContext | None = None,
    ) -> None:
        if not SNS_NOTIFICATION_TOPIC_ARN or not self._sns:
            return

        notification_id = f"{ENGINE}:{rca_id}:complete"
        report_url = f"s3://{S3_REPORT_BUCKET}/{report_s3_key}"
        if S3_REPORT_BUCKET and self._s3:
            try:
                report_url = self._s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": S3_REPORT_BUCKET, "Key": report_s3_key},
                    ExpiresIn=PRESIGNED_URL_EXPIRY,
                )
            except Exception:
                logger.warning("presigned_url_failed", rca_id=rca_id)

        body: dict = {
            "rca_id": rca_id,
            "alarm_name": alarm_name,
            "root_cause": root_cause,
            "report_url": report_url,
            "engine": ENGINE,
            "elapsed_seconds": elapsed_seconds,
            "confirmed": confirmed,
            "notification_id": notification_id,
        }
        if alarm_context:
            body["alarm_context"] = {
                "alarm_name": alarm_context.alarm_name,
                "namespace": alarm_context.namespace,
                "metric_name": alarm_context.metric_name,
                "threshold": alarm_context.threshold,
            }
        if playbook:
            body["playbook"] = {
                "playbook_id": playbook.get("playbook_id", ""),
                "failure_type": playbook.get("failure_type", ""),
                "symptom_pattern": playbook.get("symptom_pattern", ""),
                "verification_steps": playbook.get("verification_steps", []),
                "temporary_mitigation": playbook.get("temporary_mitigation", ""),
                "permanent_remediation": playbook.get("permanent_remediation", ""),
            }

        publish_args = {
            "TopicArn": SNS_NOTIFICATION_TOPIC_ARN,
            "Subject": f"[RCA] {alarm_name} — Analysis Complete ({ENGINE})",
            "Message": json.dumps(body),
            "MessageAttributes": {
                "event_type": {
                    "DataType": "String",
                    "StringValue": "cc_headless_complete",
                },
                "notification_id": {
                    "DataType": "String",
                    "StringValue": notification_id,
                },
            },
        }
        if SNS_NOTIFICATION_TOPIC_ARN.endswith(".fifo"):
            publish_args.update(
                MessageGroupId=f"{ENGINE}-completion",
                MessageDeduplicationId=notification_id,
            )

        self._sns.publish(
            **publish_args,
        )
