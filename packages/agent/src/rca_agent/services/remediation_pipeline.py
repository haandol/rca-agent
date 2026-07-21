from __future__ import annotations

import logging
import time

from rca_agent.ports.dto.models import (
    AlarmContext,
    AlarmPayload,
    AlarmTrigger,
    Playbook,
    RcaReport,
    RemediationResult,
    VerificationResult,
)
from rca_agent.services.remediation import execute_remediation
from rca_agent.services.verification import run_verification

logger = logging.getLogger(__name__)


def _parse_alarm_context(raw: dict) -> AlarmContext | None:
    data = raw.get("alarm_context")
    if not isinstance(data, dict):
        return None
    try:
        return AlarmContext.model_validate(data)
    except Exception:
        logger.warning("Failed to parse alarm_context, ignoring")
        return None


def _parse_playbook(raw: dict) -> Playbook | None:
    data = raw.get("playbook")
    if not isinstance(data, dict) or not data.get("playbook_id"):
        return None
    try:
        return Playbook.model_validate(data)
    except Exception:
        logger.warning("Failed to parse playbook from notification, ignoring")
        return None


def _report_from_notification(raw: dict) -> RcaReport:
    return RcaReport(
        rca_id=raw.get("rca_id", ""),
        incident_summary=raw.get("root_cause_summary", ""),
        root_cause=raw.get("root_cause") or raw.get("root_cause_summary", ""),
        root_cause_confirmed=bool(raw.get("confirmed", False)),
        confidence_score=1.0 if raw.get("confirmed", False) else 0.5,
    )


def _alarm_for_verification(ctx: AlarmContext | None) -> AlarmPayload:
    if ctx is None:
        return AlarmPayload(alarm_name="unknown")
    trigger = None
    if ctx.metric_name:
        trigger = AlarmTrigger(
            metric_name=ctx.metric_name,
            namespace=ctx.namespace,
            threshold=ctx.threshold,
        )
    return AlarmPayload(alarm_name=ctx.alarm_name or "unknown", trigger=trigger)


class RemediationOrchestrator:
    """Consumes RCA-completion notifications and drives execute → verify.

    Analysis and remediation are separate lifecycles (ADR agent/0012): a
    failure here never touches the RCA session state. Only confirmed root
    causes are auto-remediated; unconfirmed findings are left for humans.
    """

    def __init__(self, container):
        self._container = container

    def process_notification(self, raw: dict) -> RemediationResult | None:
        rca_id = raw.get("rca_id", "")
        if not raw.get("confirmed", False):
            logger.info("Skipping remediation for unconfirmed RCA %s", rca_id)
            return None

        report = _report_from_notification(raw)
        playbook = _parse_playbook(raw)
        alarm_context = _parse_alarm_context(raw)
        c = self._container

        remediation_result = self._run_remediation(report, playbook)
        remediation_time = time.time()

        self._run_verification(
            report=report,
            alarm_context=alarm_context,
            remediation_result=remediation_result,
            remediation_time=remediation_time,
        )

        c.notification.send(
            _build_remediation_notification(report, remediation_result),
        )
        return remediation_result

    def _run_remediation(
        self,
        report: RcaReport,
        playbook: Playbook | None,
    ) -> RemediationResult:
        c = self._container
        result = execute_remediation(
            report=report,
            playbook=playbook,
            service_host=c.healthcare_service_host,
            ecs_cluster=c.ecs_cluster_name,
            ecs_service=c.ecs_service_name,
        )
        logger.info(
            "Remediation for %s: overall_success=%s, %s",
            report.rca_id,
            result.overall_success,
            result.summary,
        )
        return result

    def _run_verification(
        self,
        *,
        report: RcaReport,
        alarm_context: AlarmContext | None,
        remediation_result: RemediationResult,
        remediation_time: float,
    ) -> VerificationResult | None:
        c = self._container
        if not any(a.executed for a in remediation_result.actions_taken):
            logger.info("No action executed for %s, skipping verification", report.rca_id)
            return None

        alarm = _alarm_for_verification(alarm_context)
        result = run_verification(
            agent=c.verification_agent,
            alarm=alarm,
            remediation=remediation_result,
            remediation_time=remediation_time,
        )
        logger.info(
            "Verification for %s: normalized=%s",
            report.rca_id,
            result.metrics_normalized,
        )
        return result


def _build_remediation_notification(report: RcaReport, result: RemediationResult):
    from rca_agent.ports.dto.models import NotificationMessage

    return NotificationMessage(
        rca_id=report.rca_id,
        root_cause_summary=f"Remediation {'succeeded' if result.overall_success else 'attempted'}: {result.summary}",
        severity="high" if not result.overall_success else "medium",
        confirmed=report.root_cause_confirmed,
        event_type="remediation_complete",
    )
