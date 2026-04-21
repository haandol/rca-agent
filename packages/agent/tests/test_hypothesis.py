import time as _time
from unittest.mock import MagicMock

import pytest

from rca_agent.hypothesis import (
    HypothesisOutput,
    _build_metric_snapshot_text,
    _build_playbook_context,
    _build_user_prompt,
    _HypothesisItem,
    run_hypothesis_generation,
)
from rca_agent.models import (
    HypothesisCategory,
    HypothesisGenerationResult,
    PlaybookMatch,
    ScopingResult,
)


@pytest.fixture()
def sample_scoping_result() -> ScopingResult:
    return ScopingResult(
        alarm_summary="CPU utilization on web-service exceeded 80%",
        blast_radius="single",
        initial_severity="high",
        metric_snapshot={"CPUUtilization": {"current": 92.5, "baseline": 45.0, "unit": "Percent"}},
        similar_playbooks=[
            PlaybookMatch(
                playbook_id="pb-001",
                title="ECS CPU spike",
                similarity=0.85,
                root_cause_summary="Task count too low after deployment",
            )
        ],
    )


def _make_hypothesis_output(count: int = 3) -> HypothesisOutput:
    items = []
    categories = list(HypothesisCategory)
    for i in range(count):
        items.append(
            _HypothesisItem(
                description=f"Hypothesis {i + 1}",
                category=categories[i % len(categories)],
                confidence_score=round(0.8 - i * 0.1, 1),
                required_evidence=[f"evidence-{i + 1}a", f"evidence-{i + 1}b"],
                referenced_playbook_id="pb-001" if i == 0 else None,
            )
        )
    return HypothesisOutput(hypotheses=items)


def _make_mock_agent(output: HypothesisOutput) -> MagicMock:
    mock_result = MagicMock()
    mock_result.structured_output = output
    mock_agent = MagicMock()
    mock_agent.return_value = mock_result
    return mock_agent


class TestBuildPlaybookContext:
    def test_empty_playbooks(self):
        result = _build_playbook_context([])
        assert result == "No similar playbooks found."

    def test_with_playbooks(self):
        playbooks = [
            PlaybookMatch(playbook_id="pb-1", title="CPU issue", similarity=0.9, root_cause_summary="Memory leak"),
        ]
        result = _build_playbook_context(playbooks)
        assert "CPU issue" in result
        assert "pb-1" in result
        assert "Memory leak" in result


class TestBuildMetricSnapshotText:
    def test_empty(self):
        assert _build_metric_snapshot_text({}) == "No metric data available."

    def test_with_data(self):
        snapshot = {"CPUUtilization": {"current": 92.5, "baseline": 45.0, "unit": "Percent"}}
        result = _build_metric_snapshot_text(snapshot)
        assert "CPUUtilization" in result
        assert "92.5" in result
        assert "45.0" in result


class TestBuildUserPrompt:
    def test_contains_scoping_details(self, sample_scoping_result: ScopingResult):
        prompt = _build_user_prompt(sample_scoping_result)
        assert "CPU utilization on web-service exceeded 80%" in prompt
        assert "single" in prompt
        assert "high" in prompt
        assert "CPUUtilization" in prompt
        assert "ECS CPU spike" in prompt


class TestRunHypothesisGeneration:
    def test_returns_hypotheses(self, sample_scoping_result: ScopingResult):
        output = _make_hypothesis_output(4)
        mock_agent = _make_mock_agent(output)

        result = run_hypothesis_generation(sample_scoping_result, mock_agent)

        assert isinstance(result, HypothesisGenerationResult)
        assert len(result.hypotheses) == 4
        assert result.tree_id
        assert result.scoping_result is sample_scoping_result

    def test_hypotheses_have_tree_id(self, sample_scoping_result: ScopingResult):
        output = _make_hypothesis_output(3)
        mock_agent = _make_mock_agent(output)

        result = run_hypothesis_generation(sample_scoping_result, mock_agent)

        for h in result.hypotheses:
            assert h.tree_id == result.tree_id
            assert h.parent_id is None
            assert h.depth == 0
            assert h.hypothesis_id

    def test_hypotheses_preserve_fields(self, sample_scoping_result: ScopingResult):
        output = _make_hypothesis_output(1)
        mock_agent = _make_mock_agent(output)

        result = run_hypothesis_generation(sample_scoping_result, mock_agent)

        h = result.hypotheses[0]
        assert h.description == "Hypothesis 1"
        assert h.category == HypothesisCategory.DEPLOYMENT
        assert h.confidence_score == 0.8
        assert h.required_evidence == ["evidence-1a", "evidence-1b"]
        assert h.referenced_playbook_id == "pb-001"

    def test_passes_structured_output_model(self, sample_scoping_result: ScopingResult):
        output = _make_hypothesis_output(3)
        mock_agent = _make_mock_agent(output)

        run_hypothesis_generation(sample_scoping_result, mock_agent)

        _, kwargs = mock_agent.call_args
        assert kwargs["structured_output_model"] is HypothesisOutput

    def test_prompt_contains_scoping_data(self, sample_scoping_result: ScopingResult):
        output = _make_hypothesis_output(3)
        mock_agent = _make_mock_agent(output)

        run_hypothesis_generation(sample_scoping_result, mock_agent)

        prompt = mock_agent.call_args[0][0]
        assert "CPU utilization on web-service exceeded 80%" in prompt
        assert "ECS CPU spike" in prompt

    def test_retries_on_failure(self, sample_scoping_result: ScopingResult):
        output = _make_hypothesis_output(3)
        mock_result = MagicMock()
        mock_result.structured_output = output

        mock_agent = MagicMock()
        mock_agent.side_effect = [
            RuntimeError("parse error"),
            mock_result,
        ]

        result = run_hypothesis_generation(sample_scoping_result, mock_agent)

        assert len(result.hypotheses) == 3
        assert mock_agent.call_count == 2

    def test_returns_empty_after_exhausting_retries(self, sample_scoping_result: ScopingResult):
        mock_agent = MagicMock(side_effect=RuntimeError("persistent failure"))

        result = run_hypothesis_generation(sample_scoping_result, mock_agent, max_retries=2)

        assert result.hypotheses == []
        assert result.tree_id
        assert mock_agent.call_count == 2

    def test_timeout_triggers_retry(self, sample_scoping_result: ScopingResult):
        output = _make_hypothesis_output(3)
        mock_result = MagicMock()
        mock_result.structured_output = output

        call_count = 0

        def side_effect(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                _time.sleep(5)
                return mock_result
            return mock_result

        mock_agent = MagicMock(side_effect=side_effect)

        result = run_hypothesis_generation(sample_scoping_result, mock_agent, timeout_seconds=1, max_retries=2)

        assert len(result.hypotheses) == 3

    def test_timeout_all_attempts_returns_empty(self, sample_scoping_result: ScopingResult):
        def slow_agent(prompt, **kwargs):
            _time.sleep(5)

        mock_agent = MagicMock(side_effect=slow_agent)

        result = run_hypothesis_generation(sample_scoping_result, mock_agent, timeout_seconds=1, max_retries=2)

        assert result.hypotheses == []
