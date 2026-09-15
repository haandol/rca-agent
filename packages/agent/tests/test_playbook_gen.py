import json
import time
from time import perf_counter
from unittest.mock import MagicMock

import pytest

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


def _make_appraisal(**overrides) -> PlaybookUpdateOutput:
    fields = {
        "applicable": True,
        "needs_update": False,
        "rationale": "CPU 증가와 메모리 증가가 이 플레이북의 장애 패턴과 일치한다.",
        "evidence": ["high CPU", "memory growth"],
    }
    fields.update(overrides)
    return PlaybookUpdateOutput(**fields)


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
    def test_proposes_knowledge_changes_without_applying_them(self):
        existing = _make_existing()
        update_output = _make_appraisal(
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
        assert result.failure_type == existing.failure_type
        assert result.verification_steps == existing.verification_steps
        assert result.severity_criteria == existing.severity_criteria
        assert result.escalation_criteria == existing.escalation_criteria
        assert result.related_metrics == existing.related_metrics
        proposal = result.comparison["proposal"]
        assert proposal["state"] == "PENDING"
        assert proposal["after"]["failure_type"] == "Memory leak (updated)"
        assert len(proposal["after"]["verification_steps"]) == 2
        assert proposal["after"]["severity_criteria"] == "Critical if OOM kills exceed 3/min"
        assert proposal["after"]["escalation_criteria"] == "Escalate to infra if not resolved in 5 min"
        assert len(proposal["after"]["related_metrics"]) == 3

    def test_presents_existing_detail_to_the_agent(self):
        existing = _make_existing(
            temporary_mitigation="Restart the worker pool",
            verification_steps=["Check memory", "Check OOM kills"],
        )
        agent = _make_mock_agent(_make_appraisal(needs_update=False))

        _try_update_existing(existing, _make_report(), agent)

        prompt = agent.call_args[0][0]
        assert "Restart the worker pool" in prompt
        assert "Check OOM kills" in prompt
        assert "not in search index" not in prompt

    def test_keeps_existing_fields_the_agent_left_empty(self):
        existing = _make_existing()
        update_output = _make_appraisal(
            needs_update=True,
            failure_type="Memory leak (updated)",
        )
        agent = _make_mock_agent(update_output)

        result = _try_update_existing(existing, _make_report(), agent)

        assert result is not None
        assert result.failure_type == existing.failure_type
        assert result.comparison["proposal"]["after"]["failure_type"] == "Memory leak (updated)"
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

    def test_preserves_existing_knowledge_when_no_update_needed(self):
        update_output = _make_appraisal(needs_update=False)
        agent = _make_mock_agent(update_output)

        result = _try_update_existing(_make_existing(), _make_report(), agent)

        assert result is not None
        assert result.playbook_id == "existing-1"
        assert result.verification_steps == _make_existing().verification_steps
        assert result.rca_id == "rca-1"

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
    def test_keeps_existing_knowledge_when_an_update_is_proposed(self):
        hit = _make_hit()
        update_output = _make_appraisal(
            needs_update=True,
            failure_type="Memory leak (updated)",
            symptom_pattern="Updated pattern",
            verification_steps=["Step 1"],
        )
        agent = _make_mock_agent(update_output)

        store = _playbook_store([hit])

        playbook = run_playbook_generation(_make_report(), agent, playbook_store=store)

        assert playbook.playbook_id == "existing-1"
        assert playbook.failure_type == _make_existing().failure_type
        assert playbook.comparison["proposal"]["after"]["failure_type"] == "Memory leak (updated)"

    def test_merges_recorded_detail_instead_of_overwriting_it(self):
        existing = _make_existing(temporary_mitigation="Restart the worker pool")
        update_output = _make_appraisal(
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

    def test_reuses_existing_when_existing_needs_no_update(self):
        hit = _make_hit()
        no_update = _make_appraisal(needs_update=False)
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

        assert playbook.playbook_id == hit.playbook_id
        assert playbook.failure_type == _make_existing().failure_type

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


# Storage fixtures exercise real completed source records and canonical transactions.
from tests.test_playbook_library import completed, hit, publish, source  # noqa: E402
from tests.test_playbook_library import storage as storage  # noqa: E402

_STORE_MODULE = "rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store"


class TestPlaybookStoreSave:
    def test_indexes_with_embed_key(self, storage):
        _, vectors, _ = storage
        value, store = completed(storage)
        assert publish(store, value, scoping_result=_make_scoping())
        vector = vectors.put_vectors.call_args.kwargs["vectors"][0]
        assert vector["key"] == "pb-1@analysis:rca-1"
        assert vector["data"]["float32"] == [0.1, 0.2]
        assert vector["metadata"]["failure_type"] == value["failure_type"]
        assert "verification_steps" not in vector["metadata"]

    def test_round_trips_tags_and_source_identity(self, storage):
        ddb, vectors, store = storage
        value, _ = completed(storage)
        value["tags"] = ["memory", "oom"]
        source(ddb, value)
        assert publish(store, value)
        head = store._library.head("pb-1")
        vectors.query_vectors.return_value = {"vectors": [hit(head, 0.05)]}
        matches = store.search_similar("query", threshold=0.9)
        assert matches[0].tags == ["memory", "oom"]
        assert matches[0].rca_id == "rca-1"
        assert matches[0].engine == "strands"


class TestPlaybookStoreLoadDetail:
    def test_loads_legacy_completed_original_with_source_annotations(self, storage):
        value, store = completed(storage)
        detail = store.load_detail(PlaybookMatch(playbook_id="pb-1", rca_id="rca-1", similarity=0.9))
        assert detail is not None
        assert detail.library_revision == "legacy"
        assert detail.source_engine == "strands"
        assert detail.source_rca_id == "rca-1"
        assert detail.failure_type == value["failure_type"]

    def test_returns_none_for_missing_or_incomplete_source(self, storage):
        ddb, _, store = storage
        match = PlaybookMatch(playbook_id="pb-1", rca_id="rca-1", similarity=0.9)
        assert store.load_detail(match) is None
        value, store = completed(storage)
        source(ddb, value, state="REPORT_GENERATION")
        assert store.load_detail(match) is None


class TestLoadDetailPrefersTheRetrospectiveRevision:
    def test_revision_wins_and_malformed_latest_never_falls_back(self, storage):
        from rca_agent.adapters.secondary.playbook.library import _pack

        ddb, _, _ = storage
        value, store = completed(storage)
        revised = dict(value, temporary_mitigation="corrected", verification_status="VERIFIED")
        item = {
            "PK": "RCA#rca-1",
            "SK": "strands#PLAYBOOK_REVISION",
            "playbook_id": "pb-1",
            "playbook": json.dumps(revised),
            "publication_status": "PUBLISHED",
            "revised_by_execution_id": "exec-9",
        }
        ddb.put_item(TableName="sessions", Item=_pack(item))
        match = PlaybookMatch(playbook_id="pb-1", rca_id="rca-1", similarity=0.9, publication_id="exec-9")
        detail = store.load_detail(match)
        assert detail.temporary_mitigation == "corrected"
        assert detail.verification_status is PlaybookVerificationStatus.VERIFIED
        item["playbook"] = "{broken"
        ddb.put_item(TableName="sessions", Item=_pack(item))
        assert store.load_detail(match) is None


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
            "commands": [
                "aws ecs update-service --cluster current --service web-service "
                "--force-new-deployment --region us-east-1"
            ],
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

        assert steps == []

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

        assert steps == []

    def test_a_missing_step_id_rejects_the_plan(self):
        steps = build_execution_steps(
            [ExecutionStepOutput(**self._step(step_id=""))],
            confirmed=True,
        )

        assert steps == []

    def test_a_generated_playbook_is_always_an_unverified_draft(self):
        """실행되지 않은 절차는 검증되지 않았다. 분석은 이 값을 바꾸지 않는다."""
        agent = _make_mock_agent(self._output(execution_steps=[ExecutionStepOutput(**self._step())]))
        store = MagicMock()
        store.search_similar.return_value = []

        playbook = run_playbook_generation(_make_report(), agent, playbook_store=store)

        assert playbook.verification_status is PlaybookVerificationStatus.DRAFT
        assert [step.step_id for step in playbook.execution_steps] == ["step-1"]

    def test_an_update_that_omits_steps_removes_historical_execution(self):
        existing = Playbook(
            playbook_id="p-1",
            failure_type="Memory leak",
            symptom_pattern="메모리 증가",
            execution_steps=[ExecutionStep(**self._step())],
        )
        agent = _make_mock_agent(_make_appraisal(needs_update=True, temporary_mitigation="재배포 후 확인"))

        updated = _try_update_existing(existing, _make_report(), agent)

        assert updated is not None
        assert updated.execution_steps == []

    def test_an_unconfirmed_update_never_restores_recorded_steps(self):
        existing = Playbook(
            playbook_id="p-1",
            failure_type="Memory leak",
            symptom_pattern="메모리 증가",
            execution_steps=[ExecutionStep(**self._step())],
            verification_status=PlaybookVerificationStatus.VERIFIED,
        )
        report = _make_report()
        report.root_cause_confirmed = False
        agent = _make_mock_agent(_make_appraisal(needs_update=True))

        updated = _try_update_existing(existing, report, agent)

        assert updated is not None
        assert updated.execution_steps == []
        assert updated.verification_status is PlaybookVerificationStatus.DRAFT

    def test_the_recorded_steps_survive_a_round_trip_through_storage(self, storage):
        ddb, _, _ = storage
        value, store = completed(storage)
        value["execution_steps"] = [ExecutionStep(**self._step()).model_dump(mode="json")]
        source(ddb, value)
        assert publish(store, value)
        detail = store.load_detail(
            PlaybookMatch(playbook_id="pb-1", rca_id="rca-1", similarity=0.9, library_revision="analysis:rca-1")
        )
        assert [step.step_id for step in detail.execution_steps] == ["step-1"]
        assert detail.execution_steps[0].success_criteria == "MemoryUtilization 이 60% 이하로 복귀"

    def test_an_empty_current_plan_demotes_a_verified_historical_playbook(self):
        existing = Playbook(
            playbook_id="p-1",
            failure_type="Memory leak",
            symptom_pattern="메모리 증가",
            execution_steps=[ExecutionStep(**self._step())],
            verification_status=PlaybookVerificationStatus.VERIFIED,
        )
        agent = _make_mock_agent(_make_appraisal(needs_update=True, temporary_mitigation="재배포 후 확인"))

        updated = _try_update_existing(existing, _make_report(), agent)

        assert updated is not None
        assert updated.verification_status is PlaybookVerificationStatus.DRAFT

    def test_an_update_with_changed_steps_demotes_a_verified_playbook(self):
        existing = Playbook(
            playbook_id="p-1",
            failure_type="Memory leak",
            symptom_pattern="메모리 증가",
            execution_steps=[ExecutionStep(**self._step())],
            verification_status=PlaybookVerificationStatus.VERIFIED,
        )
        changed_step = ExecutionStepOutput(**self._step(action="web-service 를 롤링 재시작한다"))
        agent = _make_mock_agent(_make_appraisal(needs_update=True, execution_steps=[changed_step]))

        current_steps = build_execution_steps([changed_step], confirmed=True)
        updated = _try_update_existing(existing, _make_report(), agent, current_steps=current_steps)

        assert updated is not None
        assert updated.execution_steps[0].action == "web-service 를 롤링 재시작한다"
        assert updated.execution_steps[0] is not current_steps[0]
        assert updated.verification_status is PlaybookVerificationStatus.DRAFT

    def test_an_update_with_identical_steps_preserves_verified_status(self):
        existing = Playbook(
            playbook_id="p-1",
            failure_type="Memory leak",
            symptom_pattern="메모리 증가",
            execution_steps=[ExecutionStep(**self._step())],
            verification_status=PlaybookVerificationStatus.VERIFIED,
        )
        same_step = ExecutionStepOutput(**self._step())
        agent = _make_mock_agent(_make_appraisal(needs_update=True, execution_steps=[same_step]))

        updated = _try_update_existing(
            existing, _make_report(), agent, current_steps=build_execution_steps([same_step], confirmed=True)
        )

        assert updated is not None
        assert updated.execution_steps == existing.execution_steps
        assert updated.verification_status is PlaybookVerificationStatus.VERIFIED

    def test_an_update_to_a_draft_playbook_leaves_it_a_draft(self):
        """분석은 이 값을 올릴 수 없다. 승격은 실행 뒤 회고만 수행한다."""
        existing = Playbook(playbook_id="p-1", failure_type="Memory leak", symptom_pattern="메모리 증가")
        agent = _make_mock_agent(_make_appraisal(needs_update=True, temporary_mitigation="재배포 후 확인"))

        updated = _try_update_existing(existing, _make_report(), agent)

        assert updated is not None
        assert updated.verification_status is PlaybookVerificationStatus.DRAFT

    def test_a_verified_status_survives_a_round_trip_through_storage(self, storage):
        ddb, _, _ = storage
        value, store = completed(storage)
        value["verification_status"] = "VERIFIED"
        source(ddb, value)
        assert publish(store, value)
        detail = store.load_detail(
            PlaybookMatch(playbook_id="pb-1", rca_id="rca-1", similarity=0.9, library_revision="analysis:rca-1")
        )
        assert detail.verification_status is PlaybookVerificationStatus.VERIFIED

    def test_an_unreadable_recorded_status_is_unavailable(self, storage):
        ddb, _, _ = storage
        value, store = completed(storage)
        value["verification_status"] = "SOMETHING"
        source(ddb, value)
        assert store.load_detail(PlaybookMatch(playbook_id="pb-1", rca_id="rca-1", similarity=0.9)) is None

    def test_a_recorded_step_without_an_identifier_is_unavailable(self, storage):
        ddb, _, _ = storage
        value, store = completed(storage)
        value["execution_steps"] = [{"action": "unidentified"}]
        source(ddb, value)
        assert store.load_detail(PlaybookMatch(playbook_id="pb-1", rca_id="rca-1", similarity=0.9)) is None


def test_generation_and_enrichment_receive_current_alarm_metrics_and_late_evidence():
    from rca_agent.services.playbook_gen import _build_update_prompt, _build_user_prompt

    report = _make_report()
    owner = "arn:aws:ecs:us-east-1:123456789012:task/current/observed-owner"
    report.evidence_list = ["source log: " + "observed details " * 80 + owner]
    scoping = _make_scoping()
    scoping.raw_alarm.region = "us-east-1"
    scoping.raw_alarm.trigger.dimensions = {"ServiceName": "current-service"}
    old = Playbook(playbook_id="old", failure_type="lock", symptom_pattern="blocked writes")
    for prompt in (_build_user_prompt(report, scoping), _build_update_prompt(old, report, scoping)):
        assert owner in prompt
        assert '"ServiceName": "current-service"' in prompt
        assert '"namespace": "AWS/ECS"' in prompt
        assert '"region": "us-east-1"' in prompt


@pytest.mark.parametrize("current_plan", ["empty", "invalid", "new-owner", "same-owner"])
@pytest.mark.parametrize("merge_plan", ["stale-owner", "empty"])
@pytest.mark.parametrize("needs_update", [True, False])
def test_generation_merge_uses_only_validated_current_plan(current_plan, merge_plan, needs_update):
    from tests.test_runbook_contract import command_step, wait_step

    old_steps = [command_step(), wait_step()]
    existing = _make_existing(
        execution_steps=[ExecutionStep(**step) for step in old_steps],
        verification_status=PlaybookVerificationStatus.VERIFIED,
    )
    snapshot = existing.model_dump()
    current = [ExecutionStepOutput(**step) for step in [command_step(), wait_step()]]
    if current_plan == "empty":
        current = []
    elif current_plan == "invalid":
        current[1].metric_wait["action_step_id"] = "missing"
    elif current_plan == "new-owner":
        current[0].commands = [current[0].commands[0].replace("incident-owner", "current-owner")]
    draft_output = PlaybookOutput(failure_type="lock", symptom_pattern="blocked writes", execution_steps=current)
    merge_output = _make_appraisal(
        needs_update=needs_update,
        temporary_mitigation="Enriched knowledge",
        execution_steps=[ExecutionStepOutput(**step) for step in old_steps] if merge_plan == "stale-owner" else [],
    )
    agent = MagicMock(
        side_effect=[MagicMock(structured_output=draft_output), MagicMock(structured_output=merge_output)]
    )
    store = _playbook_store([_make_hit()], detail=existing)

    result = run_playbook_generation(_make_report(), agent, playbook_store=store)

    assert [call.kwargs["structured_output_model"] for call in agent.call_args_list] == [
        PlaybookOutput,
        PlaybookUpdateOutput,
    ]
    store.load_detail.assert_called_once()
    assert result.playbook_id == existing.playbook_id
    assert result.temporary_mitigation == existing.temporary_mitigation
    if needs_update:
        assert result.comparison["proposal"]["after"]["temporary_mitigation"] == "Enriched knowledge"
    if not needs_update:
        incidental = {"execution_steps", "verification_status", "rca_id", "comparison", "source_rca_id"}
        assert result.model_dump(exclude=incidental) == existing.model_dump(exclude=incidental)
    expected = [] if current_plan in {"empty", "invalid"} else [ExecutionStep(**step.model_dump()) for step in current]
    assert result.execution_steps == expected
    expected_status = (
        PlaybookVerificationStatus.VERIFIED if current_plan == "same-owner" else PlaybookVerificationStatus.DRAFT
    )
    assert result.verification_status is expected_status
    if result.execution_steps:
        result.execution_steps[1].metric_wait["metrics"]["failures"]["dimensions"]["Service"] = "mutated"
        assert current[1].metric_wait["metrics"]["failures"]["dimensions"]["Service"] == "current-service"
    assert existing.model_dump() == snapshot


@pytest.mark.parametrize("failure", ["search", "detail", "update"])
def test_existing_lookup_failure_keeps_new_validated_draft(failure):
    from tests.test_runbook_contract import command_step, wait_step

    steps = [ExecutionStepOutput(**step) for step in [command_step(), wait_step()]]
    draft = PlaybookOutput(failure_type="current type", symptom_pattern="current pattern", execution_steps=steps)
    agent = MagicMock(side_effect=[MagicMock(structured_output=draft), RuntimeError("update unavailable")])
    store = _playbook_store([_make_hit()])
    if failure == "search":
        store.search_similar.side_effect = RuntimeError("search unavailable")
    elif failure == "detail":
        store.load_detail.side_effect = RuntimeError("detail unavailable")

    result = run_playbook_generation(_make_report(), agent, playbook_store=store)

    assert result.playbook_id != "existing-1"
    assert result.failure_type == "current type"
    assert result.execution_steps == [ExecutionStep(**step.model_dump()) for step in steps]
    assert result.verification_status is PlaybookVerificationStatus.DRAFT


@pytest.mark.parametrize("source_time", ["2025-04-03T01:02:03Z", None, ""])
def test_eval_generation_and_merge_prompts_use_only_source_incident_time(source_time):
    from datetime import UTC, datetime

    from rca_agent.services.playbook_gen import _build_update_prompt, _build_user_prompt

    scoping = _make_scoping()
    scoping.raw_alarm.state_change_time = datetime(2026, 9, 15, 12, 34, 56, tzinfo=UTC)
    scoping.raw_alarm.eval_source_metadata = {"stateChangeTime": source_time} if source_time is not None else {}
    before = scoping.model_dump()
    for prompt in (
        _build_user_prompt(_make_report(), scoping),
        _build_update_prompt(_make_existing(), _make_report(), scoping),
    ):
        assert "2026-09-15T12:34:56" not in prompt
        if source_time:
            assert source_time in prompt
        else:
            assert f'"state_change_time": {json.dumps(source_time)}' in prompt
        assert '"namespace": "AWS/ECS"' in prompt
    assert scoping.model_dump() == before


def test_production_generation_prompt_preserves_real_alarm_time():
    from datetime import UTC, datetime

    from rca_agent.services.playbook_gen import _build_user_prompt

    scoping = _make_scoping()
    scoping.raw_alarm.state_change_time = datetime(2025, 4, 3, 1, 2, 3, tzinfo=UTC)
    assert "2025-04-03T01:02:03Z" in _build_user_prompt(_make_report(), scoping)
