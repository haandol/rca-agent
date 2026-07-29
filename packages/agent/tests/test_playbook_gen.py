import time
from time import perf_counter
from unittest.mock import MagicMock, patch

import pytest

from rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store import (
    S3VectorsPlaybookStore,
)
from rca_agent.config.settings import PLAYBOOK_UPDATE_THRESHOLD
from rca_agent.ports.dto.models import (
    AlarmPayload,
    AlarmTrigger,
    Playbook,
    PlaybookMatch,
    RcaReport,
    ScopingResult,
)
from rca_agent.services.playbook_gen import (
    PlaybookOutput,
    PlaybookUpdateOutput,
    _build_embed_key,
    _try_update_existing,
    run_playbook_generation,
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


def _make_hit(**overrides) -> PlaybookMatch:
    defaults = {
        "playbook_id": "existing-1",
        "similarity": 0.9,
        "failure_type": "Memory leak",
        "symptom_pattern": "CPU spike + memory growth",
        "tags": ["memory"],
    }
    defaults.update(overrides)
    return PlaybookMatch(**defaults)


def _playbook_store(matches: list[PlaybookMatch] | None = None) -> MagicMock:
    store = MagicMock()
    store.search_similar.return_value = matches or []
    return store


class TestBuildEmbedKey:
    def test_includes_all_parts(self):
        report = _make_report()
        scoping = _make_scoping()
        key = _build_embed_key(report, scoping)
        assert "Memory leak in worker" in key
        assert "CPUUtilization" in key
        assert "CPU spike on web-service" in key

    def test_without_scoping(self):
        report = _make_report()
        key = _build_embed_key(report, None)
        assert "Memory leak in worker" in key
        assert "CPU spike on web-service" in key
        assert "CPUUtilization" not in key


class TestSearchExistingPlaybooks:
    def test_queries_store_with_embed_key_and_update_threshold(self):
        store = _playbook_store()
        report = _make_report()
        scoping = _make_scoping()

        assert search_existing_playbooks(report, scoping, playbook_store=store) == []

        store.search_similar.assert_called_once_with(
            _build_embed_key(report, scoping),
            threshold=PLAYBOOK_UPDATE_THRESHOLD,
        )

    def test_returns_store_hits(self):
        hit = _make_hit(playbook_id="pb-1", similarity=0.9)
        store = _playbook_store([hit])

        hits = search_existing_playbooks(_make_report(), _make_scoping(), playbook_store=store)

        assert [h.playbook_id for h in hits] == ["pb-1"]
        assert hits[0].similarity == pytest.approx(0.9)


class TestTryUpdateExisting:
    def test_returns_updated_playbook(self):
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

    def test_returns_none_when_no_update_needed(self):
        hit = _make_hit()
        update_output = PlaybookUpdateOutput(needs_update=False)
        agent = _make_mock_agent(update_output)

        result = _try_update_existing(hit, _make_report(), agent)

        assert result is None

    def test_returns_none_on_failure(self):
        hit = _make_hit()
        agent = MagicMock(side_effect=RuntimeError("fail"))

        result = _try_update_existing(hit, _make_report(), agent)

        assert result is None

    def test_timeout_interrupts_update_without_late_mutation(self):
        state = []

        def slow_agent(*args, **kwargs):  # noqa: ARG001
            state.append("started")
            time.sleep(0.25)
            state.append("finished")

        started = perf_counter()
        result = _try_update_existing(
            _make_hit(),
            _make_report(),
            MagicMock(side_effect=slow_agent),
            timeout_seconds=0.04,
        )
        elapsed = perf_counter() - started

        assert elapsed < 0.2
        assert result is None
        time.sleep(0.1)
        assert state == ["started"]


class TestRunPlaybookGeneration:
    def test_updates_existing_when_found(self):
        hit = _make_hit()
        update_output = PlaybookUpdateOutput(
            needs_update=True,
            failure_type="Memory leak (updated)",
            symptom_pattern="Updated pattern",
            verification_steps=["Step 1"],
        )
        agent = _make_mock_agent(update_output)

        store = _playbook_store([hit])

        playbook = run_playbook_generation(_make_report(), agent, playbook_store=store)

        assert playbook.playbook_id == "existing-1"
        assert playbook.failure_type == "Memory leak (updated)"

    def test_creates_new_when_no_existing(self):
        new_output = PlaybookOutput(
            failure_type="Memory leak",
            symptom_pattern="CPU spike",
            severity_criteria="High if sustained over 5 min",
            escalation_criteria="Escalate after 10 min",
            related_metrics=["CPUUtilization"],
            tags=["memory"],
        )
        agent = _make_mock_agent(new_output)

        store = _playbook_store()

        playbook = run_playbook_generation(_make_report(), agent, playbook_store=store)

        assert playbook.failure_type == "Memory leak"
        assert playbook.severity_criteria == "High if sustained over 5 min"
        assert playbook.escalation_criteria == "Escalate after 10 min"
        assert playbook.related_metrics == ["CPUUtilization"]
        assert playbook.rca_id == "rca-1"

    def test_creates_new_when_existing_needs_no_update(self):
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

        store = _playbook_store([hit])

        playbook = run_playbook_generation(_make_report(), agent, playbook_store=store)

        assert playbook.failure_type == "New playbook"

    def test_fallback_on_failure(self):
        agent = MagicMock(side_effect=RuntimeError("fail"))

        store = _playbook_store()

        playbook = run_playbook_generation(_make_report(), agent, playbook_store=store)

        assert playbook.failure_type == "unknown"
        assert playbook.rca_id == "rca-1"

    def test_zero_timeout_returns_fallback_without_starting_generation(self):
        agent = MagicMock()

        store = _playbook_store()

        started = perf_counter()
        playbook = run_playbook_generation(
            _make_report(),
            agent,
            playbook_store=store,
            timeout_seconds=0,
        )
        elapsed = perf_counter() - started

        assert elapsed < 0.15
        agent.assert_not_called()
        assert playbook.failure_type == "unknown"
        assert playbook.rca_id == "rca-1"

    def test_uses_one_deadline_for_updates_and_generation(self):
        hits = [_make_hit(playbook_id=f"existing-{index}") for index in range(3)]
        agent = MagicMock(side_effect=lambda *args, **kwargs: time.sleep(0.2))

        store = _playbook_store(hits)

        started = perf_counter()
        playbook = run_playbook_generation(
            _make_report(),
            agent,
            playbook_store=store,
            timeout_seconds=0.05,
        )
        elapsed = perf_counter() - started

        assert elapsed < 0.2
        assert agent.call_count == 1
        assert playbook.failure_type == "unknown"

    def test_uses_structured_output(self):
        output = PlaybookOutput(failure_type="test", symptom_pattern="test")
        agent = _make_mock_agent(output)

        store = _playbook_store()

        run_playbook_generation(_make_report(), agent, playbook_store=store)

        _, kwargs = agent.call_args
        assert kwargs["structured_output_model"] is PlaybookOutput


_STORE_MODULE = "rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store"


class TestPlaybookStoreSave:
    def _store(self, client, fake_embedding) -> S3VectorsPlaybookStore:
        return S3VectorsPlaybookStore(s3_vectors_client=client, embedding=fake_embedding)

    def test_skips_when_not_configured(self, fake_embedding):
        playbook = Playbook(playbook_id="p-1", failure_type="t", symptom_pattern="t")

        with patch(f"{_STORE_MODULE}.S3_VECTOR_BUCKET_NAME", ""):
            assert not self._store(MagicMock(), fake_embedding).save(playbook)

    @patch(f"{_STORE_MODULE}.S3_VECTOR_BUCKET_NAME", "my-bucket")
    def test_indexes_with_embed_key(self, fake_embedding):
        playbook = Playbook(
            playbook_id="p-1",
            failure_type="Memory leak",
            symptom_pattern="CPU spike",
            rca_id="rca-1",
            tags=["memory"],
            verification_steps=["Check memory"],
        )
        mock_client = MagicMock()

        result = self._store(mock_client, fake_embedding).save(playbook, scoping_result=_make_scoping())

        assert result is True
        vector = mock_client.put_vectors.call_args.kwargs["vectors"][0]
        assert vector["key"] == "p-1"
        assert vector["data"]["float32"] == [0.1] * 1024
        assert vector["metadata"]["failure_type"] == "Memory leak"
        assert vector["metadata"]["tags"] == "memory"
        assert "verification_steps" not in vector["metadata"]

    @patch(f"{_STORE_MODULE}.S3_VECTOR_BUCKET_NAME", "my-bucket")
    def test_handles_error(self, fake_embedding):
        playbook = Playbook(playbook_id="p-1", failure_type="t", symptom_pattern="t")
        mock_client = MagicMock()
        mock_client.put_vectors.side_effect = RuntimeError("fail")

        assert not self._store(mock_client, fake_embedding).save(playbook)

    @patch(f"{_STORE_MODULE}.S3_VECTOR_BUCKET_NAME", "my-bucket")
    def test_round_trips_csv_tags_through_search(self, fake_embedding):
        mock_client = MagicMock()
        mock_client.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "p-1",
                    "distance": 0.05,
                    "metadata": {
                        "failure_type": "Memory leak",
                        "symptom_pattern": "CPU spike",
                        "tags": "memory,oom",
                    },
                }
            ]
        }

        matches = self._store(mock_client, fake_embedding).search_similar("query", threshold=0.9)

        assert [m.tags for m in matches] == [["memory", "oom"]]
