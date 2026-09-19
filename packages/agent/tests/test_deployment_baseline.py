from __future__ import annotations

from copy import deepcopy

import pytest

from rca_agent.ports.dto.models import Playbook, ScopingResult
from rca_agent.services.deployment_baseline import build_rollback_context
from rca_agent.services.playbook_gen import ExecutionStepOutput, build_execution_steps
from rca_agent.services.runbook_contract import SCOPE_FIELDS

pytest_plugins = ["tests.test_incident_observation"]


def observed_plan(setup_observations):
    reader, alarm, *_ = setup_observations
    scoping = ScopingResult(
        alarm_summary="observed",
        raw_alarm=alarm,
        incident_observations=reader.observe(alarm, timeout_seconds=90),
    )
    baseline = build_rollback_context(scoping)
    assert baseline is not None
    normal, fault = baseline["normal"], baseline["current"]
    mapped = {
        **baseline["scope"],
        "cluster": baseline["scope"]["cluster_arn"],
        "service": baseline["scope"]["service_arn"],
    }
    scope = {key: mapped[key] for key in SCOPE_FIELDS}
    guard = {
        **scope,
        "expected_task_definition": fault["task_definition_arn"],
        "expected_image_digest": fault["image_digest"],
        "expected_deployment_id": fault["deployment_id"],
        "service_settings": baseline["service_settings"],
    }
    steps = [
        {
            "step_id": "rollback",
            "action": "Roll back service",
            "success_criteria": "Update accepted",
            "commands": [
                f"aws ecs update-service --cluster {scope['cluster']} --service {scope['service']} "
                f"--task-definition {normal['task_definition_arn']} --region {scope['region']}",
            ],
            "ecs_service_precondition": guard,
        },
        {
            "step_id": "converge",
            "action": "Wait for deployment",
            "success_criteria": "All service tasks match",
            "deployment_wait": {
                **scope,
                "action_step_id": "rollback",
                "task_definition": normal["task_definition_arn"],
                "image_digest": normal["image_digest"],
                "max_wait_seconds": 300,
            },
        },
        {
            "step_id": "recovery",
            "action": "Observe committed writes",
            "success_criteria": "Writes recover",
            "metric_wait": {
                "deployment_step_id": "converge",
                "region": scope["region"],
                "failure_alarm_name": alarm.alarm_name,
                "metrics": scoping.incident_observations.baseline["metrics"],
                "max_wait_seconds": 300,
            },
        },
    ]
    if baseline.get("write_accounting"):
        steps[-1]["metric_wait"]["completed_work_evidence"] = {
            "record_index": "approved_context",
            "json_pointer": "/playbook/rollback_context/write_accounting",
        }
    return scoping, baseline, steps


def test_only_server_observations_provide_approval_provenance(setup_observations):
    scoping, baseline, steps = observed_plan(setup_observations)
    outputs = [ExecutionStepOutput(**step) for step in steps]
    generated = build_execution_steps(outputs, confirmed=True, scoping_result=scoping)
    assert len(generated) == 3
    assert generated[0].ecs_service_precondition == steps[0]["ecs_service_precondition"]
    assert scoping.incident_observations.baseline_verified is True
    assert baseline["current"]["deployment_id"] == "ecs-svc/2"
    assert baseline["baseline_ref"]["key"] == "baselines/run-1/normal.json"
    assert build_execution_steps(outputs, confirmed=False, scoping_result=scoping) == []
    # Model-supplied guard fields alone cannot create trusted approval provenance.
    assert build_execution_steps(outputs, confirmed=True) == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda steps: steps[0]["ecs_service_precondition"].update(expected_deployment_id="foreign"),
        lambda steps: steps[0]["ecs_service_precondition"].update(expected_image_digest="sha256:" + "0" * 64),
        lambda steps: steps[1]["deployment_wait"].update(
            task_definition="arn:aws:ecs:us-east-1:123456789012:task-definition/x:3"
        ),
        lambda steps: steps[1]["deployment_wait"].update(image_digest="sha256:" + "0" * 64),
        lambda steps: steps[1]["deployment_wait"].update(max_wait_seconds=901),
        lambda steps: steps[2]["metric_wait"].update(deployment_step_id="rollback"),
        lambda steps: steps[2]["metric_wait"].update(failure_alarm_name="other"),
        lambda steps: steps[2]["metric_wait"]["metrics"]["failures"].update(metric_name="other"),
        lambda steps: steps.pop(),
        lambda steps: steps[0].pop("ecs_service_precondition"),
    ],
)
def test_generated_plan_cannot_drift_from_actual_observations(setup_observations, mutation):
    scoping, _, steps = observed_plan(setup_observations)
    steps = deepcopy(steps)
    mutation(steps)
    assert (
        build_execution_steps(
            [ExecutionStepOutput(**step) for step in steps],
            confirmed=True,
            scoping_result=scoping,
        )
        == []
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda o: setattr(o, "baseline_verified", False),
        lambda o: o.current["service_settings"].update(desiredCount=2),
        lambda o: o.current["tasks"][0]["containers"][0].update(imageDigest="sha256:" + "a" * 64),
        lambda o: o.current["deployments"].append(deepcopy(o.current["deployments"][0])),
        lambda o: o.current["deployments"][0].update(createdAt=o.baseline["observed_at"]),
        lambda o: o.current["service_settings"].update(networkConfiguration={"unexpected": True}),
    ],
)
def test_unknown_or_changed_deployment_cannot_become_validated_proof(setup_observations, mutation):
    scoping, _, _ = observed_plan(setup_observations)
    mutation(scoping.incident_observations)
    assert build_rollback_context(scoping) is None


def test_legacy_playbook_read_does_not_infer_deployment_arguments():
    legacy = Playbook.model_validate(
        {
            "playbook_id": "old",
            "failure_type": "incident",
            "symptom_pattern": "symptom",
            "execution_steps": [{"step_id": "old", "action": "manually inspect", "success_criteria": "checked"}],
        }
    )
    assert legacy.rollback_context is None
    assert legacy.execution_steps[0].deployment_wait is None
    assert legacy.execution_steps[0].ecs_service_precondition is None


def test_reader_context_is_accepted_by_current_execution_worker(setup_observations, monkeypatch):
    from pathlib import Path

    runtime_src = Path(__file__).resolve().parents[2] / "headless-codex" / "src"
    monkeypatch.syspath_prepend(str(runtime_src))
    from headless_codex.services.service_deployment import validate_rollback_context

    _, context, steps = observed_plan(setup_observations)
    validate_rollback_context(
        {"rollback_context": context},
        steps[0]["ecs_service_precondition"],
        steps[1]["deployment_wait"],
    )


def add_normal_accounting(baseline):
    """Use the producer's real wire strings on an already proven normal task stream."""
    event = deepcopy(baseline["observations"][0])
    event["event_id"] = "normal-accounting"
    event["message"] = {
        "event": "write_accounting",
        "observed_at": event["message"]["observed_at"],
        "metric_namespace": "Sensor",
        "service_name": "sensor",
        "attempt_metric": "attempts",
        "failure_metric": "failures",
        "attempt_semantics": "completed_successful_rows_plus_failed_rows",
        "failure_semantics": "failed_rows",
        "cancellation_semantics": "excluded_from_completed_counters",
        "success_evidence_event": "write_completed",
        "success_count_field": "count",
        "success_semantics": "committed_rows",
    }
    baseline["observations"].append(event)
    return event


def test_normal_producer_descriptor_crosses_runtime_boundary(setup_observations, monkeypatch):
    """The optional descriptor derives from normal proof, not the current fault producer."""
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "headless-codex" / "src"))
    from headless_codex.services.service_deployment import validate_rollback_context

    _, _, baseline, publish, *_ = setup_observations
    add_normal_accounting(baseline)
    publish()
    _, context, steps = observed_plan(setup_observations)
    descriptor = context["write_accounting"]
    assert descriptor["image_digest"] == context["normal"]["image_digest"]
    assert descriptor["source_ref"] == "cloudwatch-logs:///ecs/sensor/ecs/app/normal-task#normal-accounting"
    validate_rollback_context(
        {"rollback_context": context}, steps[0]["ecs_service_precondition"], steps[1]["deployment_wait"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt_semantics", "started_rows"),
        ("failure_semantics", "all_errors"),
        ("cancellation_semantics", "unknown"),
        ("success_semantics", "accepted_rows"),
        ("success_evidence_event", "http_ok"),
        ("success_count_field", "attempts"),
        ("service_name", "foreign"),
        ("attempt_metric", "different"),
        ("metric_namespace", "other"),
    ],
)
def test_unknown_producer_semantics_never_create_normal_descriptor(setup_observations, field, value):
    """Unknown producer semantics remain observations and never turn into completed writes."""
    _, _, baseline, publish, *_ = setup_observations
    add_normal_accounting(baseline)["message"][field] = value
    publish()
    _, context, _ = observed_plan(setup_observations)
    assert "write_accounting" not in context


def test_extra_metric_dimensions_refuse_descriptor(setup_observations):
    """Service-only producer declarations cannot prove a second metric dimension."""
    _, alarm, baseline, publish, *_ = setup_observations
    add_normal_accounting(baseline)
    for metric in baseline["metrics"].values():
        metric["dimensions"]["Operation"] = "unknown"
    alarm.trigger.dimensions["Operation"] = "unknown"
    publish()
    _, context, _ = observed_plan(setup_observations)
    assert "write_accounting" not in context


def test_normal_accounting_cannot_replace_missing_committed_write_proof(setup_observations):
    """A declaration does not establish that the normal image actually committed writes."""
    reader, alarm, baseline, publish, *_ = setup_observations
    add_normal_accounting(baseline)
    baseline["observations"] = [e for e in baseline["observations"] if e["message"]["event"] != "write_completed"]
    publish()
    observed = reader.observe(alarm, timeout_seconds=90)
    assert not observed.baseline_verified
    assert (
        build_rollback_context(ScopingResult(alarm_summary="x", raw_alarm=alarm, incident_observations=observed))
        is None
    )


@pytest.mark.parametrize("foreign", [False, True])
def test_rollout_refresh_preserves_causal_facts_and_original_alarm_cutoff(setup_observations, foreign):
    """The same in-progress deployment may converge; a post-alarm deployment cannot gain approval."""
    from datetime import timedelta

    reader, alarm, _, _, _, logs, ecs = setup_observations
    service = ecs.describe_services.return_value["services"][0]
    service["deployments"][0]["rolloutState"] = "IN_PROGRESS"
    ecs.list_tasks.return_value["taskArns"].append("normal-task")
    original = reader.observe(alarm, timeout_seconds=90)
    scope = ScopingResult(alarm_summary="observed", raw_alarm=alarm, incident_observations=original)
    assert build_rollback_context(scope) is None
    facts = deepcopy(original.critical_facts)
    service["deployments"][0]["rolloutState"] = "COMPLETED"
    ecs.list_tasks.return_value["taskArns"].pop()
    if foreign:
        service["deployments"][0].update(id="foreign", createdAt=alarm.state_change_time + timedelta(seconds=1))
    logs.reset_mock()
    refreshed = reader.refresh_current(alarm, original, timeout_seconds=90)
    scope.incident_observations = refreshed
    assert (build_rollback_context(scope) is None) == foreign
    assert refreshed.critical_facts == facts
    assert refreshed.baseline == original.baseline
    assert original.current["deployments"][0]["rolloutState"] == "IN_PROGRESS"
    logs.filter_log_events.assert_not_called()


def test_same_id_but_post_alarm_created_time_refuses_context(setup_observations):
    """Even a repeated deployment identity cannot bypass the original alarm cutoff."""
    from datetime import timedelta

    reader, alarm, *_ = setup_observations
    scope, _, _ = observed_plan(setup_observations)
    ecs = setup_observations[-1]
    ecs.describe_services.return_value["services"][0]["deployments"][0]["createdAt"] = (
        alarm.state_change_time + timedelta(seconds=1)
    )
    scope.incident_observations = reader.refresh_current(alarm, scope.incident_observations, timeout_seconds=90)
    assert build_rollback_context(scope) is None


def test_playbook_generation_refreshes_before_draft_without_mutating_validated_scope(setup_observations):
    """The model sees refreshed deployment control state while validated incident evidence stays fixed."""
    from unittest.mock import MagicMock, patch

    from rca_agent.services.playbook_gen import run_playbook_generation

    reader, alarm, _, _, _, _, ecs = setup_observations
    service = ecs.describe_services.return_value["services"][0]
    service["deployments"][0]["rolloutState"] = "IN_PROGRESS"
    original = ScopingResult(
        alarm_summary="x", raw_alarm=alarm, incident_observations=reader.observe(alarm, timeout_seconds=90)
    )
    service["deployments"][0]["rolloutState"] = "COMPLETED"

    def draft(report, agent, deadline, scope):
        assert build_rollback_context(scope) is not None
        assert scope.incident_observations.critical_facts == original.incident_observations.critical_facts
        assert scope.raw_alarm == original.raw_alarm
        raise RuntimeError("draft reached with refreshed context")

    with (
        patch("rca_agent.services.playbook_gen._generate_draft", side_effect=draft),
        pytest.raises(RuntimeError, match="draft reached"),
    ):
        run_playbook_generation(
            MagicMock(), MagicMock(), playbook_store=MagicMock(), scoping_result=original, incident_observer=reader
        )
    assert original.incident_observations.current["deployments"][0]["rolloutState"] == "IN_PROGRESS"


def test_ecs_service_name_differs_from_verified_metric_service_name(setup_observations, monkeypatch):
    """Real ECS service identity and application metric identity remain separately bound end to end."""
    import json
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "headless-codex" / "src"))
    from headless_codex.services.command_gate import evaluate_command
    from headless_codex.services.post_action_metrics import _completed_accounting
    from headless_codex.services.service_deployment import validate_rollback_context

    _, alarm, baseline, publish, _, _, ecs = setup_observations
    ecs_name, metric_name = "RcaAgentDevHealthcare", "healthcare-sensor-app"
    baseline["scope"]["service_name"] = ecs_name
    baseline["scope"]["service_arn"] = baseline["scope"]["service_arn"].rsplit("/", 1)[0] + "/" + ecs_name
    ecs.describe_services.return_value["services"][0]["serviceArn"] = baseline["scope"]["service_arn"]
    original_describe = ecs.describe_tasks.side_effect

    def describe(**kwargs):
        """Return the ECS service's actual task group while preserving immutable image provenance."""
        response = original_describe(**kwargs)
        for task in response["tasks"]:
            task["group"] = "service:" + ecs_name
        return response

    ecs.describe_tasks.side_effect = describe
    for role, metric in baseline["metrics"].items():
        metric.update(
            namespace="Healthcare/Sensor",
            dimensions={"ServiceName": metric_name},
            metric_name={"attempts": "VitalIngestAttempts", "failures": "VitalIngestFailures"}[role],
        )
    alarm.trigger.namespace = "Healthcare/Sensor"
    alarm.trigger.dimensions = {"ServiceName": metric_name}
    alarm.trigger.metric_name = "VitalIngestFailures"
    event = add_normal_accounting(baseline)
    event["message"].update(
        service_name=metric_name,
        metric_namespace="Healthcare/Sensor",
        attempt_metric="VitalIngestAttempts",
        failure_metric="VitalIngestFailures",
    )
    publish()
    metadata = json.loads(alarm.alarm_description)
    metadata["service"] = ecs_name
    alarm.alarm_description = json.dumps(metadata)
    _, context, steps = observed_plan(setup_observations)
    assert context["scope"]["service_name"] == ecs_name
    assert context["write_accounting"]["dimensions"] == {"ServiceName": metric_name}
    plan = {"rollback_context": context, "execution_steps": steps}
    validate_rollback_context(plan, steps[0]["ecs_service_precondition"], steps[1]["deployment_wait"])
    request = {
        **steps[2]["metric_wait"],
        "completed_work_evidence": {
            "record_index": "approved_context",
            "json_pointer": "/playbook/rollback_context/write_accounting",
        },
    }
    bound = _completed_accounting(request, [], {"playbook": plan}, "exec", [], evaluate_command)
    assert bound["descriptor"]["dimensions"] == {"ServiceName": metric_name}
    request["metrics"] = deepcopy(request["metrics"])
    for metric in request["metrics"].values():
        metric["dimensions"] = {"ServiceName": ecs_name}
    with pytest.raises(ValueError, match="completed-write accounting"):
        _completed_accounting(request, [], {"playbook": plan}, "exec", [], evaluate_command)


@pytest.mark.parametrize("field,value", [("namespace", "foreign"), ("dimensions", {"ServiceName": "foreign"})])
def test_accounting_normalizer_requires_matching_attempt_failure_coordinates(setup_observations, field, value):
    """The normalizer independently refuses disagreeing metric scopes."""
    from rca_agent.services.deployment_baseline import normal_write_accounting

    _, _, baseline, *_ = setup_observations
    add_normal_accounting(baseline)
    baseline["metrics"]["failures"][field] = value
    assert normal_write_accounting(baseline) is None


@pytest.mark.parametrize("mutation", ["missing", "before", "stop_anchor", "foreign_anchor", "foreign_region"])
def test_guarded_rollback_requires_causal_recovery_even_without_scoping(setup_observations, mutation):
    """A structurally incomplete deployment plan must fail before provenance checks."""
    from rca_agent.services.runbook_contract import validate_runbook

    _, _, steps = observed_plan(setup_observations)
    if mutation == "missing":
        steps.pop()
    elif mutation == "before":
        steps.insert(1, steps.pop())
    elif mutation == "stop_anchor":
        steps[2]["metric_wait"]["action_step_id"] = steps[2]["metric_wait"].pop("deployment_step_id")
    elif mutation == "foreign_anchor":
        steps[2]["metric_wait"]["deployment_step_id"] = "other"
    else:
        steps[2]["metric_wait"]["region"] = "us-west-2"
    with pytest.raises(ValueError):
        validate_runbook(steps)
    assert build_execution_steps([ExecutionStepOutput(**step) for step in steps], confirmed=True) == []


@pytest.mark.parametrize("seconds", [900, 150])
def test_refresh_cap_uses_300_seconds_or_remaining_stage_budget(setup_observations, seconds):
    """Metadata refresh shares the generation deadline while allowing more than the old 90 seconds."""
    from unittest.mock import MagicMock, patch

    from rca_agent.services.playbook_gen import run_playbook_generation

    reader, alarm, *_ = setup_observations
    scope = ScopingResult(
        alarm_summary="local", raw_alarm=alarm, incident_observations=reader.observe(alarm, timeout_seconds=90)
    )
    with (
        patch.object(reader, "refresh_current", wraps=reader.refresh_current) as refresh,
        patch("rca_agent.services.playbook_gen._generate_draft", side_effect=RuntimeError("draft boundary")),
        pytest.raises(RuntimeError, match="draft boundary"),
    ):
        run_playbook_generation(
            MagicMock(),
            MagicMock(),
            playbook_store=MagicMock(),
            scoping_result=scope,
            incident_observer=reader,
            timeout_seconds=seconds,
        )
    allowance = refresh.call_args.kwargs["timeout_seconds"]
    assert min(300, seconds) - 1 < allowance <= min(300, seconds)


def test_completed_draft_is_preserved_without_new_search_after_admission_budget_expires():
    """A late valid model response survives; search/detail/model comparison do not restart its budget."""
    import time
    from unittest.mock import MagicMock, patch

    from rca_agent.services.playbook_gen import run_playbook_generation

    draft = Playbook(playbook_id="current", failure_type="observed", symptom_pattern="actual response")
    store, report = MagicMock(), MagicMock()
    report.model_dump.return_value = {}
    report.rca_id = "local"
    report.analysis_parts = {}

    def finish_after_budget(*args):
        time.sleep(0.03)
        return draft

    with (
        patch("rca_agent.services.playbook_gen._generate_draft", side_effect=finish_after_budget),
        patch("rca_agent.services.playbook_gen.search_existing_playbooks") as search,
    ):
        result = run_playbook_generation(report, MagicMock(), playbook_store=store, timeout_seconds=0.01)
    assert result.playbook_id == "current" and result.symptom_pattern == "actual response"
    assert result.comparison["status"] == "SEARCH_FAILED"
    search.assert_not_called()
    store.load_detail.assert_not_called()


@pytest.mark.parametrize("normal_value,current_value", [(None, "absent"), ("absent", None), (None, []), ([], None)])
def test_capacity_provider_absence_is_canonical_without_mutating_observations(
    setup_observations, normal_value, current_value
):
    """CLI null and omitted boto optional strategy both mean an absent strategy, and nothing else changes."""
    scope, _, _ = observed_plan(setup_observations)
    baseline, current = scope.incident_observations.baseline, scope.incident_observations.current
    for settings, value in ((baseline["service_settings"], normal_value), (current["service_settings"], current_value)):
        if value == "absent":
            settings.pop("capacityProviderStrategy", None)
        else:
            settings["capacityProviderStrategy"] = deepcopy(value)
    before = scope.model_dump_json()
    result = build_rollback_context(scope)
    assert result is not None
    assert result["service_settings"]["capacityProviderStrategy"] == []
    assert scope.model_dump_json() == before


def actual_deployment_configuration():
    """Retain the current ECS parser's mutable circuit-breaker fields in the comparison."""
    return {
        "maximumPercent": 200,
        "minimumHealthyPercent": 100,
        "deploymentCircuitBreaker": {
            "enable": True,
            "rollback": True,
            "resetOnHealthyTask": True,
            "thresholdConfiguration": {"type": "COUNT", "value": 3},
        },
    }


def test_capacity_null_fix_preserves_complete_current_deployment_configuration(setup_observations):
    """A newly captured matching baseline preserves all circuit-breaker data in approval provenance."""
    scope, _, _ = observed_plan(setup_observations)
    baseline, current = scope.incident_observations.baseline, scope.incident_observations.current
    baseline["service_settings"]["capacityProviderStrategy"] = None
    settings = actual_deployment_configuration()
    baseline["service_settings"]["deploymentConfiguration"] = deepcopy(settings)
    current["service_settings"]["deploymentConfiguration"] = deepcopy(settings)
    result = build_rollback_context(scope)
    assert result is not None
    assert result["service_settings"]["deploymentConfiguration"] == settings
    assert result["service_settings"]["capacityProviderStrategy"] == []
    result["service_settings"]["deploymentConfiguration"]["deploymentCircuitBreaker"]["resetOnHealthyTask"] = False
    assert baseline["service_settings"]["deploymentConfiguration"] == settings


@pytest.mark.parametrize("change", ["reset", "threshold", "old-parser-omission", "new-mutable-field", "strategy"])
def test_real_mutable_deployment_changes_still_refuse_context(setup_observations, change):
    """Null normalization must never backfill old parser output or hide real current configuration drift."""
    scope, _, _ = observed_plan(setup_observations)
    baseline, current = scope.incident_observations.baseline, scope.incident_observations.current
    baseline["service_settings"]["capacityProviderStrategy"] = None
    baseline["service_settings"]["deploymentConfiguration"] = actual_deployment_configuration()
    current["service_settings"]["deploymentConfiguration"] = actual_deployment_configuration()
    normal = baseline["service_settings"]["deploymentConfiguration"]["deploymentCircuitBreaker"]
    fault = current["service_settings"]["deploymentConfiguration"]["deploymentCircuitBreaker"]
    if change == "reset":
        fault["resetOnHealthyTask"] = False
    elif change == "threshold":
        fault["thresholdConfiguration"]["value"] = 4
    elif change == "old-parser-omission":
        normal.pop("resetOnHealthyTask")
        normal.pop("thresholdConfiguration")
    elif change == "new-mutable-field":
        fault["additionalObservedSetting"] = "different"
    else:
        current["service_settings"]["capacityProviderStrategy"] = [
            {"capacityProvider": "FARGATE", "weight": 1, "base": 0}
        ]
    before = scope.model_dump_json()
    assert build_rollback_context(scope) is None
    assert scope.model_dump_json() == before


@pytest.mark.parametrize("value", [False, "", {}, 0])
def test_capacity_provider_falsey_values_are_not_normalized_as_absence(setup_observations, value):
    """Only null/omission is canonical; falsey but supplied values are not erased."""
    scope, _, _ = observed_plan(setup_observations)
    scope.incident_observations.baseline["service_settings"]["capacityProviderStrategy"] = value
    assert build_rollback_context(scope) is None
