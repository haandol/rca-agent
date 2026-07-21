from unittest.mock import Mock

import pytest

from rca_agent.ports.dto.models import FaultType, RcaReport
from rca_agent.prompts.remediation import (
    REMEDIATION_SYSTEM_PROMPT,
    REMEDIATION_USER_PROMPT_TEMPLATE,
)
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
        fault_type=FaultType.DB_CONNECTION_LEAK,
        service_host="healthcare",
    )

    assert result.overall_success is True
    assert result.actions_taken[0].executed is True
    assert result.actions_taken[0].success is True


@pytest.mark.parametrize(
    ("fault_type", "endpoint"),
    [
        (FaultType.DB_CONNECTION_LEAK, "/fault/db-leak/reset"),
        (FaultType.HIGH_CPU, "/fault/high-cpu/reset"),
        (FaultType.HIGH_MEMORY, "/fault/high-memory/reset"),
        (FaultType.SLOW_QUERY, "/fault/slow-query/reset"),
    ],
)
def test_supported_fault_types_map_to_targeted_reset(fault_type, endpoint):
    assert remediation._determine_reset_endpoint(fault_type) == endpoint


def test_targeted_fault_reset_uses_allowlisted_endpoint(monkeypatch):
    reset = Mock(return_value=(True, "ok"))
    monkeypatch.setattr(remediation, "_call_fault_reset", reset)

    result = remediation.execute_remediation(
        report=_report("high CPU utilization"),
        fault_type=FaultType.HIGH_CPU,
        service_host="healthcare",
    )

    reset.assert_called_once_with("healthcare", "/fault/high-cpu/reset")
    assert not hasattr(remediation, "_force_ecs_deployment")
    assert result.overall_success is True


def test_unsupported_cause_does_not_force_ecs_deployment():
    result = remediation.execute_remediation(
        report=_report("upstream certificate validation failure"),
        service_host="healthcare",
    )

    assert result.overall_success is False
    assert result.actions_taken[0].action_type == "no_action"


def test_root_cause_keywords_cannot_select_an_action():
    result = remediation.execute_remediation(
        report=_report("database leak, high CPU, memory pressure, and slow query"),
        fault_type=FaultType.UNSUPPORTED,
        service_host="healthcare",
    )

    assert result.actions_taken[0].action_type == "no_action"
    assert result.actions_taken[0].executed is False


def test_remediation_prompt_exposes_only_allowlisted_reset_actions():
    prompt = REMEDIATION_SYSTEM_PROMPT + REMEDIATION_USER_PROMPT_TEMPLATE

    assert "ECS" not in prompt
    assert "deployment" not in prompt.lower()
    assert "infrastructure changes" in prompt
    assert "fail closed" in prompt.lower()
    assert "/fault/db-leak/reset" in prompt
    assert "/fault/high-cpu/reset" in prompt
    assert "/fault/high-memory/reset" in prompt
    assert "/fault/slow-query/reset" in prompt
