from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rca_agent.evidence import (
    EvidenceCollectionResult,
    EvidenceOutput,
    _build_user_prompt,
    collect_evidence,
    run_evidence_collection,
    save_evidence_to_s3,
)
from rca_agent.models import (
    AlarmPayload,
    AlarmTrigger,
    Hypothesis,
    HypothesisCategory,
    ScopingResult,
)


@pytest.fixture()
def scoping_result() -> ScopingResult:
    return ScopingResult(
        alarm_summary="CPU spike on web-service",
        blast_radius="single",
        initial_severity="high",
        metric_snapshot={"CPUUtilization": {"current": 92.5, "baseline": 45.0, "unit": "Percent"}},
        raw_alarm=AlarmPayload(
            alarm_name="HighCPU",
            region="us-east-1",
            new_state_reason="Threshold crossed",
            trigger=AlarmTrigger(
                metric_name="CPUUtilization",
                namespace="AWS/ECS",
                dimensions={"ServiceName": "web-service"},
            ),
        ),
    )


@pytest.fixture()
def hypothesis() -> Hypothesis:
    return Hypothesis(
        hypothesis_id="h-1",
        description="Recent deployment caused connection leak",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=0.7,
        required_evidence=["deployment history", "connection metrics"],
        tree_id="tree-1",
    )


def _make_evidence_output(
    metrics: str = "CPU at 92%",
    logs: str = "ERROR: Too many connections",
    deploy: str = "Deployment at 10:00",
    summary: str = "Evidence points to recent deploy",
) -> EvidenceOutput:
    return EvidenceOutput(
        metrics_evidence=metrics,
        logs_evidence=logs,
        deploy_evidence=deploy,
        combined_summary=summary,
    )


def _make_mock_agent(output: EvidenceOutput) -> MagicMock:
    mock_result = MagicMock()
    mock_result.structured_output = output
    mock_agent = MagicMock()
    mock_agent.return_value = mock_result
    return mock_agent


class TestBuildUserPrompt:
    def test_includes_alarm_context(self, hypothesis: Hypothesis, scoping_result: ScopingResult):
        prompt = _build_user_prompt(hypothesis, scoping_result)
        assert "HighCPU" in prompt
        assert "us-east-1" in prompt
        assert "web-service" in prompt

    def test_includes_hypothesis_info(self, hypothesis: Hypothesis, scoping_result: ScopingResult):
        prompt = _build_user_prompt(hypothesis, scoping_result)
        assert "connection leak" in prompt
        assert "DEPLOYMENT" in prompt
        assert "deployment history" in prompt

    def test_includes_metric_snapshot(self, hypothesis: Hypothesis, scoping_result: ScopingResult):
        prompt = _build_user_prompt(hypothesis, scoping_result)
        assert "CPUUtilization" in prompt
        assert "92.5" in prompt

    def test_handles_no_alarm(self, hypothesis: Hypothesis):
        sr = ScopingResult(alarm_summary="test", raw_alarm=None)
        prompt = _build_user_prompt(hypothesis, sr)
        assert "N/A" in prompt


class TestCollectEvidence:
    def test_returns_combined_evidence(self, hypothesis: Hypothesis, scoping_result: ScopingResult):
        output = _make_evidence_output()
        agent = _make_mock_agent(output)

        result = collect_evidence(hypothesis, scoping_result, agent)

        assert isinstance(result, EvidenceCollectionResult)
        assert result.hypothesis_id == "h-1"
        assert "CPU at 92%" in result.evidence_text
        assert "Too many connections" in result.evidence_text
        assert "Deployment at 10:00" in result.evidence_text
        assert "metrics" in result.evidence_types
        assert "logs" in result.evidence_types
        assert "deploy_history" in result.evidence_types

    def test_handles_partial_evidence(self, hypothesis: Hypothesis, scoping_result: ScopingResult):
        output = EvidenceOutput(metrics_evidence="CPU high", logs_evidence="", deploy_evidence="")
        agent = _make_mock_agent(output)

        result = collect_evidence(hypothesis, scoping_result, agent)

        assert "CPU high" in result.evidence_text
        assert result.evidence_types == ["metrics"]

    def test_handles_empty_evidence(self, hypothesis: Hypothesis, scoping_result: ScopingResult):
        output = EvidenceOutput()
        agent = _make_mock_agent(output)

        result = collect_evidence(hypothesis, scoping_result, agent)

        assert "No evidence could be collected" in result.evidence_text

    def test_handles_timeout(self, hypothesis: Hypothesis, scoping_result: ScopingResult):
        import time as _time

        def slow_agent(prompt, **kwargs):
            _time.sleep(5)

        agent = MagicMock(side_effect=slow_agent)

        result = collect_evidence(hypothesis, scoping_result, agent, timeout_seconds=1)

        assert "timed out or failed" in result.evidence_text

    def test_handles_agent_exception(self, hypothesis: Hypothesis, scoping_result: ScopingResult):
        agent = MagicMock(side_effect=RuntimeError("boom"))

        result = collect_evidence(hypothesis, scoping_result, agent)

        assert "timed out or failed" in result.evidence_text

    def test_passes_structured_output_model(self, hypothesis: Hypothesis, scoping_result: ScopingResult):
        output = _make_evidence_output()
        agent = _make_mock_agent(output)

        collect_evidence(hypothesis, scoping_result, agent)

        _, kwargs = agent.call_args
        assert kwargs["structured_output_model"] is EvidenceOutput


class TestRunEvidenceCollection:
    def test_collects_for_all_hypotheses(self, scoping_result: ScopingResult):
        h1 = Hypothesis(
            hypothesis_id="h-1",
            description="Hypothesis 1",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            tree_id="tree-1",
        )
        h2 = Hypothesis(
            hypothesis_id="h-2",
            description="Hypothesis 2",
            category=HypothesisCategory.INFRASTRUCTURE,
            confidence_score=0.5,
            tree_id="tree-1",
        )

        output = _make_evidence_output()
        agent = _make_mock_agent(output)

        evidence_map = run_evidence_collection([h1, h2], scoping_result, agent)

        assert "h-1" in evidence_map
        assert "h-2" in evidence_map
        assert agent.call_count == 2


class TestSaveEvidenceToS3:
    def test_skips_when_no_bucket(self):
        with patch("rca_agent.evidence.S3_EVIDENCE_BUCKET", ""):
            result = save_evidence_to_s3("rca-1", {"h-1": "evidence"}, s3_client=MagicMock())
        assert result == []

    def test_skips_when_no_client(self):
        with patch("rca_agent.evidence.S3_EVIDENCE_BUCKET", "my-bucket"):
            result = save_evidence_to_s3("rca-1", {"h-1": "evidence"}, s3_client=None)
        assert result == []

    def test_saves_evidence(self):
        s3_client = MagicMock()
        evidence_map = {"h-1": "some evidence text", "h-2": "more evidence"}

        with patch("rca_agent.evidence.S3_EVIDENCE_BUCKET", "my-bucket"):
            keys = save_evidence_to_s3("rca-1", evidence_map, s3_client=s3_client)

        assert len(keys) == 2
        assert s3_client.put_object.call_count == 2

    def test_skips_empty_evidence(self):
        s3_client = MagicMock()
        evidence_map = {"h-1": "real evidence", "h-2": "  "}

        with patch("rca_agent.evidence.S3_EVIDENCE_BUCKET", "my-bucket"):
            keys = save_evidence_to_s3("rca-1", evidence_map, s3_client=s3_client)

        assert len(keys) == 1
        assert s3_client.put_object.call_count == 1

    def test_retries_on_failure(self):
        s3_client = MagicMock()
        s3_client.put_object.side_effect = [Exception("fail"), None]

        with patch("rca_agent.evidence.S3_EVIDENCE_BUCKET", "my-bucket"):
            keys = save_evidence_to_s3(
                "rca-1",
                {"h-1": "evidence"},
                s3_client=s3_client,
                base_delay=0.01,
            )

        assert len(keys) == 1
        assert s3_client.put_object.call_count == 2

    def test_gives_up_after_max_retries(self):
        s3_client = MagicMock()
        s3_client.put_object.side_effect = Exception("persistent failure")

        with patch("rca_agent.evidence.S3_EVIDENCE_BUCKET", "my-bucket"):
            keys = save_evidence_to_s3(
                "rca-1",
                {"h-1": "evidence"},
                s3_client=s3_client,
                max_retries=2,
                base_delay=0.01,
            )

        assert keys == []
        assert s3_client.put_object.call_count == 2
