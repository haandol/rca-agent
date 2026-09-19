"""Legacy and deferred publication share the full executed-procedure verification boundary."""

from copy import deepcopy

import pytest
from test_execution_pipeline import APPROVAL, PLAYBOOK, RecordingRunner, _container

from headless_codex.services.execution_pipeline import ExecutionOrchestrator
from headless_codex.services.playbook_merge import apply_retrospective_verification, merge_playbook_update

pytest_plugins = ["test_execution_pipeline"]


@pytest.mark.parametrize(
    "update,expected",
    [
        ({"rollback_context": {"scope": {"service_name": "different-service"}}}, "DRAFT"),
        ({"region": "eu-west-1"}, "DRAFT"),
        ({"account_id": "other-account"}, "DRAFT"),
        ({"target_account_id": "other-account"}, "DRAFT"),
        ({"target_region": "eu-west-1"}, "DRAFT"),
        ({"execution_binding": {"service": "different-service"}}, "DRAFT"),
        ({"tags": ["reviewed"]}, "VERIFIED"),
        ({"temporary_mitigation": "Clarified supporting knowledge"}, "VERIFIED"),
        ({}, "VERIFIED"),
    ],
)
def test_legacy_public_flow_checks_full_procedure_after_merge(update, expected):
    """A successful legacy review cannot grant verification to an unexecuted target binding."""
    before = deepcopy(PLAYBOOK)
    runner = RecordingRunner(retrospective={"update": update, "rationale": "retained review result"})
    container = _container(runner)
    assert ExecutionOrchestrator(container).process_message(APPROVAL)
    revised = container.execution_store.save_playbook_revision.call_args.args[2]
    assert revised["verification_status"] == expected
    assert container.playbook_store.save_to_s3_vectors.call_args.args[0]["verification_status"] == expected
    assert before == PLAYBOOK


@pytest.mark.parametrize("change", ["order", "step_description", "commands", "success_criteria", "metadata"])
def test_shared_verification_uses_entire_ordered_steps(change):
    """Same IDs and commands do not excuse changed order, descriptions, or success criteria."""
    approved = deepcopy(PLAYBOOK)
    approved["execution_steps"].append({**deepcopy(approved["execution_steps"][0]), "step_id": "step-2"})
    reviewed = deepcopy(approved)
    if change == "order":
        reviewed["execution_steps"].reverse()
    elif change == "step_description":
        reviewed["execution_steps"][0]["action"] = "different action description"
    elif change == "commands":
        reviewed["execution_steps"][0]["commands"] = ["aws ecs describe-clusters"]
    elif change == "success_criteria":
        reviewed["execution_steps"][0]["success_criteria"] = "different threshold"
    else:
        reviewed["tags"] = ["clarification"]
    before = deepcopy((approved, reviewed))
    assert apply_retrospective_verification(approved, reviewed)["verification_status"] == (
        "VERIFIED" if change == "metadata" else "DRAFT"
    )
    assert (approved, reviewed) == before


def test_merge_preserves_approved_order_when_model_only_reorders_existing_steps():
    """The existing additive merge ignores a reorder request; verification follows actual merged content."""
    approved = deepcopy(PLAYBOOK)
    approved["execution_steps"].append({**deepcopy(approved["execution_steps"][0]), "step_id": "step-2"})
    merged, _ = merge_playbook_update(approved, {"execution_steps": list(reversed(approved["execution_steps"]))})
    assert merged["execution_steps"] == approved["execution_steps"]
    assert apply_retrospective_verification(approved, merged)["verification_status"] == "VERIFIED"


@pytest.mark.parametrize("present", [True, False])
def test_model_update_cannot_replace_or_invent_server_provenance(present):
    """Preserve known original identity and revision fields; absent authority cannot be supplied by the model."""
    identity = {
        "rca_id": "incident-1",
        "source_rca_id": "original-source",
        "source_engine": "strands",
        "library_revision": "analysis:incident-1",
        "comparison": {"status": "UPDATE_PROPOSED", "original_sk": "retained-comparison"},
    }
    original = {**deepcopy(PLAYBOOK), **(identity if present else {})}
    before = deepcopy(original)
    updates = {field: "forged" for field in identity}
    updates["temporary_mitigation"] = "Legitimate knowledge clarification"
    merged, diff = merge_playbook_update(original, updates)
    assert original == before
    for field in identity:
        if present:
            assert merged[field] == original[field]
        else:
            assert field not in merged
        assert field not in diff.changed_fields
    assert merged["temporary_mitigation"] == updates["temporary_mitigation"]


def test_legacy_pipeline_does_not_publish_model_invented_identity():
    """The real legacy review path ignores injected provenance while preserving normal metadata correction."""
    runner = RecordingRunner(
        retrospective={
            "update": {
                "rca_id": "forged",
                "source_rca_id": "forged",
                "source_engine": "forged",
                "library_revision": "forged",
                "tags": ["reviewed"],
                "comparison": {"status": "APPLIED"},
            },
            "rationale": "Metadata clarification",
        }
    )
    container = _container(runner)
    assert ExecutionOrchestrator(container).process_message(APPROVAL)
    published = container.execution_store.save_playbook_revision.call_args.args[2]
    assert not any(
        field in published for field in ("rca_id", "source_rca_id", "source_engine", "library_revision", "comparison")
    )
    assert published["playbook_id"] == PLAYBOOK["playbook_id"]
    assert published["tags"] == ["reviewed"]
