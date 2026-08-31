"""Every completion-gate requirement must be stated in the prompt assets.

The gate runs after the run has ended. If it demands something the prompts never
asked for, a correct RCA is discarded for a format reason the agent had no way to
know — that failure mode has already cost several live runs.

These tests derive the requirements from the gate's own constants, so adding a
gate rule fails here until the prompts document it. They check that the
requirement is *mentioned*, not that the wording matches; the point is to catch
silent omissions, not to freeze prose.
"""

from pathlib import Path

import pytest

from headless_codex.services import artifact_validation as gate

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PACKAGE_ROOT / "prompts"
SKILLS_DIR = PACKAGE_ROOT / "harness" / "skills"
AGENTS_DIR = PACKAGE_ROOT / "harness"


def _prompt_corpus() -> str:
    """Everything the agents are told, as one searchable body of text."""
    roots = (PROMPTS_DIR, SKILLS_DIR, AGENTS_DIR)
    texts = [path.read_text() for root in roots for path in sorted(root.rglob("*.md")) if path.is_file()]
    assert texts, "no prompt assets found"
    return "\n".join(texts)


CORPUS = _prompt_corpus()


@pytest.mark.parametrize("field", gate._PLAYBOOK_STRING_FIELDS)
def test_every_required_playbook_string_field_is_documented(field: str) -> None:
    assert field in CORPUS, f"the gate requires playbook.{field} but no prompt mentions it"


@pytest.mark.parametrize("field", gate._PLAYBOOK_LIST_FIELDS)
def test_every_required_playbook_list_field_is_documented(field: str) -> None:
    assert field in CORPUS, f"the gate requires playbook.{field} but no prompt mentions it"


@pytest.mark.parametrize("section", gate._REPORT_SECTIONS)
def test_every_required_report_section_is_documented(section: str) -> None:
    assert section in CORPUS, f"the gate requires report section '{section}' but no prompt mentions it"


@pytest.mark.parametrize("field", gate._EXECUTION_STEP_FIELDS)
def test_every_required_execution_step_field_is_documented(field: str) -> None:
    assert field in CORPUS, f"the gate requires execution step.{field} but no prompt mentions it"


def test_the_draft_playbook_status_the_gate_demands_is_documented() -> None:
    assert gate._PLAYBOOK_DRAFT_STATUS in CORPUS


def test_report_evidence_window_labels_are_documented() -> None:
    for label in ("Current alarm window", "Historical comparison window"):
        assert label in CORPUS


def test_report_evidence_window_example_satisfies_the_gate_rule() -> None:
    """The gate wants both timestamps on the label's own line, so show that line."""
    lowered = CORPUS.lower()
    for label in ("current alarm window", "historical comparison window"):
        lines = [line for line in lowered.splitlines() if label in line]
        assert any(len(gate._ISO_TIMESTAMP.findall(line)) >= 2 for line in lines), (
            f"no prompt shows a single line carrying two ISO-8601 timestamps for {label}"
        )


def test_prompts_require_the_report_and_playbook_steps_to_agree() -> None:
    """The gate cross-checks the prose steps against the structured ones.

    A step in one and not the other means the procedure a person approved is not
    the procedure that runs, so the prompts have to name the field the gate
    matches on.
    """
    assert "step_id" in CORPUS


def test_prompts_state_that_playbook_fields_are_all_mandatory() -> None:
    assert "필수" in CORPUS


def test_prompts_tell_the_agent_to_keep_irreversible_work_out_of_execution_steps() -> None:
    # The execution layer refuses these, so a playbook that asks for one produces
    # a step that can never run. The prompts must route it to a recommendation.
    assert "permanent_remediation" in CORPUS
    assert "되돌릴 수 없" in CORPUS


def test_gate_constants_are_still_where_this_test_reads_them() -> None:
    """Guards against the gate being refactored out from under these tests."""
    for name in (
        "_PLAYBOOK_STRING_FIELDS",
        "_PLAYBOOK_LIST_FIELDS",
        "_EXECUTION_STEP_FIELDS",
        "_PLAYBOOK_DRAFT_STATUS",
        "_REPORT_SECTIONS",
        "_ISO_TIMESTAMP",
    ):
        assert hasattr(gate, name), f"the gate no longer exposes {name}; update this alignment test"
