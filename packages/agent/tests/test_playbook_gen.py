import json
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
    ExecutionStep,
    Playbook,
    PlaybookMatch,
    PlaybookVerificationStatus,
    RcaReport,
    ScopingResult,
)
from rca_agent.services.playbook_gen import (
    ExecutionStepOutput,
    PlaybookOutput,
    PlaybookUpdateOutput,
    _build_embed_key,
    _try_update_existing,
    build_execution_steps,
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
        "rca_id": "rca-0",
    }
    defaults.update(overrides)
    return PlaybookMatch(**defaults)


def _make_existing(**overrides) -> Playbook:
    defaults = {
        "playbook_id": "existing-1",
        "failure_type": "Memory leak",
        "symptom_pattern": "CPU spike + memory growth",
        "severity_criteria": "Critical if OOM kills detected",
        "verification_steps": ["Check memory"],
        "temporary_mitigation": "Restart",
        "permanent_remediation": "Fix code",
        "escalation_criteria": "Escalate if not resolved in 10 min",
        "prevention_measures": ["Add alerts"],
        "related_metrics": ["CPUUtilization", "MemoryUtilization"],
        "rca_id": "rca-0",
        "tags": ["memory"],
    }
    defaults.update(overrides)
    return Playbook(**defaults)


def _playbook_store(
    matches: list[PlaybookMatch] | None = None,
    *,
    detail: Playbook | None = None,
) -> MagicMock:
    """Store double whose search hits resolve to ``detail`` (or a full playbook)."""
    store = MagicMock()
    store.search_similar.return_value = matches or []
    store.load_detail.side_effect = lambda match: (
        detail if detail is not None else _make_existing(playbook_id=match.playbook_id, rca_id=match.rca_id)
    )
    return store


class TestBuildEmbedKey:
    """검색 텍스트는 인덱스가 저장하는 것과 같은 필드에서 나와야 한다.

    플레이북의 유형·패턴은 재사용을 위해 일반화된 서술이고, 보고서의 근본 원인·요약은
    이번 사건의 리소스와 시각을 담은 개별 서술이다. 후자로 검색하면 같은 장애도 자신의
    저장 항목과 낮은 유사도가 나오고, 그 실패가 빈 검색 결과와 구별되지 않는다.
    """

    def test_includes_all_parts(self):
        draft = _make_existing(playbook_id="pb-draft")
        key = _build_embed_key(draft, _make_scoping())
        assert draft.failure_type in key
        assert draft.symptom_pattern in key
        assert "CPUUtilization" in key

    def test_without_scoping(self):
        draft = _make_existing(playbook_id="pb-draft")
        key = _build_embed_key(draft, None)
        assert draft.failure_type in key
        assert "CPUUtilization" not in key

    def test_uses_the_playbook_fields_not_the_report_narrative(self):
        draft = _make_existing(playbook_id="pb-draft")
        report = _make_report()
        key = _build_embed_key(draft, None)

        # 저장은 플레이북 필드를 임베딩하므로 검색도 같은 필드를 써야 대칭이 된다.
        assert draft.failure_type in key
        assert report.root_cause not in key
        assert report.incident_summary not in key


class TestSearchExistingPlaybooks:
    def test_queries_store_with_embed_key_and_update_threshold(self):
        store = _playbook_store()
        draft = _make_existing(playbook_id="pb-draft")
        scoping = _make_scoping()

        assert search_existing_playbooks(draft, scoping, playbook_store=store) == []

        store.search_similar.assert_called_once_with(
            _build_embed_key(draft, scoping),
            threshold=PLAYBOOK_UPDATE_THRESHOLD,
        )

    def test_returns_store_hits(self):
        hit = _make_hit(playbook_id="pb-1", similarity=0.9)
        store = _playbook_store([hit])

        hits = search_existing_playbooks(_make_existing(playbook_id="pb-draft"), _make_scoping(), playbook_store=store)

        assert [h.playbook_id for h in hits] == ["pb-1"]
        assert hits[0].similarity == pytest.approx(0.9)

    def test_threshold_admits_a_recurrence_whose_wording_differs(self):
        # 실측: 같은 유형·패턴을 다른 문장으로 쓴 재발이 0.83, 글자까지 같으면 0.96.
        # 임계값이 그 사이에 있으면 같은 장애의 재발조차 병합되지 않는다.
        assert PLAYBOOK_UPDATE_THRESHOLD <= 0.83


class TestTryUpdateExisting:
    def test_returns_updated_playbook(self):
        existing = _make_existing()
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

        result = _try_update_existing(existing, _make_report(), agent)

        assert result is not None
        assert result.playbook_id == "existing-1"
        assert result.failure_type == "Memory leak (updated)"
        assert len(result.verification_steps) == 2
        assert result.severity_criteria == "Critical if OOM kills exceed 3/min"
        assert result.escalation_criteria == "Escalate to infra if not resolved in 5 min"
        assert len(result.related_metrics) == 3

    def test_presents_existing_detail_to_the_agent(self):
        existing = _make_existing(
            temporary_mitigation="Restart the worker pool",
            verification_steps=["Check memory", "Check OOM kills"],
        )
        agent = _make_mock_agent(PlaybookUpdateOutput(needs_update=False))

        _try_update_existing(existing, _make_report(), agent)

        prompt = agent.call_args[0][0]
        assert "Restart the worker pool" in prompt
        assert "Check OOM kills" in prompt
        assert "not in search index" not in prompt

    def test_keeps_existing_fields_the_agent_left_empty(self):
        existing = _make_existing()
        update_output = PlaybookUpdateOutput(
            needs_update=True,
            failure_type="Memory leak (updated)",
        )
        agent = _make_mock_agent(update_output)

        result = _try_update_existing(existing, _make_report(), agent)

        assert result is not None
        assert result.failure_type == "Memory leak (updated)"
        assert result.symptom_pattern == existing.symptom_pattern
        assert result.severity_criteria == existing.severity_criteria
        assert result.verification_steps == existing.verification_steps
        assert result.temporary_mitigation == existing.temporary_mitigation
        assert result.permanent_remediation == existing.permanent_remediation
        assert result.escalation_criteria == existing.escalation_criteria
        assert result.prevention_measures == existing.prevention_measures
        assert result.related_metrics == existing.related_metrics
        assert result.tags == existing.tags
        assert result.rca_id == "rca-1"

    def test_returns_none_when_no_update_needed(self):
        update_output = PlaybookUpdateOutput(needs_update=False)
        agent = _make_mock_agent(update_output)

        result = _try_update_existing(_make_existing(), _make_report(), agent)

        assert result is None

    def test_returns_none_on_failure(self):
        agent = MagicMock(side_effect=RuntimeError("fail"))

        result = _try_update_existing(_make_existing(), _make_report(), agent)

        assert result is None

    def test_timeout_interrupts_update_without_late_mutation(self):
        state = []

        def slow_agent(*args, **kwargs):  # noqa: ARG001
            state.append("started")
            time.sleep(0.25)
            state.append("finished")

        started = perf_counter()
        result = _try_update_existing(
            _make_existing(),
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

    def test_merges_recorded_detail_instead_of_overwriting_it(self):
        existing = _make_existing(temporary_mitigation="Restart the worker pool")
        update_output = PlaybookUpdateOutput(
            needs_update=True,
            failure_type="Memory leak (updated)",
        )
        agent = _make_mock_agent(update_output)

        store = _playbook_store([_make_hit()], detail=existing)

        playbook = run_playbook_generation(_make_report(), agent, playbook_store=store)

        store.load_detail.assert_called_once()
        assert playbook.playbook_id == "existing-1"
        assert playbook.temporary_mitigation == "Restart the worker pool"
        assert playbook.verification_steps == existing.verification_steps

    def test_creates_new_when_detail_is_unavailable(self):
        new_output = PlaybookOutput(failure_type="New playbook", symptom_pattern="New pattern")
        agent = _make_mock_agent(new_output)

        store = _playbook_store([_make_hit()])
        store.load_detail.side_effect = None
        store.load_detail.return_value = None

        playbook = run_playbook_generation(_make_report(), agent, playbook_store=store)

        assert playbook.playbook_id != "existing-1"
        assert playbook.failure_type == "New playbook"
        _, kwargs = agent.call_args
        assert kwargs["structured_output_model"] is PlaybookOutput

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

    @patch(f"{_STORE_MODULE}.S3_VECTOR_BUCKET_NAME", "my-bucket")
    def test_search_carries_rca_id_for_detail_lookup(self, fake_embedding):
        mock_client = MagicMock()
        mock_client.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "p-1",
                    "distance": 0.05,
                    "metadata": {"failure_type": "Memory leak", "rca_id": "rca-7"},
                }
            ]
        }

        matches = self._store(mock_client, fake_embedding).search_similar("query", threshold=0.9)

        assert [m.rca_id for m in matches] == ["rca-7"]


_TRACE_MODULE = "rca_agent.adapters.secondary.trace.dynamodb_trace_store"


class TestPlaybookStoreLoadDetail:
    def _store(self, dynamodb_client) -> S3VectorsPlaybookStore:
        return S3VectorsPlaybookStore(dynamodb_client=dynamodb_client)

    @patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_loads_recorded_detail_from_the_playbook_span(self):
        ddb = MagicMock()
        ddb.query.return_value = {
            "Items": [
                {
                    "SK": {"S": "strands#SPAN#s-1"},
                    "span_type": {"S": "PLAYBOOK"},
                    "metadata": {
                        "M": {
                            "playbook_id": {"S": "p-1"},
                            "failure_type": {"S": "Memory leak"},
                            "temporary_mitigation": {"S": "Restart the worker pool"},
                            "verification_steps": {"L": [{"S": "Check memory"}]},
                            "tags": {"L": [{"S": "memory"}]},
                        }
                    },
                }
            ]
        }

        detail = self._store(ddb).load_detail(_make_hit(playbook_id="p-1", rca_id="rca-7"))

        assert detail is not None
        assert detail.playbook_id == "p-1"
        assert detail.temporary_mitigation == "Restart the worker pool"
        assert detail.verification_steps == ["Check memory"]
        assert detail.rca_id == "rca-7"
        assert ddb.query.call_args.kwargs["ExpressionAttributeValues"][":pk"] == {"S": "RCA#rca-7"}

    @patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_returns_none_when_the_record_is_gone(self):
        ddb = MagicMock()
        ddb.query.return_value = {"Items": []}

        assert self._store(ddb).load_detail(_make_hit(rca_id="rca-7")) is None

    @patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_returns_none_for_a_different_playbook_in_the_same_rca(self):
        ddb = MagicMock()
        ddb.query.return_value = {
            "Items": [
                {
                    "SK": {"S": "strands#SPAN#s-1"},
                    "span_type": {"S": "PLAYBOOK"},
                    "metadata": {"M": {"playbook_id": {"S": "other"}}},
                }
            ]
        }

        assert self._store(ddb).load_detail(_make_hit(playbook_id="p-1", rca_id="rca-7")) is None

    def test_returns_none_without_a_dynamodb_client(self):
        assert self._store(None).load_detail(_make_hit(rca_id="rca-7")) is None

    @patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_returns_none_when_the_hit_has_no_rca_id(self):
        ddb = MagicMock()

        assert self._store(ddb).load_detail(_make_hit(rca_id="")) is None
        ddb.query.assert_not_called()


class TestLoadDetailPrefersTheRetrospectiveRevision:
    """보강 대상은 회고 개정본이다.

    분석 원본을 보강 대상으로 삼으면 병합이 모든 필드를 보존해도 결과가 교정 이전으로
    퇴행한다. 검색과 병합이 정상 동작하므로 실패로 드러나지 않아, 이 우선순위를
    테스트가 고정한다.
    """

    def _store(self, dynamodb_client) -> S3VectorsPlaybookStore:
        return S3VectorsPlaybookStore(dynamodb_client=dynamodb_client)

    def _span_item(self) -> dict:
        return {
            "SK": {"S": "strands#SPAN#s-1"},
            "span_type": {"S": "PLAYBOOK"},
            "metadata": {
                "M": {
                    "playbook_id": {"S": "p-1"},
                    "failure_type": {"S": "DB connection leak"},
                    "temporary_mitigation": {"S": "원본 조치"},
                    "verification_status": {"S": "DRAFT"},
                    "execution_steps": {
                        "L": [
                            {
                                "M": {
                                    "step_id": {"S": "step-1"},
                                    "intent": {"S": "커넥션 회수"},
                                    "action": {"S": "잘못된 인자로 서비스를 갱신한다"},
                                    "success_criteria": {"S": "커넥션 수 감소"},
                                }
                            }
                        ]
                    },
                }
            },
        }

    def _revision_item(self) -> dict:
        revised = {
            "playbook_id": "p-1",
            "failure_type": "DB connection leak",
            "temporary_mitigation": "교정된 조치",
            "verification_status": "VERIFIED",
            "execution_steps": [
                {
                    "step_id": "step-1",
                    "intent": "커넥션 회수",
                    "action": "교정된 인자로 서비스를 갱신한다",
                    "success_criteria": "커넥션 수 감소",
                },
                {
                    "step_id": "step-2",
                    "intent": "해소 확인",
                    "action": "커넥션 메트릭을 조회한다",
                    "success_criteria": "임계치 미만 유지",
                },
            ],
        }
        return {
            "SK": {"S": "cc-headless#PLAYBOOK_REVISION"},
            "playbook_id": {"S": "p-1"},
            "playbook": {"S": json.dumps(revised, ensure_ascii=False)},
            "revised_by_execution_id": {"S": "exec-9"},
        }

    @patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_revision_wins_over_the_analysis_span(self):
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [self._span_item(), self._revision_item()]}

        detail = self._store(ddb).load_detail(_make_hit(playbook_id="p-1", rca_id="rca-7"))

        assert detail is not None
        assert detail.temporary_mitigation == "교정된 조치"
        # 회고가 붙인 절차와 교정한 인자가 보강의 출발점이어야 한다.
        assert [step.step_id for step in detail.execution_steps] == ["step-1", "step-2"]
        assert detail.execution_steps[0].action == "교정된 인자로 서비스를 갱신한다"

    @patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_promotion_survives_the_reload(self):
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [self._span_item(), self._revision_item()]}

        detail = self._store(ddb).load_detail(_make_hit(playbook_id="p-1", rca_id="rca-7"))

        assert detail is not None
        assert detail.verification_status is PlaybookVerificationStatus.VERIFIED

    @patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_falls_back_to_the_span_without_a_revision(self):
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [self._span_item()]}

        detail = self._store(ddb).load_detail(_make_hit(playbook_id="p-1", rca_id="rca-7"))

        assert detail is not None
        assert detail.temporary_mitigation == "원본 조치"

    @patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_unreadable_revision_falls_back_rather_than_dropping_the_playbook(self):
        ddb = MagicMock()
        broken = self._revision_item()
        broken["playbook"] = {"S": "{not json"}
        ddb.query.return_value = {"Items": [self._span_item(), broken]}

        detail = self._store(ddb).load_detail(_make_hit(playbook_id="p-1", rca_id="rca-7"))

        # 개정본을 읽지 못한 것이 병합 포기 사유가 되면, 아직 병합 가능한 플레이북이
        # 버려지고 같은 유형이 새 식별자로 다시 생성된다.
        assert detail is not None
        assert detail.temporary_mitigation == "원본 조치"

    @patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table")
    def test_ignores_a_revision_of_a_different_playbook(self):
        ddb = MagicMock()
        other = self._revision_item()
        other["playbook_id"] = {"S": "p-other"}
        ddb.query.return_value = {"Items": [self._span_item(), other]}

        detail = self._store(ddb).load_detail(_make_hit(playbook_id="p-1", rca_id="rca-7"))

        assert detail is not None
        assert detail.temporary_mitigation == "원본 조치"


class TestExecutionStepContract:
    """플레이북이 실행 근거가 되므로 절차의 형태를 코드가 지킨다."""

    def _output(self, **overrides) -> PlaybookOutput:
        return PlaybookOutput(
            failure_type="Memory leak",
            symptom_pattern="메모리가 단조 증가한다",
            **overrides,
        )

    def _step(self, **overrides) -> dict:
        step = {
            "step_id": "step-1",
            "intent": "워커 풀 회수",
            "action": "web-service 를 강제 재배포한다",
            "success_criteria": "MemoryUtilization 이 60% 이하로 복귀",
        }
        step.update(overrides)
        return step

    def test_an_unconfirmed_root_cause_gets_no_execution_steps(self):
        """추측 절차가 승인 버튼 뒤에 놓이면 사람이 검증된 절차로 오인한다."""
        steps = build_execution_steps(
            [ExecutionStepOutput(**self._step())],
            confirmed=False,
        )

        assert steps == []

    def test_a_confirmed_root_cause_keeps_its_steps_in_order(self):
        steps = build_execution_steps(
            [
                ExecutionStepOutput(**self._step()),
                ExecutionStepOutput(**self._step(step_id="step-2", intent="증상 확인")),
            ],
            confirmed=True,
        )

        assert [step.step_id for step in steps] == ["step-1", "step-2"]
        assert steps[0].action == "web-service 를 강제 재배포한다"

    def test_a_step_without_an_observable_criterion_is_dropped(self):
        """관측 기준이 없으면 실행 에이전트가 해결을 확정할 수 없다."""
        steps = build_execution_steps(
            [
                ExecutionStepOutput(**self._step(success_criteria="  ")),
                ExecutionStepOutput(**self._step(step_id="step-2")),
            ],
            confirmed=True,
        )

        assert [step.step_id for step in steps] == ["step-2"]

    def test_a_step_without_an_action_is_dropped(self):
        steps = build_execution_steps(
            [ExecutionStepOutput(**self._step(action=""))],
            confirmed=True,
        )

        assert steps == []

    def test_a_duplicate_step_id_is_dropped(self):
        """식별자가 겹치면 증거가 어느 절차를 가리키는지 알 수 없다."""
        steps = build_execution_steps(
            [
                ExecutionStepOutput(**self._step()),
                ExecutionStepOutput(**self._step(intent="다른 의도")),
            ],
            confirmed=True,
        )

        assert len(steps) == 1
        assert steps[0].intent == "워커 풀 회수"

    def test_a_missing_step_id_gets_a_positional_one(self):
        steps = build_execution_steps(
            [ExecutionStepOutput(**self._step(step_id=""))],
            confirmed=True,
        )

        assert [step.step_id for step in steps] == ["step-1"]

    def test_a_generated_playbook_is_always_an_unverified_draft(self):
        """실행되지 않은 절차는 검증되지 않았다. 분석은 이 값을 바꾸지 않는다."""
        agent = _make_mock_agent(self._output(execution_steps=[ExecutionStepOutput(**self._step())]))
        store = MagicMock()
        store.search_similar.return_value = []

        playbook = run_playbook_generation(_make_report(), agent, playbook_store=store)

        assert playbook.verification_status is PlaybookVerificationStatus.DRAFT
        assert [step.step_id for step in playbook.execution_steps] == ["step-1"]

    def test_an_update_that_omits_steps_keeps_the_recorded_ones(self):
        existing = Playbook(
            playbook_id="p-1",
            failure_type="Memory leak",
            symptom_pattern="메모리 증가",
            execution_steps=[ExecutionStep(**self._step())],
        )
        agent = _make_mock_agent(PlaybookUpdateOutput(needs_update=True, temporary_mitigation="재배포 후 확인"))

        updated = _try_update_existing(existing, _make_report(), agent)

        assert updated is not None
        assert [step.step_id for step in updated.execution_steps] == ["step-1"]
        assert updated.execution_steps[0].action == "web-service 를 강제 재배포한다"

    def test_the_recorded_steps_survive_a_round_trip_through_the_trace(self):
        ddb = MagicMock()
        ddb.query.return_value = {
            "Items": [
                {
                    "SK": {"S": "strands#SPAN#s-1"},
                    "span_type": {"S": "PLAYBOOK"},
                    "metadata": {
                        "M": {
                            "playbook_id": {"S": "p-1"},
                            "execution_steps": {"L": [{"M": {k: {"S": v} for k, v in self._step().items()}}]},
                        }
                    },
                }
            ]
        }

        with patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table"):
            detail = S3VectorsPlaybookStore(dynamodb_client=ddb).load_detail(
                _make_hit(playbook_id="p-1", rca_id="rca-7")
            )

        assert detail is not None
        assert [step.step_id for step in detail.execution_steps] == ["step-1"]
        assert detail.execution_steps[0].success_criteria == "MemoryUtilization 이 60% 이하로 복귀"

    def test_an_update_does_not_demote_a_verified_playbook(self):
        """보강 한 번이 회고의 승격을 취소하면 검증됨이 아무것도 뜻하지 않는다."""
        existing = Playbook(
            playbook_id="p-1",
            failure_type="Memory leak",
            symptom_pattern="메모리 증가",
            execution_steps=[ExecutionStep(**self._step())],
            verification_status=PlaybookVerificationStatus.VERIFIED,
        )
        agent = _make_mock_agent(PlaybookUpdateOutput(needs_update=True, temporary_mitigation="재배포 후 확인"))

        updated = _try_update_existing(existing, _make_report(), agent)

        assert updated is not None
        assert updated.verification_status is PlaybookVerificationStatus.VERIFIED

    def test_an_update_to_a_draft_playbook_leaves_it_a_draft(self):
        """분석은 이 값을 올릴 수 없다. 승격은 실행 뒤 회고만 수행한다."""
        existing = Playbook(playbook_id="p-1", failure_type="Memory leak", symptom_pattern="메모리 증가")
        agent = _make_mock_agent(PlaybookUpdateOutput(needs_update=True, temporary_mitigation="재배포 후 확인"))

        updated = _try_update_existing(existing, _make_report(), agent)

        assert updated is not None
        assert updated.verification_status is PlaybookVerificationStatus.DRAFT

    def test_a_verified_status_survives_a_round_trip_through_the_trace(self):
        ddb = MagicMock()
        ddb.query.return_value = {
            "Items": [
                {
                    "SK": {"S": "strands#SPAN#s-1"},
                    "span_type": {"S": "PLAYBOOK"},
                    "metadata": {
                        "M": {
                            "playbook_id": {"S": "p-1"},
                            "verification_status": {"S": "VERIFIED"},
                        }
                    },
                }
            ]
        }

        with patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table"):
            detail = S3VectorsPlaybookStore(dynamodb_client=ddb).load_detail(
                _make_hit(playbook_id="p-1", rca_id="rca-7")
            )

        assert detail is not None
        assert detail.verification_status is PlaybookVerificationStatus.VERIFIED

    def test_an_unreadable_recorded_status_loads_as_a_draft(self):
        """읽을 수 없는 값이 검증됨으로 되살아나면 미입증 절차가 검증된 것으로 보인다."""
        for recorded in ({"S": "SOMETHING"}, {"N": "1"}):
            ddb = MagicMock()
            ddb.query.return_value = {
                "Items": [
                    {
                        "SK": {"S": "strands#SPAN#s-1"},
                        "span_type": {"S": "PLAYBOOK"},
                        "metadata": {
                            "M": {
                                "playbook_id": {"S": "p-1"},
                                "verification_status": recorded,
                            }
                        },
                    }
                ]
            }

            with patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table"):
                detail = S3VectorsPlaybookStore(dynamodb_client=ddb).load_detail(
                    _make_hit(playbook_id="p-1", rca_id="rca-7")
                )

            assert detail is not None
            assert detail.verification_status is PlaybookVerificationStatus.DRAFT

    def test_a_recorded_step_without_an_identifier_is_dropped_on_load(self):
        ddb = MagicMock()
        ddb.query.return_value = {
            "Items": [
                {
                    "SK": {"S": "strands#SPAN#s-1"},
                    "span_type": {"S": "PLAYBOOK"},
                    "metadata": {
                        "M": {
                            "playbook_id": {"S": "p-1"},
                            "execution_steps": {"L": [{"M": {"action": {"S": "무언가"}}}]},
                        }
                    },
                }
            ]
        }

        with patch(f"{_TRACE_MODULE}.DYNAMODB_TABLE_NAME", "rca-table"):
            detail = S3VectorsPlaybookStore(dynamodb_client=ddb).load_detail(
                _make_hit(playbook_id="p-1", rca_id="rca-7")
            )

        assert detail is not None
        assert detail.execution_steps == []
