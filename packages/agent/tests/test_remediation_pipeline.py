from unittest.mock import Mock

import pytest

from rca_agent.ports.dto.models import (
    FaultType,
    RcaSessionState,
    RemediationAction,
    RemediationContext,
    RemediationResult,
    VerificationResult,
)
from rca_agent.services import remediation_pipeline
from rca_agent.services.remediation_pipeline import (
    RemediationOrchestrator,
    _alarm_for_verification,
    _parse_alarm_context,
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
        "root_cause": "database connection pool exhausted",
        "confirmed": True,
        "selected_hypothesis_id": "h-selected",
        "fault_type": FaultType.DB_CONNECTION_LEAK,
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
        self.healthcare_service_host = "healthcare.local"
        self.session_store = Mock()
        self.context = context or _context()
        self.session_store.get_remediation_context.return_value = self.context
        self.session_store.claim_remediation.return_value = "claim-1"
        self.session_store.release_remediation.return_value = True

        def complete_remediation(*_args):
            self.context.remediation_status = "COMPLETED"
            return True

        self.session_store.complete_remediation.side_effect = complete_remediation


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
            metrics_normalized=True,
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
    # 복구 결과 알림은 remediation_complete 로 발행되어 큐로 되돌아오지 않는다
    sent = container.notification.send.call_args[0][0]
    assert sent.event_type == "remediation_complete"
    container.session_store.complete_remediation.assert_called_once_with(
        "rca-1",
        "claim-1",
        executed,
        run_verification.return_value,
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
    orch._container.session_store.complete_remediation.assert_called_once_with(
        "rca-1",
        "claim-1",
        skipped,
        None,
    )


def test_alarm_context_round_trips_into_verification_payload():
    ctx = _parse_alarm_context(_notification())
    assert ctx is not None
    assert ctx.metric_name == "DatabaseConnections"

    alarm = _alarm_for_verification(ctx)
    assert alarm.trigger is not None
    assert alarm.trigger.namespace == "AWS/RDS"
    assert alarm.trigger.threshold == 30.0


def test_alarm_context_only_affects_read_only_verification(monkeypatch):
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
    verify = Mock(
        return_value=VerificationResult(
            rca_id="rca-1",
            metrics_normalized=True,
            verification_summary="normalized",
        )
    )
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", execute)
    monkeypatch.setattr(remediation_pipeline, "run_verification", verify)

    RemediationOrchestrator(_Container()).process_notification(
        _notification(
            alarm_context={
                "alarm_name": "UntrustedHighCpuAlarm",
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

    verification_alarm = verify.call_args.kwargs["alarm"]
    assert verification_alarm.trigger is not None
    assert verification_alarm.trigger.metric_name == "CPUUtilization"
    assert verification_alarm.trigger.namespace == "AWS/ECS"


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
        metrics_normalized=False,
        verification_summary="Metric remains above threshold",
        remaining_issues=["DatabaseConnections remains high"],
    )
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", Mock(return_value=executed))
    monkeypatch.setattr(remediation_pipeline, "run_verification", Mock(return_value=verification))

    container = _Container()
    RemediationOrchestrator(container).process_notification(_notification())

    sent = container.notification.send.call_args.args[0]
    assert sent.severity == "high"
    assert "verification" in sent.root_cause_summary.lower()
    assert "metric remains above threshold" in sent.root_cause_summary.lower()


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
            metrics_normalized=True,
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
