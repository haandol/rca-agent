"""Exercise the orchestrator's real private-stage storage path with deterministic role outputs."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from rca_agent.ports.dto.models import AlarmPayload, Hypothesis, HypothesisCategory, RcaReport, ScopingResult
from rca_agent.services import analysis_workflow
from rca_agent.services.pipeline import PipelineOrchestrator, RunContext
from rca_agent.utils.agent_invocation import InvocationStoppedError, invocation_scope
from tests.test_analysis_parts import part_store as part_store


def wired_pipeline(part_store, monkeypatch):
    """Replace model boundaries while retaining real orchestration, S3 bytes and DynamoDB conditions."""
    alarm = AlarmPayload(alarm_name="fault")
    scoping = ScopingResult(alarm_summary="frozen fault", raw_alarm=alarm)
    container = SimpleNamespace(
        analysis_part_store=part_store,
        session_store=Mock(),
        source_artifacts=[],
        recovery_agent=object(),
        report_agent=object(),
        code_preview_agent=object(),
        operations_agent=object(),
        incident_observer=None,
        playbook_store=Mock(),
        report_store=Mock(save=Mock(return_value="reports/final.md")),
    )
    orchestrator = PipelineOrchestrator(container)
    run = RunContext(rca_id="rca-1", claim_token="claim", attempt=1, trace=MagicMock(), start_time=time.monotonic())
    monkeypatch.setattr(orchestrator, "_run_scoping", Mock(return_value=scoping))
    hypothesis = Hypothesis(
        hypothesis_id="h1", description="actual cause", category=HypothesisCategory.DEPLOYMENT, confidence_score=0.93
    )
    monkeypatch.setattr(orchestrator, "_run_hypothesis_generation", Mock(return_value=[hypothesis]))
    state = SimpleNamespace(
        hypotheses=[hypothesis],
        termination=None,
        all_judgments=[],
        evidence_map={},
        rejected_descriptions=[],
        timeline=[],
    )
    monkeypatch.setattr(orchestrator, "_run_validation_loop", Mock(return_value=state))
    monkeypatch.setattr(orchestrator, "_finalize_hypotheses", Mock(return_value=(hypothesis, True)))
    monkeypatch.setattr(
        analysis_workflow,
        "run_report_generation",
        Mock(
            return_value=RcaReport(
                rca_id="temporary",
                incident_summary="root summary",
                root_cause="actual cause",
                root_cause_confirmed=True,
                confidence_score=0.93,
                selected_hypothesis_id="h1",
            )
        ),
    )
    monkeypatch.setattr(orchestrator, "_flush_completion_handoff", Mock(return_value=True))
    from rca_agent.ports.dto.models import Playbook

    public = Playbook(playbook_id="public-knowledge", rca_id="rca-1", failure_type="known", symptom_pattern="observed")
    monkeypatch.setattr(orchestrator, "_run_playbook", Mock(return_value=(public, "span", public)))
    return orchestrator, container, alarm, run


@pytest.mark.parametrize("execution", ["PENDING_APPROVAL", "RUNNING", "UNRESOLVED", "RESOLVED"])
def test_pipeline_publishes_recovery_then_root_then_operations_without_execution_gate(
    part_store, monkeypatch, execution
):
    """Every execution outcome leaves the logical analysis order and immutable early result unchanged."""
    orchestrator, container, alarm, run = wired_pipeline(part_store, monkeypatch)
    container.execution_state = execution
    order = []

    def root(alarm_arg, scoping_arg, hypotheses, run_arg):
        """Observe the early publication before any root inference proceeds."""
        order.append("root")
        assert part_store.read_part("rca-1", "recovery")["record"]["status"] == "COMPLETED"
        assert part_store._get("rca-1", "ANALYSIS#SESSION")["state"] != "COMPLETED"
        assert scoping_arg.alarm_summary == "frozen fault"
        return SimpleNamespace(
            hypotheses=hypotheses,
            termination=None,
            all_judgments=[],
            evidence_map={},
            rejected_descriptions=[],
            timeline=[],
        )

    def operations(incident, root_result, agent):
        """Require durable root output while the overall analysis is still active."""
        order.append("operations")
        assert part_store.read_part("rca-1", "root_cause")["record"]["status"] == "COMPLETED"
        assert incident["scoping"]["alarm_summary"] == "frozen fault"
        assert root_result["result"]["root_cause"]["confirmed"] is True
        assert part_store._get("rca-1", "ANALYSIS#SESSION")["state"] != "COMPLETED"
        return {"title": "prevention", "summary": "proposed only", "findings": [], "recommendations": []}

    monkeypatch.setattr(orchestrator, "_run_validation_loop", root)
    monkeypatch.setattr(analysis_workflow, "generate_operations", operations)
    assert orchestrator._run_pipeline_in_context(alarm, run)
    assert order == ["root", "operations"]
    assert part_store._get("rca-1", "ANALYSIS#SESSION")["state"] == "COMPLETED"
    assert part_store.read_part("rca-1", "recovery")["record"]["approval_status"] == "UNAVAILABLE"
    orchestrator._run_playbook.assert_called_once()
    assert orchestrator._run_playbook.call_args.kwargs == {"frozen_incident": True, "knowledge_only": True}
    container.report_store.save.assert_called_once()
    container.report_store.save_vectors.assert_called_once()
    assert set(container.report_store.save.call_args.args[0].analysis_part_refs) == {
        "recovery",
        "root_cause",
        "operations",
    }


def test_root_failure_is_published_before_operations_and_preserves_recovery(part_store, monkeypatch):
    """A failed root agent does not undo the early part or suppress prevention work."""
    orchestrator, container, alarm, run = wired_pipeline(part_store, monkeypatch)
    orchestrator._run_hypothesis_generation.side_effect = ValueError("model failed")

    def operations(incident, root_result, agent):
        """Receive the explicit failed result without pretending a root cause was confirmed."""
        assert root_result["status"] == "FAILED"
        assert part_store.read_part("rca-1", "root_cause")["payload"]["status"] == "FAILED"
        return {"title": "limited review", "summary": "root evidence incomplete"}

    monkeypatch.setattr(analysis_workflow, "generate_operations", operations)
    assert orchestrator._run_pipeline_in_context(alarm, run)
    assert part_store._get("rca-1", "ANALYSIS#SESSION")["state"] == "FAILED"
    assert part_store.read_part("rca-1", "recovery")["record"]["status"] == "COMPLETED"
    assert part_store.read_part("rca-1", "operations")["record"]["status"] == "COMPLETED"


def test_redelivery_reuses_original_incident_and_completed_recovery(part_store, monkeypatch):
    """Stopping after early publication cannot cause a rollback's new state to replace incident input."""
    orchestrator, container, alarm, run = wired_pipeline(part_store, monkeypatch)
    orchestrator._run_hypothesis_generation.side_effect = InvocationStoppedError("stop")
    with pytest.raises(InvocationStoppedError):
        orchestrator._run_pipeline_in_context(alarm, run)
    original = part_store.read_incident("rca-1")
    recovery = part_store.read_part("rca-1", "recovery")
    orchestrator._run_scoping.side_effect = AssertionError("must not recollect after rollback")
    orchestrator._run_hypothesis_generation.side_effect = None
    monkeypatch.setattr(analysis_workflow, "prepare_recovery_evidence", Mock(side_effect=AssertionError("no refreeze")))
    monkeypatch.setattr(analysis_workflow, "generate_operations", Mock(return_value={"title": "ops", "summary": "ops"}))
    assert orchestrator._run_pipeline_in_context(alarm, run)
    assert part_store.read_incident("rca-1") == original
    assert part_store.read_part("rca-1", "recovery") == recovery


def test_exhausted_budget_publishes_skips_without_starting_agents(part_store, monkeypatch):
    """An exhausted shared budget records unavailable stages instead of granting three fresh budgets."""
    orchestrator, container, alarm, run = wired_pipeline(part_store, monkeypatch)
    with invocation_scope(admission_deadline=time.monotonic() - 1):
        assert orchestrator._run_pipeline_in_context(alarm, run)
    for part in ("recovery", "root_cause", "operations"):
        result = part_store.read_part("rca-1", part)
        assert result["record"]["status"] == "SKIPPED"
        assert "started_at" not in result["record"]
    orchestrator._run_hypothesis_generation.assert_not_called()


def test_frozen_alarm_keeps_original_unknown_fields_and_trigger(part_store, monkeypatch):
    """Early approval can recover exact original alarm metadata rather than a lossy typed projection."""
    from dataclasses import replace

    orchestrator, container, _, run = wired_pipeline(part_store, monkeypatch)
    raw = {
        "AlarmName": "original",
        "AlarmDescription": "source details",
        "NewStateValue": "ALARM",
        "StateChangeTime": "2026-09-17T01:00:00Z",
        "NewStateReason": "source reason",
        "Region": "us-east-1",
        "UnknownProvenance": {"retained": True},
        "Trigger": {
            "MetricName": "Failures",
            "Namespace": "App",
            "Dimensions": [{"name": "ServiceName", "value": "app"}],
            "UnknownTrigger": "keep",
        },
    }
    alarm = AlarmPayload.from_cloudwatch_sns(raw)
    orchestrator._run_scoping.return_value = ScopingResult(alarm_summary="frozen", raw_alarm=alarm)
    run = replace(run, alarm_data=raw)
    monkeypatch.setattr(analysis_workflow, "generate_operations", Mock(return_value={"summary": "ops"}))
    assert orchestrator._run_pipeline_in_context(alarm, run)
    assert part_store.read_incident("rca-1")["payload"]["alarm"] == raw


def test_production_root_keeps_live_collection_flag_and_passes_captured_source(part_store, monkeypatch):
    """Production additional reads remain enabled, while received source bytes feed the real preview boundary."""
    orchestrator, container, alarm, run = wired_pipeline(part_store, monkeypatch)
    captured = {"path": "app.py", "text": "x=1\n", "sha256": "test", "source_ref": "received"}

    def root(alarm_arg, scope_arg, hypotheses, run_arg):
        """Assert the model-eval-only bypass remains unset during production root collection."""
        assert orchestrator._precollected_evidence is None
        return SimpleNamespace(
            hypotheses=hypotheses,
            termination=None,
            all_judgments=[],
            evidence_map={},
            rejected_descriptions=[],
            timeline=[],
            source_artifacts=[captured],
        )

    def preview(report, incident, agent):
        """Check captured production source reaches preview generation separately from the immutable incident."""
        assert incident["source_artifacts"] == [captured]
        return {"status": "UNAVAILABLE", "files": [], "limitations": ["test only"]}

    monkeypatch.setattr(orchestrator, "_run_validation_loop", root)
    monkeypatch.setattr(analysis_workflow, "generate_code_preview", preview)
    monkeypatch.setattr(analysis_workflow, "generate_operations", Mock(return_value={"summary": "ops"}))
    assert orchestrator._run_pipeline_in_context(alarm, run)
    assert part_store.read_incident("rca-1")["payload"]["source_artifacts"] == []
    assert part_store.read_part("rca-1", "root_cause")["payload"]["result"]["source_artifacts"] == [captured]


def test_branching_path_full_report_and_final_metadata_are_preserved(part_store, monkeypatch):
    """The new stages retain parent ancestry, report detail and validated fault metadata through final storage."""
    from rca_agent.ports.dto.models import FaultType

    orchestrator, container, alarm, run = wired_pipeline(part_store, monkeypatch)
    parent = Hypothesis(
        hypothesis_id="parent",
        description="parent mechanism",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=0.6,
        tree_id="tree",
    )
    child = Hypothesis(
        hypothesis_id="child",
        parent_id="parent",
        description="selected mechanism",
        category=HypothesisCategory.DEPENDENCY,
        confidence_score=0.95,
        tree_id="tree",
        validated_fault_type=FaultType.SLOW_QUERY,
    )
    state = SimpleNamespace(
        hypotheses=[parent, child],
        termination=None,
        all_judgments=[],
        evidence_map={},
        rejected_descriptions=["rejected other"],
        timeline=["actual timeline"],
    )
    monkeypatch.setattr(orchestrator, "_run_validation_loop", Mock(return_value=state))
    monkeypatch.setattr(orchestrator, "_finalize_hypotheses", Mock(return_value=(child, True)))

    def report(scope, selected, confirmed, path, evidence, rejected, timeline, agent):
        """Use the actual ancestry provided to report generation and preserve all original sections."""
        assert path == ["parent mechanism", "selected mechanism"]
        return RcaReport(
            rca_id="temporary",
            incident_summary="incident",
            severity="critical",
            root_cause=selected.description,
            root_cause_confirmed=confirmed,
            confidence_score=0.95,
            selected_hypothesis_id=selected.hypothesis_id,
            hypothesis_path=path,
            impact_summary="real impact",
            detection_method="original detection",
            timeline=timeline,
            five_whys=["evidenced why"],
            temporary_mitigation="original mitigation",
            permanent_remediation="original fix",
            action_items=["original action"],
            lessons_learned="original lesson",
            rejected_hypotheses=rejected,
        )

    monkeypatch.setattr(analysis_workflow, "run_report_generation", report)
    monkeypatch.setattr(
        analysis_workflow, "generate_operations", Mock(return_value={"summary": "ops", "recommendations": []})
    )
    assert orchestrator._run_pipeline_in_context(alarm, run)
    saved = container.report_store.save.call_args.args[0]
    for field, expected in {
        "severity": "critical",
        "impact_summary": "real impact",
        "detection_method": "original detection",
        "timeline": ["actual timeline"],
        "five_whys": ["evidenced why"],
        "temporary_mitigation": "original mitigation",
        "permanent_remediation": "original fix",
        "lessons_learned": "original lesson",
    }.items():
        assert getattr(saved, field) == expected
    assert saved.hypothesis_path == ["parent mechanism", "selected mechanism"]
    parent_record = part_store._get("rca-1", "ANALYSIS#SESSION")
    assert parent_record["severity"] == "critical" and parent_record["fault_type"] == "SLOW_QUERY"
    assert parent_record["report_s3_key"] == "reports/final.md"
    assert parent_record["playbook_id"] == "public-knowledge"
    import json

    assert json.loads(parent_record["completion_notification"])["severity"] == "critical"


def test_three_part_caller_retains_actual_final_generation_archive_report_and_publication(part_store, monkeypatch):
    """Exercise the existing finalizer and archive caller after all parts; never publish the private early plan."""
    from rca_agent.ports.dto.models import Playbook
    from rca_agent.services import pipeline
    from tests.test_comparison_archive_integration import thin_copy

    orchestrator, container, alarm, run = wired_pipeline(part_store, monkeypatch)
    order = []
    public = Playbook(
        playbook_id="final-public",
        rca_id="rca-1",
        failure_type="cause",
        symptom_pattern="symptom",
        comparison={
            "comparison_id": "comparison",
            "status": "NO_MATCH",
            "selected_playbook_id": "",
            "inputs": {},
            "candidates": [],
        },
    )
    container.playbook_agent = object()
    run.trace.start_span.return_value = SimpleNamespace(span_id="public-span")

    def generate(report, agent, **kwargs):
        """A distinct final knowledge draft starts only after all role outputs are durable."""
        assert all(part_store.read_part("rca-1", part)["payload"] for part in ("recovery", "root_cause", "operations"))
        assert part_store._get("rca-1", "ANALYSIS#SESSION")["state"] != "COMPLETED"
        assert kwargs["incident_observer"] is None
        order.append("generate")
        return public

    def archive(book, rca_id, engine):
        """Retain the existing comparison archive validation boundary and thin reference."""
        order.append("archive")
        return thin_copy(book, rca_id, engine)

    def save(report, **kwargs):
        """The final report preserves both comparison and durable three-part lineage."""
        order.append("report")
        assert kwargs["playbook"].comparison["comparison_id"] == "comparison"
        assert set(report.analysis_part_refs) == {"recovery", "root_cause", "operations"}
        assert report.evidence_list == []
        return "reports/final.md"

    def publish(book, **kwargs):
        """Only final public knowledge is indexed after completion authority is committed."""
        order.append("publish")
        assert book.playbook_id == "final-public"
        assert part_store._get("rca-1", "ANALYSIS#SESSION")["state"] == "COMPLETED"
        return True

    monkeypatch.setattr(pipeline, "run_playbook_generation", generate)
    monkeypatch.setattr(orchestrator, "_run_playbook", PipelineOrchestrator._run_playbook.__get__(orchestrator))
    monkeypatch.setattr(
        orchestrator, "_flush_completion_handoff", PipelineOrchestrator._flush_completion_handoff.__get__(orchestrator)
    )
    container.playbook_store.archive_comparison.side_effect = archive
    container.playbook_store.save.side_effect = publish
    container.report_store.save.side_effect = save
    container.notification = Mock(send=Mock(return_value=True))
    container.session_store.mark_playbook_indexed.return_value = True
    container.session_store.mark_completion_notified.return_value = True
    monkeypatch.setattr(analysis_workflow, "generate_operations", Mock(return_value={"summary": "ops"}))
    assert orchestrator._run_pipeline_in_context(alarm, run)
    assert order == ["generate", "archive", "report", "publish"]
    container.report_store.save_vectors.assert_called_once()


@pytest.mark.parametrize("initial_rollout", ["COMPLETED", "IN_PROGRESS"])
@pytest.mark.parametrize("change", ["none", "settings", "target", "image", "unstable", "cancel", "claim"])
def test_ready_recovery_is_durable_before_root_and_remains_distinct_from_final_public_book(
    part_store, monkeypatch, compatible, initial_rollout, change
):
    """Fresh controls gate early publication without replacing the incident or bypassing ownership."""
    from copy import deepcopy

    from botocore.exceptions import ClientError

    from rca_agent.services import analysis_roles
    from tests.test_deployment_baseline import observed_plan

    orchestrator, container, _, run = wired_pipeline(part_store, monkeypatch)
    scope, context, steps = observed_plan(compatible)
    reader, alarm, _, _, _, logs, ecs = compatible
    service = ecs.describe_services.return_value["services"][0]
    service["deployments"][0]["rolloutState"] = initial_rollout
    scope.incident_observations = reader.observe(alarm, timeout_seconds=90)
    original = deepcopy(scope.incident_observations.model_dump(mode="json"))
    log_calls = logs.filter_log_events.call_count
    service["deployments"][0]["rolloutState"] = "COMPLETED"
    if change == "settings":
        service["deploymentConfiguration"] = {"deploymentCircuitBreaker": {"enable": True, "rollback": True}}
    elif change == "target":
        service["deployments"][0]["id"] = "ecs-svc/foreign"
    elif change == "image":
        previous_tasks = ecs.describe_tasks.side_effect

        def changed_tasks(**kwargs):
            """Return a different live image while preserving all other task metadata."""
            response = deepcopy(previous_tasks(**kwargs))
            response["tasks"][0]["containers"][0]["imageDigest"] = "sha256:" + "c" * 64
            return response

        ecs.describe_tasks.side_effect = changed_tasks
    elif change == "unstable":
        service["deployments"][0]["rolloutState"] = "IN_PROGRESS"
    frozen_before_refresh = []

    def refresh(*args, **kwargs):
        """Use the real metadata-only reader and simulate changes during its bounded request."""
        frozen_before_refresh.append(part_store.read_incident("rca-1"))
        assert kwargs == {"timeout_seconds": 300}
        result = reader.refresh_current(*args, **kwargs)
        if change == "cancel":
            run.trace.check_cancelled.side_effect = InvocationStoppedError("cancelled during refresh")
        elif change == "claim":
            part_store.ddb.update_item(
                TableName="parts",
                Key={"PK": {"S": "RCA#rca-1"}, "SK": {"S": "ANALYSIS#SESSION"}},
                UpdateExpression="SET claim_token = :claim",
                ExpressionAttributeValues={":claim": {"S": "new-owner"}},
            )
        return result

    container.incident_observer = SimpleNamespace(refresh_current=Mock(side_effect=refresh))
    steps[-1]["success_criteria"] = (
        steps[-1]["metric_wait"]["failure_alarm_name"]
        + " OK; "
        + steps[-1]["metric_wait"]["metrics"]["failures"]["metric_name"]
        + " zero; committed writes positive"
    )
    orchestrator._run_scoping.return_value = scope

    def recovery_model(agent, prompt, output_model, timeout):
        """Run the real observed-plan output validator without a paid model call."""
        return output_model.model_validate(
            {
                "title": "early rollback",
                "summary": "verified return",
                "reason": "normal input proof",
                "recommendation": "ROLLBACK",
                "playbook": {
                    "failure_type": "unconfirmed incident",
                    "symptom_pattern": "failed writes",
                    "execution_steps": steps,
                },
            }
        )

    model = Mock(side_effect=recovery_model)
    monkeypatch.setattr(analysis_roles, "invoke_agent", model)
    original_loop = orchestrator._run_validation_loop

    def root(alarm, scoping, hypotheses, context_run):
        """Observe READY before root result exists, without changing its immutable playbook later."""
        recovery = part_store.read_part("rca-1", "recovery")
        assert recovery["record"]["approval_status"] == ("READY" if change == "none" else "UNAVAILABLE")
        if change == "none":
            assert recovery["payload"]["result"]["playbook"]["rollback_context"] == context
        assert part_store.read_part("rca-1", "root_cause")["payload"] is None
        return original_loop(alarm, scoping, hypotheses, context_run)

    monkeypatch.setattr(orchestrator, "_run_validation_loop", root)
    monkeypatch.setattr(analysis_workflow, "generate_operations", Mock(return_value={"summary": "ops"}))
    if change in {"cancel", "claim"}:
        with pytest.raises(InvocationStoppedError if change == "cancel" else ClientError):
            orchestrator._run_pipeline_in_context(scope.raw_alarm, run)
        assert part_store.read_part("rca-1", "recovery")["payload"] is None
    else:
        assert orchestrator._run_pipeline_in_context(scope.raw_alarm, run)
    assert len(frozen_before_refresh) == 1
    assert part_store.read_incident("rca-1") == frozen_before_refresh[0]
    assert scope.incident_observations.model_dump(mode="json") == original
    assert logs.filter_log_events.call_count == log_calls
    if change in {"cancel", "claim"}:
        return
    recovery = part_store.read_part("rca-1", "recovery")
    result = recovery["payload"]["result"]
    if change == "none":
        assert recovery["record"]["approval_status"] == "READY"
        assert result["playbook"]["playbook_id"] != "public-knowledge"
        control = result["verification"]["current_control"]
        assert control["observed_at"] > original["current"]["observed_at"]
        assert control["deployments"][0]["rolloutState"] == "COMPLETED"
        assert control["service_settings"] == original["current"]["service_settings"]
        assert "observations" not in control
        assert result["verification"]["witnesses"]
    else:
        assert result["verification"]["valid"] is False
        assert result["playbook"] is None
        model.assert_not_called()
    assert part_store._get("rca-1", "ANALYSIS#SESSION")["playbook_id"] == "public-knowledge"


pytest_plugins = ["tests.test_recovery_evidence"]


def test_final_three_part_markdown_separates_proposals_from_evidence_and_late_commands(part_store, monkeypatch):
    """The durable report uses explicit role templates and never labels a late model plan as current approval."""
    from rca_agent.adapters.secondary.report.s3_report_store import _render_markdown
    from rca_agent.ports.dto.models import ExecutionStep, Playbook

    orchestrator, container, alarm, run = wired_pipeline(part_store, monkeypatch)
    monkeypatch.setattr(
        analysis_workflow,
        "generate_operations",
        Mock(
            return_value={
                "summary": "ops proposal",
                "recommendations": [
                    {
                        "title": "schema lint",
                        "description": "proposal",
                        "validation_status": "NOT_RUN",
                        "check": "schema check",
                        "failure_condition": "mismatch",
                        "verification_plan": "run locally",
                    }
                ],
            }
        ),
    )
    assert orchestrator._run_pipeline_in_context(alarm, run)
    report = container.report_store.save.call_args.args[0]
    late = Playbook(
        playbook_id="late",
        rca_id="rca-1",
        failure_type="knowledge",
        symptom_pattern="symptom",
        execution_steps=[
            ExecutionStep(step_id="late", action="late", success_criteria="late", commands=["LATE_COMMAND_SENTINEL"])
        ],
    )
    rendered = _render_markdown(report, late)
    assert "## 1. 신속 복구" in rendered and "## 2. 근본 원인" in rendered and "## 3. 운영 예방" in rendered
    assert "schema lint" in rendered and "NOT_RUN" in rendered and "## 산출물 manifest" in rendered
    assert "LATE_COMMAND_SENTINEL" not in rendered
    assert not report.evidence_list
    assert "analysis_parts" not in report.model_dump()
    assert set(report.analysis_part_refs) == {"recovery", "root_cause", "operations"}


def test_part_store_capability_accepts_alternate_adapter_and_missing_configuration_fails(part_store, monkeypatch):
    """A nonconcrete local adapter runs the new workflow; an invalid adapter cannot silently choose legacy RCA-first."""
    orchestrator, container, alarm, run = wired_pipeline(part_store, monkeypatch)

    class Adapter:
        """Delegate the declared capability without inheriting the AWS implementation."""

        def __getattr__(self, name):
            """Expose the same port methods while preserving the real local transaction behavior."""
            return getattr(part_store, name)

    container.analysis_part_store = Adapter()
    monkeypatch.setattr(analysis_workflow, "generate_operations", Mock(return_value={"summary": "ops"}))
    assert orchestrator._run_pipeline_in_context(alarm, run)
    container.analysis_part_store = object()
    with pytest.raises(AttributeError):
        orchestrator._run_pipeline_in_context(alarm, run)
