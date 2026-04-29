from unittest.mock import MagicMock

from rca_agent.branching import (
    BranchingOutput,
    _ChildItem,
    _is_duplicate,
    run_branching,
)
from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
)


def _make_parent(depth=0) -> Hypothesis:
    return Hypothesis(
        hypothesis_id="parent-1",
        description="Parent hypothesis",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=0.5,
        tree_id="tree-1",
        depth=depth,
    )


def _make_mock_agent(output: BranchingOutput) -> MagicMock:
    mock_result = MagicMock()
    mock_result.structured_output = output
    agent = MagicMock()
    agent.return_value = mock_result
    return agent


class TestIsDuplicate:
    def test_detects_parent_duplicate(self):
        parent = _make_parent()
        assert _is_duplicate("Parent hypothesis", parent, [])

    def test_detects_rejected_duplicate(self):
        parent = _make_parent()
        assert _is_duplicate("Old rejected idea", parent, ["Old rejected idea"])

    def test_case_insensitive(self):
        parent = _make_parent()
        assert _is_duplicate("PARENT HYPOTHESIS", parent, [])

    def test_allows_unique(self):
        parent = _make_parent()
        assert not _is_duplicate("A new child", parent, ["something else"])


class TestRunBranching:
    def test_generates_children(self):
        parent = _make_parent()
        output = BranchingOutput(
            children=[
                _ChildItem(description="Child 1", category=HypothesisCategory.DEPLOYMENT, confidence_score=0.4),
                _ChildItem(description="Child 2", category=HypothesisCategory.DEPLOYMENT, confidence_score=0.3),
            ]
        )
        agent = _make_mock_agent(output)

        result = run_branching(parent, "some evidence", [], agent)

        assert len(result.children) == 2
        assert result.parent_id == "parent-1"
        assert result.tree_id == "tree-1"
        for child in result.children:
            assert child.parent_id == "parent-1"
            assert child.depth == 1

    def test_uses_structured_output(self):
        parent = _make_parent()
        cat = HypothesisCategory.DEPLOYMENT
        output = BranchingOutput(children=[_ChildItem(description="c", category=cat, confidence_score=0.3)])
        agent = _make_mock_agent(output)

        run_branching(parent, "", [], agent)

        _, kwargs = agent.call_args
        assert kwargs["structured_output_model"] is BranchingOutput

    def test_filters_duplicates(self):
        parent = _make_parent()
        cat = HypothesisCategory.DEPLOYMENT
        output = BranchingOutput(
            children=[
                _ChildItem(description="Parent hypothesis", category=cat, confidence_score=0.4),
                _ChildItem(description="Already rejected", category=cat, confidence_score=0.3),
                _ChildItem(description="Valid child", category=cat, confidence_score=0.5),
            ]
        )
        agent = _make_mock_agent(output)

        result = run_branching(parent, "", ["Already rejected"], agent)

        assert len(result.children) == 1
        assert result.children[0].description == "Valid child"

    def test_max_depth_returns_empty(self):
        parent = _make_parent(depth=3)
        agent = MagicMock()

        result = run_branching(parent, "", [], agent, max_depth=3)

        assert result.children == []
        agent.assert_not_called()

    def test_failure_returns_empty(self):
        parent = _make_parent()
        agent = MagicMock(side_effect=RuntimeError("fail"))

        result = run_branching(parent, "", [], agent)

        assert result.children == []
