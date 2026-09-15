"""Archive originals before tracing and retain the exact current plan on archive failure."""

from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from rca_agent.ports.dto.models import PlaybookVerificationStatus
from rca_agent.services.pipeline import PipelineOrchestrator
from rca_agent.services.playbook_gen import archive_incident_comparison
from tests.test_main import _make_container, _run_context, _scoping
from tests.test_playbook_comparison import _run
from tests.test_playbook_gen import _make_appraisal, _make_hit, _make_report


def thin_copy(book, rca_id, engine):
    """Represent storage's 60-day original reference without copying original fields into state."""
    result = book.model_copy(deep=True)
    original = book.comparison
    result.comparison = {
        "status": original["status"],
        "selected_playbook_id": original["selected_playbook_id"],
        "original_sk": f"{engine}#PLAYBOOK_COMPARISON#{original['comparison_id']}",
        "original_expires_at": 2_000_000_000,
    }
    if "proposal" in original:
        result.comparison["proposal"] = {key: original["proposal"][key] for key in ("proposal_id", "state")}
    return result


def test_archive_precedes_trace_and_report_keeps_full_comparison_separately():
    """The trace gets only a reference while report rendering retains the same original and runbook."""
    original, _, _, _ = _run([_make_hit()], appraisals=[_make_appraisal(needs_update=True, tags=["new"])])
    container = _make_container()
    run = _run_context()
    order = []

    def archive(book, rca_id, engine):
        """Record the archive boundary before any trace persistence."""
        order.append("archive")
        return thin_copy(book, rca_id, engine)

    container.playbook_store.archive_comparison.side_effect = archive
    run.trace.end_span.side_effect = lambda *a, **k: order.append("trace")
    with patch("rca_agent.services.pipeline.run_playbook_generation", return_value=original):
        thin, _, report = PipelineOrchestrator(container)._run_playbook(_make_report(), _scoping(), run)
    assert order == ["archive", "trace"]
    assert report.comparison == original.comparison
    assert thin.execution_steps == report.execution_steps == original.execution_steps
    assert thin.playbook_id == report.playbook_id
    assert set(thin.comparison) == {"status", "selected_playbook_id", "original_sk", "original_expires_at", "proposal"}
    assert thin.comparison["proposal"] == {
        "proposal_id": original.comparison["proposal"]["proposal_id"],
        "state": "PENDING",
    }
    assert run.trace.end_span.call_args.kwargs["metadata"]["comparison"] == thin.comparison
    assert container.playbook_store.archive_comparison.call_args.args[1:] == ("rca-1", "strands")


@pytest.mark.parametrize("fault", ["unavailable", "full_state", "changed_plan"])
def test_archive_failure_discards_originals_and_proposal_but_keeps_current_plan(fault):
    """Failure is explicit; failed archival cannot leak original data or leave an actionable proposal."""
    original, _, _, _ = _run([_make_hit()], appraisals=[_make_appraisal(needs_update=True, tags=["new"])])
    original_before = deepcopy(original.model_dump(mode="json"))
    store = Mock()
    if fault == "unavailable":
        store.archive_comparison.side_effect = RuntimeError("unavailable")
    elif fault == "full_state":
        store.archive_comparison.return_value = original.model_copy(deep=True)
    else:
        changed = thin_copy(original, "rca-1", "strands")
        changed.execution_steps = []
        store.archive_comparison.return_value = changed
    report, state = archive_incident_comparison(original, store=store, rca_id="rca-1")
    assert state == report
    assert state.execution_steps == original.execution_steps
    assert state.comparison == {
        "status": "SEARCH_FAILED",
        "selected_playbook_id": "",
        "failure_reason": "비교 원본 보관에 실패하여 제안을 사용할 수 없습니다.",
    }
    assert state.playbook_id != original.playbook_id
    assert original.model_dump(mode="json") == original_before


def test_failed_archive_of_verified_match_creates_draft_with_identical_current_commands():
    """Losing the preserved baseline ends the verified-match exception for the new fallback asset."""
    from rca_agent.ports.dto.models import ExecutionStep
    from tests.test_playbook_comparison import _draft
    from tests.test_playbook_gen import _make_existing

    existing = _make_existing(
        verification_status=PlaybookVerificationStatus.VERIFIED,
        execution_steps=[ExecutionStep(**step.model_dump()) for step in _draft().execution_steps],
    )
    matched, _, _, _ = _run([_make_hit()], details=[existing], appraisals=[_make_appraisal()])
    assert matched.verification_status is PlaybookVerificationStatus.VERIFIED
    store = Mock()
    store.archive_comparison.side_effect = RuntimeError("archive unavailable")
    report, state = archive_incident_comparison(matched, store=store, rca_id="rca-1")
    assert state.playbook_id != matched.playbook_id
    assert state.execution_steps == matched.execution_steps
    assert state.verification_status is PlaybookVerificationStatus.DRAFT
    assert state.comparison["status"] == "SEARCH_FAILED"
    assert report == state
