from unittest.mock import Mock

from rca_agent.ports.dto.models import RemediationAction, RemediationResult
from rca_agent.services import remediation_pipeline
from rca_agent.services.remediation_pipeline import (
    RemediationOrchestrator,
    _alarm_for_verification,
    _parse_alarm_context,
    _parse_playbook,
    _report_from_notification,
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


class _Container:
    def __init__(self):
        self.notification = Mock()
        self.verification_agent = Mock()
        self.healthcare_service_host = "healthcare.local"
        self.ecs_cluster_name = "cluster"
        self.ecs_service_name = "service"


def test_unconfirmed_rca_is_not_auto_remediated():
    orch = RemediationOrchestrator(_Container())

    result = orch.process_notification(_notification(confirmed=False))

    assert result is None


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
    run_verification = Mock()
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


def test_verification_skipped_when_no_action_executed(monkeypatch):
    skipped = RemediationResult(
        rca_id="rca-1",
        actions_taken=[
            RemediationAction(action_type="no_action", description="none", executed=False)
        ],
        overall_success=False,
        summary="[SKIPPED] none",
    )
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", Mock(return_value=skipped))
    run_verification = Mock()
    monkeypatch.setattr(remediation_pipeline, "run_verification", run_verification)

    orch = RemediationOrchestrator(_Container())
    orch.process_notification(_notification())

    run_verification.assert_not_called()


def test_alarm_context_round_trips_into_verification_payload():
    ctx = _parse_alarm_context(_notification())
    assert ctx is not None
    assert ctx.metric_name == "DatabaseConnections"

    alarm = _alarm_for_verification(ctx)
    assert alarm.trigger is not None
    assert alarm.trigger.namespace == "AWS/RDS"
    assert alarm.trigger.threshold == 30.0


def test_report_and_playbook_parsing_tolerates_missing_fields():
    report = _report_from_notification({"rca_id": "x", "root_cause_summary": "s"})
    assert report.rca_id == "x"
    assert report.root_cause == "s"

    assert _parse_playbook({}) is None
    assert _parse_playbook({"playbook": {"no_id": True}}) is None
