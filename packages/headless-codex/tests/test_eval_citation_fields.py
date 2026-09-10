"""Regression coverage for same-judgment evidence-summary citation mapping."""

import json

import pytest

from headless_codex import eval_adapter
from headless_codex.services.analysis_contract import AnalysisContractError

SCENARIO = {
    "observations": [{"id": name} for name in ("root-a", "root-b", "flat", "healthy", "flat-extra")],
    "expectation": {
        "competingCauses": [
            {"id": "alternative", "requiredEvidenceIds": ["flat", "healthy"]},
        ],
    },
}


def write_validation(tmp_path, number=1, **buckets):
    (tmp_path / f"validation-{number}.json").write_text(json.dumps(buckets))


def entry(hypothesis_id="h1", *, reasoning="Observed causal relation.", evidence_summary=None):
    return {
        "hypothesis_id": hypothesis_id,
        "fault_type": "db-leak",
        "reasoning": reasoning,
        "evidence_summary": [] if evidence_summary is None else evidence_summary,
    }


def judgment(tmp_path, scenario=SCENARIO):
    return eval_adapter._competing_cause_judgments(tmp_path, scenario)[0]


def test_root_credits_same_entry_summary_and_reasoning_in_scenario_order(analysis_artifacts, save_validation, request):
    """Use a real selected judgment while retaining citation ordering."""
    make_judgment = request.getfixturevalue("judgment")
    save_validation(
        1,
        confirmed=[
            make_judgment("root", 0.95, reasoning="[root-b] establishes cause.", evidence=["[root-a]", "[root-b]"])
        ],
    )
    assert eval_adapter._root_cause_evidence_ids(analysis_artifacts, SCENARIO) == ["root-a", "root-b"]


def test_root_credits_summary_when_reasoning_has_no_identifier(analysis_artifacts, save_validation, request):
    """Effective evidence summaries retain their citations."""
    make_judgment = request.getfixturevalue("judgment")
    save_validation(1, confirmed=[make_judgment("root", 0.95, evidence=["[root-a]"])])
    assert eval_adapter._root_cause_evidence_ids(analysis_artifacts, SCENARIO) == ["root-a"]


def test_root_summary_matching_keeps_exact_identifier_boundaries(analysis_artifacts, save_validation, request):
    """A longer identifier cannot credit its prefix."""
    make_judgment = request.getfixturevalue("judgment")
    save_validation(1, confirmed=[make_judgment("root", 0.95, evidence=["[flat-extra]"])])
    assert eval_adapter._root_cause_evidence_ids(analysis_artifacts, SCENARIO) == ["flat-extra"]


@pytest.mark.parametrize(
    ("reasoning", "evidence_summary"),
    [
        ("Observed evidence excludes the alternative.", ["[flat]", "[healthy]"]),
        ("[flat] excludes a traffic increase.", ["[healthy]"]),
    ],
)
def test_rejection_credits_both_fields_of_one_entry(tmp_path, reasoning, evidence_summary):
    write_validation(
        tmp_path,
        rejected=[entry(reasoning=reasoning, evidence_summary=evidence_summary)],
    )
    result = judgment(tmp_path)
    assert result["judgment"] == "rejected"
    assert result["evidenceIds"] == ["flat", "healthy"]
    assert result["rationale"] == "\n".join([reasoning, *evidence_summary])


def test_rejection_never_combines_evidence_from_different_entries(tmp_path):
    write_validation(
        tmp_path,
        rejected=[
            entry("h1", reasoning="[flat]"),
            entry("h2", evidence_summary=["[healthy]"]),
        ],
    )
    assert judgment(tmp_path)["judgment"] == "inconclusive"


@pytest.mark.parametrize("bucket", ["confirmed", "closed", "needs_investigation"])
def test_other_classifications_do_not_become_rejections(tmp_path, bucket):
    write_validation(tmp_path, **{bucket: [entry(evidence_summary=["[flat]", "[healthy]"])]})
    assert judgment(tmp_path)["judgment"] == "inconclusive"
    assert judgment(tmp_path)["evidenceIds"] == []


@pytest.mark.parametrize("bucket", ["confirmed", "closed", "needs_investigation", "rejected"])
def test_latest_same_id_entry_does_not_reuse_superseded_summary(tmp_path, bucket):
    write_validation(tmp_path, rejected=[entry(evidence_summary=["[flat]", "[healthy]"])])
    write_validation(tmp_path, 2, **{bucket: [entry(reasoning="Later judgment without cited evidence.")]})
    assert judgment(tmp_path)["judgment"] == "inconclusive"


def test_rejection_stays_effective_when_only_a_different_id_is_updated(tmp_path):
    write_validation(tmp_path, rejected=[entry("h1", evidence_summary=["[flat]", "[healthy]"])])
    write_validation(tmp_path, 2, confirmed=[entry("h2", evidence_summary=["[root-a]"])])
    assert judgment(tmp_path)["judgment"] == "rejected"


def test_root_selection_follows_reducer_when_a_new_root_is_confirmed(analysis_artifacts, save_validation, request):
    """An earlier confirmed root must not lend citations to a different selected root."""
    make_judgment = request.getfixturevalue("judgment")
    save_validation(1, confirmed=[make_judgment("root", 0.85, evidence=["[root-a]"])])
    save_validation(
        2, confirmed=[make_judgment("other", 0.95)], rejected=[make_judgment("third", 0.1, evidence=["[root-b]"])]
    )
    assert eval_adapter._root_cause_evidence_ids(analysis_artifacts, SCENARIO) == []


def test_global_artifact_citations_do_not_supply_judgment_evidence(analysis_artifacts, save_validation, request):
    """Citations in other artifact fields cannot become judgment evidence."""
    make_judgment = request.getfixturevalue("judgment")
    save_validation(1, confirmed=[make_judgment("root", 0.95)], rejected=[make_judgment("other", 0.1)])
    for filename in ("report.md", "playbook.json", "scoping.json", "hypotheses.json"):
        path = analysis_artifacts / filename
        value = json.loads(path.read_text()) if path.exists() else {}
        path.write_text(json.dumps({**value, "text": "[root-a] [flat] [healthy]"}))
    assert eval_adapter._root_cause_evidence_ids(analysis_artifacts, SCENARIO) == []
    assert judgment(analysis_artifacts)["judgment"] == "inconclusive"


def test_one_rejected_summary_cannot_credit_two_causes(tmp_path):
    scenario = {
        **SCENARIO,
        "expectation": {
            "competingCauses": [
                {"id": "traffic", "requiredEvidenceIds": ["flat"]},
                {"id": "resources", "requiredEvidenceIds": ["healthy"]},
            ],
        },
    }
    write_validation(tmp_path, rejected=[entry(evidence_summary=["[flat]", "[healthy]"])])
    results = eval_adapter._competing_cause_judgments(tmp_path, scenario)
    assert [result["judgment"] for result in results] == ["rejected", "inconclusive"]


@pytest.mark.parametrize("summary", ["[root-a] [flat] [healthy]", {"text": "[root-a] [flat] [healthy]"}, [None, 123]])
def test_non_schema_summary_values_are_not_stringified_into_evidence(
    tmp_path, summary, analysis_artifacts, save_validation, request
):
    """Invalid root artifacts fail verification; malformed counterevidence still cannot be credited."""
    make_judgment = request.getfixturevalue("judgment")
    save_validation(1, confirmed=[make_judgment("root", 0.95)])
    path = analysis_artifacts / "validation-1.json"
    value = json.loads(path.read_text())
    value["confirmed"][0]["evidence_summary"] = summary
    path.write_text(json.dumps(value))
    with pytest.raises(AnalysisContractError, match="evidence_summary"):
        eval_adapter._root_cause_evidence_ids(analysis_artifacts, SCENARIO)
    write_validation(tmp_path, rejected=[entry("other", evidence_summary=summary)])
    assert judgment(tmp_path)["judgment"] == "inconclusive"


def test_rejected_summary_requires_exact_ids(tmp_path):
    write_validation(tmp_path, rejected=[entry(evidence_summary=["[flat-extra]", "[healthy]"])])
    assert judgment(tmp_path)["judgment"] == "inconclusive"
