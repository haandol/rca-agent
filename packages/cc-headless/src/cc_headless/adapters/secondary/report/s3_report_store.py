from __future__ import annotations

import json

import structlog

from cc_headless.config.settings import ENGINE, PRESIGNED_URL_EXPIRY, S3_REPORT_BUCKET, SNS_NOTIFICATION_TOPIC_ARN
from cc_headless.ports.interfaces.report_store import ReportStorePort

logger = structlog.get_logger()


class S3ReportStore(ReportStorePort):
    def __init__(self, s3_client=None, sns_client=None):
        self._s3 = s3_client
        self._sns = sns_client

    def save_report(self, rca_id: str, report_markdown: str) -> str:
        key = f"reports/{ENGINE}/{rca_id}.md"
        if not S3_REPORT_BUCKET or not self._s3:
            return key
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
    ) -> None:
        if not SNS_NOTIFICATION_TOPIC_ARN or not self._sns:
            return

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

        self._sns.publish(
            TopicArn=SNS_NOTIFICATION_TOPIC_ARN,
            Subject=f"[RCA] {alarm_name} — Analysis Complete ({ENGINE})",
            Message=json.dumps(body),
        )
