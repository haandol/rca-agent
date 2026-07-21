from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import structlog

from cc_headless.ports.dto.models import CcResult
from cc_headless.services import execution_context, pipeline
from cc_headless.services.execution_context import artifact_dir_for_token
from cc_headless.services.pipeline import PipelineOrchestrator

ALARM_DATA = {
    "AlarmName": "HighCPU",
    "NewStateReason": "threshold crossed",
    "Region": "us-east-1",
    "Trigger": {
        "MetricName": "CPUUtilization",
        "Namespace": "AWS/ECS",
    },
}


class FinishedThread:
    def join(self, timeout=None):
        self.timeout = timeout


def _container(runner):
    store = SimpleNamespace(
        update_state=Mock(),
        is_terminated=Mock(return_value=False),
        mark_failed=Mock(),
        mark_completed=Mock(),
    )
    report_store = SimpleNamespace(save_report=Mock(return_value="reports/rca.md"), send_notification=Mock())
    playbook_store = SimpleNamespace(load_playbook=Mock(return_value=None), save_to_s3_vectors=Mock())
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


def test_successful_run_requires_report_artifact(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CcResult(True, "stdout fallback", "{}")))
    container = _container(runner)
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca("rca-1", ALARM_DATA, structlog.get_logger())

    assert result is False
    container.session_store.mark_failed.assert_called_once()
    container.session_store.mark_completed.assert_not_called()
    container.report_store.save_report.assert_not_called()


def test_report_artifact_is_uploaded_without_using_cli_fallback(monkeypatch, tmp_path):
    report = "## 근본 원인\nDB connection leak"

    class ReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker):
            artifact_dir_for_token(execution_token).joinpath("report.md").write_text(report)
            return CcResult(True, "different cli output", "{}")

    container = _container(ReportWriter())
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca("rca-1", ALARM_DATA, structlog.get_logger())

    assert result is True
    container.report_store.save_report.assert_called_once_with("rca-1", report)
    container.session_store.mark_completed.assert_called_once_with("rca-1", "DB connection leak")
    container.report_store.send_notification.assert_called_once()


def test_failed_cc_run_never_publishes_report(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CcResult(False, "model failed", "diagnostic")))
    container = _container(runner)
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca("rca-1", ALARM_DATA, structlog.get_logger())

    assert result is False
    container.session_store.mark_failed.assert_called_once_with("rca-1", "model failed")
    container.report_store.save_report.assert_not_called()
    container.session_store.mark_completed.assert_not_called()


def test_terminated_session_does_not_publish_artifacts(monkeypatch, tmp_path):
    runner = SimpleNamespace(run=Mock(return_value=CcResult(True, "complete", "{}")))
    container = _container(runner)
    container.session_store.is_terminated.side_effect = [True]
    _patch_runtime(monkeypatch, tmp_path)

    result = PipelineOrchestrator(container)._run_rca("rca-1", ALARM_DATA, structlog.get_logger())

    assert result is True
    container.report_store.save_report.assert_not_called()
    container.session_store.mark_completed.assert_not_called()


def test_execution_token_and_watcher_use_the_same_path_but_keep_rca_id(monkeypatch, tmp_path):
    captured = {}

    class ReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker):
            captured["token"] = execution_token
            captured["runner_path"] = artifact_dir_for_token(execution_token)
            captured["runner_path"].joinpath("report.md").write_text("## 근본 원인\nisolated")
            return CcResult(True, "complete", "{}")

    def _start_watcher(artifact_dir, rca_id, ddb):
        captured["watcher_path"] = artifact_dir
        captured["watcher_rca_id"] = rca_id
        return FinishedThread(), Mock()

    container = _container(ReportWriter())
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "start_watcher", _start_watcher)

    result = PipelineOrchestrator(container)._run_rca("ddb-rca-id", ALARM_DATA, structlog.get_logger())

    assert result is True
    assert captured["watcher_path"] == captured["runner_path"]
    assert captured["watcher_rca_id"] == "ddb-rca-id"
    assert not captured["watcher_path"].exists()


def test_consecutive_same_rca_id_runs_use_distinct_dirs_without_touching_existing_artifacts(monkeypatch, tmp_path):
    tokens = []
    artifact_dirs = []
    legacy_report = tmp_path / "rca-same-id" / "report.md"
    legacy_report.parent.mkdir()
    legacy_report.write_text("previous run")

    class ReportWriter:
        def run(self, prompt, *, execution_token, cancel_checker):
            tokens.append(execution_token)
            artifact_dir = artifact_dir_for_token(execution_token)
            artifact_dirs.append(artifact_dir)
            artifact_dir.joinpath("report.md").write_text("## 근본 원인\nfresh run")
            return CcResult(True, "complete", "{}")

    container = _container(ReportWriter())
    _patch_runtime(monkeypatch, tmp_path)
    orchestrator = PipelineOrchestrator(container)

    assert orchestrator._run_rca("same-id", ALARM_DATA, structlog.get_logger()) is True
    assert orchestrator._run_rca("same-id", ALARM_DATA, structlog.get_logger()) is True

    assert len(set(tokens)) == 2
    assert len(set(artifact_dirs)) == 2
    assert all(not path.exists() for path in artifact_dirs)
    assert legacy_report.read_text() == "previous run"
