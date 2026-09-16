import hashlib
import json

import pytest

from headless_codex.services.execution_evidence import (
    MAX_OUTPUT_CHARS,
    capture_command_output,
    capture_ecs_control_output,
    redact,
)
from headless_codex.services.execution_outcome import _captured_output

pytest_plugins = ["test_service_deployment"]


def test_public_rollback_and_write_recovery_succeed_with_large_service_history(plan, runtime, metric_data, monkeypatch):
    """The real public execution flow must not confuse event-history previews with control loss."""
    from test_deployment_recovery_proof import (
        test_verified_normal_producer_accounting_resolves_full_public_flow as verified_flow,
    )

    _, ecs = runtime
    original = ecs.service

    def with_history():
        service = original()
        service["events"] = [{"message": "historical event " * 40} for _ in range(100)]
        return service

    monkeypatch.setattr(ecs, "service", with_history)
    verified_flow(plan, runtime, metric_data, monkeypatch)
    workspace, _ = runtime
    projections = [r for r in workspace.read_records() if r.get("ecs_control_projection")]
    assert projections
    assert all(not r["stdout_truncated"] for r in projections)


@pytest.mark.parametrize("field", ["services", "service"])
def test_long_service_history_does_not_remove_control_fields(field):
    service = {
        "serviceArn": "arn:aws:ecs:us-east-1:123456789012:service/cluster/service",
        "taskDefinition": "definition:2",
        "deploymentConfiguration": {
            "deploymentCircuitBreaker": {
                "enable": True,
                "rollback": True,
                "resetOnHealthyTask": True,
                "thresholdConfiguration": {"type": "BOUNDED_PERCENT", "value": 50},
            },
            "events": "a configurable nested field must not be removed",
        },
        "deployments": [{"id": "current", "rolloutState": "COMPLETED"}],
        "events": [{"message": "historical event " * 40} for _ in range(100)],
    }
    document = {field: [service] if field == "services" else service, "failures": [], "nextToken": "keep-pagination"}
    raw = json.dumps(document)
    assert capture_command_output(raw, "")["stdout_truncated"]
    captured = capture_ecs_control_output(raw, "")
    assert not captured["stdout_truncated"]
    expected = json.loads(raw)
    (expected[field][0] if field == "services" else expected[field]).pop("events")
    assert json.loads(captured["stdout"]) == expected
    assert (
        captured["ecs_control_projection"]["redacted_source_sha256"] == hashlib.sha256(redact(raw).encode()).hexdigest()
    )
    assert document[field]  # Original input remains intact.
    assert service["events"]
    assert _captured_output(captured)["ecs_control_projection"] == captured["ecs_control_projection"]


def test_large_control_configuration_still_reports_truncation():
    raw = json.dumps({"service": {"deploymentConfiguration": {"futureField": "x" * MAX_OUTPUT_CHARS}, "events": []}})
    captured = capture_ecs_control_output(raw, "")
    assert captured["stdout_truncated"]
    assert captured["stdout_omitted_chars"] > 0


@pytest.mark.parametrize("raw", ['{"services":', '["an event list requested directly"]', '{"tasks": []}'])
def test_unrecognized_or_malformed_output_is_not_reclassified_as_a_projection(raw):
    assert capture_ecs_control_output(raw, "") == capture_command_output(raw, "")


def test_projection_counts_and_fingerprint_follow_redaction():
    raw = json.dumps({"service": {"password": "PRIVATE-CANARY-KEY", "events": [{"message": "history"}]}})
    captured = capture_ecs_control_output(raw, "")
    assert "PRIVATE-CANARY-KEY" not in json.dumps(captured)
    assert captured["ecs_control_projection"]["redacted_source_chars"] == len(redact(raw))
