from unittest.mock import MagicMock, patch

from rca_agent.models import Playbook, RcaReport
from rca_agent.playbook_gen import (
    PlaybookOutput,
    run_playbook_generation,
    save_playbook_to_s3_vectors,
)


def _make_report() -> RcaReport:
    return RcaReport(
        rca_id="rca-1",
        incident_summary="CPU spike on web-service",
        root_cause="Memory leak in worker",
        root_cause_confirmed=True,
        confidence_score=0.9,
        evidence_list=["high CPU", "memory growth"],
        temporary_mitigation="Restart tasks",
        permanent_remediation="Fix leak",
    )


def _make_mock_agent(output: PlaybookOutput) -> MagicMock:
    mock_result = MagicMock()
    mock_result.structured_output = output
    agent = MagicMock()
    agent.return_value = mock_result
    return agent


class TestRunPlaybookGeneration:
    def test_generates_playbook(self):
        output = PlaybookOutput(
            failure_type="Memory leak",
            symptom_pattern="CPU spike + memory growth",
            verification_steps=["Check memory metrics", "Check for OOM kills"],
            temporary_mitigation="Restart",
            permanent_remediation="Fix code",
            prevention_measures=["Add memory alerts"],
            tags=["memory", "cpu"],
        )
        agent = _make_mock_agent(output)

        playbook = run_playbook_generation(_make_report(), agent)

        assert isinstance(playbook, Playbook)
        assert playbook.failure_type == "Memory leak"
        assert playbook.rca_id == "rca-1"
        assert len(playbook.verification_steps) == 2
        assert playbook.tags == ["memory", "cpu"]

    def test_uses_structured_output(self):
        output = PlaybookOutput(failure_type="test", symptom_pattern="test")
        agent = _make_mock_agent(output)

        run_playbook_generation(_make_report(), agent)

        _, kwargs = agent.call_args
        assert kwargs["structured_output_model"] is PlaybookOutput

    def test_fallback_on_failure(self):
        agent = MagicMock(side_effect=RuntimeError("fail"))

        playbook = run_playbook_generation(_make_report(), agent)

        assert playbook.failure_type == "unknown"
        assert playbook.rca_id == "rca-1"


class TestSavePlaybookToS3Vectors:
    def test_skips_when_not_configured(self):
        playbook = Playbook(playbook_id="p-1", failure_type="t", symptom_pattern="t")
        assert not save_playbook_to_s3_vectors(playbook)

    @patch("rca_agent.playbook_gen.S3_VECTOR_BUCKET_NAME", "my-bucket")
    def test_indexes_playbook(self):
        playbook = Playbook(
            playbook_id="p-1",
            failure_type="Memory leak",
            symptom_pattern="CPU spike",
            rca_id="rca-1",
            tags=["memory"],
        )
        mock_client = MagicMock()

        result = save_playbook_to_s3_vectors(playbook, s3_vectors_client=mock_client)

        assert result is True
        mock_client.put_vectors.assert_called_once()

    @patch("rca_agent.playbook_gen.S3_VECTOR_BUCKET_NAME", "my-bucket")
    def test_handles_error(self):
        playbook = Playbook(playbook_id="p-1", failure_type="t", symptom_pattern="t")
        mock_client = MagicMock()
        mock_client.put_vectors.side_effect = RuntimeError("fail")

        result = save_playbook_to_s3_vectors(playbook, s3_vectors_client=mock_client)

        assert result is False
