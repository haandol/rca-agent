"""C/D context contracts only; model reasoning and remediation still need live evaluation."""

import json

from rca_agent import eval_adapter
from rca_agent.ports.dto.models import AlarmPayload, MetricObservation, Playbook, RcaReport, ScopingResult
from rca_agent.prompts.branching import BRANCHING_SYSTEM_PROMPT
from rca_agent.prompts.playbook import CONTROL_PROVENANCE_RULES, PLAYBOOK_SYSTEM_PROMPT, PLAYBOOK_UPDATE_SYSTEM_PROMPT
from rca_agent.prompts.report import REPORT_SYSTEM_PROMPT
from rca_agent.services import hypothesis, playbook_gen, prioritization


def test_source_observations_survive_scoping_summary_without_expectation_leaks():
    """Both planners see original discriminators while grading-only sentinel values stay private."""
    observed = json.dumps(
        {
            "window": {"start": "2026-09-10T02:53:45Z", "end": "2026-09-10T02:53:47Z"},
            "resource": {"schema": "proof_neutral"},
            "unit": "Milliseconds",
            "before": {"pool_size": 3, "checkins": 1, "query_revision": "r1"},
            "after": {"pool_size": 3, "checkins": 1, "query_revision": "r1", "wait_event_type": "Lock"},
        }
    )
    scenario = {
        "executionModes": ["model-eval"],
        "alarm": {"name": "Symptom", "stateReason": "Write failure"},
        "observations": [{"id": "signal-17", "source": "capture", "summary": observed}],
        "expectation": {
            "competingCauses": [{"id": "PRIVATE_CAUSE_SENTINEL", "requiredEvidenceIds": ["PRIVATE_EXPECTATION"]}],
            "acceptedRootFaultTypes": ["PRIVATE_TYPE_SENTINEL"],
        },
    }
    alarm = AlarmPayload.from_cloudwatch_sns(
        eval_adapter._alarm_envelope(scenario, state_change_time="2026-09-10T03:00:00.000000+0000")
    )
    scoped = ScopingResult(
        alarm_summary="A deliberately incomplete model summary",
        raw_alarm=alarm,
        metric_observations=[MetricObservation(metric_name="elapsed", datapoints=[5, 500], unit="Milliseconds")],
    )
    prompts = [hypothesis._build_user_prompt(scoped), prioritization._build_user_prompt(scoped, [])]
    for prompt in prompts:
        assert observed in prompt
        assert "[signal-17]" in prompt
        assert "PRIVATE_CAUSE_SENTINEL" not in prompt
        assert "PRIVATE_EXPECTATION" not in prompt
        assert "PRIVATE_TYPE_SENTINEL" not in prompt
        assert "[5, 500]" in prompt


def test_missing_source_context_is_not_filled_with_invented_evidence():
    """A normal scope without original observations explicitly keeps that gap."""
    scoped = ScopingResult(alarm_summary="Symptom only")
    for prompt in [hypothesis._build_user_prompt(scoped), prioritization._build_user_prompt(scoped, [])]:
        assert "No source incident context was provided." in prompt
        assert "No metric data available." in prompt
    assert "one falsifiable causal claim" in BRANCHING_SYSTEM_PROMPT


def test_create_and_update_keep_late_control_evidence_and_proposal_provenance():
    """Evidence after the old five-entry cutoff remains available in both playbook paths."""
    evidence = [f"[signal-{i}] measured fact {i}" for i in range(6)]
    evidence.append("[control-7] owner-verified job release; target chosen at execution")
    report = RcaReport(
        rca_id="rca",
        incident_summary="Incident",
        root_cause="Blocking transaction",
        confidence_score=0.92,
        evidence_list=[*evidence, evidence[0]],
        temporary_mitigation="A recommendation whose availability is not proven",
        permanent_remediation="A proposed design change",
    )
    existing = Playbook(playbook_id="existing", failure_type="Blocking transaction", symptom_pattern="Writes wait")
    for prompt in [
        playbook_gen._build_user_prompt(report),
        playbook_gen._build_update_prompt(existing, report),
    ]:
        for item in evidence:
            # The narrative and literal-copy choices each retain one distinct
            # occurrence; a repeated source entry cannot become another option.
            assert prompt.count(item) == 2
        assert "Proposed Mitigation (not execution evidence)" in prompt
        assert "Mitigation Applied" not in prompt
        assert report.temporary_mitigation in prompt
    for system_prompt in [PLAYBOOK_SYSTEM_PROMPT, PLAYBOOK_UPDATE_SYSTEM_PROMPT]:
        assert CONTROL_PROVENANCE_RULES in system_prompt
        assert "Do not require recovery" in system_prompt
        assert "Natural expiry or passive waiting is not an approved remediation action" in system_prompt
    assert "A proposed mitigation is not an applied mitigation" in REPORT_SYSTEM_PROMPT
