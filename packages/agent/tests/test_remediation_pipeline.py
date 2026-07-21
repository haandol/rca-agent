from unittest.mock import Mock, PropertyMock

import pytest

from rca_agent.ports.dto.models import (
    FaultType,
    RcaSessionState,
    RemediationAction,
    RemediationContext,
    RemediationHandoff,
    RemediationResult,
    VerificationResult,
    VerificationStatus,
)
from rca_agent.services import remediation_pipeline
from rca_agent.services.remediation_pipeline import (
    RemediationOrchestrator,
    RemediationPublicationContendedError,
)


def _notification(**overrides) -> dict:
    base = {
        "rca_id": "rca-1",
        "root_cause_summary": "db connection pool exhausted",
        "root_cause": "database connection pool exhausted",
        "confirmed": True,
        "alarm_context": {
            "alarm_name": "RcaAgentDev-Healthcare-RdsHighConnections",
            "namespace": "AWS/RDS",
            "metric_name": "DatabaseConnections",
            "threshold": 30.0,
        },
    }
    base.update(overrides)
    return base


def _context(**overrides) -> RemediationContext:
    data = {
        "rca_id": "rca-1",
        "state": RcaSessionState.COMPLETED,
        "alarm_name": "Session-RdsHighConnections",
        "region": "ap-northeast-2",
        "root_cause": "database connection pool exhausted",
        "confirmed": True,
        "selected_hypothesis_id": "h-selected",
        "fault_type": FaultType.DB_CONNECTION_LEAK,
        "validated_fault_type": FaultType.DB_CONNECTION_LEAK,
        "validated_root_cause": "database connection pool exhausted",
        "evidence_summary": "DatabaseConnections exceeded the pool limit",
    }
    data.update(overrides)
    return RemediationContext(**data)


class _Container:
    def __init__(self, context=None):
        self.notification = Mock()
        self.notification.send.return_value = True
        self.verification_agent = Mock()
        self.cloudwatch_client = Mock()
        self.cloudwatch_client_for_region = Mock(return_value=self.cloudwatch_client)
        self.healthcare_service_host = "healthcare.local"
        self.session_store = Mock()
        self.context = context or _context()
        self.session_store.get_remediation_context.return_value = self.context
        self.session_store.claim_remediation.return_value = "claim-1"
        self.session_store.release_remediation.return_value = True
        self.session_store.claim_remediation_publication.return_value = "publication-claim-1"
        self.session_store.release_remediation_publication.return_value = True
        self.handoff = None
        self.session_store.get_remediation_handoff.side_effect = lambda _rca_id: self.handoff

        def complete_remediation(_rca_id, _claim_token, result, verification, notification):
            self.context.remediation_status = "COMPLETED"
            self.handoff = RemediationHandoff(
                rca_id=self.context.rca_id,
                remediation_status="COMPLETED",
                publication_status="PENDING",
                notification=notification,
                result=result,
                verification=verification,
            )
            return True

        def mark_remediation_published(_rca_id, _publication_claim_token):
            self.handoff.publication_status = "SENT"
            return True

        self.session_store.complete_remediation.side_effect = complete_remediation
        self.session_store.mark_remediation_published.side_effect = mark_remediation_published


def test_unconfirmed_rca_is_not_auto_remediated():
    container = _Container(_context(confirmed=False))
    orch = RemediationOrchestrator(container)

    result = orch.process_notification(_notification(confirmed=True))

    assert result is None
    container.session_store.claim_remediation.assert_not_called()


def test_confirmed_rca_executes_remediation_and_notifies(monkeypatch):
    executed = RemediationResult(
        rca_id="rca-1",
        actions_taken=[
            RemediationAction(
                action_type="fault_reset_api",
                description="reset",
                executed=True,
                success=True,
            )
        ],
        overall_success=True,
        summary="[SUCCESS] reset",
    )
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", Mock(return_value=executed))
    run_verification = Mock(
        return_value=VerificationResult(
            rca_id="rca-1",
            status=VerificationStatus.NORMALIZED,
            verification_summary="normalized",
        )
    )
    monkeypatch.setattr(remediation_pipeline, "run_verification", run_verification)

    container = _Container()
    orch = RemediationOrchestrator(container)
    result = orch.process_notification(_notification())

    assert result is executed
    # 확정 원인 → 복구 실행 후 검증까지 수행
    run_verification.assert_called_once()
    assert run_verification.call_args.kwargs["cloudwatch_client"] is container.cloudwatch_client
    container.cloudwatch_client_for_region.assert_called_once_with("ap-northeast-2")
    assert run_verification.call_args.kwargs["alarm_name"] == "Session-RdsHighConnections"
    # 복구 결과 알림은 remediation_complete 로 발행되어 큐로 되돌아오지 않는다
    sent = container.notification.send.call_args[0][0]
    assert sent.event_type == "remediation_complete"
    assert sent.verification_status is VerificationStatus.NORMALIZED
    assert sent.severity == "medium"
    container.session_store.complete_remediation.assert_called_once_with(
        "rca-1",
        "claim-1",
        executed,
        run_verification.return_value,
        sent,
    )
    container.session_store.claim_remediation_publication.assert_called_once_with(
        "rca-1",
        lease_seconds=remediation_pipeline.PUBLICATION_LEASE_SECONDS,
    )


def test_verification_skipped_when_no_action_executed(monkeypatch):
    skipped = RemediationResult(
        rca_id="rca-1",
        actions_taken=[RemediationAction(action_type="no_action", description="none", executed=False)],
        overall_success=False,
        summary="[SKIPPED] none",
    )
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", Mock(return_value=skipped))
    run_verification = Mock()
    monkeypatch.setattr(remediation_pipeline, "run_verification", run_verification)

    orch = RemediationOrchestrator(_Container())
    orch.process_notification(_notification())

    run_verification.assert_not_called()
    container = orch._container
    sent = container.notification.send.call_args.args[0]
    container.session_store.complete_remediation.assert_called_once_with(
        "rca-1",
        "claim-1",
        skipped,
        None,
        sent,
    )
    assert sent.verification_status is VerificationStatus.PENDING
    assert sent.severity == "high"


def test_raw_alarm_context_cannot_override_session_alarm_binding(monkeypatch):
    executed = RemediationResult(
        rca_id="rca-1",
        actions_taken=[
            RemediationAction(
                action_type="fault_reset_api",
                description="reset database connections",
                executed=True,
                success=True,
            )
        ],
        overall_success=True,
        summary="[SUCCESS] reset database connections",
    )
    execute = Mock(return_value=executed)

    def evaluate_session_alarm(**kwargs):
        kwargs["cloudwatch_client"].describe_alarms(
            AlarmNames=[kwargs["alarm_name"]],
        )
        return VerificationResult(
            rca_id="rca-1",
            status=VerificationStatus.NORMALIZED,
            verification_summary="normalized",
        )

    verify = Mock(side_effect=evaluate_session_alarm)
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", execute)
    monkeypatch.setattr(remediation_pipeline, "run_verification", verify)

    container = _Container()
    RemediationOrchestrator(container).process_notification(
        _notification(
            alarm_context={
                "alarm_name": "Other-Normal-Alarm",
                "namespace": "AWS/ECS",
                "metric_name": "CPUUtilization",
                "threshold": 95.0,
            }
        )
    )

    action_report = execute.call_args.kwargs["report"]
    assert action_report.root_cause == "database connection pool exhausted"
    assert set(execute.call_args.kwargs) == {"report", "fault_type", "service_host"}
    assert execute.call_args.kwargs["fault_type"] == FaultType.DB_CONNECTION_LEAK

    container.cloudwatch_client_for_region.assert_called_once_with("ap-northeast-2")
    assert verify.call_args.kwargs["cloudwatch_client"] is container.cloudwatch_client
    assert verify.call_args.kwargs["alarm_name"] == "Session-RdsHighConnections"
    container.cloudwatch_client.describe_alarms.assert_called_once_with(
        AlarmNames=["Session-RdsHighConnections"],
    )


def test_verification_failure_is_reflected_in_final_notification(monkeypatch):
    executed = RemediationResult(
        rca_id="rca-1",
        actions_taken=[
            RemediationAction(
                action_type="fault_reset_api",
                description="reset",
                executed=True,
                success=True,
            )
        ],
        overall_success=True,
        summary="[SUCCESS] reset",
    )
    verification = VerificationResult(
        rca_id="rca-1",
        status=VerificationStatus.FAILED,
        verification_summary="Metric remains above threshold",
        remaining_issues=["DatabaseConnections remains high"],
    )
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", Mock(return_value=executed))
    monkeypatch.setattr(remediation_pipeline, "run_verification", Mock(return_value=verification))

    container = _Container()
    RemediationOrchestrator(container).process_notification(_notification())

    sent = container.notification.send.call_args.args[0]
    assert sent.severity == "high"
    assert sent.verification_status is VerificationStatus.FAILED
    assert "verification" in sent.root_cause_summary.lower()
    assert "metric remains above threshold" in sent.root_cause_summary.lower()


def test_pending_verification_keeps_final_notification_high_severity(monkeypatch):
    executed = RemediationResult(
        rca_id="rca-1",
        actions_taken=[
            RemediationAction(
                action_type="fault_reset_api",
                description="reset",
                executed=True,
                success=True,
            )
        ],
        overall_success=True,
        summary="[SUCCESS] reset",
    )
    verification = VerificationResult(
        rca_id="rca-1",
        status=VerificationStatus.PENDING,
        verification_summary="Insufficient CloudWatch datapoints",
    )
    monkeypatch.setattr(
        remediation_pipeline,
        "execute_remediation",
        Mock(return_value=executed),
    )
    monkeypatch.setattr(
        remediation_pipeline,
        "run_verification",
        Mock(return_value=verification),
    )

    container = _Container()
    RemediationOrchestrator(container).process_notification(_notification())

    sent = container.notification.send.call_args.args[0]
    assert sent.severity == "high"
    assert sent.verification_status is VerificationStatus.PENDING
    assert "verification pending" in sent.root_cause_summary.lower()


@pytest.mark.parametrize(
    "status",
    [VerificationStatus.PENDING, VerificationStatus.NORMALIZED],
)
def test_verification_agent_initialization_failure_does_not_block_result(
    monkeypatch,
    status,
):
    executed = RemediationResult(
        rca_id="rca-1",
        actions_taken=[
            RemediationAction(
                action_type="fault_reset_api",
                description="reset",
                executed=True,
                success=True,
            )
        ],
        overall_success=True,
        summary="[SUCCESS] reset",
    )
    verification = VerificationResult(
        rca_id="rca-1",
        status=status,
        verification_summary=f"CloudWatch status is {status.value}",
    )
    verify = Mock(return_value=verification)
    monkeypatch.setattr(
        remediation_pipeline,
        "execute_remediation",
        Mock(return_value=executed),
    )
    monkeypatch.setattr(remediation_pipeline, "run_verification", verify)

    container = _Container()
    monkeypatch.setattr(
        type(container),
        "verification_agent",
        PropertyMock(side_effect=RuntimeError("model initialization failed")),
        raising=False,
    )

    result = RemediationOrchestrator(container).process_notification(_notification())

    assert result is executed
    assert verify.call_args.kwargs["agent"] is None
    sent = container.notification.send.call_args.args[0]
    assert sent.verification_status is status
    container.session_store.complete_remediation.assert_called_once_with(
        "rca-1",
        "claim-1",
        executed,
        verification,
        sent,
    )
    container.session_store.release_remediation.assert_not_called()


def test_cloudwatch_client_initialization_failure_is_persisted_as_pending(
    monkeypatch,
):
    executed = RemediationResult(
        rca_id="rca-1",
        actions_taken=[
            RemediationAction(
                action_type="fault_reset_api",
                description="reset",
                executed=True,
                success=True,
            )
        ],
        overall_success=True,
        summary="[SUCCESS] reset",
    )
    pending = VerificationResult(
        rca_id="rca-1",
        status=VerificationStatus.PENDING,
        verification_summary="CloudWatch client is unavailable.",
    )
    verify = Mock(return_value=pending)
    monkeypatch.setattr(
        remediation_pipeline,
        "execute_remediation",
        Mock(return_value=executed),
    )
    monkeypatch.setattr(remediation_pipeline, "run_verification", verify)

    container = _Container()
    container.cloudwatch_client_for_region.side_effect = RuntimeError("client initialization failed")

    result = RemediationOrchestrator(container).process_notification(_notification())

    assert result is executed
    assert verify.call_args.kwargs["cloudwatch_client"] is None
    sent = container.notification.send.call_args.args[0]
    assert sent.verification_status is VerificationStatus.PENDING
    container.session_store.complete_remediation.assert_called_once_with(
        "rca-1",
        "claim-1",
        executed,
        pending,
        sent,
    )


def test_duplicate_remediation_notification_is_idempotent(monkeypatch):
    executed = RemediationResult(
        rca_id="rca-1",
        actions_taken=[
            RemediationAction(
                action_type="fault_reset_api",
                description="reset",
                executed=True,
                success=True,
            )
        ],
        overall_success=True,
        summary="[SUCCESS] reset",
    )
    execute = Mock(return_value=executed)
    verify = Mock(
        return_value=VerificationResult(
            rca_id="rca-1",
            status=VerificationStatus.NORMALIZED,
            verification_summary="normalized",
        )
    )
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", execute)
    monkeypatch.setattr(remediation_pipeline, "run_verification", verify)

    container = _Container()
    orchestrator = RemediationOrchestrator(container)
    notification = _notification()
    orchestrator.process_notification(notification)
    orchestrator.process_notification(notification)

    execute.assert_called_once()
    verify.assert_called_once()
    container.notification.send.assert_called_once()


def test_publish_exception_releases_publication_claim_without_releasing_completed_remediation(
    monkeypatch,
):
    executed = RemediationResult(
        rca_id="rca-1",
        actions_taken=[],
        overall_success=False,
        summary="no action",
    )
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", Mock(return_value=executed))
    container = _Container()
    container.notification.send.side_effect = RuntimeError("SNS unavailable")

    with pytest.raises(RuntimeError, match="SNS unavailable"):
        RemediationOrchestrator(container).process_notification(_notification())

    container.session_store.release_remediation_publication.assert_called_once_with(
        "rca-1",
        "publication-claim-1",
    )
    container.session_store.release_remediation.assert_not_called()


def test_contended_publication_claim_is_retryable_and_does_not_publish():
    container = _Container()
    notification = remediation_pipeline._build_remediation_notification(
        remediation_pipeline._report_from_context(container.context),
        RemediationResult(rca_id="rca-1", summary="persisted"),
    )
    container.context.remediation_status = "COMPLETED"
    container.handoff = RemediationHandoff(
        rca_id="rca-1",
        remediation_status="COMPLETED",
        publication_status="PUBLISHING",
        notification=notification,
    )
    container.session_store.claim_remediation_publication.return_value = None

    with pytest.raises(RemediationPublicationContendedError, match="already in progress"):
        RemediationOrchestrator(container).process_notification(_notification())

    container.notification.send.assert_not_called()
    container.session_store.release_remediation.assert_not_called()


def test_event_root_cause_and_confirmation_do_not_override_authoritative_context(
    monkeypatch,
):
    execute = Mock(
        return_value=RemediationResult(
            rca_id="rca-1",
            actions_taken=[],
            overall_success=False,
            summary="no action",
        )
    )
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", execute)

    container = _Container()
    RemediationOrchestrator(container).process_notification(
        _notification(
            root_cause="high CPU utilization",
            root_cause_summary="high CPU utilization",
            confirmed=False,
            playbook={
                "playbook_id": "untrusted",
                "failure_type": "high memory",
                "symptom_pattern": "memory pressure",
            },
        )
    )

    report = execute.call_args.kwargs["report"]
    assert report.root_cause == "database connection pool exhausted"
    assert report.root_cause_confirmed is True
    assert "playbook" not in execute.call_args.kwargs


def test_missing_confirmed_evidence_fails_before_claim():
    container = _Container(_context(evidence_summary=""))

    with pytest.raises(RuntimeError, match="no validated root cause evidence"):
        RemediationOrchestrator(container).process_notification(_notification())

    container.session_store.claim_remediation.assert_not_called()


@pytest.mark.parametrize(
    "context",
    [
        _context(
            fault_type=FaultType.DB_CONNECTION_LEAK,
            validated_fault_type=FaultType.HIGH_CPU,
        ),
        _context(validated_fault_type=FaultType.UNSUPPORTED),
    ],
)
def test_mismatched_or_missing_validated_type_fails_before_claim_and_reset(
    monkeypatch,
    context,
):
    execute = Mock()
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", execute)
    container = _Container(context)

    with pytest.raises(RuntimeError, match="no supported validated fault type"):
        RemediationOrchestrator(container).process_notification(_notification())

    container.session_store.claim_remediation.assert_not_called()
    execute.assert_not_called()


def test_independently_validated_high_cpu_is_the_only_executed_type(monkeypatch):
    executed = RemediationResult(
        rca_id="rca-1",
        actions_taken=[],
        overall_success=False,
        summary="captured",
    )
    execute = Mock(return_value=executed)
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", execute)
    container = _Container(
        _context(
            root_cause="initial DB_CONNECTION_LEAK suggestion",
            fault_type=FaultType.HIGH_CPU,
            validated_fault_type=FaultType.HIGH_CPU,
            validated_root_cause="sustained CPU saturation",
            evidence_summary="CPUUtilization remained above 95%",
        )
    )

    RemediationOrchestrator(container).process_notification(_notification())

    assert execute.call_args.kwargs["fault_type"] == FaultType.HIGH_CPU


def test_processing_exception_releases_claim_and_propagates(monkeypatch):
    monkeypatch.setattr(
        remediation_pipeline,
        "execute_remediation",
        Mock(side_effect=RuntimeError("reset failed")),
    )
    container = _Container()

    with pytest.raises(RuntimeError, match="reset failed"):
        RemediationOrchestrator(container).process_notification(_notification())

    container.session_store.release_remediation.assert_called_once_with(
        "rca-1",
        "claim-1",
        error_reason="reset failed",
    )
    container.session_store.complete_remediation.assert_not_called()
