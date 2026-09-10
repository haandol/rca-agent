import copy
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from structlog.testing import capture_logs

from headless_codex.adapters.secondary.evidence import s3_evidence_store
from headless_codex.adapters.secondary.evidence.s3_evidence_store import S3EvidenceStore
from headless_codex.ports.dto.models import CodexResult
from headless_codex.ports.interfaces.execution_store import (
    ExecutionClaim,
    ExecutionClaimDisposition,
    ExecutionTarget,
    ExecutionTargetUnavailableError,
)
from headless_codex.services import execution_workspace
from headless_codex.services.execution_evidence import capture_command_output
from headless_codex.services.execution_pipeline import ExecutionOrchestrator
from headless_codex.services.execution_state import ExecutionState

RCA_ID = "rca-1"
ENGINE = "headless-codex"
CLAIM_TOKEN = "claim-token"
PLAYBOOK = {
    "playbook_id": "pb-1",
    "failure_type": "DB 커넥션 누수",
    "symptom_pattern": "DatabaseConnections 80 초과",
    "related_metrics": ["AWS/RDS/DatabaseConnections"],
    "verification_status": "DRAFT",
    "temporary_mitigation": "서비스 재배포",
    "permanent_remediation": "커넥션 반환 누락 수정",
    "execution_steps": [
        {
            "step_id": "step-1",
            "intent": "커넥션 회수",
            "action": "api 서비스를 강제 재배포",
            "success_criteria": "DatabaseConnections 20 이하",
        }
    ],
}
APPROVAL = json.dumps(
    {
        "execution_id": "exec-1",
        "rca_id": RCA_ID,
        "engine": ENGINE,
        "approval_id": "approval-1",
        "requested_by": "operator",
        "report_s3_key": "reports/headless-codex/rca-1/report.md",
        "approved_playbook_s3_key": "approved/rca-1/exec-1/playbook.json",
        "playbook_digest": "a" * 64,
    }
)


def _target(playbook: dict | None = None) -> ExecutionTarget:
    return ExecutionTarget(
        rca_id=RCA_ID,
        engine=ENGINE,
        alarm_name="VitalIngestFailure",
        playbook=playbook if playbook is not None else PLAYBOOK,
        alarm_data={"AlarmName": "VitalIngestFailure", "Trigger": {"MetricName": "VitalIngestFailure"}},
        report_s3_key="reports/headless-codex/rca-1/report.md",
    )


def _resolved_records() -> list[dict]:
    return [
        {"type": "attempt", "step_id": "step-1", "command": "aws ecs update-service", "succeeded": True},
        {
            "type": "step_outcome",
            "step_id": "step-1",
            "success_criteria": "DatabaseConnections 20 이하",
            "observation": "DatabaseConnections 12",
            "criteria_met": True,
        },
        {"type": "resolution", "observation": "증상 지표 정상", "resolved": True},
    ]


class RecordingRunner:
    """실행 하네스 대역. 서버가 기록할 증거를 워크스페이스에 직접 쓴다."""

    def __init__(
        self,
        records: list[dict] | None = None,
        *,
        success: bool = True,
        cancelled: bool = False,
        retrospective: dict | None = None,
        retrospective_success: bool = True,
    ):
        self._records = records if records is not None else _resolved_records()
        self._success = success
        self._cancelled = cancelled
        self._retrospective = retrospective
        self._retrospective_success = retrospective_success
        self.execution_prompts: list[str] = []
        self.retrospective_prompts: list[str] = []

    def run_execution(
        self,
        prompt,
        *,
        execution_token,
        execution_id,
        approved_step_ids,
        approved_success_criteria,
        cancel_checker=None,
    ):
        self.execution_prompts.append(prompt)
        self.approved_step_ids = approved_step_ids
        self.approved_success_criteria = approved_success_criteria
        self.observation_context = json.loads(
            execution_workspace.observation_context_path_for_token(execution_token).read_text()
        )
        self.cancel_checker = cancel_checker
        path = execution_workspace.evidence_path_for_token(execution_token)
        with path.open("a", encoding="utf-8") as handle:
            for record in self._records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return CodexResult(
            success=self._success,
            result="done" if self._success else "failed",
            raw_output="",
            cancelled=self._cancelled,
        )

    def run_retrospective(self, prompt, *, execution_token, execution_id, cancel_checker=None):
        self.retrospective_cancel_checker = cancel_checker
        self.retrospective_prompts.append(prompt)
        if self._retrospective is not None:
            execution_workspace.retrospective_path_for_token(execution_token).write_text(
                json.dumps(self._retrospective, ensure_ascii=False),
                encoding="utf-8",
            )
        return CodexResult(
            success=self._retrospective_success,
            result="retrospective done" if self._retrospective_success else "retrospective failed",
            raw_output="",
        )


def _container(runner, *, target=None, claim=None, retrospective_claimed=True):
    execution_store = SimpleNamespace(
        claim_execution=Mock(return_value=claim or ExecutionClaim(ExecutionClaimDisposition.CLAIMED, CLAIM_TOKEN, 1)),
        load_target=Mock(return_value=target if target is not None else _target()),
        update_state=Mock(),
        load_state=Mock(return_value=ExecutionState.EXECUTING),
        is_execution_current=Mock(return_value=True),
        claim_retrospective=Mock(return_value=retrospective_claimed),
        record_retrospective=Mock(),
        save_playbook_revision=Mock(),
        publish_playbook_revision=Mock(),
    )
    evidence_store = SimpleNamespace(
        load_approved_playbook=Mock(return_value=(target.playbook if target is not None else PLAYBOOK)),
        save_execution_evidence=Mock(return_value="executions/rca-1/exec-1/evidence.json"),
        save_playbook_snapshot=Mock(return_value="executions/rca-1/exec-1/playbook-before.json"),
        save_retrospective_diff=Mock(return_value="executions/rca-1/exec-1/retrospective-diff.json"),
    )
    return SimpleNamespace(
        execution_store=execution_store,
        evidence_store=evidence_store,
        playbook_store=SimpleNamespace(save_to_s3_vectors=Mock(return_value=True)),
        execution_runner=runner,
    )


@pytest.fixture(autouse=True)
def isolated_workspaces(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(execution_workspace, "_WORKSPACE_ROOT", tmp_path / "executions")


def _states(container) -> list[ExecutionState]:
    return [call.kwargs["state"] for call in container.execution_store.update_state.call_args_list]


def test_a_message_that_is_not_an_approval_never_reaches_the_runner():
    """실행은 사용자 승인 없이 시작될 수 없다."""
    runner = RecordingRunner()
    container = _container(runner)
    orchestrator = ExecutionOrchestrator(container)

    handled = orchestrator.process_message(json.dumps({"AlarmName": "HighCPU", "NewStateValue": "ALARM"}))

    assert handled
    assert runner.execution_prompts == []
    container.execution_store.claim_execution.assert_not_called()


def test_an_approved_execution_runs_the_playbook_steps_and_resolves():
    runner = RecordingRunner()
    container = _container(runner)

    assert ExecutionOrchestrator(container).process_message(APPROVAL)

    assert _states(container) == [ExecutionState.VERIFYING, ExecutionState.RESOLVED]
    assert "step-1" in runner.execution_prompts[0]
    assert "VitalIngestFailure" in runner.execution_prompts[0]
    assert runner.approved_step_ids == ("step-1",)
    assert runner.approved_success_criteria == {"step-1": "DatabaseConnections 20 이하"}


def test_observation_context_is_approved_server_data_not_rendered_prompt(monkeypatch):
    target = _target()
    target.alarm_data["ExtraUntrustedField"] = {"verbatim": "observed context"}
    runner = RecordingRunner()
    container = _container(runner, target=target)
    monkeypatch.setattr(
        "headless_codex.services.execution_pipeline.build_execution_prompt",
        lambda *args, **kwargs: "A deliberately unrelated prompt",
    )

    assert ExecutionOrchestrator(container).process_message(APPROVAL)
    assert runner.observation_context == {
        "playbook": target.playbook,
        "alarm_data": target.alarm_data,
        "alarm_name": target.alarm_name,
    }
    container.evidence_store.load_approved_playbook.assert_called_once_with(
        "approved/rca-1/exec-1/playbook.json", playbook_digest="a" * 64
    )


@pytest.mark.parametrize("current", [True, False, RuntimeError("store unavailable")])
def test_cancel_callback_checks_claim_and_fails_closed(current):
    runner = RecordingRunner()
    container = _container(runner)
    check = container.execution_store.is_execution_current
    if isinstance(current, Exception):
        check.side_effect = current
    else:
        check.return_value = current
    assert ExecutionOrchestrator(container).process_message(APPROVAL)
    assert runner.cancel_checker() is (current is not True)
    check.assert_called_once_with("exec-1", rca_id=RCA_ID, claim_token=CLAIM_TOKEN)
    container.execution_store.load_state.assert_not_called()


def test_shutdown_aborts_control_without_store_read():
    from threading import Event

    shutdown = Event()
    runner = RecordingRunner()
    container = _container(runner)
    assert ExecutionOrchestrator(container, shutdown_event=shutdown).process_message(APPROVAL)
    shutdown.set()
    assert runner.cancel_checker() is True
    container.execution_store.is_execution_current.assert_not_called()


def test_persisted_evidence_keeps_command_output_and_source_times_with_actual_runner_boundaries():
    """The existing S3 save port receives full retained streams and actual times, without AWS calls."""
    records = _resolved_records()
    output = json.dumps({"padding": "x" * 8000, "taskArn": "task/customer-approved-stop", "lastStatus": "STOPPED"})
    records[0].update(
        capture_command_output(output, "warning on success")
        | {
            "started_at": "2026-09-09T23:59:58+00:00",
            "ended_at": "2026-09-09T23:59:59+00:00",
            "recorded_at": "2026-09-10T00:00:00+00:00",
        }
    )
    records[1]["recorded_at"] = "2026-09-10T00:00:01+00:00"
    records[2]["recorded_at"] = "2026-09-10T00:00:02+00:00"
    runner = RecordingRunner(records=records)
    container = _container(runner)
    before = datetime.now(UTC)
    assert ExecutionOrchestrator(container).process_message(APPROVAL)
    after = datetime.now(UTC)

    persisted = container.evidence_store.save_execution_evidence.call_args.kwargs["evidence"]
    assert before <= datetime.fromisoformat(persisted["started_at"])
    assert persisted["started_at"] <= persisted["ended_at"]
    assert datetime.fromisoformat(persisted["ended_at"]) <= after
    attempt = persisted["steps"][0]["attempts"][0]
    assert attempt["stdout"] == output
    assert attempt["stderr"] == "warning on success"
    for field in ("started_at", "ended_at", "recorded_at"):
        assert attempt[field] == records[0][field]
    assert persisted["steps"][0]["outcomes"][0]["recorded_at"] == records[1]["recorded_at"]
    assert persisted["resolution_records"][0]["recorded_at"] == records[2]["recorded_at"]
    assert _states(container) == [ExecutionState.VERIFYING, ExecutionState.RESOLVED]


def test_a_redelivered_approval_does_not_run_a_second_time():
    runner = RecordingRunner()
    container = _container(runner, claim=ExecutionClaim(ExecutionClaimDisposition.TERMINAL_DUPLICATE))

    handled = ExecutionOrchestrator(container).process_message(APPROVAL)

    assert handled
    assert runner.execution_prompts == []


@pytest.mark.parametrize(
    "disposition",
    [ExecutionClaimDisposition.REJECTED, ExecutionClaimDisposition.EXPIRED_FAILED],
)
def test_rejected_or_expired_reservations_are_acknowledged_without_execution(disposition):
    runner = RecordingRunner()
    container = _container(runner, claim=ExecutionClaim(disposition))

    handled = ExecutionOrchestrator(container).process_message(APPROVAL)

    assert handled
    assert runner.execution_prompts == []


def test_a_contended_claim_leaves_the_request_on_the_queue():
    runner = RecordingRunner()
    container = _container(runner, claim=ExecutionClaim(ExecutionClaimDisposition.CONTENDED))

    handled = ExecutionOrchestrator(container).process_message(APPROVAL)

    assert not handled
    assert runner.execution_prompts == []


def test_a_playbook_without_execution_steps_is_not_run():
    runner = RecordingRunner()
    container = _container(runner, target=_target({**PLAYBOOK, "execution_steps": []}))

    assert ExecutionOrchestrator(container).process_message(APPROVAL)

    assert runner.execution_prompts == []
    assert _states(container) == [ExecutionState.FAILED]


def test_an_unavailable_target_fails_the_execution_without_running_anything():
    runner = RecordingRunner()
    container = _container(runner)
    container.execution_store.load_target = Mock(
        side_effect=ExecutionTargetUnavailableError("analysis is ANALYZING, not COMPLETED")
    )

    assert ExecutionOrchestrator(container).process_message(APPROVAL)

    assert runner.execution_prompts == []
    assert _states(container) == [ExecutionState.FAILED]


def test_evidence_is_preserved_when_the_execution_fails():
    runner = RecordingRunner(
        records=[
            {
                "type": "attempt",
                "step_id": "step-1",
                "command": "aws ecs update-service",
                "succeeded": False,
                "failure_class": "INVALID_ARGUMENT",
                "error_output": "ValidationError",
            }
        ],
        success=False,
    )
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    saved = container.evidence_store.save_execution_evidence.call_args.kwargs["evidence"]

    assert _states(container)[-1] is ExecutionState.FAILED
    assert saved["steps"][0]["attempts"][0]["failure_class"] == "INVALID_ARGUMENT"
    assert saved["final_state"] == str(ExecutionState.FAILED)


def test_a_blocked_step_is_recorded_and_the_execution_still_finishes():
    runner = RecordingRunner(
        records=[
            {
                "type": "attempt",
                "step_id": "step-1",
                "command": "aws ecs delete-service",
                "succeeded": False,
                "blocked": True,
                "block_reason": "ecs delete-service is an irreversible operation",
                "failure_class": "BLOCKED_DESTRUCTIVE",
            },
            {
                "type": "step_outcome",
                "step_id": "step-1",
                "success_criteria": "DatabaseConnections 20 이하",
                "observation": "차단됨",
                "criteria_met": False,
                "manual_action_required": True,
            },
            {"type": "resolution", "observation": "수동 조치 필요", "resolved": False},
        ]
    )
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    saved = container.evidence_store.save_execution_evidence.call_args.kwargs["evidence"]

    assert _states(container)[-1] is ExecutionState.UNRESOLVED
    assert saved["steps"][0]["manual_action_required"] is True
    assert "irreversible" in saved["steps"][0]["attempts"][0]["block_reason"]


def test_an_unobserved_result_does_not_become_a_resolved_execution():
    runner = RecordingRunner(
        records=[
            {"type": "attempt", "step_id": "step-1", "command": "aws ecs update-service", "succeeded": True},
        ]
    )
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    assert _states(container)[-1] is ExecutionState.UNRESOLVED
    container.execution_store.claim_retrospective.assert_not_called()


def test_a_cancelled_execution_records_its_evidence_and_does_not_retrospect():
    runner = RecordingRunner(records=[], cancelled=True, success=False)
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    assert _states(container) == [ExecutionState.CANCELLED]
    container.execution_store.claim_retrospective.assert_not_called()
    container.evidence_store.save_execution_evidence.assert_called_once()


def test_the_playbook_snapshot_is_saved_before_the_run_so_the_diff_has_a_baseline():
    runner = RecordingRunner()
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    container.evidence_store.save_playbook_snapshot.assert_not_called()
    assert (
        container.execution_store.record_retrospective.call_args.kwargs["playbook_snapshot_s3_key"]
        == "approved/rca-1/exec-1/playbook.json"
    )


def test_a_resolved_execution_runs_the_retrospective_and_revises_the_playbook():
    runner = RecordingRunner(
        retrospective={
            "update": {"execution_steps": [{"step_id": "step-1", "action": "api 서비스를 강제 재배포하고 30초 대기"}]},
            "rationale": "첫 시도가 대기 없이 지표를 조회해 실패했다",
        }
    )
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    revision = container.execution_store.save_playbook_revision.call_args
    recorded = container.execution_store.record_retrospective.call_args.kwargs

    assert revision.args[2]["execution_steps"][0]["action"].endswith("30초 대기")
    assert recorded["status"] == "UPDATED"
    assert recorded["playbook_snapshot_s3_key"]
    assert recorded["diff_s3_key"]


def test_a_retrospective_that_omits_fields_does_not_delete_the_existing_playbook():
    runner = RecordingRunner(
        retrospective={
            "update": {"symptom_pattern": "", "execution_steps": []},
            "rationale": "고칠 것이 없다",
        }
    )
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    revised = container.execution_store.save_playbook_revision.call_args.args[2]

    # 빈 필드는 "고칠 것이 없다"는 뜻이므로 기록된 값이 그대로 남는다.
    assert revised["symptom_pattern"] == "DatabaseConnections 80 초과"
    assert revised["execution_steps"][0]["action"] == "api 서비스를 강제 재배포"
    assert container.execution_store.record_retrospective.call_args.kwargs["status"] == "NO_CHANGE"


def test_a_retrospective_with_no_correction_still_promotes_the_procedure():
    # 절차가 그대로 이슈를 해소했다면 그것이 가장 강한 검증이다.
    runner = RecordingRunner(
        retrospective={
            "update": {"symptom_pattern": "", "execution_steps": []},
            "rationale": "고칠 것이 없다",
        }
    )
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    revised = container.execution_store.save_playbook_revision.call_args.args[2]
    indexed = container.playbook_store.save_to_s3_vectors.call_args.args[0]

    assert revised["verification_status"] == "VERIFIED"
    # 다음 실행은 개정본을, 다음 RCA 의 보강은 인덱스를 읽으므로 양쪽이 같아야 한다.
    assert indexed["verification_status"] == "VERIFIED"


class LocalEvidenceObjects:
    """Injected S3 client double: bytes remain readable after the execution workspace is removed."""

    def __init__(self, *, fail_diff=False):
        self.objects = {}
        self.fail_diff = fail_diff

    def put_object(self, **request):
        if self.fail_diff and request["Key"].endswith("retrospective-diff.json"):
            raise OSError("attestation storage unavailable")
        self.objects[request["Key"]] = request["Body"]

    def get_object(self, **request):
        return {"Body": io.BytesIO(self.objects[request["Key"]])}


@pytest.mark.parametrize(
    ("update", "expected_status", "verification"),
    [
        ({}, "NO_CHANGE", "VERIFIED"),
        ({"symptom_pattern": "", "execution_steps": [], "verification_status": "VERIFIED"}, "NO_CHANGE", "VERIFIED"),
        ({"temporary_mitigation": "관측된 완화 근거를 보존"}, "UPDATED", "VERIFIED"),
        ({"execution_steps": [{"step_id": "step-1", "action": "대기 후 재확인"}]}, "UPDATED", "DRAFT"),
    ],
)
def test_public_orchestrator_persists_readable_attestation_before_publication(
    monkeypatch, update, expected_status, verification
):
    rationale = "승인된 절차와 실제 관측을 대조한 근거. " * 250 + "마지막 관측도 보존한다."
    saved = {"update": update, "rationale": rationale}
    saved_before = copy.deepcopy(saved)
    runner = RecordingRunner(retrospective=saved)
    container = _container(runner)
    client = LocalEvidenceObjects()
    monkeypatch.setattr(s3_evidence_store, "S3_EVIDENCE_BUCKET", "offline-evidence")
    container.evidence_store = S3EvidenceStore(client)
    approval = json.loads(APPROVAL)
    snapshot = json.dumps(PLAYBOOK, ensure_ascii=False).encode()
    snapshot_key = approval["approved_playbook_s3_key"]
    client.objects[snapshot_key] = snapshot
    approval["playbook_digest"] = hashlib.sha256(snapshot).hexdigest()
    original_playbook = copy.deepcopy(PLAYBOOK)
    diff_key = "executions/rca-1/exec-1/retrospective-diff.json"

    def assert_attested_before_publication(*args, **kwargs):
        assert diff_key in client.objects
        assert json.loads(client.objects[diff_key])["rationale"] == rationale

    container.execution_store.save_playbook_revision.side_effect = assert_attested_before_publication
    assert ExecutionOrchestrator(container).process_message(json.dumps(approval))

    recorded = container.execution_store.record_retrospective.call_args.kwargs
    assert recorded["status"] == expected_status
    assert recorded["diff_s3_key"] == diff_key
    assert recorded["summary"] == rationale[:500]
    assert recorded["playbook_snapshot_s3_key"] == snapshot_key
    stored = json.loads(client.get_object(Bucket="offline-evidence", Key=recorded["diff_s3_key"])["Body"].read())
    assert stored["rationale"] == rationale
    assert stored["proposed_update"] == update
    assert stored["update"] == ({} if expected_status == "NO_CHANGE" else update)
    assert all(name in stored for name in ("changed_fields", "corrected_steps", "added_steps", "preserved_steps"))
    if expected_status == "NO_CHANGE":
        assert not (stored["changed_fields"] or stored["corrected_steps"] or stored["added_steps"])
    assert container.execution_store.publish_playbook_revision.call_args.args[2]["verification_status"] == verification
    assert client.objects[snapshot_key] == snapshot
    assert original_playbook == PLAYBOOK
    assert saved == saved_before
    assert _states(container)[-1] is ExecutionState.RESOLVED


@pytest.mark.parametrize("update", [{}, {"temporary_mitigation": "clarified mitigation"}])
def test_public_orchestrator_attestation_s3_failure_blocks_completion_and_publication(monkeypatch, update):
    runner = RecordingRunner(retrospective={"update": update, "rationale": "observed evidence rationale"})
    container = _container(runner)
    client = LocalEvidenceObjects(fail_diff=True)
    monkeypatch.setattr(s3_evidence_store, "S3_EVIDENCE_BUCKET", "offline-evidence")
    container.evidence_store = S3EvidenceStore(client)
    approval = json.loads(APPROVAL)
    snapshot = json.dumps(PLAYBOOK).encode()
    client.objects[approval["approved_playbook_s3_key"]] = snapshot
    approval["playbook_digest"] = hashlib.sha256(snapshot).hexdigest()

    assert ExecutionOrchestrator(container).process_message(json.dumps(approval))

    assert container.execution_store.record_retrospective.call_args.kwargs["status"] == "FAILED"
    assert _states(container)[-1] is ExecutionState.RESOLVED
    container.execution_store.save_playbook_revision.assert_not_called()
    container.execution_store.publish_playbook_revision.assert_not_called()
    container.playbook_store.save_to_s3_vectors.assert_not_called()
    assert client.objects[approval["approved_playbook_s3_key"]] == snapshot


def test_no_change_empty_diff_key_does_not_claim_durable_attestation():
    container = _container(RecordingRunner(retrospective={"update": {}, "rationale": "no procedure defects"}))
    container.evidence_store.save_retrospective_diff.return_value = ""
    assert ExecutionOrchestrator(container).process_message(APPROVAL)
    assert container.execution_store.record_retrospective.call_args.kwargs["status"] == "FAILED"
    container.execution_store.save_playbook_revision.assert_not_called()
    container.playbook_store.save_to_s3_vectors.assert_not_called()


@pytest.mark.parametrize(
    "message",
    [
        "retrospective evidence metadata exceeds JSON budget; durable evidence remains available",
        'diagnostic password="CANARY secret" Authorization: Bearer CANARY-token '
        '{"name":"AWS_SECRET_ACCESS_KEY","value":"CANARY-key"} '
        "postgresql://CANARY-user:CANARY-pass@db.local " + "bounded detail " * 100,
    ],
)
def test_retrospective_exception_diagnostic_is_visible_bounded_and_redacted(monkeypatch, message):
    container = _container(RecordingRunner())

    def fail_prompt(*args, **kwargs):
        raise ValueError(message)

    monkeypatch.setattr("headless_codex.services.execution_pipeline.build_retrospective_prompt", fail_prompt)
    with capture_logs() as logs:
        assert ExecutionOrchestrator(container).process_message(APPROVAL)

    event = next(entry for entry in logs if entry["event"] == "retrospective_failed")
    assert event["phase"] == "build_prompt"
    assert event["error_type"] == "ValueError"
    assert 0 < len(event["detail"]) <= 500
    assert "CANARY" not in json.dumps(logs)
    assert "exc_info" not in event
    assert "traceback" not in event
    if message.startswith("retrospective evidence"):
        assert event["detail"] == message
    recorded = container.execution_store.record_retrospective.call_args.kwargs
    assert recorded["status"] == "FAILED"
    assert recorded["summary"].startswith("build_prompt: ValueError: ")
    assert len(recorded["summary"]) <= 500
    assert "CANARY" not in recorded["summary"]
    assert _states(container)[-1] is ExecutionState.RESOLVED
    container.execution_store.save_playbook_revision.assert_not_called()


@pytest.mark.parametrize("initial_status", ["DRAFT", "VERIFIED"])
def test_a_corrected_procedure_is_published_as_a_draft(initial_status):
    runner = RecordingRunner(
        retrospective={
            "update": {"execution_steps": [{"step_id": "step-1", "action": "api 서비스를 강제 재배포하고 30초 대기"}]},
            "rationale": "첫 시도가 대기 없이 지표를 조회해 실패했다",
        }
    )
    container = _container(runner, target=_target({**PLAYBOOK, "verification_status": initial_status}))

    ExecutionOrchestrator(container).process_message(APPROVAL)

    revised = container.execution_store.save_playbook_revision.call_args.args[2]

    assert revised["verification_status"] == "DRAFT"
    assert revised["execution_steps"][0]["action"].endswith("30초 대기")


def test_an_added_execution_step_keeps_the_revision_a_draft():
    runner = RecordingRunner(
        retrospective={
            "update": {
                "execution_steps": [
                    {
                        "step_id": "step-2",
                        "intent": "회복 확인",
                        "action": "오류 지표를 조회",
                        "success_criteria": "오류 지표가 0",
                    }
                ]
            },
            "rationale": "해결 확인 절차가 누락됐다",
        }
    )
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    revised = container.execution_store.save_playbook_revision.call_args.args[2]

    assert revised["verification_status"] == "DRAFT"
    assert [step["step_id"] for step in revised["execution_steps"]] == ["step-1", "step-2"]


def test_a_metadata_only_update_promotes_the_unchanged_procedure():
    runner = RecordingRunner(
        retrospective={
            "update": {"temporary_mitigation": "서비스 재배포 후 지표 확인"},
            "rationale": "완화 설명을 실행 결과에 맞게 명확히 했다",
        }
    )
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    revised = container.execution_store.save_playbook_revision.call_args.args[2]

    assert revised["verification_status"] == "VERIFIED"
    assert revised["temporary_mitigation"].endswith("지표 확인")
    assert container.execution_store.record_retrospective.call_args.kwargs["status"] == "UPDATED"


def test_a_failed_retrospective_leaves_the_playbook_a_draft():
    # 갱신을 반영하지 못한 회고는 절차를 확인한 것이 아니므로 승격도 없다.
    runner = RecordingRunner(retrospective_success=False)
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    container.execution_store.save_playbook_revision.assert_not_called()
    container.playbook_store.save_to_s3_vectors.assert_not_called()


def test_zero_exit_retrospective_without_persisted_attestation_is_not_promoted():
    runner = RecordingRunner(retrospective_success=True)
    container = _container(runner)

    assert ExecutionOrchestrator(container).process_message(APPROVAL)

    recorded = container.execution_store.record_retrospective.call_args.kwargs
    assert recorded["status"] == "FAILED"
    assert "without a persisted attestation" in recorded["summary"]
    container.execution_store.save_playbook_revision.assert_not_called()
    container.execution_store.publish_playbook_revision.assert_not_called()
    container.playbook_store.save_to_s3_vectors.assert_not_called()


def test_an_agent_that_does_nothing_still_leaves_its_response_to_read():
    """실측에서 에이전트가 절차를 하나도 시도하지 않고 성공 종료했다.

    기록된 관측이 없으므로 판정은 미해결로 떨어지지만, 왜 수행하지 않았는지는 최종
    응답에만 남아 있다. 남기지 않으면 원인을 사후에 읽을 방법이 없다.
    """
    runner = RecordingRunner(records=[])
    container = _container(runner)

    with capture_logs() as logs:
        ExecutionOrchestrator(container).process_message(APPROVAL)

    returned = [entry for entry in logs if entry.get("event") == "execution_agent_returned"]

    assert returned, "the agent's final response must be logged even on a clean exit"
    assert returned[0]["detail"] == "done"
    assert _states(container)[-1] is ExecutionState.UNRESOLVED


def test_an_unresolved_execution_never_promotes_the_playbook():
    # 이슈를 해소하지 못한 실행의 절차는 올바름이 입증되지 않았다.
    runner = RecordingRunner(
        records=[
            {"type": "attempt", "step_id": "step-1", "command": "aws ecs update-service", "succeeded": True},
            {
                "type": "step_outcome",
                "step_id": "step-1",
                "success_criteria": "DatabaseConnections 20 이하",
                "observation": "DatabaseConnections 78",
                "criteria_met": False,
            },
            {"type": "resolution", "observation": "지표가 여전히 높다", "resolved": False},
        ]
    )
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    assert _states(container)[-1] is ExecutionState.UNRESOLVED
    container.execution_store.save_playbook_revision.assert_not_called()
    container.playbook_store.save_to_s3_vectors.assert_not_called()


def test_a_failed_retrospective_does_not_undo_the_resolved_execution():
    runner = RecordingRunner(retrospective_success=False)
    container = _container(runner)

    assert ExecutionOrchestrator(container).process_message(APPROVAL)

    assert _states(container)[-1] is ExecutionState.RESOLVED
    assert container.execution_store.record_retrospective.call_args.kwargs["status"] == "FAILED"
    container.execution_store.save_playbook_revision.assert_not_called()


def test_a_duplicate_retrospective_claim_stops_the_second_run():
    runner = RecordingRunner()
    container = _container(runner, retrospective_claimed=False)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    assert runner.retrospective_prompts == []
    container.execution_store.save_playbook_revision.assert_not_called()


def test_the_retrospective_prompt_carries_the_evidence_and_the_pre_execution_steps():
    runner = RecordingRunner()
    container = _container(runner)

    ExecutionOrchestrator(container).process_message(APPROVAL)

    prompt = runner.retrospective_prompts[0]

    assert "step-1" in prompt
    assert "DatabaseConnections 12" in prompt
    assert "TRANSIENT" in prompt


@pytest.mark.parametrize("failure_mode", ["empty-key", "save-error"])
def test_missing_durable_evidence_fails_retrospective_without_hiding_resolution(failure_mode):
    runner = RecordingRunner()
    container = _container(runner)
    if failure_mode == "empty-key":
        container.evidence_store.save_execution_evidence = Mock(return_value="")
    else:
        container.evidence_store.save_execution_evidence = Mock(side_effect=RuntimeError("s3 down"))

    assert ExecutionOrchestrator(container).process_message(APPROVAL)

    assert _states(container)[-1] is ExecutionState.RESOLVED
    terminal_update = container.execution_store.update_state.call_args_list[-1].kwargs
    assert terminal_update["evidence_s3_key"] == ""
    assert "durable execution evidence is unavailable" in terminal_update["retrospective_failure_reason"]
    assert runner.retrospective_prompts == []
    container.execution_store.claim_retrospective.assert_not_called()
    container.execution_store.save_playbook_revision.assert_not_called()
    container.playbook_store.save_to_s3_vectors.assert_not_called()


def test_a_vector_publication_failure_leaves_only_a_staged_revision_and_reports_failed():
    runner = RecordingRunner(
        retrospective={
            "update": {},
            "rationale": "실행 증거에서 교정할 절차 결함을 찾지 못했다",
        }
    )
    container = _container(runner)
    container.playbook_store.save_to_s3_vectors = Mock(return_value=False)

    assert ExecutionOrchestrator(container).process_message(APPROVAL)

    recorded = container.execution_store.record_retrospective.call_args.kwargs

    assert recorded["status"] == "FAILED"
    staged = container.execution_store.save_playbook_revision.call_args
    assert staged.args[2]["verification_status"] == "VERIFIED"
    container.execution_store.publish_playbook_revision.assert_not_called()
    assert container.playbook_store.save_to_s3_vectors.call_args.kwargs["publication_id"] == "exec-1"


def test_a_revision_staging_failure_never_publishes_verified_to_the_index():
    runner = RecordingRunner(
        retrospective={
            "update": {"temporary_mitigation": "서비스 재배포 후 지표 확인"},
            "rationale": "완화 설명을 실행 결과에 맞게 명확히 했다",
        }
    )
    container = _container(runner)
    container.execution_store.save_playbook_revision = Mock(side_effect=RuntimeError("ddb down"))

    assert ExecutionOrchestrator(container).process_message(APPROVAL)

    recorded = container.execution_store.record_retrospective.call_args.kwargs

    assert recorded["status"] == "FAILED"
    container.playbook_store.save_to_s3_vectors.assert_not_called()
    container.execution_store.publish_playbook_revision.assert_not_called()


def test_a_revision_commit_failure_leaves_verified_search_fail_closed():
    runner = RecordingRunner(
        retrospective={
            "update": {},
            "rationale": "실행 증거에서 교정할 절차 결함을 찾지 못했다",
        }
    )
    container = _container(runner)
    container.execution_store.publish_playbook_revision.side_effect = RuntimeError("ddb down")

    assert ExecutionOrchestrator(container).process_message(APPROVAL)

    assert container.execution_store.record_retrospective.call_args.kwargs["status"] == "FAILED"
    container.execution_store.save_playbook_revision.assert_called_once()
    assert container.playbook_store.save_to_s3_vectors.call_args.kwargs["publication_id"] == "exec-1"


def test_retrospective_shutdown_callback_does_not_require_executing_claim():
    runner = RecordingRunner()
    container = _container(runner)
    orchestrator = ExecutionOrchestrator(container)
    assert orchestrator.process_message(APPROVAL)
    assert runner.retrospective_prompts
    container.execution_store.is_execution_current.return_value = False
    assert runner.retrospective_cancel_checker() is False
    orchestrator._shutdown_event.set()
    assert runner.retrospective_cancel_checker() is True


def test_fixed_wait_journal_survives_evidence_publication_and_workspace_cleanup():
    waits = [
        {
            "type": "metric_wait",
            "phase": "started",
            "step_id": "step-1",
            "binding": {"start": "2026-09-10T12:35:00+00:00", "end": "2026-09-10T12:37:00+00:00"},
        },
        {
            "type": "metric_wait",
            "phase": "poll",
            "step_id": "step-1",
            "observed_at": "2026-09-10T12:37:01+00:00",
            "response": {"ok": False, "stdout": '{"partial":', "stderr": "query failed", "exit_status": 255},
        },
        {"type": "metric_wait", "phase": "terminal", "step_id": "step-1", "status": "UNOBSERVABLE"},
    ]
    runner = RecordingRunner(records=_resolved_records() + waits)
    container = _container(runner)
    assert ExecutionOrchestrator(container).process_message(APPROVAL)
    persisted = container.evidence_store.save_execution_evidence.call_args.kwargs["evidence"]
    assert persisted["metric_wait_records"] == waits
    assert persisted["final_state"] == "UNRESOLVED"
    assert not runner.retrospective_prompts
