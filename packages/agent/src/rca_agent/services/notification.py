from __future__ import annotations

import json
import logging

from rca_agent.config.settings import S3_REPORT_BUCKET, SNS_NOTIFICATION_TOPIC_ARN
from rca_agent.ports.dto.models import (
    AlarmContext,
    AlarmPayload,
    FaultType,
    NotificationMessage,
    Playbook,
    RcaReport,
)
from rca_agent.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0


def _build_alarm_context(alarm: AlarmPayload | None) -> AlarmContext | None:
    if alarm is None:
        return None
    trigger = alarm.trigger
    return AlarmContext(
        alarm_name=alarm.alarm_name,
        region=alarm.region,
        namespace=trigger.namespace if trigger else "",
        metric_name=trigger.metric_name if trigger else "",
        dimensions=trigger.dimensions if trigger else {},
        statistic=trigger.statistic if trigger else "Average",
        period=trigger.period if trigger else 300,
        threshold=trigger.threshold if trigger else None,
        comparison_operator=trigger.comparison_operator if trigger else None,
    )


def build_notification(
    report: RcaReport,
    report_s3_key: str,
    elapsed_seconds: int,
    *,
    playbook: Playbook | None = None,
    dashboard_url: str = "",
    alarm: AlarmPayload | None = None,
    selected_hypothesis_id: str = "",
    fault_type: FaultType = FaultType.UNSUPPORTED,
) -> NotificationMessage:
    playbook_data = None
    if playbook:
        playbook_data = {
            "playbook_id": playbook.playbook_id,
            "failure_type": playbook.failure_type,
            "symptom_pattern": playbook.symptom_pattern,
            "severity_criteria": playbook.severity_criteria,
            "verification_steps": playbook.verification_steps,
            "temporary_mitigation": playbook.temporary_mitigation,
            "permanent_remediation": playbook.permanent_remediation,
            "escalation_criteria": playbook.escalation_criteria,
        }
    return NotificationMessage(
        rca_id=report.rca_id,
        root_cause_summary=(
            report.root_cause[:200]
            if report.root_cause_confirmed
            else f"Root cause unconfirmed — manual review needed. Best candidate: {report.root_cause[:100]}"
        ),
        severity="high" if report.root_cause_confirmed else "medium",
        report_s3_key=report_s3_key,
        dashboard_url=dashboard_url,
        elapsed_seconds=elapsed_seconds,
        confirmed=report.root_cause_confirmed,
        playbook=playbook_data,
        root_cause=report.root_cause,
        selected_hypothesis_id=selected_hypothesis_id,
        fault_type=fault_type,
        alarm_context=_build_alarm_context(alarm),
    )


def _generate_presigned_url(s3_client, report_s3_key: str) -> str:
    if not S3_REPORT_BUCKET or not report_s3_key or s3_client is None:
        return ""
    try:
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_REPORT_BUCKET, "Key": report_s3_key},
            ExpiresIn=86400,
        )
    except Exception:
        logger.warning("Failed to generate presigned URL, falling back to dashboard URL")
        return ""


def send_notification(
    notification: NotificationMessage,
    *,
    sns_client=None,
    s3_client=None,
    max_retries: int = _MAX_RETRIES,
    base_delay: float = _BASE_DELAY,
) -> bool:
    if not SNS_NOTIFICATION_TOPIC_ARN or sns_client is None:
        logger.info("SNS not configured, skipping notification")
        return False

    report_url = _generate_presigned_url(s3_client, notification.report_s3_key)
    if not report_url:
        report_url = notification.dashboard_url

    message_body = {
        "rca_id": notification.rca_id,
        "root_cause_summary": notification.root_cause_summary,
        "severity": notification.severity,
        "report_url": report_url,
        "elapsed_seconds": notification.elapsed_seconds,
        "confirmed": notification.confirmed,
        "selected_hypothesis_id": notification.selected_hypothesis_id,
        "fault_type": notification.fault_type.value,
        "verification_status": notification.verification_status.value,
    }
    if notification.playbook:
        message_body["playbook"] = notification.playbook

    def publish() -> bool:
        sns_client.publish(
            TopicArn=SNS_NOTIFICATION_TOPIC_ARN,
            Subject=f"RCA Complete: {notification.rca_id}",
            Message=json.dumps(message_body),
        )
        logger.info("Notification sent for RCA %s", notification.rca_id)
        return True

    result = retry_with_backoff(
        publish,
        max_retries=max_retries,
        base_delay=base_delay,
        operation="notification",
        default=False,
    )
    return bool(result)

    return False
