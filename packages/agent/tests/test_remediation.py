from unittest.mock import Mock

from rca_agent.ports.dto.models import RcaReport
from rca_agent.services import remediation


def _report(root_cause: str) -> RcaReport:
    return RcaReport(
        rca_id="rca-1",
        incident_summary="test incident",
        root_cause=root_cause,
        confidence_score=0.9,
    )


def test_no_matching_action_is_not_reported_as_success():
    result = remediation.execute_remediation(
        report=_report("unknown failure"),
        playbook=None,
        service_host="healthcare",
    )

    assert result.overall_success is False
    assert result.actions_taken[0].action_type == "no_action"
    assert result.actions_taken[0].executed is False
    assert "[SKIPPED]" in result.summary


def test_success_requires_at_least_one_successful_execution(monkeypatch):
    monkeypatch.setattr(remediation, "_call_fault_reset", Mock(return_value=(True, "ok")))

    result = remediation.execute_remediation(
        report=_report("database connection pool exhausted"),
        playbook=None,
        service_host="healthcare",
    )

    assert result.overall_success is True
    assert result.actions_taken[0].executed is True
    assert result.actions_taken[0].success is True
