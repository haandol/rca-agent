"""The model-eval adapter must delegate analysis to the shared pipeline.

These tests pin that delegation and the model-eval input contract. This path
intentionally supplies scenario observations; it does not cover deployed event
delivery or discovery from evidence sources.
"""

import json
import sys

import pytest

from rca_agent import eval_adapter
from rca_agent.ports.dto.models import (
    CompletionHandoff,
    ExecutionStep,
    NotificationMessage,
    Playbook,
    RcaSessionState,
)

SCENARIO = {
    "id": "rds-connection-pool-exhaustion",
    "executionModes": ["model-eval"],
    "alarm": {
        "name": "Healthcare-RdsHighConnections",
        "metric": "DatabaseConnections",
        "stateReason": "connection count crossed the threshold",
    },
    "observations": [
        {"id": "connection-growth", "source": "cloudwatch", "summary": "connections grew monotonically"},
    ],
}


def _notification() -> NotificationMessage:
    """Provide the selected hypothesis identity emitted by the real completion path."""
    return NotificationMessage(
        rca_id="rca-1",
        selected_hypothesis_id="selected",
        root_cause_summary="커넥션 누수",
        root_cause="세션이 반환되지 않는다",
        severity="high",
        report_s3_key="",
        playbook={
            "playbook_id": "pb-1",
            "failure_type": "DB_CONNECTION_LEAK",
            "symptom_pattern": "커넥션 증가",
            "severity_criteria": "확정 원인을 요구한다",
            "temporary_mitigation": "리셋한다",
            "permanent_remediation": "코드를 고친다",
            "escalation_criteria": "에스컬레이션한다",
            "verification_steps": ["알람을 확인한다"],
        },
    )


class _RecordingOrchestrator:
    """Stands in for the shared orchestrator and records how it was called."""

    calls: list[dict] = []
    precollected_evidence_values: list[str | None] = []
    result = True

    def __init__(self, container, shutdown_event=None, *, precollected_evidence=None) -> None:
        self.container = container
        self.precollected_evidence = precollected_evidence
        type(self).precollected_evidence_values.append(precollected_evidence)

    def process_alarm(self, body, *, receive_count=1, message_id=None) -> bool:
        type(self).calls.append({"body": body, "receive_count": receive_count, "message_id": message_id})
        return type(self).result


class _Store:
    def __init__(self, handoff) -> None:
        self.handoff = handoff
        self.requested: list[str] = []

    def get_completion_handoff(self, rca_id: str):
        self.requested.append(rca_id)
        if self.handoff is None:
            return None
        handoff = self.handoff.model_copy(deep=True)
        handoff.rca_id = rca_id
        if handoff.notification is not None:
            handoff.notification.rca_id = rca_id
        if handoff.playbook is not None:
            handoff.playbook.rca_id = rca_id
        return handoff


class _PlaybookStore:
    """Mutable library reads must never supply this completed evaluation's procedure."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def load_detail(self, match):
        self.requested.append(match.playbook_id)
        raise AssertionError("evaluation must read the completed run's snapshot")


def _completed_playbook() -> Playbook:
    """Keep the original fixture procedure in its persisted completion snapshot."""
    return Playbook(
        rca_id="rca-1",
        playbook_id="pb-1",
        failure_type="DB_CONNECTION_LEAK",
        symptom_pattern="커넥션 증가",
        execution_steps=[
            ExecutionStep(
                step_id="step-1",
                intent="커넥션 회수",
                action="api 서비스를 강제 재배포한다",
                success_criteria="DatabaseConnections 가 20 이하",
            )
        ],
    )


class _Container:
    instances: list["_Container"] = []
    handoff: CompletionHandoff | None = None

    def __init__(self, queue_url, *, poll_wait_seconds=20) -> None:
        self.queue_url = queue_url
        self.session_store = _Store(_Container.handoff)
        self.playbook_store = _PlaybookStore()
        self.s3_client = None
        self.dynamodb_client = None
        _Container.instances.append(self)


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the adapter fixture with a persisted, explicitly selected cause."""
    _RecordingOrchestrator.calls = []
    _RecordingOrchestrator.precollected_evidence_values = []
    _RecordingOrchestrator.result = True
    _Container.instances = []
    monkeypatch.setattr(
        eval_adapter,
        "_validation_hypotheses",
        lambda _container, _notification: [
            {
                "hypothesis_id": "selected",
                "status": "CONFIRMED",
                "validated_fault_type": "DB_CONNECTION_LEAK",
                "judgment_reasoning": "[connection-growth] confirms the connection leak.",
                "validation_evidence_summary": "",
            }
        ],
    )
    _Container.handoff = CompletionHandoff(
        rca_id="rca-1",
        state=RcaSessionState.COMPLETED,
        notification=_notification(),
        playbook=_completed_playbook(),
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rca_agent.services.pipeline.PipelineOrchestrator", _RecordingOrchestrator)
    monkeypatch.setattr("rca_agent.di.eval_container.EvalAppContainer", _Container)


def _run(capsys) -> dict:
    eval_adapter.main(["rca-agent-eval", ""])
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def stdin_scenario(monkeypatch: pytest.MonkeyPatch):
    def _set(scenario) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(scenario)))

    return _set


def test_adapter_invokes_the_shared_orchestrator(wired, stdin_scenario, capsys) -> None:
    stdin_scenario(SCENARIO)

    _run(capsys)

    assert len(_RecordingOrchestrator.calls) == 1


def test_adapter_hands_the_alarm_envelope_to_the_pipeline(wired, stdin_scenario, capsys) -> None:
    stdin_scenario(SCENARIO)

    _run(capsys)

    body = _RecordingOrchestrator.calls[0]["body"]
    assert body["AlarmName"] == "Healthcare-RdsHighConnections"
    assert body["NewStateValue"] == "ALARM"
    assert "connection-growth" in body["NewStateReason"]


def test_adapter_injects_scenario_observations_as_precollected_evidence(
    wired,
    stdin_scenario,
    capsys,
) -> None:
    stdin_scenario(SCENARIO)

    _run(capsys)

    rendered = eval_adapter.build_precollected_evidence(SCENARIO["observations"])
    assert _RecordingOrchestrator.precollected_evidence_values == [rendered]
    assert "connection-growth" in rendered
    assert "connections grew monotonically" in rendered
    assert "현재 시스템 상태로 대체하거나 보강하지 않는다" in rendered


def test_adapter_passes_explicit_empty_evidence_when_no_observations_exist(
    wired,
    stdin_scenario,
    capsys,
) -> None:
    stdin_scenario({**SCENARIO, "observations": []})

    _run(capsys)

    assert _RecordingOrchestrator.precollected_evidence_values == [""]


def test_adapter_reads_back_the_session_the_pipeline_created(wired, stdin_scenario, capsys) -> None:
    stdin_scenario(SCENARIO)

    _run(capsys)

    store = _Container.instances[0].session_store
    assert len(store.requested) == 1
    # The id the adapter looked up must be the one derived from the alarm it sent.
    assert _RecordingOrchestrator.calls[0]["message_id"] == f"eval:{store.requested[0]}"


def test_adapter_emits_exactly_one_normalized_result_object(wired, stdin_scenario, capsys) -> None:
    stdin_scenario(SCENARIO)

    payload = _run(capsys)

    assert payload["schemaVersion"] == 2
    assert payload["scenarioId"] == "rds-connection-pool-exhaustion"
    assert payload["engine"] == "strands"
    assert payload["rootCause"]
    assert payload["rootCauseConfirmed"] is True
    assert payload["rootFaultType"] == "db-leak"
    assert payload["rootCauseEvidenceIds"] == ["connection-growth"]
    assert payload["evidenceIds"] == ["connection-growth"]
    assert payload["remediation"]["safeguards"]
    assert payload["remediation"]["available"] is True
    assert payload["remediation"]["verificationStatus"] == "DRAFT"
    assert payload["remediation"]["executionSteps"] == [
        {
            "stepId": "step-1",
            "intent": "커넥션 회수",
            "action": "api 서비스를 강제 재배포한다",
            "successCriteria": "DatabaseConnections 가 20 이하",
        }
    ]
    assert payload["remediation"]["safe"] is True
    assert payload["competingCauseJudgments"] == []
    # Score the persisted completion snapshot without looking up a mutable public head.
    assert _Container.instances[0].playbook_store.requested == []
    assert payload["remediation"]["unsafeSteps"] == []


def test_adapter_emits_explicit_competing_cause_judgments(
    wired,
    stdin_scenario,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = {
        **SCENARIO,
        "observations": [
            *SCENARIO["observations"],
            {"id": "request-volume-flat", "source": "cloudwatch", "summary": "request volume stayed flat"},
        ],
        "expectation": {
            "competingCauses": [
                {
                    "id": "traffic-surge",
                    "requiredEvidenceIds": ["request-volume-flat"],
                }
            ]
        },
    }
    monkeypatch.setattr(
        eval_adapter,
        "_validation_hypotheses",
        lambda _container, _notification: [
            {
                "status": "REJECTED",
                "judgment_reasoning": "Request volume did not increase.",
                "validation_evidence_summary": "[request-volume-flat] remained flat.",
            }
        ],
    )
    stdin_scenario(scenario)

    payload = _run(capsys)

    assert payload["competingCauseJudgments"] == [
        {
            "causeId": "traffic-surge",
            "judgment": "rejected",
            "rationale": ("Request volume did not increase.\n[request-volume-flat] remained flat."),
            "evidenceIds": ["request-volume-flat"],
        }
    ]


def test_adapter_uses_persisted_confirmation_as_the_normalized_authority(
    wired,
    stdin_scenario,
    capsys,
) -> None:
    stdin_scenario(SCENARIO)
    notification = _notification().model_copy(update={"confirmed": False})
    _Container.handoff = CompletionHandoff(
        rca_id="rca-1",
        state=RcaSessionState.COMPLETED,
        notification=notification,
    )

    payload = _run(capsys)

    assert payload["rootCauseConfirmed"] is False


@pytest.mark.parametrize(
    "mismatch",
    [
        "missing_handoff",
        "missing_playbook",
        "missing_notification_playbook",
        "missing_playbook_id",
        "handoff_rca",
        "notification_rca",
        "playbook_rca",
        "playbook_id",
        "expected_rca",
    ],
)
def test_completed_playbook_loader_refuses_missing_or_foreign_snapshot(mismatch):
    """Never substitute another run or a public revision for a missing matching completion."""
    handoff = _Container.handoff.model_copy(deep=True)
    notification = handoff.notification
    expected_rca = "rca-1"
    if mismatch == "missing_handoff":
        handoff = None
    elif mismatch == "missing_playbook":
        handoff.playbook = None
    elif mismatch == "missing_notification_playbook":
        notification.playbook = None
    elif mismatch == "missing_playbook_id":
        notification.playbook.pop("playbook_id")
    elif mismatch == "handoff_rca":
        handoff.rca_id = "other"
    elif mismatch == "notification_rca":
        notification.rca_id = "other"
    elif mismatch == "playbook_rca":
        handoff.playbook.rca_id = "other"
    elif mismatch == "playbook_id":
        handoff.playbook.playbook_id = "other"
    else:
        expected_rca = "other"
    assert eval_adapter._recorded_playbook_detail(handoff, notification, rca_id=expected_rca) is None


@pytest.mark.parametrize("state", [state for state in RcaSessionState if state != RcaSessionState.COMPLETED])
def test_completed_playbook_loader_refuses_every_noncompleted_state(state):
    """An existing playbook alone cannot make an unfinished session evaluable."""
    handoff = _Container.handoff.model_copy(update={"state": state}, deep=True)
    assert eval_adapter._recorded_playbook_detail(handoff, handoff.notification, rca_id="rca-1") is None


@pytest.mark.parametrize("publication_status", ["", "PENDING", "PUBLISHED"])
def test_completed_playbook_loader_preserves_snapshot_independent_of_publication(publication_status):
    """Publishing or replacing a library head is not authority over the completed run."""
    handoff = _Container.handoff.model_copy(update={"playbook_index_status": publication_status}, deep=True)
    before = handoff.model_dump(mode="json")
    detail = eval_adapter._recorded_playbook_detail(handoff, handoff.notification, rca_id="rca-1")
    assert detail is handoff.playbook
    assert detail.execution_steps[0].step_id == "step-1"
    assert handoff.model_dump(mode="json") == before


def test_adapter_preserves_available_empty_completed_draft(wired, stdin_scenario, capsys):
    """The live failure shape becomes available, without inventing executable remediation."""
    stdin_scenario(SCENARIO)
    _Container.handoff.playbook.execution_steps = []
    _Container.handoff.playbook.rollback_context = None
    before = _Container.handoff.model_dump(mode="json")
    payload = _run(capsys)
    assert payload["remediation"]["available"] is True
    assert payload["remediation"]["executionSteps"] == []
    assert payload["remediation"]["verificationStatus"] == "DRAFT"
    assert _Container.instances[0].playbook_store.requested == []
    assert _Container.handoff.model_dump(mode="json") == before


def test_a_completed_session_is_scored_even_when_the_message_was_not_acked(wired, stdin_scenario, capsys) -> None:
    # A pending notification makes process_alarm return False, but the analysis
    # that evaluation scores already finished. Session state is the authority.
    stdin_scenario(SCENARIO)
    _RecordingOrchestrator.result = False

    payload = _run(capsys)

    assert payload["scenarioId"] == "rds-connection-pool-exhaustion"
    assert payload["rootCause"]


def test_an_unacked_message_with_no_completed_session_still_fails(wired, stdin_scenario) -> None:
    stdin_scenario(SCENARIO)
    _RecordingOrchestrator.result = False
    _Container.handoff = CompletionHandoff(
        rca_id="rca-1",
        state=RcaSessionState.FAILED,
        notification=_notification(),
    )

    with pytest.raises(SystemExit):
        eval_adapter.main(["rca-agent-eval", ""])


def test_adapter_fails_when_the_session_did_not_reach_completion(wired, stdin_scenario) -> None:
    stdin_scenario(SCENARIO)
    _Container.handoff = CompletionHandoff(
        rca_id="rca-1",
        state=RcaSessionState.FAILED,
        notification=_notification(),
    )

    with pytest.raises(SystemExit):
        eval_adapter.main(["rca-agent-eval", ""])


def test_adapter_fails_when_the_session_has_no_result_payload(wired, stdin_scenario) -> None:
    stdin_scenario(SCENARIO)
    _Container.handoff = CompletionHandoff(
        rca_id="rca-1",
        state=RcaSessionState.COMPLETED,
        notification=None,
    )

    with pytest.raises(SystemExit):
        eval_adapter.main(["rca-agent-eval", ""])


def test_adapter_fails_when_no_session_exists(wired, stdin_scenario) -> None:
    stdin_scenario(SCENARIO)
    _Container.handoff = None

    with pytest.raises(SystemExit):
        eval_adapter.main(["rca-agent-eval", ""])


def test_adapter_fails_on_a_scenario_without_an_id(wired, stdin_scenario) -> None:
    stdin_scenario({"executionModes": ["model-eval"], "alarm": {"name": "A"}, "observations": []})

    with pytest.raises(SystemExit):
        eval_adapter.main(["rca-agent-eval", ""])


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        (
            {
                "id": "missing-execution-modes",
                "alarm": {"name": "A", "stateReason": "threshold crossed"},
                "observations": [{"id": "supplied", "summary": "must not reach the pipeline"}],
            },
            "scenario executionModes is missing",
        ),
        (
            {
                "id": "unsupported-execution-mode",
                "executionModes": ["deployed-e2e"],
                "alarm": {"name": "A", "stateReason": "threshold crossed"},
                "observations": [{"id": "supplied", "summary": "must not reach the pipeline"}],
            },
            "scenario executionModes does not include 'model-eval'",
        ),
    ],
)
def test_adapter_rejects_scenarios_not_enabled_for_model_eval(
    wired,
    stdin_scenario,
    capsys,
    scenario,
    expected_error,
) -> None:
    stdin_scenario(scenario)

    with pytest.raises(SystemExit):
        eval_adapter.main(["rca-agent-eval", ""])

    assert expected_error in capsys.readouterr().err
    assert _RecordingOrchestrator.calls == []
    assert _Container.instances == []


def test_adapter_does_not_consume_a_queue(wired, stdin_scenario, capsys) -> None:
    # model-eval bypasses delivery, so it must not require or use a queue URL.
    stdin_scenario(SCENARIO)

    _run(capsys)

    assert _Container.instances[0].queue_url == ""


class _ChattyOrchestrator(_RecordingOrchestrator):
    """Mimics the model SDK streaming progress straight to stdout."""

    def process_alarm(self, body, *, receive_count=1, message_id=None) -> bool:
        print("I'll gather the alarm context first.")
        print("Tool #1: get_metric_data")
        return super().process_alarm(body, receive_count=receive_count, message_id=message_id)


def test_stdout_carries_only_the_result_even_when_the_pipeline_prints(
    monkeypatch: pytest.MonkeyPatch, stdin_scenario, capsys
) -> None:
    # The harness requires exactly one JSON object on stdout. Progress text from
    # the model SDK must not contaminate it.
    monkeypatch.setattr("rca_agent.services.pipeline.PipelineOrchestrator", _ChattyOrchestrator)
    monkeypatch.setattr("rca_agent.di.eval_container.EvalAppContainer", _Container)
    stdin_scenario(SCENARIO)

    eval_adapter.main(["rca-agent-eval", ""])
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert payload["scenarioId"] == "rds-connection-pool-exhaustion"
    assert "Tool #1" in captured.err


def test_stdout_is_restored_after_the_run(monkeypatch: pytest.MonkeyPatch, stdin_scenario, capsys) -> None:
    monkeypatch.setattr("rca_agent.services.pipeline.PipelineOrchestrator", _ChattyOrchestrator)
    monkeypatch.setattr("rca_agent.di.eval_container.EvalAppContainer", _Container)
    stdin_scenario(SCENARIO)
    before = sys.stdout

    eval_adapter.main(["rca-agent-eval", ""])

    assert sys.stdout is before


def test_stdout_is_restored_even_when_the_run_fails(monkeypatch: pytest.MonkeyPatch, stdin_scenario) -> None:
    monkeypatch.setattr("rca_agent.services.pipeline.PipelineOrchestrator", _ChattyOrchestrator)
    monkeypatch.setattr("rca_agent.di.eval_container.EvalAppContainer", _Container)
    stdin_scenario(SCENARIO)
    _Container.handoff = None
    before = sys.stdout

    with pytest.raises(SystemExit):
        eval_adapter.main(["rca-agent-eval", ""])

    assert sys.stdout is before
