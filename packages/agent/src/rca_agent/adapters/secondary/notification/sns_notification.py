from __future__ import annotations

import json
import logging
import time

from rca_agent.config.settings import S3_REPORT_BUCKET, SNS_NOTIFICATION_TOPIC_ARN
from rca_agent.ports.dto.models import NotificationMessage
from rca_agent.ports.interfaces.notification import NotificationPort

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0


class SnsNotificationAdapter(NotificationPort):
    def __init__(self, sns_client=None, s3_client=None):
        self._sns = sns_client
        self._s3 = s3_client

    def generate_report_url(self, report_s3_key: str) -> str:
        if not S3_REPORT_BUCKET or not report_s3_key or self._s3 is None:
            return ""
        try:
            return self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_REPORT_BUCKET, "Key": report_s3_key},
                ExpiresIn=86400,
            )
        except Exception:
            logger.warning("Failed to generate presigned URL, falling back to dashboard URL")
            return ""

    def send(self, notification: NotificationMessage) -> bool:
        if not SNS_NOTIFICATION_TOPIC_ARN or self._sns is None:
            logger.info("SNS not configured, skipping notification")
            return False

        report_url = self.generate_report_url(notification.report_s3_key)
        if not report_url:
            report_url = notification.dashboard_url

        message_body = {
            "rca_id": notification.rca_id,
            "root_cause_summary": notification.root_cause_summary,
            "severity": notification.severity,
            "report_url": report_url,
            "elapsed_seconds": notification.elapsed_seconds,
            "confirmed": notification.confirmed,
        }
        if notification.playbook:
            message_body["playbook"] = notification.playbook

        for attempt in range(_MAX_RETRIES):
            try:
                self._sns.publish(
                    TopicArn=SNS_NOTIFICATION_TOPIC_ARN,
                    Subject=f"RCA Complete: {notification.rca_id}",
                    Message=json.dumps(message_body),
                )
                logger.info("Notification sent for RCA %s", notification.rca_id)
                return True
            except Exception:
                if attempt == _MAX_RETRIES - 1:
                    logger.exception("Failed to send notification after %d attempts", _MAX_RETRIES)
                    return False
                delay = _BASE_DELAY * (2**attempt)
                logger.warning("Notification attempt %d failed, retrying in %.1fs", attempt + 1, delay)
                time.sleep(delay)
        return False
