"""Exercise the completion archive boundary using the real incident pipeline and local doubles."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_pipeline import TestPlaybookComparison as _ComparisonScenario
from test_pipeline import _container


def test_completion_archives_full_reference_inputs_but_persists_only_thin_state(monkeypatch, tmp_path):
    """The report retains fixed observations while completion, notification and vector handoff use thin state."""
    scenario = _ComparisonScenario()
    container = _container(None)
    container.playbook_store.search_similar.return_value = [scenario._hit()]
    container.playbook_store.load_detail.return_value = scenario._existing()
    order = []
    archive = container.playbook_store.archive_comparison.side_effect

    def record_archive(book, rca_id, engine):
        """Make original persistence observable before report and completion writes."""
        order.append("archive")
        return archive(book, rca_id, engine)

    container.playbook_store.archive_comparison.side_effect = record_archive
    container.report_store.save_report.side_effect = lambda *a, **k: order.append("report") or "reports/current.md"
    container.session_store.mark_completed.side_effect = lambda *a, **k: order.append("completed")
    assert scenario._run(container, monkeypatch, tmp_path)
    assert order == ["archive", "report", "completed"]
    full = container.playbook_store.archive_comparison.call_args.args[0]
    completed = container.session_store.mark_completed.call_args.kwargs
    thin = completed["playbook"]
    report = container.report_store.save_report.call_args.args[1]
    assert thin == completed["completion_notification"]["playbook"] == scenario._saved(container)
    assert thin["execution_steps"] == full["execution_steps"]
    assert thin["playbook_id"] == full["playbook_id"]
    assert set(thin["comparison"]) == {
        "status",
        "selected_playbook_id",
        "original_sk",
        "original_expires_at",
        "proposal",
    }
    assert full["comparison"]["baseline"]["playbook"]["playbook_id"] == "pb-existing"
    assert full["comparison"]["evidence"][0]["source_rca_id"] == "rca-1"
    runbook_reference = next(
        item for item in full["comparison"]["used_references"] if item["role"] == "current-runbook-input"
    )
    assert runbook_reference == {
        "role": "current-runbook-input",
        "ref": "analysis",
        "source_rca_id": "rca-1",
        "source_engine": "headless-codex",
    }
    assert full["comparison"]["inputs"][runbook_reference["ref"]]["root_cause_confirmed"] is True
    assert "생성에 사용한 참고 자료" in report
    assert '"baseline"' in report
    assert container.playbook_store.archive_comparison.call_args.args[1:] == ("rca-1", "headless-codex")


@pytest.mark.parametrize("fault", ["unavailable", "full_state", "changed_plan"])
def test_archive_failure_keeps_report_deliverable_without_originals_in_completion(monkeypatch, tmp_path, fault):
    """Failure cannot publish a valid proposal or silently restore historical execution steps."""
    scenario = _ComparisonScenario()
    container = _container(None)
    container.playbook_store.search_similar.return_value = [scenario._hit()]
    container.playbook_store.load_detail.return_value = scenario._existing()
    if fault == "unavailable":
        container.playbook_store.archive_comparison.side_effect = RuntimeError("unavailable")
    elif fault == "full_state":
        container.playbook_store.archive_comparison.side_effect = lambda book, *_: book
    else:
        archive = container.playbook_store.archive_comparison.side_effect

        def change_plan(book, rca_id, engine):
            """Return a corrupt adapter result to prove the pipeline refuses a changed plan."""
            result = archive(book, rca_id, engine)
            result["execution_steps"] = []
            return result

        container.playbook_store.archive_comparison.side_effect = change_plan
    assert scenario._run(container, monkeypatch, tmp_path)
    attempted = deepcopy(container.playbook_store.archive_comparison.call_args.args[0])
    thin = scenario._saved(container)
    report = container.report_store.save_report.call_args.args[1]
    assert thin["execution_steps"] == attempted["execution_steps"]
    assert thin["playbook_id"] != attempted["playbook_id"]
    assert thin["comparison"] == {
        "status": "SEARCH_FAILED",
        "selected_playbook_id": "",
        "failure_reason": "비교 원본 보관에 실패하여 제안을 사용할 수 없습니다.",
    }
    assert "검색·비교 실패" in report
    assert '"baseline"' not in report
    assert '"proposal_id"' not in report


def test_failed_archive_of_verified_match_creates_draft_with_identical_current_commands():
    """A new fallback asset cannot inherit verification once the matching baseline could not be archived."""
    from test_artifact_validation import _playbook

    from headless_codex.services.playbook_comparison import archive_incident_comparison

    draft = _playbook(playbook_id="new-incident")
    matched = {
        **deepcopy(draft),
        "playbook_id": "published",
        "verification_status": "VERIFIED",
        "comparison": {
            "status": "NO_CHANGE",
            "selected_playbook_id": "published",
            "inputs": {"current_playbook": deepcopy(draft)},
        },
    }
    store = SimpleNamespace(archive_comparison=Mock(side_effect=RuntimeError("unavailable")))
    report, state = archive_incident_comparison(matched, store=store, rca_id="current")
    assert state["playbook_id"] != matched["playbook_id"]
    assert state["execution_steps"] == matched["execution_steps"]
    assert state["verification_status"] == "DRAFT"
    assert state["comparison"]["status"] == "SEARCH_FAILED"
    assert report == state


@pytest.mark.parametrize("has_incident_id", [True, False])
def test_cross_engine_current_ownership_does_not_rewrite_historical_comparison(has_incident_id):
    """Current report/state use trusted incident ownership; baseline and proposal originals retain their source."""
    from headless_codex.services.playbook_comparison import archive_incident_comparison

    historical = {"playbook_id": "shared", "rca_id": "old-strands"}
    full = {
        "playbook_id": "shared",
        "execution_steps": [{"step_id": "current", "commands": ["current command"]}],
        "verification_status": "DRAFT",
        "comparison": {
            "comparison_id": "comparison-1",
            "status": "UPDATE_PROPOSED",
            "selected_playbook_id": "shared",
            "baseline": {"playbook": deepcopy(historical), "source_rca_id": "old-strands"},
            "proposal": {
                "proposal_id": "proposal-1",
                "state": "PENDING",
                "before": deepcopy(historical),
                "after": deepcopy(historical),
            },
        },
    }
    if has_incident_id:
        full["rca_id"] = "old-strands"
    container = _container(None)
    report, state = archive_incident_comparison(full, store=container.playbook_store, rca_id="current-headless")
    archived = container.playbook_store.archive_comparison.call_args.args[0]
    for document in (report, state, archived):
        assert document.get("rca_id") == ("current-headless" if has_incident_id else None)
    assert report["comparison"]["baseline"]["playbook"]["rca_id"] == "old-strands"
    assert report["comparison"]["proposal"]["before"]["rca_id"] == "old-strands"
    assert report["comparison"]["proposal"]["after"]["rca_id"] == "old-strands"
    assert full.get("rca_id") == ("old-strands" if has_incident_id else None)
