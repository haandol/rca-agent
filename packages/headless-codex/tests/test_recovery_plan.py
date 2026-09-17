"""Native observation proof precedes complete server-built early rollback eligibility."""

import copy

from test_recovery_evidence import _prepare
from test_recovery_evidence import compatible as compatible

from headless_codex.services.execution_contract import validate_steps
from headless_codex.services.recovery_plan import build_recovery_plan

pytest_plugins = ["recovery_fixture"]


def test_server_builds_complete_plan_only_from_verified_current_and_input_evidence(compatible):
    reader, alarm, baseline, publish, *_ = compatible
    accounting = copy.deepcopy(baseline["observations"][0])
    accounting["event_id"] = "normal-accounting"
    accounting["message"].update(
        event="write_accounting",
        metric_namespace=baseline["metrics"]["attempts"]["namespace"],
        service_name=baseline["metrics"]["attempts"]["dimensions"]["ServiceName"],
        attempt_metric="attempts",
        failure_metric="failures",
        attempt_semantics="completed_successful_rows_plus_failed_rows",
        failure_semantics="failed_rows",
        cancellation_semantics="excluded_from_completed_counters",
        success_evidence_event="write_completed",
        success_count_field="count",
        success_semantics="committed_rows",
    )
    baseline["observations"].append(accounting)
    publish()
    scoped, prepared = _prepare(compatible)
    before = copy.deepcopy(prepared)
    result = build_recovery_plan("rca", {"AlarmName": scoped.raw_alarm.alarm_name}, prepared)
    assert result["approval_status"] == "READY", result
    book = result["playbook"]
    steps = validate_steps(book)
    assert len(steps) == 5 and book["verification_status"] == "DRAFT"
    assert (
        steps[1]["ecs_service_precondition"]["expected_task_definition"]
        == prepared["context"]["current"]["task_definition_arn"]
    )
    assert steps[2]["deployment_wait"]["task_definition"] == prepared["context"]["normal"]["task_definition_arn"]
    assert scoped.raw_alarm.alarm_name in steps[-1]["success_criteria"]
    assert prepared == before


def test_missing_server_compatibility_never_yields_commands():
    result = build_recovery_plan(
        "rca", {"AlarmName": "alarm"}, {"context": {"model": "claim"}, "verification": {"status": "UNAVAILABLE"}}
    )
    assert result["approval_status"] == "UNAVAILABLE" and not result.get("playbook")
