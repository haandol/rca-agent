from unittest.mock import MagicMock, patch

from rca_agent.ports.dto.models import (
    AlarmPayload,
    AlarmTrigger,
    Playbook,
    RcaReport,
    ScopingResult,
)
from rca_agent.services.playbook_gen import (
    PlaybookOutput,
    PlaybookUpdateOutput,
    _build_embed_key,
    _ExistingPlaybookHit,
    _try_update_existing,
    run_playbook_generation,
    save_playbook_to_s3_vectors,
    search_existing_playbooks,
)


def _make_report() -> RcaReport:
    return RcaReport(
        rca_id="rca-1",
        incident_summary="CPU spike on web-service",
        severity="high",
        impact_summary="50% error rate for 15 minutes",
        detection_method="CloudWatch CPUUtilization alarm",
        root_cause="Memory leak in worker",
        root_cause_confirmed=True,
        confidence_score=0.9,
        evidence_list=["high CPU", "memory growth"],
        temporary_mitigation="Restart tasks",
        permanent_remediation="Fix leak",
        action_items=["[prevent] Add memory alerts"],
    )


def _make_scoping() -> ScopingResult:
    alarm = AlarmPayload(
        alarm_name="HighCPU",
        trigger=AlarmTrigger(
            metric_name="CPUUtilization",
            namespace="AWS/ECS",
        ),
    )
    return ScopingResult(alarm_summary="CPU spike", raw_alarm=alarm)


def _make_mock_agent(output) -> MagicMock:
    mock_result = MagicMock()
    mock_result.structured_output = output
    agent = MagicMock()
    agent.return_value = mock_result
    return agent


def _make_hit(**overrides) -> _ExistingPlaybookHit:
    defaults = {
        "playbook_id": "existing-1",
        "similarity": 0.9,
        "failure_type": "Memory leak",
        "symptom_pattern": "CPU spike + memory growth",
        "severity_criteria": "Critical if OOM kills detected",
        "verification_steps": ["Check memory"],
        "temporary_mitigation": "Restart",
        "permanent_remediation": "Fix code",
        "escalation_criteria": "Escalate if not resolved in 10 min",
        "prevention_measures": ["Add alerts"],
        "related_metrics": ["CPUUtilization", "MemoryUtilization"],
        "tags": ["memory"],
    }
    defaults.update(overrides)
    return _ExistingPlaybookHit(**defaults)


class TestBuildEmbedKey:
    def test_includes_all_parts(self, fake_embedding):
        report = _make_report()
        scoping = _make_scoping()
        key = _build_embed_key(report, scoping)
        assert "Memory leak in worker" in key
        assert "CPUUtilization" in key
        assert "CPU spike on web-service" in key

    def test_without_scoping(self, fake_embedding):
        report = _make_report()
        key = _build_embed_key(report, None)
        assert "Memory leak in worker" in key
        assert "CPU spike on web-service" in key
        assert "CPUUtilization" not in key


class TestSearchExistingPlaybooks:
    def test_skips_when_not_configured(self, fake_embedding):
        result = search_existing_playbooks(_make_report(), None, embedding=fake_embedding)
        assert result == []

    @patch("rca_agent.services.playbook_gen.S3_VECTOR_BUCKET_NAME", "my-bucket")
    def test_returns_hits_above_threshold(self, fake_embedding):
        mock_client = MagicMock()
        mock_client.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "pb-1",
                    "distance": 0.9,
                    "metadata": {
                        "failure_type": "Memory leak",
                        "symptom_pattern": "CPU spike",
                    },
                },
                {
                    "key": "pb-2",
                    "distance": 0.5,
                    "metadata": {"failure_type": "Other"},
                },
            ]
        }
        hits = search_existing_playbooks(
            _make_report(),
            _make_scoping(),
            embedding=fake_embedding,
            s3_vectors_client=mock_client,
        )
        assert len(hits) == 1
        assert hits[0].playbook_id == "pb-1"

    @patch("rca_agent.services.playbook_gen.S3_VECTOR_BUCKET_NAME", "my-bucket")
    def test_handles_search_failure(self, fake_embedding):
        mock_client = MagicMock()
        mock_client.query_vectors.side_effect = RuntimeError("fail")
        hits = search_existing_playbooks(
            _make_report(),
            None,
            embedding=fake_embedding,
            s3_vectors_client=mock_client,
            max_retries=1,
            base_delay=0,
        )
        assert hits == []


class TestTryUpdateExisting:
    def test_returns_updated_playbook(self, fake_embedding):
        hit = _make_hit()
        update_output = PlaybookUpdateOutput(
            needs_update=True,
            failure_type="Memory leak (updated)",
            symptom_pattern="CPU spike + memory growth + OOM",
            severity_criteria="Critical if OOM kills exceed 3/min",
            verification_steps=["Check memory", "Check OOM kills"],
            escalation_criteria="Escalate to infra if not resolved in 5 min",
            related_metrics=["CPUUtilization", "MemoryUtilization", "OOMKillCount"],
            tags=["memory", "oom"],
        )
        agent = _make_mock_agent(update_output)

        result = _try_update_existing(hit, _make_report(), agent)

        assert result is not None
        assert result.playbook_id == "existing-1"
        assert result.failure_type == "Memory leak (updated)"
        assert len(result.verification_steps) == 2
        assert result.severity_criteria == "Critical if OOM kills exceed 3/min"
        assert result.escalation_criteria == "Escalate to infra if not resolved in 5 min"
        assert len(result.related_metrics) == 3

    def test_returns_none_when_no_update_needed(self, fake_embedding):
        hit = _make_hit()
        update_output = PlaybookUpdateOutput(needs_update=False)
        agent = _make_mock_agent(update_output)

        result = _try_update_existing(hit, _make_report(), agent)

        assert result is None

    def test_returns_none_on_failure(self, fake_embedding):
        hit = _make_hit()
        agent = MagicMock(side_effect=RuntimeError("fail"))

        result = _try_update_existing(hit, _make_report(), agent)

        assert result is None


class TestRunPlaybookGeneration:
    def test_updates_existing_when_found(self, fake_embedding):
        hit = _make_hit()
        update_output = PlaybookUpdateOutput(
            needs_update=True,
            failure_type="Memory leak (updated)",
            symptom_pattern="Updated pattern",
            verification_steps=["Step 1"],
        )
        agent = _make_mock_agent(update_output)

        with patch(
            "rca_agent.services.playbook_gen.search_existing_playbooks",
            return_value=[hit],
        ):
            playbook = run_playbook_generation(_make_report(), agent, embedding=fake_embedding)

        assert playbook.playbook_id == "existing-1"
        assert playbook.failure_type == "Memory leak (updated)"

    def test_creates_new_when_no_existing(self, fake_embedding):
        new_output = PlaybookOutput(
            failure_type="Memory leak",
            symptom_pattern="CPU spike",
            severity_criteria="High if sustained over 5 min",
            escalation_criteria="Escalate after 10 min",
            related_metrics=["CPUUtilization"],
            tags=["memory"],
        )
        agent = _make_mock_agent(new_output)

        with patch(
            "rca_agent.services.playbook_gen.search_existing_playbooks",
            return_value=[],
        ):
            playbook = run_playbook_generation(_make_report(), agent, embedding=fake_embedding)

        assert playbook.failure_type == "Memory leak"
        assert playbook.severity_criteria == "High if sustained over 5 min"
        assert playbook.escalation_criteria == "Escalate after 10 min"
        assert playbook.related_metrics == ["CPUUtilization"]
        assert playbook.rca_id == "rca-1"

    def test_creates_new_when_existing_needs_no_update(self, fake_embedding):
        hit = _make_hit()
        no_update = PlaybookUpdateOutput(needs_update=False)
        new_output = PlaybookOutput(
            failure_type="New playbook",
            symptom_pattern="New pattern",
        )

        call_count = 0

        def mock_call(prompt, structured_output_model=None):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if structured_output_model is PlaybookUpdateOutput:
                mock_result.structured_output = no_update
            else:
                mock_result.structured_output = new_output
            return mock_result

        agent = MagicMock(side_effect=mock_call)

        with patch(
            "rca_agent.services.playbook_gen.search_existing_playbooks",
            return_value=[hit],
        ):
            playbook = run_playbook_generation(_make_report(), agent, embedding=fake_embedding)

        assert playbook.failure_type == "New playbook"

    def test_fallback_on_failure(self, fake_embedding):
        agent = MagicMock(side_effect=RuntimeError("fail"))

        with patch(
            "rca_agent.services.playbook_gen.search_existing_playbooks",
            return_value=[],
        ):
            playbook = run_playbook_generation(_make_report(), agent, embedding=fake_embedding)

        assert playbook.failure_type == "unknown"
        assert playbook.rca_id == "rca-1"

    def test_uses_structured_output(self, fake_embedding):
        output = PlaybookOutput(failure_type="test", symptom_pattern="test")
        agent = _make_mock_agent(output)

        with patch(
            "rca_agent.services.playbook_gen.search_existing_playbooks",
            return_value=[],
        ):
            run_playbook_generation(_make_report(), agent, embedding=fake_embedding)

        _, kwargs = agent.call_args
        assert kwargs["structured_output_model"] is PlaybookOutput


class TestSavePlaybookToS3Vectors:
    def test_skips_when_not_configured(self, fake_embedding):
        playbook = Playbook(playbook_id="p-1", failure_type="t", symptom_pattern="t")
        assert not save_playbook_to_s3_vectors(playbook, embedding=fake_embedding)

    @patch("rca_agent.services.playbook_gen.S3_VECTOR_BUCKET_NAME", "my-bucket")
    def test_indexes_with_embed_key(self, fake_embedding):
        playbook = Playbook(
            playbook_id="p-1",
            failure_type="Memory leak",
            symptom_pattern="CPU spike",
            rca_id="rca-1",
            tags=["memory"],
        )
        scoping = _make_scoping()
        mock_client = MagicMock()

        result = save_playbook_to_s3_vectors(
            playbook,
            embedding=fake_embedding,
            scoping_result=scoping,
            s3_vectors_client=mock_client,
        )

        assert result is True
        call_args = mock_client.put_vectors.call_args
        vector = call_args[1]["vectors"][0]
        assert vector["data"]["float32"] == [0.1] * 1024
        assert vector["metadata"]["failure_type"] == "Memory leak"
        assert vector["metadata"]["tags"] == "memory"
        assert "verification_steps" not in vector["metadata"]

    @patch("rca_agent.services.playbook_gen.S3_VECTOR_BUCKET_NAME", "my-bucket")
    def test_handles_error(self, fake_embedding):
        playbook = Playbook(playbook_id="p-1", failure_type="t", symptom_pattern="t")
        mock_client = MagicMock()
        mock_client.put_vectors.side_effect = RuntimeError("fail")

        result = save_playbook_to_s3_vectors(playbook, embedding=fake_embedding, s3_vectors_client=mock_client)

        assert result is False
