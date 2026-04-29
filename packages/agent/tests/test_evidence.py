from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rca_agent.ports.dto.models import (
    AlarmPayload,
    AlarmTrigger,
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    ScopingResult,
)
from rca_agent.services.evidence import (
    EVIDENCE_FAILED_SENTINEL,
    EvidenceCollectionResult,
    EvidenceCollectionSummary,
    EvidenceOutput,
    _build_parent_context,
    _build_user_prompt,
    collect_evidence,
    run_evidence_collection,
    save_evidence_to_s3,
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
    code_change: str = "",
    summary: str = "Evidence points to recent deploy",
) -> EvidenceOutput:
    return EvidenceOutput(
        metrics_evidence=metrics,
        logs_evidence=logs,
        deploy_evidence=deploy,
        code_change_evidence=code_change,
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


class TestBuildParentContext:
    def test_no_parent_returns_empty(self):
        h = Hypothesis(
            hypothesis_id="h-1",
            description="Root hypothesis",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
        )
        assert _build_parent_context(h, None, None) == ""

    def test_parent_with_evidence_summary(self):
        parent = Hypothesis(
            hypothesis_id="h-1",
            description="Parent hypothesis",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            status=HypothesisStatus.NEEDS_INVESTIGATION,
        )
        child = Hypothesis(
            hypothesis_id="h-1-a",
            description="Child hypothesis",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.5,
            parent_id="h-1",
            depth=1,
        )
        by_id = {"h-1": parent}
        ev_map = {"h-1": "CPU spike correlated with deploy at 10:00"}

        result = _build_parent_context(child, by_id, ev_map)

        assert "Parent hypothesis" in result
        assert "DEPLOYMENT" in result
        assert "CPU spike correlated with deploy" in result

    def test_rejected_parent_shows_minimal_info(self):
        parent = Hypothesis(
            hypothesis_id="h-1",
            description="Rejected parent",
            category=HypothesisCategory.TRAFFIC,
            confidence_score=0.2,
            status=HypothesisStatus.REJECTED,
        )
        child = Hypothesis(
            hypothesis_id="h-1-a",
            description="Child",
            category=HypothesisCategory.TRAFFIC,
            confidence_score=0.5,
            parent_id="h-1",
            depth=1,
        )
        by_id = {"h-1": parent}
        ev_map = {"h-1": "Full evidence that should not appear"}

        result = _build_parent_context(child, by_id, ev_map)

        assert "REJECTED" in result
        assert "Rejected parent" in result
        assert "Full evidence that should not appear" not in result

    def test_parent_not_in_lookup_returns_empty(self):
        child = Hypothesis(
            hypothesis_id="h-1-a",
            description="Child",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.5,
            parent_id="h-missing",
            depth=1,
        )
        assert _build_parent_context(child, {}, {"h-missing": "data"}) == ""

    def test_parent_with_no_evidence_returns_empty(self):
        parent = Hypothesis(
            hypothesis_id="h-1",
            description="Parent",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            status=HypothesisStatus.NEEDS_INVESTIGATION,
        )
        child = Hypothesis(
            hypothesis_id="h-1-a",
            description="Child",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.5,
            parent_id="h-1",
            depth=1,
        )
        by_id = {"h-1": parent}
        assert _build_parent_context(child, by_id, {}) == ""


class TestBuildUserPromptWithParentContext:
    def test_child_prompt_includes_parent_context(self, scoping_result):
        parent = Hypothesis(
            hypothesis_id="h-1",
            description="Parent: deployment caused leak",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            status=HypothesisStatus.NEEDS_INVESTIGATION,
        )
        child = Hypothesis(
            hypothesis_id="h-1-a",
            description="Specific connection pool exhaustion",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.5,
            required_evidence=["pool metrics"],
            parent_id="h-1",
            depth=1,
            tree_id="tree-1",
        )
        by_id = {"h-1": parent, "h-1-a": child}
        ev_map = {"h-1": "Deploy at 10:00 caused connection spike"}

        prompt = _build_user_prompt(
            child,
            scoping_result,
            hypotheses_by_id=by_id,
            evidence_map=ev_map,
        )

        assert "Parent Hypothesis Evidence" in prompt
        assert "Deploy at 10:00 caused connection spike" in prompt
        assert "Specific connection pool exhaustion" in prompt

    def test_root_prompt_has_no_parent_context(self, hypothesis, scoping_result):
        prompt = _build_user_prompt(
            hypothesis,
            scoping_result,
            hypotheses_by_id={"h-1": hypothesis},
            evidence_map={},
        )
        assert "Parent Hypothesis" not in prompt


class TestCollectEvidence:
    @patch("rca_agent.services.evidence.create_evidence_collection_agent")
    def test_returns_combined_evidence(self, mock_create, hypothesis, scoping_result):
        output = _make_evidence_output()
        mock_create.return_value = _make_mock_agent(output)

        result = collect_evidence(hypothesis, scoping_result)

        assert isinstance(result, EvidenceCollectionResult)
        assert result.hypothesis_id == "h-1"
        assert "CPU at 92%" in result.full_evidence
        assert "Too many connections" in result.full_evidence
        assert "Deployment at 10:00" in result.full_evidence
        assert "metrics" in result.evidence_types
        assert "logs" in result.evidence_types
        assert "deploy_history" in result.evidence_types
        assert not result.failed

    @patch("rca_agent.services.evidence.create_evidence_collection_agent")
    def test_summary_from_combined_summary(self, mock_create, hypothesis, scoping_result):
        output = _make_evidence_output(summary="Deploy caused CPU spike")
        mock_create.return_value = _make_mock_agent(output)

        result = collect_evidence(hypothesis, scoping_result)

        assert result.summary == "Deploy caused CPU spike"

    @patch("rca_agent.services.evidence.create_evidence_collection_agent")
    def test_includes_code_change_evidence(self, mock_create, hypothesis, scoping_result):
        output = _make_evidence_output(code_change="Removed connection.close() in db.py:42")
        mock_create.return_value = _make_mock_agent(output)

        result = collect_evidence(hypothesis, scoping_result)

        assert "connection.close()" in result.full_evidence
        assert "code_change" in result.evidence_types

    @patch("rca_agent.services.evidence.create_evidence_collection_agent")
    def test_handles_partial_evidence(self, mock_create, hypothesis, scoping_result):
        output = EvidenceOutput(metrics_evidence="CPU high", logs_evidence="", deploy_evidence="")
        mock_create.return_value = _make_mock_agent(output)

        result = collect_evidence(hypothesis, scoping_result)

        assert "CPU high" in result.full_evidence
        assert result.evidence_types == ["metrics"]

    @patch("rca_agent.services.evidence.create_evidence_collection_agent")
    def test_handles_empty_evidence(self, mock_create, hypothesis, scoping_result):
        output = EvidenceOutput()
        mock_create.return_value = _make_mock_agent(output)

        result = collect_evidence(hypothesis, scoping_result)

        assert "No evidence could be collected" in result.full_evidence

    @patch("rca_agent.services.evidence.create_evidence_collection_agent")
    def test_handles_timeout(self, mock_create, hypothesis, scoping_result):
        import time as _time

        def slow_agent(prompt, **kwargs):
            _time.sleep(5)

        mock_agent = MagicMock(side_effect=slow_agent)
        mock_create.return_value = mock_agent

        result = collect_evidence(hypothesis, scoping_result, timeout_seconds=1)

        assert result.failed
        assert result.summary == EVIDENCE_FAILED_SENTINEL

    @patch("rca_agent.services.evidence.create_evidence_collection_agent")
    def test_handles_agent_exception(self, mock_create, hypothesis, scoping_result):
        mock_agent = MagicMock(side_effect=RuntimeError("boom"))
        mock_create.return_value = mock_agent

        result = collect_evidence(hypothesis, scoping_result)

        assert result.failed
        assert result.summary == EVIDENCE_FAILED_SENTINEL

    @patch("rca_agent.services.evidence.create_evidence_collection_agent")
    def test_passes_structured_output_model(self, mock_create, hypothesis, scoping_result):
        output = _make_evidence_output()
        mock_agent = _make_mock_agent(output)
        mock_create.return_value = mock_agent

        collect_evidence(hypothesis, scoping_result)

        _, kwargs = mock_agent.call_args
        assert kwargs["structured_output_model"] is EvidenceOutput

    @patch("rca_agent.services.evidence.create_evidence_collection_agent")
    def test_passes_mcp_clients(self, mock_create, hypothesis, scoping_result):
        output = _make_evidence_output()
        mock_create.return_value = _make_mock_agent(output)
        mcp_clients = [MagicMock(), MagicMock()]

        collect_evidence(hypothesis, scoping_result, mcp_clients=mcp_clients)

        mock_create.assert_called_once_with(mcp_clients=mcp_clients)

    @patch("rca_agent.services.evidence.create_evidence_collection_agent")
    def test_creates_fresh_agent_per_call(self, mock_create, hypothesis, scoping_result):
        output = _make_evidence_output()
        mock_create.return_value = _make_mock_agent(output)

        collect_evidence(hypothesis, scoping_result)
        collect_evidence(hypothesis, scoping_result)

        assert mock_create.call_count == 2


class TestRunEvidenceCollection:
    @patch("rca_agent.services.evidence.collect_evidence")
    def test_collects_for_all_hypotheses(self, mock_collect, scoping_result):
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

        mock_collect.side_effect = [
            EvidenceCollectionResult(
                hypothesis_id="h-1",
                summary="Evidence for h-1",
                full_evidence="Full evidence for h-1",
                evidence_types=["metrics"],
            ),
            EvidenceCollectionResult(
                hypothesis_id="h-2",
                summary="Evidence for h-2",
                full_evidence="Full evidence for h-2",
                evidence_types=["logs"],
            ),
        ]

        summary = run_evidence_collection([h1, h2], scoping_result)

        assert isinstance(summary, EvidenceCollectionSummary)
        assert "h-1" in summary.evidence_map
        assert "h-2" in summary.evidence_map
        assert len(summary.failed_ids) == 0
        assert mock_collect.call_count == 2

    @patch("rca_agent.services.evidence.collect_evidence")
    def test_tracks_failed_hypotheses(self, mock_collect, scoping_result):
        h1 = Hypothesis(
            hypothesis_id="h-1",
            description="Hypothesis 1",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            tree_id="tree-1",
        )

        mock_collect.return_value = EvidenceCollectionResult(
            hypothesis_id="h-1",
            summary=EVIDENCE_FAILED_SENTINEL,
            full_evidence=EVIDENCE_FAILED_SENTINEL,
            failed=True,
        )

        summary = run_evidence_collection([h1], scoping_result)

        assert "h-1" in summary.failed_ids
        assert summary.evidence_map["h-1"] == EVIDENCE_FAILED_SENTINEL

    @patch("rca_agent.services.evidence._save_single_evidence_to_s3")
    @patch("rca_agent.services.evidence.collect_evidence")
    def test_saves_to_s3_when_rca_id_provided(self, mock_collect, mock_s3_save, scoping_result):
        h1 = Hypothesis(
            hypothesis_id="h-1",
            description="Hypothesis 1",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            tree_id="tree-1",
        )

        mock_collect.return_value = EvidenceCollectionResult(
            hypothesis_id="h-1",
            summary="Evidence summary",
            full_evidence="Full evidence text",
            evidence_types=["metrics"],
        )
        s3_client = MagicMock()

        run_evidence_collection(
            [h1],
            scoping_result,
            rca_id="rca-123",
            s3_client=s3_client,
        )

        mock_s3_save.assert_called_once_with(
            "rca-123",
            "h-1",
            "Full evidence text",
            s3_client=s3_client,
        )

    @patch("rca_agent.services.evidence._save_single_evidence_to_s3")
    @patch("rca_agent.services.evidence.collect_evidence")
    def test_skips_s3_for_failed_evidence(self, mock_collect, mock_s3_save, scoping_result):
        h1 = Hypothesis(
            hypothesis_id="h-1",
            description="Hypothesis 1",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            tree_id="tree-1",
        )

        mock_collect.return_value = EvidenceCollectionResult(
            hypothesis_id="h-1",
            summary=EVIDENCE_FAILED_SENTINEL,
            full_evidence=EVIDENCE_FAILED_SENTINEL,
            failed=True,
        )

        run_evidence_collection(
            [h1],
            scoping_result,
            rca_id="rca-123",
            s3_client=MagicMock(),
        )

        mock_s3_save.assert_not_called()

    @patch("rca_agent.services.evidence.collect_evidence")
    def test_updates_trace(self, mock_collect, scoping_result):
        h1 = Hypothesis(
            hypothesis_id="h-1",
            description="Hypothesis 1",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            tree_id="tree-1",
        )

        mock_collect.return_value = EvidenceCollectionResult(
            hypothesis_id="h-1",
            summary="Evidence summary",
            full_evidence="Full evidence text",
            evidence_types=["metrics"],
        )
        trace = MagicMock()

        run_evidence_collection([h1], scoping_result, trace=trace)

        trace.update_hypothesis_evidence.assert_called_once_with(
            "h-1",
            evidence_summary="Evidence summary",
        )

    @patch("rca_agent.services.evidence.collect_evidence")
    def test_passes_mcp_clients_and_timeout(self, mock_collect, scoping_result):
        h1 = Hypothesis(
            hypothesis_id="h-1",
            description="Hypothesis 1",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            tree_id="tree-1",
        )

        mock_collect.return_value = EvidenceCollectionResult(
            hypothesis_id="h-1",
            summary="s",
            full_evidence="f",
            evidence_types=[],
        )
        mcp_clients = [MagicMock()]

        run_evidence_collection(
            [h1],
            scoping_result,
            mcp_clients=mcp_clients,
            timeout_seconds=30,
        )

        _, kwargs = mock_collect.call_args
        assert kwargs["mcp_clients"] is mcp_clients
        assert kwargs["timeout_seconds"] == 30

    @patch("rca_agent.services.evidence.collect_evidence")
    def test_passes_existing_evidence_map_for_parent_lookup(self, mock_collect, scoping_result):
        child = Hypothesis(
            hypothesis_id="h-1-a",
            description="Child hypothesis",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.5,
            parent_id="h-1",
            depth=1,
            tree_id="tree-1",
        )

        mock_collect.return_value = EvidenceCollectionResult(
            hypothesis_id="h-1-a",
            summary="Child evidence",
            full_evidence="Full child evidence",
            evidence_types=["metrics"],
        )
        existing = {"h-1": "Parent evidence summary"}

        run_evidence_collection(
            [child],
            scoping_result,
            existing_evidence_map=existing,
        )

        _, kwargs = mock_collect.call_args
        assert "h-1" in kwargs["evidence_map"]
        assert kwargs["evidence_map"]["h-1"] == "Parent evidence summary"

    @patch("rca_agent.services.evidence.collect_evidence")
    def test_returns_only_new_evidence(self, mock_collect, scoping_result):
        child = Hypothesis(
            hypothesis_id="h-1-a",
            description="Child",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.5,
            parent_id="h-1",
            depth=1,
            tree_id="tree-1",
        )

        mock_collect.return_value = EvidenceCollectionResult(
            hypothesis_id="h-1-a",
            summary="Child evidence",
            full_evidence="Full",
            evidence_types=[],
        )

        summary = run_evidence_collection(
            [child],
            scoping_result,
            existing_evidence_map={"h-1": "Parent summary"},
        )

        assert "h-1-a" in summary.evidence_map
        assert "h-1" not in summary.evidence_map

    @patch("rca_agent.services.evidence.collect_evidence")
    def test_passes_all_hypotheses_for_parent_lookup(self, mock_collect, scoping_result):
        parent = Hypothesis(
            hypothesis_id="h-1",
            description="Parent",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            tree_id="tree-1",
        )
        child = Hypothesis(
            hypothesis_id="h-1-a",
            description="Child",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.5,
            parent_id="h-1",
            depth=1,
            tree_id="tree-1",
        )

        mock_collect.return_value = EvidenceCollectionResult(
            hypothesis_id="h-1-a",
            summary="s",
            full_evidence="f",
            evidence_types=[],
        )

        run_evidence_collection(
            [child],
            scoping_result,
            existing_evidence_map={"h-1": "Parent evidence"},
            all_hypotheses=[parent, child],
        )

        _, kwargs = mock_collect.call_args
        assert "h-1" in kwargs["hypotheses_by_id"]
        assert "h-1-a" in kwargs["hypotheses_by_id"]


class TestSaveEvidenceToS3:
    def test_skips_when_no_bucket(self):
        with patch("rca_agent.services.evidence.S3_EVIDENCE_BUCKET", ""):
            result = save_evidence_to_s3("rca-1", {"h-1": "evidence"}, s3_client=MagicMock())
        assert result == []

    def test_skips_when_no_client(self):
        with patch("rca_agent.services.evidence.S3_EVIDENCE_BUCKET", "my-bucket"):
            result = save_evidence_to_s3("rca-1", {"h-1": "evidence"}, s3_client=None)
        assert result == []

    def test_saves_evidence(self):
        s3_client = MagicMock()
        evidence_map = {"h-1": "some evidence text", "h-2": "more evidence"}

        with patch("rca_agent.services.evidence.S3_EVIDENCE_BUCKET", "my-bucket"):
            keys = save_evidence_to_s3("rca-1", evidence_map, s3_client=s3_client)

        assert len(keys) == 2
        assert s3_client.put_object.call_count == 2

    def test_skips_empty_evidence(self):
        s3_client = MagicMock()
        evidence_map = {"h-1": "real evidence", "h-2": "  "}

        with patch("rca_agent.services.evidence.S3_EVIDENCE_BUCKET", "my-bucket"):
            keys = save_evidence_to_s3("rca-1", evidence_map, s3_client=s3_client)

        assert len(keys) == 1
        assert s3_client.put_object.call_count == 1

    def test_retries_on_failure(self):
        s3_client = MagicMock()
        s3_client.put_object.side_effect = [Exception("fail"), None]

        with patch("rca_agent.services.evidence.S3_EVIDENCE_BUCKET", "my-bucket"):
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

        with patch("rca_agent.services.evidence.S3_EVIDENCE_BUCKET", "my-bucket"):
            keys = save_evidence_to_s3(
                "rca-1",
                {"h-1": "evidence"},
                s3_client=s3_client,
                max_retries=2,
                base_delay=0.01,
            )

        assert keys == []
        assert s3_client.put_object.call_count == 2
