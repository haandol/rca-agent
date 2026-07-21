import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import structlog

from cc_headless.ports.dto.models import CcResult
from cc_headless.ports.interfaces.session_store import ClaimDisposition, SessionClaim, SessionOwnershipCheckError
from cc_headless.services import execution_context, pipeline
from cc_headless.services.execution_context import artifact_dir_for_token
from cc_headless.services.pipeline import PipelineOrchestrator, extract_root_cause

ALARM_DATA = {
    "AlarmName": "HighCPU",
    "NewStateReason": "threshold crossed",
    "Region": "us-east-1",
    "Trigger": {
        "MetricName": "CPUUtilization",
        "Namespace": "AWS/ECS",
    },
}
CLAIM_TOKEN = "claim-token"


class FinishedThread:
    def join(self, timeout=None):
        self.timeout = timeout


def _container(runner):
    store = SimpleNamespace(
        claim_session=Mock(return_value=SessionClaim(ClaimDisposition.CLAIMED, CLAIM_TOKEN, 1)),
        update_state=Mock(),
        is_terminated=Mock(return_value=False),
        mark_failed=Mock(),
        mark_outdated=Mock(),
        mark_completed=Mock(),
        acquire_side_effect_lease=Mock(return_value="lease-token"),
        release_side_effect_lease=Mock(),
    )
    report_store = SimpleNamespace(save_report=Mock(return_value="reports/rca.md"), send_notification=Mock())

    def _load_playbook(artifact_dir):
        path = artifact_dir / "playbook.json"
        return json.loads(path.read_text()) if path.is_file() else None

    playbook_store = SimpleNamespace(load_playbook=Mock(side_effect=_load_playbook), save_to_s3_vectors=Mock())
    return SimpleNamespace(
        session_store=store,
        report_store=report_store,
        playbook_store=playbook_store,
        cc_runner=runner,
        dynamodb_client=None,
    )


def _patch_runtime(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    monkeypatch.setattr(pipeline, "build_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(pipeline, "start_watcher", lambda *args: (FinishedThread(), Mock()))


def _valid_report(root_cause: str = "DB connection leak") -> str:
    return f"""## 인시던트 요약
현재 알람 조사
## 영향
서비스 영향
## 증거 시간 범위
Current alarm window: 2026-07-21T00:00:00Z to 2026-07-21T00:10:00Z
Historical comparison window: 2026-07-20T00:00:00Z to 2026-07-20T00:10:00Z
## 근본 원인
{root_cause}
## 5 Whys
증거 기반 분석
## 뒷받침 증거
Current alarm window 관측값
## 가설 분석 경로
hypothesis-1 검토
## 복구 결과
status: NOT_ATTEMPTED
fault_type: N/A
endpoint_path: N/A
validation_artifact: validation-1.json
## 검증 상태
PENDING
## Action Items
후속 관측
"""


def _write_required_report_artifacts(
    artifact_dir: Path,
    report: str,
    *,
    include_playbook: bool = True,
) -> None:
    artifact_dir.joinpath("scoping.json").write_text(
        json.dumps(
            {
                "stage": "SCOPING",
                "alarm_name": "HighCPU",
                "impact_scope": "service",
                "severity": "high",
                "metric_snapshot": {"CPUUtilization": 95},
                "summary": "scoped",
                "output_summary": "service/high",
            }
        )
    )
    artifact_dir.joinpath("hypotheses.json").write_text(
        json.dumps(
            {
                "stage": "HYPOTHESIS_GENERATION",
                "tree_id": "tree-1",
                "hypotheses": [
                    {
                        "hypothesis_id": "hypothesis-1",
                        "tree_id": "tree-1",
                        "title": "Connection leak",
                        "description": "Connections are not released",
                        "fault_type": "db-leak",
                        "category": "INFRASTRUCTURE",
                        "confidence_score": 0.7,
                        "required_evidence": ["metric"],
                        "status": "PENDING",
                        "parent_id": None,
                        "depth": 0,
                    }
                ],
                "summary": "one hypothesis",
                "output_summary": "one hypothesis",
            }
        )
    )
    artifact_dir.joinpath("validation-1.json").write_text(
        json.dumps(
            {
                "stage": "VALIDATION",
                "loop_index": 1,
                "confirmed": [],
                "rejected": [],
                "needs_investigation": [
                    {
                        "hypothesis_id": "hypothesis-1",
                        "confidence": 0.7,
                        "reasoning": "more evidence needed",
                    }
                ],
                "closed": [],
                "new_hypotheses": [],
                "summary": "validation complete",
                "output_summary": "unconfirmed",
            }
        )
    )
    artifact_dir.joinpath("report.md").write_text(report)
    if include_playbook:
        artifact_dir.joinpath("playbook.json").write_text(
            json.dumps(
                {
                    "stage": "PLAYBOOK",
                    "playbook_id": "playbook-1",
                    "failure_type": "test",
                    "symptom_pattern": "test pattern",
                    "severity_criteria": "high",
                    "related_metrics": ["AWS/ECS CPUUtilization"],
                    "verification_steps": ["check metric"],
                    "temporary_mitigation": "not attempted",
                    "permanent_remediation": "fix leak",
                    "escalation_criteria": "metric remains high",
                    "prevention_measures": ["test"],
                    "tags": ["test"],
                    "remediation_result": {
                        "status": "NOT_ATTEMPTED",
                        "fault_type": None,
                        "endpoint_path": None,
                        "reason": "unconfirmed root cause",
                        "validation_artifact": "validation-1.json",
                        "verification": {
                            "status": "PENDING",
                            "reason": "reset was not attempted",
                        },
                    },
                    "summary": "playbook complete",
                    "output_summary": "not attempted",
                }
            )
        )


def _write_confirmed_report_artifacts(
    artifact_dir: Path,
    *,
    remediation_status: str = "SUCCEEDED",
    playbook_status: str | None = None,
    fault_type: str = "db-leak",
    endpoint_path: str | None = "/fault/db-leak/reset",
    remediation_reason: str = "Healthcare reset completed via /fault/db-leak/reset",
    verification_status: str = "NORMALIZED",
) -> None:
    endpoint_text = endpoint_path or "N/A"
    report = (
        _valid_report("DB connection leak")
        .replace(
            """status: NOT_ATTEMPTED
fault_type: N/A
endpoint_path: N/A
validation_artifact: validation-1.json""",
            f"""status: {remediation_status}
fault_type: {fault_type}
endpoint_path: {endpoint_text}
validation_artifact: validation-1.json""",
        )
        .replace("## 검증 상태\nPENDING", f"## 검증 상태\n{verification_status}")
    )
    _write_required_report_artifacts(artifact_dir, report)

    validation = json.loads(artifact_dir.joinpath("validation-1.json").read_text())
    validation["confirmed"] = [
        {
            "hypothesis_id": "hypothesis-1",
            "confidence": 0.95,
            "fault_type": fault_type,
            "reasoning": "connection leak confirmed",
        }
    ]
    validation["needs_investigation"] = []
    artifact_dir.joinpath("validation-1.json").write_text(json.dumps(validation))

    hypotheses = json.loads(artifact_dir.joinpath("hypotheses.json").read_text())
    hypotheses["hypotheses"][0]["fault_type"] = fault_type
    artifact_dir.joinpath("hypotheses.json").write_text(json.dumps(hypotheses))

    remediation = {
        "stage": "REMEDIATION",
        "status": remediation_status,
        "fault_type": fault_type,
        "endpoint_path": endpoint_path,
        "validation_artifact": "validation-1.json",
        "confirmed_hypothesis_ids": ["hypothesis-1"],
        "summary": remediation_reason,
        "output_summary": f"{remediation_status}: {remediation_reason}",
        "verification": {
            "status": verification_status,
            "reason": f"verification status: {verification_status}",
        },
    }
    artifact_dir.joinpath("remediation.json").write_text(json.dumps(remediation))

    playbook = json.loads(artifact_dir.joinpath("playbook.json").read_text())
    playbook["remediation_result"] = {
        "status": playbook_status or remediation_status,
        "fault_type": fault_type,
        "endpoint_path": endpoint_path,
        "reason": remediation_reason,
        "validation_artifact": "validation-1.json",
        "verification": {
            "status": verification_status,
            "reason": f"verification status: {verification_status}",
        },
    }
    artifact_dir.joinpath("playbook.json").write_text(json.dumps(playbook))


def test_extract_root_cause_skips_report_status_metadata():
    report = """# RCA 최종 보고서

## 근본 원인

**상태: 확정 (Confirmed)**
**신뢰도: 0.95**

> Healthcare fault injection API leaked 35 database connections.

**확정 가설 ID**: `hypothesis-1`

## 복구 결과

SUCCEEDED
"""

    assert extract_root_cause(report) == "Healthcare fault injection API leaked 35 database connections."


def test_redelivered_message_runs_only_after_atomic_session_claim(monkeypatch):
    container = _container(SimpleNamespace())
    orchestrator = PipelineOrchestrator(container)
    run_rca = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_rca", run_rca)

    result = orchestrator.process_message(json.dumps(ALARM_DATA), receive_count=2)

    assert result is True
    claim = container.session_store.claim_session.call_args
    assert claim.kwargs["receive_count"] == 2
    assert claim.kwargs["alarm_data"] == ALARM_DATA
    assert run_rca.call_args.args[3] == CLAIM_TOKEN


def test_35_minute_redelivery_bypasses_initial_alarm_staleness_check(monkeypatch):
    container = _container(SimpleNamespace())
    orchestrator = PipelineOrchestrator(container)
    run_rca = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_rca", run_rca)
    stale_alarm = {
        **ALARM_DATA,
        "StateChangeTime": (datetime.now(UTC) - timedelta(minutes=35)).isoformat(),
    }

    result = orchestrator.process_message(json.dumps(stale_alarm), receive_count=2)

    assert result is True
    run_rca.assert_called_once()
    container.session_store.mark_outdated.assert_not_called()


def test_initial_stale_alarm_is_still_rejected(monkeypatch):
    container = _container(SimpleNamespace())
    orchestrator = PipelineOrchestrator(container)
    run_rca = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_rca", run_rca)
    stale_alarm = {
        **ALARM_DATA,
        "StateChangeTime": (datetime.now(UTC) - timedelta(minutes=35)).isoformat(),
    }

    result = orchestrator.process_message(json.dumps(stale_alarm), receive_count=1)

    assert result is True
    run_rca.assert_not_called()
    container.session_store.mark_outdated.assert_called_once()


def test_competing_delivery_is_acknowledged_without_duplicate_execution(monkeypatch):
    container = _container(SimpleNamespace())
    container.session_store.claim_session.return_value = SessionClaim(ClaimDisposition.CONTENDED)
    orchestrator = PipelineOrchestrator(container)
    run_rca = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_rca", run_rca)

    result = orchestrator.process_message(json.dumps(ALARM_DATA), receive_count=2)

    assert result is False
    run_rca.assert_not_called()


def test_terminal_duplicate_is_acknowledged_without_execution(monkeypatch):
    container = _container(SimpleNamespace())
    container.session_store.claim_session.return_value = SessionClaim(ClaimDisposition.TERMINAL_DUPLICATE)
    orchestrator = PipelineOrchestrator(container)
    run_rca = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_rca", run_rca)

    assert orchestrator.process_message(json.dumps(ALARM_DATA), receive_count=2) is True
    run_rca.assert_not_called()


def test_successful_run_requires_report_artifact(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CcResult(True, "stdout fallback", "{}")))
    container = _container(runner)
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is False
    container.session_store.mark_failed.assert_called_once()
    container.session_store.mark_completed.assert_not_called()
    container.report_store.save_report.assert_not_called()


def test_report_artifact_is_uploaded_without_using_cli_fallback(monkeypatch, tmp_path):
    report = _valid_report("DB connection leak")

    class ReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_required_report_artifacts(artifact_dir_for_token(execution_token), report)
            return CcResult(True, "different cli output", "{}")

    container = _container(ReportWriter())
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is True
    container.report_store.save_report.assert_called_once_with(
        "rca-1",
        report,
        claim_token=CLAIM_TOKEN,
        attempt=1,
    )
    container.session_store.mark_completed.assert_called_once_with(
        "rca-1",
        "DB connection leak",
        "reports/rca.md",
        claim_token=CLAIM_TOKEN,
        side_effect_lease_token="lease-token",
    )
    container.report_store.send_notification.assert_called_once()


def test_unconfirmed_result_does_not_request_remediation(monkeypatch, tmp_path):
    class UnconfirmedReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            artifact_dir = artifact_dir_for_token(execution_token)
            _write_required_report_artifacts(
                artifact_dir,
                _valid_report("Most likely connection leak"),
            )
            return CcResult(True, "complete", "{}")

    container = _container(UnconfirmedReportWriter())
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is True
    notification = container.report_store.send_notification.call_args
    assert notification.kwargs["confirmed"] is False


def test_confirmed_completion_uses_server_owned_remediation_result(monkeypatch, tmp_path):
    class ConfirmedReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_confirmed_report_artifacts(artifact_dir_for_token(execution_token))
            return CcResult(True, "complete", "{}")

    container = _container(ConfirmedReportWriter())
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is True
    assert container.report_store.send_notification.call_args.kwargs["confirmed"] is True


def test_remediation_mismatch_prevents_completion_and_all_publication(monkeypatch, tmp_path):
    class MismatchedReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_confirmed_report_artifacts(
                artifact_dir_for_token(execution_token),
                playbook_status="FAILED",
            )
            return CcResult(True, "complete", "{}")

    container = _container(MismatchedReportWriter())
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is False
    container.report_store.save_report.assert_not_called()
    container.report_store.send_notification.assert_not_called()
    container.session_store.mark_completed.assert_not_called()


def test_confirmed_unsupported_remediation_can_complete_with_blocked_report(monkeypatch, tmp_path):
    class UnsupportedReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_confirmed_report_artifacts(
                artifact_dir_for_token(execution_token),
                remediation_status="BLOCKED",
                fault_type="unsupported",
                endpoint_path=None,
                remediation_reason="confirmed root cause has no allowlisted remediation action",
                verification_status="PENDING",
            )
            return CcResult(True, "complete", "{}")

    container = _container(UnsupportedReportWriter())
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is True
    container.report_store.send_notification.assert_called_once()
    container.session_store.mark_completed.assert_called_once()


def test_confirmed_known_fault_mismatch_can_complete_with_blocked_report(monkeypatch, tmp_path):
    class MismatchedActionReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_confirmed_report_artifacts(
                artifact_dir_for_token(execution_token),
                remediation_status="BLOCKED",
                fault_type="high-cpu",
                endpoint_path=None,
                remediation_reason="requested action does not uniquely match the confirmed root cause",
                verification_status="PENDING",
            )
            return CcResult(True, "complete", "{}")

    container = _container(MismatchedActionReportWriter())
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is True
    container.report_store.send_notification.assert_called_once()
    container.session_store.mark_completed.assert_called_once()


def test_failed_cc_run_never_publishes_report(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CcResult(False, "model failed", "diagnostic")))
    container = _container(runner)
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is False
    container.session_store.mark_failed.assert_called_once_with(
        "rca-1",
        "model failed",
        claim_token=CLAIM_TOKEN,
    )
    container.report_store.save_report.assert_not_called()
    container.session_store.mark_completed.assert_not_called()


def test_sigterm_failure_keeps_message_for_sqs_redelivery(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CcResult(True, "cancelled", "{}")))
    container = _container(runner)
    shutdown_event = Event()
    shutdown_event.set()
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container, shutdown_event=shutdown_event)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is False
    container.session_store.mark_failed.assert_called_once_with(
        "rca-1",
        "Aborted due to SIGTERM shutdown",
        claim_token=CLAIM_TOKEN,
    )
    container.session_store.mark_completed.assert_not_called()


def test_notification_failure_does_not_finalize_completed_session(monkeypatch, tmp_path):
    class ReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_required_report_artifacts(
                artifact_dir_for_token(execution_token),
                _valid_report("Connection leak"),
            )
            return CcResult(True, "complete", "{}")

    container = _container(ReportWriter())
    container.report_store.send_notification.side_effect = RuntimeError("SNS unavailable")
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is False
    container.report_store.send_notification.assert_called_once()
    container.session_store.mark_failed.assert_called_once()
    container.session_store.mark_completed.assert_not_called()


def test_terminated_session_does_not_publish_artifacts(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CcResult(True, "complete", "{}")))
    container = _container(runner)
    container.session_store.is_terminated.side_effect = [True]
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is False
    container.report_store.save_report.assert_not_called()
    container.session_store.mark_completed.assert_not_called()


def test_ownership_read_error_keeps_message_for_redelivery(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CcResult(True, "cancelled", "{}")))
    container = _container(runner)
    container.session_store.is_terminated.side_effect = SessionOwnershipCheckError("DDB unavailable")
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is False
    container.report_store.save_report.assert_not_called()
    container.session_store.mark_completed.assert_not_called()


def test_execution_token_and_watcher_use_the_same_path_but_keep_rca_id(monkeypatch, tmp_path):
    captured = {}

    class ReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            captured["token"] = execution_token
            captured["runner_path"] = artifact_dir_for_token(execution_token)
            _write_required_report_artifacts(captured["runner_path"], _valid_report("isolated"))
            return CcResult(True, "complete", "{}")

    def _start_watcher(artifact_dir, rca_id, claim_token, ddb):
        captured["watcher_path"] = artifact_dir
        captured["watcher_rca_id"] = rca_id
        captured["watcher_claim_token"] = claim_token
        return FinishedThread(), Mock()

    container = _container(ReportWriter())
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "start_watcher", _start_watcher)

    result = PipelineOrchestrator(container)._run_rca(
        "ddb-rca-id",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is True
    assert captured["watcher_path"] == captured["runner_path"]
    assert captured["watcher_rca_id"] == "ddb-rca-id"
    assert captured["watcher_claim_token"] == CLAIM_TOKEN
    assert not captured["watcher_path"].exists()


def test_consecutive_same_rca_id_runs_use_distinct_dirs_without_touching_existing_artifacts(monkeypatch, tmp_path):
    tokens = []
    artifact_dirs = []
    legacy_report = tmp_path / "rca-same-id" / "report.md"
    legacy_report.parent.mkdir()
    legacy_report.write_text("previous run")

    class ReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            tokens.append(execution_token)
            artifact_dir = artifact_dir_for_token(execution_token)
            artifact_dirs.append(artifact_dir)
            _write_required_report_artifacts(artifact_dir, _valid_report("fresh run"))
            return CcResult(True, "complete", "{}")

    container = _container(ReportWriter())
    _patch_runtime(monkeypatch, tmp_path)
    orchestrator = PipelineOrchestrator(container)

    assert orchestrator._run_rca("same-id", ALARM_DATA, structlog.get_logger(), CLAIM_TOKEN) is True
    assert orchestrator._run_rca("same-id", ALARM_DATA, structlog.get_logger(), CLAIM_TOKEN) is True

    assert len(set(tokens)) == 2
    assert len(set(artifact_dirs)) == 2
    assert all(not path.exists() for path in artifact_dirs)
    assert legacy_report.read_text() == "previous run"


def test_successful_run_requires_valid_playbook_artifact(monkeypatch, tmp_path):
    class ReportOnlyWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_required_report_artifacts(
                artifact_dir_for_token(execution_token),
                _valid_report("Connection leak"),
                include_playbook=False,
            )
            return CcResult(True, "complete", "{}")

    container = _container(ReportOnlyWriter())
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is False
    container.session_store.mark_failed.assert_called_once_with(
        "rca-1",
        "Completion artifact validation failed: playbook.json is missing",
        claim_token=CLAIM_TOKEN,
    )
    container.session_store.mark_completed.assert_not_called()
