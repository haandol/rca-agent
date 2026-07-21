import json

import pytest

from cc_headless.services.artifact_validation import (
    ArtifactValidationError,
    validate_remediation_evidence,
    validate_validation_artifacts,
)


def _hypothesis(
    hypothesis_id: str,
    *,
    parent_id: str | None = None,
    depth: int = 0,
    tree_id: str = "tree-1",
    fault_type: str = "db-leak",
) -> dict:
    return {
        "hypothesis_id": hypothesis_id,
        "tree_id": tree_id,
        "title": f"hypothesis {hypothesis_id}",
        "description": f"description for {hypothesis_id}",
        "fault_type": fault_type,
        "category": "INFRASTRUCTURE",
        "confidence_score": 0.7,
        "required_evidence": ["metric"],
        "status": "PENDING",
        "parent_id": parent_id,
        "depth": depth,
    }


def _validation(
    loop_index: int,
    *,
    confirmed: list[dict] | None = None,
    rejected: list[dict] | None = None,
    needs_investigation: list[dict] | None = None,
    closed: list[dict] | None = None,
    new_hypotheses: list[dict] | None = None,
) -> dict:
    return {
        "stage": "VALIDATION",
        "loop_index": loop_index,
        "confirmed": confirmed or [],
        "rejected": rejected or [],
        "needs_investigation": needs_investigation or [],
        "closed": closed or [],
        "new_hypotheses": new_hypotheses or [],
        "summary": f"validation loop {loop_index}",
        "output_summary": f"validation loop {loop_index} complete",
    }


def _write_json(path, value: dict) -> None:
    path.write_text(json.dumps(value))


def _result(hypothesis_id: str, *, fault_type: str | None = None, confidence: float = 0.95) -> dict:
    result = {
        "hypothesis_id": hypothesis_id,
        "confidence": confidence,
        "reasoning": f"{hypothesis_id} classified",
    }
    if fault_type is not None:
        result["fault_type"] = fault_type
    return result


@pytest.fixture
def artifact_dir(tmp_path):
    _write_json(
        tmp_path / "hypotheses.json",
        {
            "stage": "HYPOTHESIS_GENERATION",
            "tree_id": "tree-1",
            "hypotheses": [_hypothesis("root")],
            "summary": "root hypothesis",
            "output_summary": "one root hypothesis",
        },
    )
    return tmp_path


def test_later_validation_can_confirm_child_from_previous_loop(artifact_dir):
    child = _hypothesis("child", parent_id="root", depth=1)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, new_hypotheses=[child]),
    )
    _write_json(
        artifact_dir / "validation-2.json",
        _validation(
            2,
            confirmed=[
                {
                    "hypothesis_id": "child",
                    "confidence": 0.95,
                    "fault_type": "db-leak",
                    "reasoning": "child confirmed by metrics",
                }
            ],
        ),
    )

    evidence = validate_remediation_evidence(artifact_dir)

    assert evidence.validation_artifact == "validation-2.json"
    assert evidence.confirmed_hypothesis_ids == ("child",)


def test_remediation_evidence_allows_single_confirmed_hypothesis(artifact_dir):
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, confirmed=[_result("root", fault_type="db-leak")]),
    )

    evidence = validate_remediation_evidence(artifact_dir)

    assert evidence.fault_type.value == "db-leak"
    assert evidence.confirmed_hypothesis_ids == ("root",)


def test_remediation_evidence_allows_multiple_confirmed_hypotheses_with_same_fault(artifact_dir):
    hypotheses = json.loads(artifact_dir.joinpath("hypotheses.json").read_text())
    hypotheses["hypotheses"].append(_hypothesis("same-fault"))
    _write_json(artifact_dir / "hypotheses.json", hypotheses)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(
            1,
            confirmed=[
                _result("root", fault_type="db-leak"),
                _result("same-fault", fault_type="db-leak"),
            ],
        ),
    )

    evidence = validate_remediation_evidence(artifact_dir)

    assert evidence.confirmed_hypothesis_ids == ("root", "same-fault")


def test_remediation_evidence_blocks_competing_fault_needing_investigation(artifact_dir):
    hypotheses = json.loads(artifact_dir.joinpath("hypotheses.json").read_text())
    hypotheses["hypotheses"].append(_hypothesis("competitor", fault_type="high-cpu"))
    _write_json(artifact_dir / "hypotheses.json", hypotheses)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(
            1,
            confirmed=[_result("root", fault_type="db-leak")],
            needs_investigation=[_result("competitor")],
        ),
    )

    with pytest.raises(ArtifactValidationError, match="unresolved competing hypothesis competitor"):
        validate_remediation_evidence(artifact_dir)


def test_remediation_evidence_blocks_unclassified_competing_fault(artifact_dir):
    hypotheses = json.loads(artifact_dir.joinpath("hypotheses.json").read_text())
    hypotheses["hypotheses"].append(_hypothesis("competitor", fault_type="high-cpu"))
    _write_json(artifact_dir / "hypotheses.json", hypotheses)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, confirmed=[_result("root", fault_type="db-leak")]),
    )

    with pytest.raises(ArtifactValidationError, match="unresolved competing hypothesis competitor"):
        validate_remediation_evidence(artifact_dir)


def test_remediation_evidence_blocks_unclassified_competing_fault_branched_in_previous_loop(artifact_dir):
    competitor = _hypothesis(
        "competitor",
        parent_id="root",
        depth=1,
        fault_type="high-cpu",
    )
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, new_hypotheses=[competitor]),
    )
    _write_json(
        artifact_dir / "validation-2.json",
        _validation(2, confirmed=[_result("root", fault_type="db-leak")]),
    )

    with pytest.raises(ArtifactValidationError, match="unresolved competing hypothesis competitor"):
        validate_remediation_evidence(artifact_dir)


@pytest.mark.parametrize("fault_type", ["db-leak", "unsupported"])
def test_remediation_evidence_allows_noncompeting_unclassified_hypothesis(artifact_dir, fault_type):
    hypotheses = json.loads(artifact_dir.joinpath("hypotheses.json").read_text())
    hypotheses["hypotheses"].append(_hypothesis("noncompetitor", fault_type=fault_type))
    _write_json(artifact_dir / "hypotheses.json", hypotheses)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, confirmed=[_result("root", fault_type="db-leak")]),
    )

    evidence = validate_remediation_evidence(artifact_dir)

    assert evidence.confirmed_hypothesis_ids == ("root",)


@pytest.mark.parametrize("terminal_bucket", ["rejected", "closed"])
def test_remediation_evidence_allows_terminal_competing_fault(artifact_dir, terminal_bucket):
    hypotheses = json.loads(artifact_dir.joinpath("hypotheses.json").read_text())
    hypotheses["hypotheses"].append(_hypothesis("competitor", fault_type="high-cpu"))
    _write_json(artifact_dir / "hypotheses.json", hypotheses)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(
            1,
            confirmed=[_result("root", fault_type="db-leak")],
            **{terminal_bucket: [_result("competitor")]},
        ),
    )

    evidence = validate_remediation_evidence(artifact_dir)

    assert evidence.fault_type.value == "db-leak"


def test_remediation_evidence_accumulates_competing_confirmation_from_previous_loop(artifact_dir):
    hypotheses = json.loads(artifact_dir.joinpath("hypotheses.json").read_text())
    hypotheses["hypotheses"].append(_hypothesis("competitor", fault_type="high-cpu"))
    _write_json(artifact_dir / "hypotheses.json", hypotheses)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, confirmed=[_result("competitor", fault_type="high-cpu")]),
    )
    _write_json(
        artifact_dir / "validation-2.json",
        _validation(2, confirmed=[_result("root", fault_type="db-leak")]),
    )

    with pytest.raises(ArtifactValidationError, match="unresolved competing hypothesis competitor"):
        validate_remediation_evidence(artifact_dir)


@pytest.mark.parametrize("terminal_bucket", ["rejected", "closed"])
def test_remediation_evidence_allows_later_terminal_resolution_of_competing_confirmation(
    artifact_dir,
    terminal_bucket,
):
    hypotheses = json.loads(artifact_dir.joinpath("hypotheses.json").read_text())
    hypotheses["hypotheses"].append(_hypothesis("competitor", fault_type="high-cpu"))
    _write_json(artifact_dir / "hypotheses.json", hypotheses)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, confirmed=[_result("competitor", fault_type="high-cpu")]),
    )
    _write_json(
        artifact_dir / "validation-2.json",
        _validation(
            2,
            confirmed=[_result("root", fault_type="db-leak")],
            **{terminal_bucket: [_result("competitor")]},
        ),
    )

    evidence = validate_remediation_evidence(artifact_dir)

    assert evidence.confirmed_hypothesis_ids == ("root",)


@pytest.mark.parametrize(
    "missing_field",
    [
        "hypothesis_id",
        "tree_id",
        "title",
        "description",
        "fault_type",
        "category",
        "confidence_score",
        "required_evidence",
        "status",
        "parent_id",
        "depth",
    ],
)
def test_new_hypothesis_requires_all_schema_fields(artifact_dir, missing_field):
    child = _hypothesis("child", parent_id="root", depth=1)
    child.pop(missing_field)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, new_hypotheses=[child]),
    )

    with pytest.raises(ArtifactValidationError):
        validate_validation_artifacts(artifact_dir)


def test_new_hypothesis_rejects_parent_from_future_loop(artifact_dir):
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(
            1,
            new_hypotheses=[
                _hypothesis("child", parent_id="future-parent", depth=2),
            ],
        ),
    )
    _write_json(
        artifact_dir / "validation-2.json",
        _validation(
            2,
            new_hypotheses=[
                _hypothesis("future-parent", parent_id="root", depth=1),
            ],
        ),
    )

    with pytest.raises(ArtifactValidationError, match="validation-1.json new hypothesis parent_id is unknown"):
        validate_validation_artifacts(artifact_dir)


def test_new_hypothesis_rejects_unknown_parent(artifact_dir):
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(
            1,
            new_hypotheses=[
                _hypothesis("child", parent_id="missing", depth=1),
            ],
        ),
    )

    with pytest.raises(ArtifactValidationError, match="new hypothesis parent_id is unknown"):
        validate_validation_artifacts(artifact_dir)


def test_new_hypothesis_rejects_duplicate_id_from_previous_loop(artifact_dir):
    child = _hypothesis("child", parent_id="root", depth=1)
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(1, new_hypotheses=[child]),
    )
    _write_json(
        artifact_dir / "validation-2.json",
        _validation(2, new_hypotheses=[child]),
    )

    with pytest.raises(ArtifactValidationError, match="new hypothesis IDs must be unique"):
        validate_validation_artifacts(artifact_dir)


@pytest.mark.parametrize(
    ("tree_id", "depth", "error"),
    [
        ("wrong-tree", 1, "new hypothesis tree_id is invalid"),
        ("tree-1", 2, "new hypothesis depth must equal parent depth \\+ 1"),
    ],
)
def test_new_hypothesis_rejects_invalid_tree_or_depth(artifact_dir, tree_id, depth, error):
    _write_json(
        artifact_dir / "validation-1.json",
        _validation(
            1,
            new_hypotheses=[
                _hypothesis("child", parent_id="root", depth=depth, tree_id=tree_id),
            ],
        ),
    )

    with pytest.raises(ArtifactValidationError, match=error):
        validate_validation_artifacts(artifact_dir)
