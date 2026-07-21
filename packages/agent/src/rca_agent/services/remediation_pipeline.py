from __future__ import annotations

import logging
import time
import uuid

from rca_agent.config.aws_sdk import (
    PUBLICATION_LEASE_SECONDS,
    PUBLICATION_MARK_ATTEMPTS,
    PUBLICATION_MARK_RETRY_DELAY_SECONDS,
)
from rca_agent.ports.dto.models import (
    FaultType,
    RcaReport,
    RcaSessionState,
    RemediationContext,
    RemediationResult,
    VerificationResult,
    VerificationStatus,
)
from rca_agent.services.remediation import execute_remediation
from rca_agent.services.verification import run_verification

logger = logging.getLogger(__name__)


class RemediationPublicationContendedError(RuntimeError):
    pass


def _report_from_context(context: RemediationContext) -> RcaReport:
    return RcaReport(
        rca_id=context.rca_id,
        incident_summary=context.root_cause,
        root_cause=context.validated_root_cause,
        root_cause_confirmed=True,
        confidence_score=1.0,
        evidence_list=[context.evidence_summary],
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
            self._flush_remediation_notification(rca_id)
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
        if context.validated_fault_type == FaultType.UNSUPPORTED or context.validated_fault_type != context.fault_type:
            raise RuntimeError(f"RCA {rca_id} has no supported validated fault type")

        claim_token = c.session_store.claim_remediation(rca_id)
        if claim_token is None:
            current = c.session_store.get_remediation_context(rca_id)
            if current is not None and current.remediation_status == "COMPLETED":
                self._flush_remediation_notification(rca_id)
                return None
            raise RuntimeError(f"Remediation already in progress for {rca_id}")

        report = _report_from_context(context)
        try:
            remediation_result = self._run_remediation(
                report,
                context.validated_fault_type,
            )
            remediation_time = time.time()

            verification_result = self._run_verification(
                report=report,
                context=context,
                remediation_result=remediation_result,
                remediation_time=remediation_time,
            )

            notification = _build_remediation_notification(
                report,
                remediation_result,
                verification_result,
            )
            if not c.session_store.complete_remediation(
                rca_id,
                claim_token,
                remediation_result,
                verification_result,
                notification,
            ):
                raise RuntimeError(f"Failed to complete remediation claim for {rca_id}")
        except Exception as exc:
            released = c.session_store.release_remediation(
                rca_id,
                claim_token,
                error_reason=str(exc),
            )
            if not released:
                logger.error("Failed to release remediation claim for %s", rca_id)
            raise
        self._flush_remediation_notification(rca_id)
        return remediation_result

    def _flush_remediation_notification(self, rca_id: str) -> None:
        c = self._container
        handoff = c.session_store.get_remediation_handoff(rca_id)
        if handoff is None or handoff.remediation_status != "COMPLETED" or handoff.notification is None:
            raise RuntimeError(f"Persisted remediation notification is unavailable for {rca_id}")
        if handoff.publication_status == "SENT":
            logger.info("Skipping published remediation notification for %s", rca_id)
            return

        publication_claim = c.session_store.claim_remediation_publication(
            rca_id,
            lease_seconds=PUBLICATION_LEASE_SECONDS,
        )
        if publication_claim is None:
            raise RemediationPublicationContendedError(
                f"Remediation notification publication already in progress for {rca_id}"
            )

        try:
            sent = c.notification.send(handoff.notification)
        except Exception:
            if not c.session_store.release_remediation_publication(rca_id, publication_claim):
                logger.error("Failed to release remediation publication claim for %s", rca_id)
            raise
        if not sent:
            if not c.session_store.release_remediation_publication(rca_id, publication_claim):
                logger.error("Failed to release remediation publication claim for %s", rca_id)
            raise RuntimeError(f"Failed to publish remediation result for {rca_id}")

        for attempt in range(PUBLICATION_MARK_ATTEMPTS):
            try:
                marked = c.session_store.mark_remediation_published(rca_id, publication_claim)
            except Exception:
                marked = False
                logger.exception(
                    "Failed to mark remediation notification published for %s (attempt %d)",
                    rca_id,
                    attempt + 1,
                )
            if marked:
                return
            if attempt < PUBLICATION_MARK_ATTEMPTS - 1:
                time.sleep(PUBLICATION_MARK_RETRY_DELAY_SECONDS)
        raise RuntimeError(f"Published remediation result but failed to persist SENT status for {rca_id}")

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
        context: RemediationContext,
        remediation_result: RemediationResult,
        remediation_time: float,
    ) -> VerificationResult | None:
        c = self._container
        if not any(a.executed for a in remediation_result.actions_taken):
            logger.info("No action executed for %s, skipping verification", report.rca_id)
            return None

        try:
            agent = c.verification_agent
        except Exception as exc:
            logger.warning(
                "Optional verification agent is unavailable for %s: %s",
                report.rca_id,
                exc,
            )
            agent = None

        try:
            cloudwatch_client = c.cloudwatch_client_for_region(context.region)
        except Exception as exc:
            logger.warning(
                "CloudWatch client is unavailable for %s: %s",
                report.rca_id,
                exc,
            )
            cloudwatch_client = None

        result = run_verification(
            cloudwatch_client=cloudwatch_client,
            agent=agent,
            alarm_name=context.alarm_name,
            remediation=remediation_result,
            remediation_time=remediation_time,
        )
        logger.info(
            "Verification for %s: status=%s",
            report.rca_id,
            result.status.value,
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
    if verification is not None and verification.status is not VerificationStatus.NORMALIZED:
        summary = f"{summary}; Verification {verification.status.value.lower()}: {verification.verification_summary}"
        severity = "high"

    return NotificationMessage(
        rca_id=report.rca_id,
        publication_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"remediation-publication:{report.rca_id}")),
        root_cause_summary=summary,
        severity=severity,
        confirmed=report.root_cause_confirmed,
        verification_status=(verification.status if verification is not None else VerificationStatus.PENDING),
        event_type="remediation_complete",
    )
