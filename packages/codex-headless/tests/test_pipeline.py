import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import structlog
from structlog.testing import capture_logs

from codex_headless.adapters.secondary.session.dynamodb_session_store import (
    build_idempotency_key,
    build_rca_id,
)
from codex_headless.config.settings import (
    ALARM_STALENESS_SECONDS,
    CODEX_TIMEOUT_SECONDS,
    PLAYBOOK_UPDATE_THRESHOLD,
)
from codex_headless.ports.dto.models import CodexResult
from codex_headless.ports.interfaces.session_store import (
    ClaimDisposition,
    IncidentAlarm,
    IncidentClaim,
    IncidentClaimDisposition,
    SessionClaim,
    SessionOwnershipCheckError,
)
from codex_headless.services import execution_context, pipeline
from codex_headless.services.execution_context import artifact_dir_for_token
from codex_headless.services.pipeline import PipelineOrchestrator, extract_root_cause

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
        claim_incident=Mock(
            side_effect=lambda alarm, **_: IncidentClaim(
                IncidentClaimDisposition.PROCEED,
                build_rca_id(build_idempotency_key(alarm)),
                1,
            )
        ),
        record_recovery=Mock(return_value=True),
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

    playbook_store = SimpleNamespace(
        load_playbook=Mock(side_effect=_load_playbook),
        save_to_s3_vectors=Mock(return_value=True),
        # 기본값은 축적된 플레이북이 없는 첫 분석이다.
        search_similar=Mock(return_value=[]),
        load_detail=Mock(return_value=None),
    )
    return SimpleNamespace(
        session_store=store,
        report_store=report_store,
        playbook_store=playbook_store,
        codex_runner=runner,
        dynamodb_client=None,
    )


def _patch_runtime(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    monkeypatch.setattr(pipeline, "build_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(pipeline, "start_watcher", lambda *args: (FinishedThread(), Mock()))


def _valid_report(root_cause: str = "DB connection leak", *, step_ids: tuple[str, ...] = ()) -> str:
    steps = "\n".join(f"- {step_id}: 절차" for step_id in step_ids) or "확정 원인이 없어 실행 절차가 없다"
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
## 대응 플레이북
검증되지 않은 DRAFT 절차
{steps}
## Action Items
후속 관측
"""


def _write_required_report_artifacts(
    artifact_dir: Path,
    report: str,
    *,
    include_playbook: bool = True,
    execution_steps: list[dict] | None = None,
) -> None:
    artifact_dir.joinpath("scoping.json").write_text(
        json.dumps(
            {
                "stage": "SCOPING",
                "alarm_name": "HighCPU",
                "impact_scope": "service",
                "severity": "high",
                "metric_observations": [
                    {
                        "metric_name": "DatabaseConnections",
                        "datapoints": [2, 12, 20, 27, 30],
                        "trend": "rising",
                    }
                ],
                "concurrent_alarms": [],
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
                    "verification_status": "DRAFT",
                    "execution_steps": execution_steps or [],
                    "summary": "playbook complete",
                    "output_summary": "draft playbook",
                }
            )
        )


_STEP = {
    "step_id": "step-1",
    "intent": "누수 커넥션을 해소한다",
    "action": "healthcare 서비스를 재시작한다",
    "success_criteria": "DatabaseConnections가 30 미만으로 복귀한다",
}


def _write_confirmed_report_artifacts(
    artifact_dir: Path,
    *,
    fault_type: str = "db-leak",
    execution_steps: list[dict] | None = None,
) -> None:
    steps = execution_steps if execution_steps is not None else [dict(_STEP)]
    _write_required_report_artifacts(
        artifact_dir,
        _valid_report("DB connection leak", step_ids=tuple(step["step_id"] for step in steps)),
        execution_steps=steps,
    )

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


def _past_staleness_boundary() -> str:
    """건너뛰기 기준을 막 넘긴 시각. 기준 값이 바뀌어도 이 테스트의 의도는 유지된다."""
    return (datetime.now(UTC) - timedelta(seconds=ALARM_STALENESS_SECONDS + 300)).isoformat()


def test_redelivery_bypasses_initial_alarm_staleness_check(monkeypatch):
    container = _container(SimpleNamespace())
    orchestrator = PipelineOrchestrator(container)
    run_rca = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_rca", run_rca)
    stale_alarm = {**ALARM_DATA, "StateChangeTime": _past_staleness_boundary()}

    result = orchestrator.process_message(json.dumps(stale_alarm), receive_count=2)

    assert result is True
    run_rca.assert_called_once()
    container.session_store.mark_outdated.assert_not_called()


def test_initial_stale_alarm_is_still_rejected(monkeypatch):
    container = _container(SimpleNamespace())
    orchestrator = PipelineOrchestrator(container)
    run_rca = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_rca", run_rca)
    stale_alarm = {**ALARM_DATA, "StateChangeTime": _past_staleness_boundary()}

    result = orchestrator.process_message(json.dumps(stale_alarm), receive_count=1)

    assert result is True
    run_rca.assert_not_called()
    container.session_store.claim_incident.assert_not_called()
    container.session_store.mark_outdated.assert_called_once()


def test_staleness_boundary_is_not_shorter_than_the_analysis_budget():
    """예산 초과 한 회차가 뒤따르는 알람을 폐기하게 해서는 안 된다.

    이 워커는 한 세션씩 직렬로 처리하므로 한 회차가 예산을 다 쓰면 그만큼의 대기가
    다음 알람에 그대로 전가된다. 건너뛰기 기준이 예산보다 짧으면 초과 한 번이 그 뒤
    여러 알람을 통째로 버린다 — 라이브에서 4건이 그렇게 사라졌다.
    """
    assert ALARM_STALENESS_SECONDS >= CODEX_TIMEOUT_SECONDS


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


def test_evaluated_suppressed_incident_is_acknowledged_without_session_claim(monkeypatch):
    container = _container(SimpleNamespace())
    container.session_store.claim_incident.side_effect = None
    container.session_store.claim_incident.return_value = IncidentClaim(
        IncidentClaimDisposition.SUPPRESSED,
        "existing-rca",
        1,
        "strands#SESSION is SCOPING",
    )
    orchestrator = PipelineOrchestrator(container)
    run_rca = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_rca", run_rca)

    assert orchestrator.process_message(json.dumps(ALARM_DATA)) is True
    container.session_store.claim_session.assert_not_called()
    run_rca.assert_not_called()


def test_suppressed_incident_is_retried_until_delayed_recovery_is_recorded(monkeypatch):
    container = _container(SimpleNamespace())
    alarm_at = datetime.now(UTC)
    alarm_body = {**ALARM_DATA, "StateChangeTime": alarm_at.isoformat()}
    candidate = build_rca_id(
        build_idempotency_key(
            IncidentAlarm(
                alarm_name=alarm_body["AlarmName"],
                region=alarm_body["Region"],
                state_change_time=alarm_at,
            )
        )
    )
    container.session_store.claim_incident.side_effect = [
        IncidentClaim(
            IncidentClaimDisposition.SUPPRESSED,
            "previous-rca",
            1,
            "incident has no recovery observation",
            retryable=True,
        ),
        IncidentClaim(IncidentClaimDisposition.PROCEED, candidate, 2),
        IncidentClaim(IncidentClaimDisposition.PROCEED, candidate, 2),
    ]
    container.session_store.claim_session.side_effect = [
        SessionClaim(ClaimDisposition.CLAIMED, CLAIM_TOKEN, 2),
        SessionClaim(ClaimDisposition.TERMINAL_DUPLICATE),
    ]
    orchestrator = PipelineOrchestrator(container)
    run_rca = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_rca", run_rca)

    assert orchestrator.process_message(json.dumps(alarm_body), receive_count=1) is False
    assert (
        orchestrator.process_message(
            json.dumps(
                {
                    **alarm_body,
                    "NewStateValue": "OK",
                    "StateChangeTime": (alarm_at - timedelta(seconds=301)).isoformat(),
                }
            )
        )
        is True
    )
    assert orchestrator.process_message(json.dumps(alarm_body), receive_count=2) is True
    assert orchestrator.process_message(json.dumps(alarm_body), receive_count=3) is True

    container.session_store.record_recovery.assert_called_once()
    assert run_rca.call_count == 1


def test_contended_incident_is_not_acknowledged(monkeypatch):
    container = _container(SimpleNamespace())
    container.session_store.claim_incident.side_effect = None
    container.session_store.claim_incident.return_value = IncidentClaim(
        IncidentClaimDisposition.CONTENDED,
        "candidate-rca",
    )
    orchestrator = PipelineOrchestrator(container)
    run_rca = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_rca", run_rca)

    assert orchestrator.process_message(json.dumps(ALARM_DATA)) is False
    container.session_store.claim_session.assert_not_called()
    run_rca.assert_not_called()


def test_ok_records_recovery_before_non_alarm_skip():
    container = _container(SimpleNamespace())
    body = {
        **ALARM_DATA,
        "AlarmArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:HighCPU",
        "NewStateValue": "OK",
        "StateChangeTime": "2026-08-07T14:05:00+00:00",
    }

    assert PipelineOrchestrator(container).process_message(json.dumps(body)) is True
    recovery_alarm = container.session_store.record_recovery.call_args.args[0]
    assert recovery_alarm.new_state == "OK"
    assert recovery_alarm.alarm_arn == body["AlarmArn"]
    container.session_store.claim_incident.assert_not_called()
    container.session_store.claim_session.assert_not_called()


def test_ok_is_not_acknowledged_when_recovery_cannot_be_recorded():
    container = _container(SimpleNamespace())
    container.session_store.record_recovery.return_value = False
    body = {**ALARM_DATA, "NewStateValue": "OK"}

    assert PipelineOrchestrator(container).process_message(json.dumps(body)) is False
    container.session_store.claim_incident.assert_not_called()
    container.session_store.claim_session.assert_not_called()


def test_successful_run_requires_report_artifact(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CodexResult(True, "stdout fallback", "{}")))
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
            return CodexResult(True, "different cli output", "{}")

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
    completed = container.session_store.mark_completed.call_args
    assert completed.args == ("rca-1", "DB connection leak", "reports/rca.md")
    assert completed.kwargs["playbook"]["playbook_id"] == "playbook-1"
    assert completed.kwargs["confirmed"] is False
    assert completed.kwargs["claim_token"] == CLAIM_TOKEN
    assert completed.kwargs["side_effect_lease_token"] == "lease-token"
    container.report_store.send_notification.assert_called_once()


def test_unconfirmed_result_completes_without_execution_steps(monkeypatch, tmp_path):
    class UnconfirmedReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            artifact_dir = artifact_dir_for_token(execution_token)
            _write_required_report_artifacts(
                artifact_dir,
                _valid_report("Most likely connection leak"),
            )
            return CodexResult(True, "complete", "{}")

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


def test_confirmed_completion_publishes_a_report_with_its_playbook(monkeypatch, tmp_path):
    class ConfirmedReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_confirmed_report_artifacts(artifact_dir_for_token(execution_token))
            return CodexResult(True, "complete", "{}")

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
    completed = container.session_store.mark_completed.call_args.kwargs
    assert completed["playbook"]["playbook_id"] == "playbook-1"
    assert completed["confirmed"] is True


class TestPlaybookSearchFirstMerge:
    """같은 유형의 기존 플레이북이 있으면 그것을 보강한다.

    새 식별자로 분기하면 같은 증상의 플레이북이 여럿이 되어 회고가 쌓아 온 검증된 절차가
    다음 실행의 근거가 되지 못한다.
    """

    def _run(self, container, monkeypatch, tmp_path) -> bool:
        class ConfirmedReportWriter:
            def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
                _write_confirmed_report_artifacts(artifact_dir_for_token(execution_token))
                return CodexResult(True, "complete", "{}")

        container.codex_runner = ConfirmedReportWriter()
        _patch_runtime(monkeypatch, tmp_path)
        return PipelineOrchestrator(container)._run_rca(
            "rca-1",
            ALARM_DATA,
            structlog.get_logger(),
            CLAIM_TOKEN,
        )

    def _hit(self, playbook_id: str = "pb-existing") -> SimpleNamespace:
        return SimpleNamespace(
            playbook_id=playbook_id,
            similarity=0.93,
            failure_type="DB connection leak",
            symptom_pattern="커넥션 수 상승",
            tags=[],
            rca_id="rca-old",
            verification_status="VERIFIED",
        )

    def _existing(self) -> dict:
        return {
            "playbook_id": "pb-existing",
            "failure_type": "DB connection leak",
            "symptom_pattern": "커넥션 수 상승",
            "verification_status": "VERIFIED",
            "escalation_criteria": "기존 에스컬레이션 기준",
            "execution_steps": [
                {
                    "step_id": "step-legacy",
                    "intent": "커넥션 회수",
                    "action": "회고가 교정한 인자로 서비스를 갱신한다",
                    "success_criteria": "커넥션 수 감소",
                }
            ],
        }

    def _saved(self, container) -> dict:
        return container.playbook_store.save_to_s3_vectors.call_args.args[0]

    def test_keeps_the_existing_identifier_instead_of_branching(self, monkeypatch, tmp_path):
        container = _container(None)
        container.playbook_store.search_similar = Mock(return_value=[self._hit()])
        container.playbook_store.load_detail = Mock(return_value=self._existing())

        assert self._run(container, monkeypatch, tmp_path) is True
        assert self._saved(container)["playbook_id"] == "pb-existing"
        # The session owns the exact validated artifact for this analysis. The
        # search-index merge is future knowledge and must not replace it.
        assert container.session_store.mark_completed.call_args.kwargs["playbook"]["playbook_id"] == "playbook-1"

    def test_merge_never_drops_accumulated_steps(self, monkeypatch, tmp_path):
        container = _container(None)
        container.playbook_store.search_similar = Mock(return_value=[self._hit()])
        container.playbook_store.load_detail = Mock(return_value=self._existing())

        self._run(container, monkeypatch, tmp_path)
        saved = self._saved(container)

        step_ids = [step["step_id"] for step in saved["execution_steps"]]
        assert "step-legacy" in step_ids
        # 회고가 교정한 인자가 살아남아야 한다 — 여기가 퇴행하는 지점이었다.
        legacy = next(step for step in saved["execution_steps"] if step["step_id"] == "step-legacy")
        assert legacy["action"] == "회고가 교정한 인자로 서비스를 갱신한다"
        # 기존 절차는 유지되고 새 절차는 뒤에 붙는다. 순서를 재배치하면 과거 실행 증거가
        # 가리키는 절차를 찾을 수 없다.
        assert step_ids[0] == "step-legacy"
        assert len(step_ids) > 1

    def test_analysis_may_enrich_a_field_the_existing_playbook_already_had(self, monkeypatch, tmp_path):
        container = _container(None)
        container.playbook_store.search_similar = Mock(return_value=[self._hit()])
        container.playbook_store.load_detail = Mock(return_value=self._existing())

        self._run(container, monkeypatch, tmp_path)

        # 병합은 삭제만 금지한다. 새 분석이 값을 제공하면 그것이 보강이고, 값을 비워
        # 반환했을 때만 기존 값이 유지된다.
        assert self._saved(container)["escalation_criteria"] == "metric remains high"

    def test_merge_keeps_fields_the_new_analysis_never_mentioned(self, monkeypatch, tmp_path):
        container = _container(None)
        existing = self._existing()
        # 새 분석의 산출물에 없는 필드. 회고가 붙였을 수도 있는 축적이다.
        existing["runbook_url"] = "https://runbook.example/db-leak"
        container.playbook_store.search_similar = Mock(return_value=[self._hit()])
        container.playbook_store.load_detail = Mock(return_value=existing)

        self._run(container, monkeypatch, tmp_path)

        # 새 분석이 언급하지 않은 필드가 사라지면 축적이 조용히 되돌아간다.
        assert self._saved(container)["runbook_url"] == "https://runbook.example/db-leak"

    def test_changed_execution_steps_are_downgraded_to_draft(self, monkeypatch, tmp_path):
        container = _container(None)
        container.playbook_store.search_similar = Mock(return_value=[self._hit()])
        container.playbook_store.load_detail = Mock(return_value=self._existing())

        self._run(container, monkeypatch, tmp_path)

        assert self._saved(container)["verification_status"] == "DRAFT"

    def test_unchanged_execution_steps_preserve_verified_status(self, monkeypatch, tmp_path):
        container = _container(None)
        existing = self._existing()
        existing["execution_steps"] = [dict(_STEP)]
        container.playbook_store.search_similar = Mock(return_value=[self._hit()])
        container.playbook_store.load_detail = Mock(return_value=existing)

        self._run(container, monkeypatch, tmp_path)

        assert self._saved(container)["verification_status"] == "VERIFIED"

    def test_uses_the_merge_threshold_not_a_plain_retrieval_cutoff(self, monkeypatch, tmp_path):
        container = _container(None)
        container.playbook_store.search_similar = Mock(return_value=[])

        self._run(container, monkeypatch, tmp_path)

        assert container.playbook_store.search_similar.call_args.kwargs["threshold"] == PLAYBOOK_UPDATE_THRESHOLD

    def test_threshold_admits_a_recurrence_whose_wording_differs(self):
        # 실측: 같은 유형·패턴을 다른 문장으로 쓴 재발이 0.83, 글자까지 같으면 0.96.
        # 임계값이 그 사이에 있으면 같은 장애의 재발조차 병합되지 않고, 그 실패는 빈
        # 검색 결과로 나타나 정상적인 신규 생성과 구별되지 않는다.
        assert PLAYBOOK_UPDATE_THRESHOLD <= 0.83

    def test_searches_with_the_playbook_fields_the_index_stores(self, monkeypatch, tmp_path):
        container = _container(None)
        container.playbook_store.search_similar = Mock(return_value=[])

        self._run(container, monkeypatch, tmp_path)

        query = container.playbook_store.search_similar.call_args.args[0]
        saved = self._saved(container)
        # 저장과 검색이 같은 필드에서 나와야 유사도가 의미를 가진다.
        assert saved["failure_type"][:40] in query
        assert saved["symptom_pattern"][:40] in query

    def test_skips_a_candidate_whose_detail_cannot_be_read(self, monkeypatch, tmp_path):
        container = _container(None)
        container.playbook_store.search_similar = Mock(return_value=[self._hit()])
        container.playbook_store.load_detail = Mock(return_value=None)

        assert self._run(container, monkeypatch, tmp_path) is True
        # 절차를 보지 못한 "보강"은 축적을 되돌리므로 신규 생성으로 떨어진다.
        assert self._saved(container)["playbook_id"] != "pb-existing"

    def test_search_failure_does_not_block_the_analysis(self, monkeypatch, tmp_path):
        container = _container(None)
        container.playbook_store.search_similar = Mock(side_effect=RuntimeError("index down"))

        # 플레이북은 미래를 위한 자산이고 이번 RCA 의 결과물은 리포트다.
        assert self._run(container, monkeypatch, tmp_path) is True
        container.playbook_store.save_to_s3_vectors.assert_called_once()

    def test_first_analysis_of_a_symptom_creates_its_own_playbook(self, monkeypatch, tmp_path):
        container = _container(None)

        assert self._run(container, monkeypatch, tmp_path) is True
        container.playbook_store.load_detail.assert_not_called()


def test_report_omitting_a_structured_step_prevents_completion_and_all_publication(monkeypatch, tmp_path):
    # The prose is what a person approves. A step present only in the structure
    # would run without approval, so the session must not complete.
    class MismatchedReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            artifact_dir = artifact_dir_for_token(execution_token)
            _write_confirmed_report_artifacts(artifact_dir)
            playbook = json.loads(artifact_dir.joinpath("playbook.json").read_text())
            playbook["execution_steps"].append({**_STEP, "step_id": "step-2"})
            artifact_dir.joinpath("playbook.json").write_text(json.dumps(playbook))
            return CodexResult(True, "complete", "{}")

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


def test_unconfirmed_run_declaring_execution_steps_prevents_completion(monkeypatch, tmp_path):
    # Steps for an unconfirmed cause would put guesswork behind the approval
    # button, so the gate refuses the run rather than letting it be approved.
    class OvereagerReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            artifact_dir = artifact_dir_for_token(execution_token)
            _write_required_report_artifacts(
                artifact_dir,
                _valid_report("Most likely connection leak", step_ids=("step-1",)),
                execution_steps=[dict(_STEP)],
            )
            return CodexResult(True, "complete", "{}")

    container = _container(OvereagerReportWriter())
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is False
    container.session_store.mark_completed.assert_not_called()


def test_a_run_never_publishes_a_recovery_it_did_not_perform(monkeypatch, tmp_path):
    # This engine only analyses. Nothing in the completed session may claim a
    # recovery outcome, because execution happens later and elsewhere.
    class ConfirmedReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_confirmed_report_artifacts(artifact_dir_for_token(execution_token))
            return CodexResult(True, "complete", "{}")

    container = _container(ConfirmedReportWriter())
    _patch_runtime(monkeypatch, tmp_path)

    PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    playbook = container.report_store.send_notification.call_args.kwargs["playbook"]
    assert "remediation_result" not in playbook
    assert playbook["verification_status"] == "DRAFT"


def test_failed_cc_run_never_publishes_report(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CodexResult(False, "model failed", "diagnostic")))
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
    runner = SimpleNamespace(run=Mock(return_value=CodexResult(True, "cancelled", "{}")))
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
            return CodexResult(True, "complete", "{}")

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


def test_missing_report_key_does_not_finalize_completed_session(monkeypatch, tmp_path):
    class ReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_required_report_artifacts(
                artifact_dir_for_token(execution_token),
                _valid_report("Connection leak"),
            )
            return CodexResult(True, "complete", "{}")

    container = _container(ReportWriter())
    container.report_store.save_report.return_value = ""
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca(
        "rca-1",
        ALARM_DATA,
        structlog.get_logger(),
        CLAIM_TOKEN,
    )

    assert result is False
    container.session_store.mark_failed.assert_called_once()
    container.report_store.send_notification.assert_not_called()
    container.session_store.mark_completed.assert_not_called()


def test_playbook_save_false_releases_lease_and_keeps_message_for_redelivery(monkeypatch, tmp_path):
    class ReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_required_report_artifacts(
                artifact_dir_for_token(execution_token),
                _valid_report("Connection leak"),
            )
            return CodexResult(True, "complete", "{}")

    container = _container(ReportWriter())
    container.playbook_store.save_to_s3_vectors.return_value = False
    _patch_runtime(monkeypatch, tmp_path)

    with capture_logs() as logs:
        result = PipelineOrchestrator(container).process_message(json.dumps(ALARM_DATA))

    assert result is False
    failed_rca_id = container.session_store.mark_failed.call_args.args[0]
    container.session_store.release_side_effect_lease.assert_called_once_with(
        failed_rca_id,
        claim_token=CLAIM_TOKEN,
        lease_token="lease-token",
    )
    container.session_store.mark_failed.assert_called_once_with(
        failed_rca_id,
        "Unhandled pipeline exception",
        claim_token=CLAIM_TOKEN,
    )
    container.report_store.save_report.assert_not_called()
    container.report_store.send_notification.assert_not_called()
    container.session_store.mark_completed.assert_not_called()
    assert all(entry["event"] != "playbook_saved" for entry in logs)


def test_playbook_save_exception_releases_lease_and_keeps_message_for_redelivery(monkeypatch, tmp_path):
    class ReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker, **kwargs):
            _write_required_report_artifacts(
                artifact_dir_for_token(execution_token),
                _valid_report("Connection leak"),
            )
            return CodexResult(True, "complete", "{}")

    container = _container(ReportWriter())
    container.playbook_store.save_to_s3_vectors.side_effect = RuntimeError("S3 Vectors unavailable")
    _patch_runtime(monkeypatch, tmp_path)

    with capture_logs() as logs:
        result = PipelineOrchestrator(container).process_message(json.dumps(ALARM_DATA))

    assert result is False
    failed_rca_id = container.session_store.mark_failed.call_args.args[0]
    container.session_store.release_side_effect_lease.assert_called_once_with(
        failed_rca_id,
        claim_token=CLAIM_TOKEN,
        lease_token="lease-token",
    )
    container.session_store.mark_failed.assert_called_once_with(
        failed_rca_id,
        "Unhandled pipeline exception",
        claim_token=CLAIM_TOKEN,
    )
    container.report_store.save_report.assert_not_called()
    container.report_store.send_notification.assert_not_called()
    container.session_store.mark_completed.assert_not_called()
    assert all(entry["event"] != "playbook_saved" for entry in logs)


def test_terminated_session_does_not_publish_artifacts(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CodexResult(True, "complete", "{}")))
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
    runner = SimpleNamespace(run=Mock(return_value=CodexResult(True, "cancelled", "{}")))
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
            return CodexResult(True, "complete", "{}")

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
            return CodexResult(True, "complete", "{}")

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
            return CodexResult(True, "complete", "{}")

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
