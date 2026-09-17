"""Execute the real three-part runner with a scripted subprocess transport, never models or live AWS."""

import time

import pytest
from test_pipeline import _valid_report, _write_required_report_artifacts

from headless_codex.adapters.secondary.codex import codex_harness as harness
from headless_codex.adapters.secondary.codex.codex_subprocess_runner import CodexSubprocessRunner
from headless_codex.ports.dto.models import CodexResult
from headless_codex.services import execution_context
from headless_codex.services.analysis_part_workspace import RESULT_FILES, read_object, write_once
from headless_codex.services.three_part_analysis import AnalysisPartInterruptedError, AnalysisPartsRun, LocalPartStore


@pytest.fixture
def work(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "artifacts")
    context = execution_context.ExecutionContext.create("incident")
    context.prepare()
    return context


class Scripted(CodexSubprocessRunner):
    def __init__(self, failures=None, recommendation="UNAVAILABLE", missing=None):
        self.failures = dict(failures or {})
        self.recommendation = recommendation
        self.missing = missing
        self.calls = []
        self.report_files = {}

    def _run_single(self, prompt, **kwargs):
        profile = kwargs["profile"]
        token = kwargs["execution_token"]
        base = execution_context.artifact_dir_for_token(token)
        self.calls.append((profile, kwargs["deadline"], token))
        if self.failures.get(profile, 0):
            self.failures[profile] -= 1
            return CodexResult(success=False, result="explicit provider process failure", raw_output="")
        if profile.endswith("recovery"):
            if self.missing != "recovery":
                write_once(
                    token,
                    RESULT_FILES["recovery"],
                    {
                        "title": "회복 검토",
                        "summary": "근거 검토",
                        "reason": "선택 사유",
                        "evidence_refs": [],
                        "limitations": [],
                        "recommendation": self.recommendation,
                    },
                )
        elif profile.endswith("root-rca"):
            assert read_object(token, "analysis-part-recovery.json")["payload"]["status"] in ("COMPLETED", "FAILED")
            _write_required_report_artifacts(base, _valid_report())
            for name in ("report.md", "playbook.json"):
                self.report_files[name] = (base / name).read_text()
                (base / name).unlink()
        elif profile.endswith("root-report"):
            for name, text in self.report_files.items():
                (base / name).write_text(text)
        elif profile.endswith("operations"):
            assert read_object(token, "analysis-part-root_cause.json")["payload"]["status"] in ("COMPLETED", "FAILED")
            write_once(
                token,
                RESULT_FILES["operations"],
                {
                    "title": "운영 검토",
                    "summary": "예방 제안",
                    "findings": [],
                    "recommendations": [],
                    "limitations": ["CI 설정 미확인"],
                },
            )
        return CodexResult(success=True, result="saved", raw_output="")


def context(store=None, **kwargs):
    return AnalysisPartsRun(
        store=store or LocalPartStore(),
        rca_id="incident",
        incident={"alarm": {"AlarmName": "alarm"}, "scoping": {}, "observations": {}, "source_artifacts": []},
        **kwargs,
    )


def run(runner, parts, work, **kwargs):
    return runner.run(
        "original incident",
        execution_token=work.token,
        profile=harness.MODEL_EVAL_PROFILE if parts.model_eval else harness.ANALYSIS_PROFILE,
        report_prompt="root report",
        deadline=time.monotonic() + 60,
        analysis_parts=parts,
        **kwargs,
    )


def test_real_runner_publishes_three_ordered_parts_without_waiting_for_execution(work):
    runner = Scripted()
    published = []
    parts = context(model_eval=True, on_published=lambda name, value: published.append((name, len(runner.calls))))
    result = run(runner, parts, work)
    assert result.success
    assert [p for p, _, _ in runner.calls] == [
        "model-eval-recovery",
        "model-eval-root-rca",
        "model-eval-root-report",
        "model-eval-operations",
    ]
    assert published == [("recovery", 1), ("root_cause", 3), ("operations", 4)]
    assert len({deadline for _, deadline, _ in runner.calls}) == 1
    assert all(item["record"]["status"] == "COMPLETED" for item in parts.outcomes.values())
    assert parts.outcomes["recovery"]["record"]["approval_status"] == "UNAVAILABLE"


@pytest.mark.parametrize(
    "profile", ["model-eval-recovery", "model-eval-root-rca", "model-eval-root-report", "model-eval-operations"]
)
def test_terminal_failure_retries_same_role_once_with_identical_deadline_and_token(work, profile):
    runner = Scripted({profile: 1})
    parts = context(model_eval=True)
    assert run(runner, parts, work).success
    selected = [call for call in runner.calls if call[0] == profile]
    assert len(selected) == 2 and selected[0] == selected[1]
    assert all(item["record"]["status"] == "COMPLETED" for item in parts.outcomes.values())


def test_retry_exhaustion_publishes_failed_root_before_operations(work):
    runner = Scripted({"model-eval-root-rca": 10})
    parts = context(model_eval=True)
    assert run(runner, parts, work).success
    assert [call[0] for call in runner.calls] == [
        "model-eval-recovery",
        "model-eval-root-rca",
        "model-eval-root-rca",
        "model-eval-operations",
    ]
    assert parts.outcomes["root_cause"]["record"]["status"] == "FAILED"
    assert parts.outcomes["operations"]["record"]["status"] == "COMPLETED"


def test_successful_process_missing_artifact_does_not_get_terminal_retry(work):
    runner = Scripted(missing="recovery")
    parts = context(model_eval=True)
    run(runner, parts, work)
    assert [call[0] for call in runner.calls].count("model-eval-recovery") == 1
    assert parts.outcomes["recovery"]["record"]["status"] == "FAILED"
    assert parts.outcomes["operations"]["record"]["status"] == "COMPLETED"


@pytest.mark.parametrize(
    "model_recommendation,native_ready,expected",
    [("UNAVAILABLE", True, "UNAVAILABLE"), ("ROLLBACK", False, "UNAVAILABLE"), ("ROLLBACK", True, "ROLLBACK")],
)
def test_only_native_ready_and_model_rollback_enable_the_server_plan(
    work, model_recommendation, native_ready, expected
):
    trusted = {
        "approval_status": "READY" if native_ready else "UNAVAILABLE",
        "verification": {"valid": native_ready},
        "playbook": {"private_server_plan": True},
        "runbook_digest": "a" * 64,
    }
    parts = context(recovery_eligibility=trusted)
    run(Scripted(recommendation=model_recommendation), parts, work)
    result = parts.outcomes["recovery"]["payload"]["result"]
    assert result["recommendation"] == expected
    assert result["playbook"] == (trusted["playbook"] if expected == "ROLLBACK" else None)


def test_model_eval_never_enables_live_approval_even_with_supplied_native_flags(work):
    parts = context(
        model_eval=True,
        recovery_eligibility={
            "approval_status": "READY",
            "playbook": {"forged": True},
            "verification": {"valid": True},
            "runbook_digest": "a" * 64,
        },
    )
    run(Scripted(recommendation="ROLLBACK"), parts, work)
    assert parts.outcomes["recovery"]["payload"]["result"]["playbook"] is None
    assert parts.outcomes["recovery"]["record"]["approval_status"] == "UNAVAILABLE"


def test_reentry_reuses_frozen_parts_and_exact_root_files_without_model_reinvocation(work, monkeypatch, tmp_path):
    store = LocalPartStore()
    parts = context(store, model_eval=True)
    run(Scripted(), parts, work)
    original = (work.artifact_dir / "playbook.json").read_bytes()
    next_work = execution_context.ExecutionContext.create("incident")
    next_work.prepare()
    resumed = context(store, model_eval=True)
    runner = Scripted()
    assert run(runner, resumed, next_work).success
    assert runner.calls == []
    assert (next_work.artifact_dir / "playbook.json").read_bytes() == original


def test_ownership_loss_after_recovery_publication_stops_new_roles(work):
    cancelled = False

    def publish(name, value):
        nonlocal cancelled
        cancelled = True

    parts = context(model_eval=True, on_published=publish)
    runner = Scripted()
    with pytest.raises(AnalysisPartInterruptedError):
        run(runner, parts, work, cancel_checker=lambda: cancelled)
    assert len(runner.calls) == 1
    assert read_object(work.token, "analysis-parts-control.json")["active"] is False


def test_expired_shared_deadline_records_failure_and_skips_without_new_model_calls(work):
    class Slow(Scripted):
        def _run_single(self, prompt, **kwargs):
            result = super()._run_single(prompt, **kwargs)
            time.sleep(0.15)
            return result

    runner = Slow()
    parts = context(model_eval=True)
    result = runner.run(
        "input",
        execution_token=work.token,
        profile=harness.MODEL_EVAL_PROFILE,
        deadline=time.monotonic() + 0.1,
        analysis_parts=parts,
    )
    assert not result.success
    assert len(runner.calls) == 1
    assert [item["record"]["status"] for item in parts.outcomes.values()] == ["FAILED", "SKIPPED", "SKIPPED"]


def test_storage_failure_blocks_later_roles_instead_of_pretending_publication(work):
    class Failing(LocalPartStore):
        def publish_part(self, *args, **kwargs):
            raise RuntimeError("durable store unavailable")

    runner = Scripted()
    parts = context(Failing(), model_eval=True)
    with pytest.raises(RuntimeError, match="durable store"):
        run(runner, parts, work)
    assert len(runner.calls) == 1 and not parts.outcomes
    assert read_object(work.token, "analysis-parts-control.json")["active"] is False
