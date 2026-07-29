import json
from unittest.mock import Mock

import pytest

from rca_agent.ports.dto.models import FaultType, RcaReport
from rca_agent.services import remediation


class FakeHttpResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.limit: int | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit: int) -> bytes:
        self.limit = limit
        return self.body[:limit]


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
    "body",
    [
        {"leaked_connections": 0},
        {"status": None},
        {"status": "stopped"},
        {"status": "not_running"},
    ],
)
def test_call_fault_reset_accepts_valid_success_responses(monkeypatch, body):
    response = FakeHttpResponse(json.dumps(body).encode())
    urlopen = Mock(return_value=response)
    monkeypatch.setattr(remediation.urllib.request, "urlopen", urlopen)

    success, response_text = remediation._call_fault_reset("healthcare", "/fault/db-leak/reset")

    assert success is True
    assert json.loads(response_text) == body
    assert response.limit == remediation._MAX_RESET_RESPONSE_BYTES + 1
    request = urlopen.call_args.args[0]
    assert request.full_url == "http://healthcare:8000/fault/db-leak/reset"


@pytest.mark.parametrize(
    "reset_status",
    ["stop_timeout", "already_running", "failed", "unknown"],
)
def test_call_fault_reset_rejects_non_success_statuses(monkeypatch, reset_status):
    response = FakeHttpResponse(json.dumps({"status": reset_status}).encode())
    monkeypatch.setattr(remediation.urllib.request, "urlopen", Mock(return_value=response))

    success, error = remediation._call_fault_reset("healthcare", "/fault/high-cpu/reset")

    assert success is False
    assert error == f"non-success status: {reset_status}"


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (b"\xff", "response body is not valid JSON"),
        (b"not-json", "response body is not valid JSON"),
        (b"[]", "response JSON must be an object"),
        (b"{" + b"x" * (64 * 1024), "response body exceeds size limit"),
    ],
)
def test_call_fault_reset_rejects_invalid_response_bodies(monkeypatch, body, expected_error):
    response = FakeHttpResponse(body)
    monkeypatch.setattr(remediation.urllib.request, "urlopen", Mock(return_value=response))

    success, error = remediation._call_fault_reset("healthcare", "/fault/high-memory/reset")

    assert success is False
    assert error == expected_error


def test_call_fault_reset_returns_network_error(monkeypatch):
    monkeypatch.setattr(
        remediation.urllib.request,
        "urlopen",
        Mock(side_effect=OSError("connection refused")),
    )

    success, error = remediation._call_fault_reset("healthcare", "/fault/slow-query/reset")

    assert success is False
    assert error == "connection refused"


def test_reset_response_error_marks_action_and_overall_result_failed(monkeypatch):
    monkeypatch.setattr(
        remediation,
        "_call_fault_reset",
        Mock(return_value=(False, "non-success status: stop_timeout")),
    )

    result = remediation.execute_remediation(
        report=_report("high CPU utilization"),
        fault_type=FaultType.HIGH_CPU,
        service_host="healthcare",
    )

    assert result.overall_success is False
    assert result.actions_taken[0].executed is True
    assert result.actions_taken[0].success is False
    assert result.actions_taken[0].error == "non-success status: stop_timeout"
    assert "[FAILED]" in result.summary


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


def test_reset_allowlist_is_closed_over_the_four_injected_fault_types():
    assert remediation._RESET_ENDPOINTS == {
        FaultType.DB_CONNECTION_LEAK: "/fault/db-leak/reset",
        FaultType.HIGH_CPU: "/fault/high-cpu/reset",
        FaultType.HIGH_MEMORY: "/fault/high-memory/reset",
        FaultType.SLOW_QUERY: "/fault/slow-query/reset",
    }
    assert remediation._determine_reset_endpoint(FaultType.UNSUPPORTED) is None
