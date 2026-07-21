from __future__ import annotations

import logging
import time

from rca_agent.ports.dto.models import (
    AlarmContext,
    AlarmPayload,
    AlarmTrigger,
    FaultType,
    RcaReport,
    RcaSessionState,
    RemediationContext,
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


def _report_from_context(context: RemediationContext) -> RcaReport:
    return RcaReport(
        rca_id=context.rca_id,
        incident_summary=context.root_cause,
        root_cause=context.validated_root_cause,
        root_cause_confirmed=True,
        confidence_score=1.0,
        evidence_list=[context.evidence_summary],
    )


def _alarm_for_verification(ctx: AlarmContext | None) -> AlarmPayload:
    if ctx is None:
        return AlarmPayload(alarm_name="unknown")
    trigger = None
    if ctx.metric_name:
        trigger = AlarmTrigger(
            metric_name=ctx.metric_name,
            namespace=ctx.namespace,
            dimensions=ctx.dimensions,
            statistic=ctx.statistic,
            period=ctx.period,
            threshold=ctx.threshold,
            comparison_operator=ctx.comparison_operator,
        )
    return AlarmPayload(
        alarm_name=ctx.alarm_name or "unknown",
        region=ctx.region,
        trigger=trigger,
    )


class RemediationOrchestrator:
    """Consumes RCA-completion notifications and drives execute → verify.

    A failure here never touches the RCA session state. Only confirmed root
    causes are auto-remediated; unconfirmed findings are left for humans.
    """

    def __init__(self, container):
        self._container = container

    def process_notification(self, raw: dict) -> RemediationResult | None:
        rca_id = raw.get("rca_id")
        if not isinstance(rca_id, str) or not rca_id:
            raise ValueError("Remediation notification is missing rca_id")

        c = self._container
        context = c.session_store.get_remediation_context(rca_id)
        if context is None:
            raise RuntimeError(f"Authoritative RCA session not found: {rca_id}")
        if context.remediation_status == "COMPLETED":
            logger.info("Skipping duplicate remediation for %s", rca_id)
            return None
        if context.state != RcaSessionState.COMPLETED or not context.confirmed:
            logger.info(
                "Skipping remediation for unauthorized RCA %s: state=%s confirmed=%s",
                rca_id,
                context.state,
                context.confirmed,
            )
            return None
        if not context.root_cause or not context.validated_root_cause or not context.evidence_summary:
            raise RuntimeError(f"RCA {rca_id} has no validated root cause evidence")

        claim_token = c.session_store.claim_remediation(rca_id)
        if claim_token is None:
            current = c.session_store.get_remediation_context(rca_id)
            if current is not None and current.remediation_status == "COMPLETED":
                logger.info("Skipping duplicate remediation for %s", rca_id)
                return None
            raise RuntimeError(f"Remediation already in progress for {rca_id}")

        report = _report_from_context(context)
        alarm_context = _parse_alarm_context(raw)
        try:
            remediation_result = self._run_remediation(report, context.fault_type)
            remediation_time = time.time()

            verification_result = self._run_verification(
                report=report,
                alarm_context=alarm_context,
                remediation_result=remediation_result,
                remediation_time=remediation_time,
            )

            sent = c.notification.send(
                _build_remediation_notification(
                    report,
                    remediation_result,
                    verification_result,
                ),
            )
            if not sent:
                raise RuntimeError(f"Failed to publish remediation result for {rca_id}")
            if not c.session_store.complete_remediation(
                rca_id,
                claim_token,
                remediation_result,
                verification_result,
            ):
                raise RuntimeError(f"Failed to complete remediation claim for {rca_id}")
            return remediation_result
        except Exception as exc:
            released = c.session_store.release_remediation(
                rca_id,
                claim_token,
                error_reason=str(exc),
            )
            if not released:
                logger.error("Failed to release remediation claim for %s", rca_id)
            raise

    def _run_remediation(
        self,
        report: RcaReport,
        fault_type: FaultType,
    ) -> RemediationResult:
        c = self._container
        result = execute_remediation(
            report=report,
            fault_type=fault_type,
            service_host=c.healthcare_service_host,
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


def _build_remediation_notification(
    report: RcaReport,
    result: RemediationResult,
    verification: VerificationResult | None = None,
):
    from rca_agent.ports.dto.models import NotificationMessage

    summary = f"Remediation {'succeeded' if result.overall_success else 'attempted'}: {result.summary}"
    severity = "high" if not result.overall_success else "medium"
    if verification is not None and not verification.metrics_normalized:
        summary = f"{summary}; Verification failed: {verification.verification_summary}"
        severity = "high"

    return NotificationMessage(
        rca_id=report.rca_id,
        root_cause_summary=summary,
        severity=severity,
        confirmed=report.root_cause_confirmed,
        event_type="remediation_complete",
    )
