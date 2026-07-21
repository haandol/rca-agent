from __future__ import annotations

from string import Formatter

import pytest
from pydantic import ValidationError

from rca_agent.prompts.branching import (
    BRANCHING_SYSTEM_PROMPT,
    BRANCHING_USER_PROMPT_TEMPLATE,
)
from rca_agent.prompts.evidence import (
    EVIDENCE_COLLECTION_SYSTEM_PROMPT,
    EVIDENCE_COLLECTION_USER_PROMPT_TEMPLATE,
)
from rca_agent.prompts.hypothesis import (
    HYPOTHESIS_GENERATION_SYSTEM_PROMPT,
    HYPOTHESIS_GENERATION_USER_PROMPT_TEMPLATE,
)
from rca_agent.prompts.playbook import (
    PLAYBOOK_SYSTEM_PROMPT,
    PLAYBOOK_UPDATE_SYSTEM_PROMPT,
    PLAYBOOK_UPDATE_USER_PROMPT_TEMPLATE,
    PLAYBOOK_USER_PROMPT_TEMPLATE,
)
from rca_agent.prompts.prioritization import (
    PRIORITIZATION_SYSTEM_PROMPT,
    PRIORITIZATION_USER_PROMPT_TEMPLATE,
)
from rca_agent.prompts.report import REPORT_SYSTEM_PROMPT, REPORT_USER_PROMPT_TEMPLATE
from rca_agent.prompts.scoping import SCOPING_SYSTEM_PROMPT, SCOPING_USER_PROMPT_TEMPLATE
from rca_agent.prompts.validation import VALIDATION_SYSTEM_PROMPT, VALIDATION_USER_PROMPT_TEMPLATE
from rca_agent.prompts.verification import VERIFICATION_SYSTEM_PROMPT, VERIFICATION_USER_PROMPT_TEMPLATE
from rca_agent.services.branching import BranchingOutput
from rca_agent.services.hypothesis import HypothesisOutput


def _fields(template: str) -> set[str]:
    return {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name is not None}


SYSTEM_PROMPTS = [
    BRANCHING_SYSTEM_PROMPT,
    EVIDENCE_COLLECTION_SYSTEM_PROMPT,
    HYPOTHESIS_GENERATION_SYSTEM_PROMPT,
    PLAYBOOK_SYSTEM_PROMPT,
    PLAYBOOK_UPDATE_SYSTEM_PROMPT,
    PRIORITIZATION_SYSTEM_PROMPT,
    REPORT_SYSTEM_PROMPT,
    SCOPING_SYSTEM_PROMPT,
    VALIDATION_SYSTEM_PROMPT,
    VERIFICATION_SYSTEM_PROMPT,
]


@pytest.mark.parametrize("prompt", SYSTEM_PROMPTS)
def test_all_agent_system_prompts_preserve_korean_language_contract(prompt):
    assert "## Language" in prompt
    assert "in Korean" in prompt
    assert "technical terms" in prompt


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        (
            SCOPING_USER_PROMPT_TEMPLATE,
            {
                "alarm_name",
                "state_reason",
                "state_change_time",
                "region",
                "namespace",
                "metric_name",
                "dimensions",
                "statistic",
                "period",
                "threshold",
                "comparison_operator",
                "report_context",
            },
        ),
        (
            HYPOTHESIS_GENERATION_USER_PROMPT_TEMPLATE,
            {
                "alarm_summary",
                "anomaly_start_time",
                "blast_radius",
                "initial_severity",
                "metric_snapshot",
                "report_context",
            },
        ),
        (
            PRIORITIZATION_USER_PROMPT_TEMPLATE,
            {"scoping_summary", "hypotheses_text"},
        ),
        (
            EVIDENCE_COLLECTION_USER_PROMPT_TEMPLATE,
            {
                "alarm_name",
                "alarm_region",
                "service_name",
                "resource_id",
                "state_change_time",
                "blast_radius",
                "initial_severity",
                "metric_context",
                "parent_context",
                "hypothesis_description",
                "hypothesis_category",
                "required_evidence",
            },
        ),
        (
            VALIDATION_USER_PROMPT_TEMPLATE,
            {"description", "evidence_text"},
        ),
        (
            BRANCHING_USER_PROMPT_TEMPLATE,
            {
                "parent_description",
                "parent_category",
                "parent_confidence",
                "parent_fault_type",
                "evidence_text",
                "rejected_text",
            },
        ),
        (
            REPORT_USER_PROMPT_TEMPLATE,
            {
                "incident_summary",
                "alarm_name",
                "metric_name",
                "confirmed",
                "root_cause_description",
                "confidence",
                "hypothesis_path",
                "evidence_text",
                "rejected_text",
                "timeline_text",
            },
        ),
        (
            PLAYBOOK_USER_PROMPT_TEMPLATE,
            {
                "failure_type",
                "root_cause",
                "severity",
                "evidence_highlights",
                "detection_method",
                "mitigation_text",
                "remediation_text",
                "action_items_text",
            },
        ),
        (
            PLAYBOOK_UPDATE_USER_PROMPT_TEMPLATE,
            {
                "existing_failure_type",
                "existing_symptom_pattern",
                "existing_severity_criteria",
                "existing_verification_steps",
                "existing_temporary_mitigation",
                "existing_permanent_remediation",
                "existing_escalation_criteria",
                "existing_prevention_measures",
                "existing_related_metrics",
                "root_cause",
                "severity",
                "evidence_highlights",
                "detection_method",
                "mitigation_text",
                "remediation_text",
            },
        ),
        (
            VERIFICATION_USER_PROMPT_TEMPLATE,
            {
                "alarm_name",
                "namespace",
                "metric_name",
                "dimensions",
                "statistic",
                "period",
                "threshold",
                "comparison_operator",
                "evaluation_periods",
                "datapoints_to_alarm",
                "server_status",
                "server_evaluation",
                "remediation_summary",
                "seconds_since_remediation",
            },
        ),
    ],
)
def test_user_prompt_placeholder_contracts(template, expected):
    assert _fields(template) == expected
    template.format(**dict.fromkeys(expected, "contract-value"))


def test_hypothesis_prompt_and_dto_agree_on_maximum_count():
    assert "3 to 5 hypotheses" in HYPOTHESIS_GENERATION_SYSTEM_PROMPT
    field = HypothesisOutput.model_fields["hypotheses"]
    assert any(getattr(metadata, "max_length", None) == 5 for metadata in field.metadata)


def test_branching_prompt_and_dto_agree_on_maximum_count():
    assert "exactly 2-3" in BRANCHING_SYSTEM_PROMPT
    field = BranchingOutput.model_fields["children"]
    assert any(getattr(metadata, "max_length", None) == 3 for metadata in field.metadata)


def test_hypothesis_dto_rejects_fewer_than_three_items():
    with pytest.raises(ValidationError):
        HypothesisOutput(hypotheses=[])


def test_branching_dto_rejects_fewer_than_two_children():
    with pytest.raises(ValidationError):
        BranchingOutput(children=[])


def test_report_prompt_requires_engine_neutral_rca_dimensions():
    required_dimensions = [
        "incident summary",
        "impact assessment",
        "root cause",
        "5 Whys",
        "hypothesis path",
        "evidence",
        "timeline",
        "temporary mitigation",
        "permanent remediation",
        "action items",
        "lessons learned",
    ]
    for dimension in required_dimensions:
        assert dimension in REPORT_SYSTEM_PROMPT


def test_evidence_prompt_forbids_judgment_and_requires_bounded_tool_use():
    assert "Do NOT make judgments" in EVIDENCE_COLLECTION_SYSTEM_PROMPT
    assert "at most 3-4 tool calls per evidence type" in EVIDENCE_COLLECTION_SYSTEM_PROMPT
    assert "/ecs/RcaAgentDev/<service>" in EVIDENCE_COLLECTION_SYSTEM_PROMPT
