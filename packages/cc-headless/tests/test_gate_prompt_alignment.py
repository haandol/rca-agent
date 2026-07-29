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

from cc_headless.services import artifact_validation as gate

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PACKAGE_ROOT / "prompts"
SKILLS_DIR = PACKAGE_ROOT / ".claude" / "skills"
AGENTS_DIR = PACKAGE_ROOT / ".claude" / "agents"


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


@pytest.mark.parametrize("status", sorted(gate._REMEDIATION_STATUSES))
def test_every_remediation_status_is_documented(status: str) -> None:
    assert status in CORPUS, f"the gate accepts remediation status {status} but no prompt mentions it"


@pytest.mark.parametrize("status", sorted(gate._VERIFICATION_STATUSES))
def test_every_verification_status_is_documented(status: str) -> None:
    assert status in CORPUS, f"the gate accepts verification status {status} but no prompt mentions it"


@pytest.mark.parametrize("state", sorted(gate._AMBIGUOUS_HYPOTHESIS_STATES))
def test_every_ambiguous_hypothesis_state_is_documented(state: str) -> None:
    assert state in CORPUS.lower(), f"the gate treats '{state}' as ambiguous but no prompt mentions it"


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


def test_the_placeholder_for_absent_server_values_is_documented() -> None:
    """A null server value must be rendered as a literal the gate looks for.

    The gate renders `None` as "N/A" and then requires that string to appear in
    the report's remediation section, so the prompts have to say so.
    """
    assert "N/A" in CORPUS


def test_prompts_require_copying_server_owned_remediation_values_verbatim() -> None:
    # The gate compares these against remediation.json exactly, so paraphrase fails.
    assert "remediation.json" in CORPUS
    assert "글자 그대로" in CORPUS


def test_prompts_state_that_playbook_fields_are_all_mandatory() -> None:
    assert "필수" in CORPUS


def test_gate_constants_are_still_where_this_test_reads_them() -> None:
    """Guards against the gate being refactored out from under these tests."""
    for name in (
        "_PLAYBOOK_STRING_FIELDS",
        "_PLAYBOOK_LIST_FIELDS",
        "_REPORT_SECTIONS",
        "_REMEDIATION_STATUSES",
        "_VERIFICATION_STATUSES",
        "_AMBIGUOUS_HYPOTHESIS_STATES",
        "_ISO_TIMESTAMP",
    ):
        assert hasattr(gate, name), f"the gate no longer exposes {name}; update this alignment test"
