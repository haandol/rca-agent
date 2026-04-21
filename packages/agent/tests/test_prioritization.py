from unittest.mock import MagicMock

from rca_agent.models import (
    Hypothesis,
    HypothesisCategory,
    PrioritizationResult,
    ScopingResult,
)
from rca_agent.prioritization import (
    PrioritizationOutput,
    _apply_fallback_order,
    _PrioritizedItem,
    run_prioritization,
)


def _make_hypotheses(count=3) -> list[Hypothesis]:
    categories = list(HypothesisCategory)
    return [
        Hypothesis(
            hypothesis_id=f"h-{i}",
            description=f"Hypothesis {i}",
            category=categories[i % len(categories)],
            confidence_score=0.7,
            tree_id="tree-1",
        )
        for i in range(count)
    ]


def _make_scoping() -> ScopingResult:
    return ScopingResult(alarm_summary="CPU spike on web-service", initial_severity="high")


def _make_mock_agent(output: PrioritizationOutput) -> MagicMock:
    mock_result = MagicMock()
    mock_result.structured_output = output
    mock_agent = MagicMock()
    mock_agent.return_value = mock_result
    return mock_agent


class TestFallbackOrder:
    def test_orders_by_category(self):
        kw = {"confidence_score": 0.5, "tree_id": "t"}
        hyps = [
            Hypothesis(hypothesis_id="h-config", description="config", category=HypothesisCategory.CONFIGURATION, **kw),
            Hypothesis(hypothesis_id="h-deploy", description="deploy", category=HypothesisCategory.DEPLOYMENT, **kw),
            Hypothesis(hypothesis_id="h-traffic", description="traffic", category=HypothesisCategory.TRAFFIC, **kw),
        ]
        result = _apply_fallback_order(hyps)
        assert result[0].hypothesis_id == "h-deploy"
        assert result[1].hypothesis_id == "h-traffic"
        assert result[2].hypothesis_id == "h-config"


class TestRunPrioritization:
    def test_returns_prioritized_result(self):
        hyps = _make_hypotheses(3)
        output = PrioritizationOutput(
            prioritized=[
                _PrioritizedItem(hypothesis_id="h-0", priority_rank=1, tools=["cloudwatch"], estimated_seconds=30),
                _PrioritizedItem(hypothesis_id="h-1", priority_rank=2, tools=["cloudtrail"], estimated_seconds=60),
                _PrioritizedItem(hypothesis_id="h-2", priority_rank=3, tools=["xray"], estimated_seconds=45),
            ]
        )
        agent = _make_mock_agent(output)

        result = run_prioritization(_make_scoping(), hyps, agent)

        assert isinstance(result, PrioritizationResult)
        assert len(result.prioritized) == 3
        assert result.prioritized[0].priority_rank == 1
        assert result.prioritized[0].validation_plan.tools == ["cloudwatch"]

    def test_uses_structured_output(self):
        hyps = _make_hypotheses(2)
        output = PrioritizationOutput(
            prioritized=[
                _PrioritizedItem(hypothesis_id="h-0", priority_rank=1),
                _PrioritizedItem(hypothesis_id="h-1", priority_rank=2),
            ]
        )
        agent = _make_mock_agent(output)

        run_prioritization(_make_scoping(), hyps, agent)

        _, kwargs = agent.call_args
        assert kwargs["structured_output_model"] is PrioritizationOutput

    def test_fallback_on_failure(self):
        hyps = _make_hypotheses(3)
        agent = MagicMock(side_effect=RuntimeError("LLM error"))

        result = run_prioritization(_make_scoping(), hyps, agent)

        assert len(result.prioritized) == 3
        assert result.prioritized[0].priority_rank == 1
