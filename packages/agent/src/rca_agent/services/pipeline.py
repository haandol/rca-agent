from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from threading import Event

from rca_agent.adapters.secondary.session.dynamodb_session_store import (
    InvalidStateTransitionError,
    SessionCancelledError,
    build_idempotency_key,
    build_rca_id,
)
from rca_agent.adapters.secondary.trace.dynamodb_trace_store import SpanStatus, SpanType, TraceStore
from rca_agent.config import settings
from rca_agent.config.aws_sdk import SIDE_EFFECT_LEASE_SECONDS
from rca_agent.config.settings import (
    ALARM_STALENESS_SECONDS,
    RCA_BEAM_WIDTH,
    RCA_MAX_REGENERATION_ROUNDS,
    REJECTION_THRESHOLD,
)
from rca_agent.ports.dto.models import (
    AlarmPayload,
    FaultType,
    Hypothesis,
    HypothesisStatus,
    Playbook,
    RcaSessionState,
    TerminationDecision,
    TerminationReason,
    ValidationJudgment,
    ValidationResult,
)
from rca_agent.ports.interfaces.session_store import (
    ClaimDisposition,
    SideEffectLeaseUnavailableError,
)
from rca_agent.services.branching import run_branching
from rca_agent.services.evidence import run_evidence_collection
from rca_agent.services.hypothesis import run_hypothesis_generation
from rca_agent.services.notification import build_notification
from rca_agent.services.playbook_gen import run_playbook_generation
from rca_agent.services.prioritization import run_prioritization
from rca_agent.services.report import run_report_generation
from rca_agent.services.review_gate import ReviewGateResult, run_review_gate
from rca_agent.services.scoping import run_scoping
from rca_agent.services.termination import check_termination
from rca_agent.services.validation import run_validation

logger = logging.getLogger(__name__)

_SIDE_EFFECT_LEASE_SECONDS = SIDE_EFFECT_LEASE_SECONDS


class _LoopAction(Enum):
    CONTINUE = auto()
    BREAK = auto()
    PROCEED = auto()


_CLOSE_REASON_MAP: dict[TerminationReason, str] = {
    TerminationReason.CONFIRMED: "확정된 근본원인 발견으로 기각",
    TerminationReason.TIME_BUDGET: "시간 예산 소진",
    TerminationReason.TOKEN_BUDGET: "토큰 예산 소진",
    TerminationReason.MAX_DEPTH: "최대 트리 깊이 초과",
    TerminationReason.MAX_LOOPS: "최대 검증 루프 초과",
    TerminationReason.ALL_REJECTED: "전체 가설 기각",
}


def parse_sns_envelope(body: dict) -> dict:
    if "Message" in body and isinstance(body["Message"], str):
        return json.loads(body["Message"])
    return body


def should_process(alarm_data: dict) -> bool:
    if not alarm_data.get("AlarmName"):
        return False
    return alarm_data.get("NewStateValue", "ALARM") == "ALARM"


def select_beam(hypotheses, prioritization_result, beam_width):
    rank_map = {p.hypothesis_id: p.priority_rank for p in prioritization_result.prioritized}
    candidates = [
        h
        for h in hypotheses
        if h.status
        in (
            HypothesisStatus.PENDING,
            HypothesisStatus.NEEDS_INVESTIGATION,
        )
    ]
    candidates.sort(key=lambda h: rank_map.get(h.hypothesis_id, 9999))
    return candidates[:beam_width]


def prune_subtree(rejected_id: str, hypotheses: list) -> list[str]:
    pruned: list[str] = []
    queue = [rejected_id]
    while queue:
        parent_id = queue.pop()
        for h in hypotheses:
            if h.parent_id == parent_id and h.status != HypothesisStatus.REJECTED:
                h.status = HypothesisStatus.REJECTED
                pruned.append(h.hypothesis_id)
                queue.append(h.hypothesis_id)
    return pruned


@dataclass(frozen=True)
class RunContext:
    """What every stage of one RCA run needs to identify and record itself.

    These five values are fixed for the whole run and were previously threaded
    through each stage as separate positional arguments. Passing them as one
    object is what makes the ownership rules hard to get wrong: a stage that
    writes without the claim token, or records against another run's trace, is a
    stage that has silently escaped the fencing — and as bare positionals, an
    ``rca_id`` and a ``claim_token`` are two strings that swap without complaint.
    """

    rca_id: str
    claim_token: str
    attempt: int
    trace: TraceStore
    start_time: float


@dataclass
class ValidationLoopState:
    hypotheses: list[Hypothesis]
    all_judgments: list[ValidationJudgment] = field(default_factory=list)
    rejected_descriptions: list[str] = field(default_factory=list)
    evidence_map: dict[str, str] = field(default_factory=dict)
    evidence_failed_ids: set[str] = field(default_factory=set)
    timeline: list[str] = field(default_factory=list)
    loop_count: int = 0
    regeneration_count: int = 0
    consecutive_blocked_loops: int = 0
    termination: TerminationDecision | None = None


class ShutdownRequestedError(Exception):
    pass


class PipelineOrchestrator:
    def __init__(
        self,
        container,
        shutdown_event: Event | None = None,
        *,
        precollected_evidence: str | None = None,
    ):
        self._container = container
        self._shutdown_event = shutdown_event or Event()
        # ``None`` keeps the operational discovery path. Any string, including
        # an empty one, means the caller owns the evidence boundary and live
        # evidence collection must not supplement or replace it.
        self._precollected_evidence = precollected_evidence

    def _check_shutdown(self) -> None:
        if self._shutdown_event.is_set():
            raise ShutdownRequestedError

    @contextmanager
    def _side_effect_lease(self, rca_id: str, claim_token: str, effect_name: str):
        store = self._container.session_store
        lease_token = store.acquire_side_effect_lease(
            rca_id,
            claim_token,
            effect_name,
            lease_seconds=_SIDE_EFFECT_LEASE_SECONDS,
        )
        try:
            yield
        finally:
            if not store.release_side_effect_lease(rca_id, claim_token, lease_token):
                raise SideEffectLeaseUnavailableError(f"{rca_id}: failed to release {effect_name} lease")

    def process_alarm(
        self,
        body: dict,
        *,
        receive_count: int = 1,
        message_id: str | None = None,
    ) -> bool:
        start_time = time.monotonic()
        alarm_data = parse_sns_envelope(body)

        if not should_process(alarm_data):
            logger.info(
                "Skipping non-alarm message: AlarmName=%s, NewStateValue=%s",
                alarm_data.get("AlarmName"),
                alarm_data.get("NewStateValue"),
            )
            return True

        alarm = AlarmPayload.from_cloudwatch_sns(alarm_data)
        logger.info(
            "Parsed alarm: name=%s, resource=%s, service=%s",
            alarm.alarm_name,
            alarm.resource_id,
            alarm.service_name,
        )

        store = self._container.session_store
        rca_id = build_rca_id(build_idempotency_key(alarm))
        effective_receive_count = max(receive_count, 1)
        effective_message_id = message_id or f"direct:{rca_id}"
        try:
            claim = store.claim_session(
                alarm,
                receive_count=effective_receive_count,
                message_id=effective_message_id,
                alarm_data=alarm_data,
            )
        except Exception:
            logger.exception("Failed to claim RCA session for alarm %s", alarm.alarm_name)
            return False
        if claim.disposition is ClaimDisposition.TERMINAL_DUPLICATE:
            logger.info("Acknowledging terminal duplicate alarm: %s", alarm.alarm_name)
            try:
                return self._flush_completion_handoff(rca_id, claim_token=claim.claim_token)
            except Exception:
                logger.exception("Failed to flush terminal RCA handoff: rca_id=%s", rca_id)
                return False
        if not claim.acquired or not claim.claim_token:
            logger.info(
                "RCA session claim contended: alarm=%s receive_count=%d",
                alarm.alarm_name,
                effective_receive_count,
            )
            return False

        claim_token = claim.claim_token
        attempt = claim.attempt or effective_receive_count
        if effective_receive_count == 1 and self._skip_if_stale(
            alarm,
            store,
            rca_id=rca_id,
            claim_token=claim_token,
        ):
            return True

        trace = TraceStore(
            rca_id,
            claim_token=claim_token,
            attempt=attempt,
            dynamodb_client=self._container.dynamodb_client,
        )
        run = RunContext(
            rca_id=rca_id,
            claim_token=claim_token,
            attempt=attempt,
            trace=trace,
            start_time=start_time,
        )

        try:
            return self._run_pipeline(alarm, run)
        except ShutdownRequestedError:
            logger.info(
                "Pipeline aborted by SIGTERM for alarm %s (rca_id=%s)",
                alarm.alarm_name,
                rca_id,
            )
            try:
                store.mark_failed(
                    rca_id,
                    error_reason="Aborted due to SIGTERM shutdown",
                    claim_token=claim_token,
                )
            except Exception:
                logger.exception("Failed to record shutdown for RCA session %s", rca_id)
            return False
        except (SessionCancelledError, SideEffectLeaseUnavailableError):
            logger.info(
                "Pipeline claim lost or cancelled for alarm %s (rca_id=%s)",
                alarm.alarm_name,
                rca_id,
            )
            return False
        except InvalidStateTransitionError:
            logger.exception(
                "Invalid state transition for alarm %s (rca_id=%s)",
                alarm.alarm_name,
                rca_id,
            )
            return False
        except Exception:
            logger.exception("Pipeline failed for alarm %s", alarm.alarm_name)
            try:
                store.mark_failed(
                    rca_id,
                    error_reason="Unhandled pipeline exception",
                    claim_token=claim_token,
                )
            except Exception:
                logger.exception("Failed to record pipeline failure for RCA session %s", rca_id)
            return False

    def _flush_completion_handoff(self, rca_id: str, *, claim_token: str | None) -> bool:
        handoff = self._container.session_store.get_completion_handoff(rca_id)
        if handoff is None:
            logger.warning("Duplicate RCA %s has no persisted session handoff", rca_id)
            return False
        if handoff.state != RcaSessionState.COMPLETED:
            logger.info(
                "Terminal duplicate RCA %s requires no completion handoff: state=%s",
                rca_id,
                handoff.state,
            )
            return handoff.state in (RcaSessionState.OUTDATED, RcaSessionState.CANCELLED)
        if handoff.notification_status in ("", "SENT"):
            return True
        if handoff.notification_status != "PENDING" or handoff.notification is None:
            logger.error("RCA %s has an invalid pending completion handoff", rca_id)
            return False
        if not self._container.notification.send(handoff.notification):
            return False
        return self._container.session_store.mark_completion_notified(
            rca_id,
            claim_token=claim_token,
        )

    def _skip_if_stale(
        self,
        alarm: AlarmPayload,
        store,
        *,
        rca_id: str,
        claim_token: str,
    ) -> bool:
        if not alarm.state_change_time:
            return False
        age_seconds = (datetime.now(UTC) - alarm.state_change_time).total_seconds()
        if age_seconds <= ALARM_STALENESS_SECONDS:
            return False
        logger.info(
            "Skipping stale alarm: %s (age=%.0fs > %ds)",
            alarm.alarm_name,
            age_seconds,
            ALARM_STALENESS_SECONDS,
        )
        store.mark_outdated(
            rca_id,
            reason=(f"Alarm age {int(age_seconds)}s exceeds {ALARM_STALENESS_SECONDS}s threshold"),
            claim_token=claim_token,
        )
        return True

    def _run_pipeline(self, alarm, run: RunContext) -> bool:
        store = self._container.session_store

        self._check_shutdown()
        scoping_result = self._run_scoping(alarm, run)

        self._check_shutdown()
        hypotheses = self._run_hypothesis_generation(scoping_result, run)
        if not hypotheses:
            store.mark_failed(
                run.rca_id,
                error_reason="No hypotheses generated",
                claim_token=run.claim_token,
            )
            return False

        state = self._run_validation_loop(alarm, scoping_result, hypotheses, run)

        self._check_shutdown()

        best_hypothesis, confirmed = self._finalize_hypotheses(
            state.hypotheses,
            state.termination,
            state.all_judgments,
            trace=run.trace,
        )

        return self._run_report_and_notify(
            scoping_result,
            best_hypothesis,
            confirmed,
            run,
            alarm=alarm,
            hypothesis_path=[best_hypothesis.description] if best_hypothesis else [],
            evidence_texts=[e for e in state.evidence_map.values() if e],
            rejected_descriptions=state.rejected_descriptions,
            timeline=state.timeline,
        )

    def _run_scoping(self, alarm, run: RunContext):
        c = self._container
        trace = run.trace
        c.session_store.update_state(
            run.rca_id,
            RcaSessionState.SCOPING,
            claim_token=run.claim_token,
        )
        with trace.span(
            SpanType.SCOPING,
            input_summary=f"알람={alarm.alarm_name}, 리전={alarm.region}",
        ) as s:
            scoping_result = run_scoping(
                alarm,
                c.scoping_agent,
                report_store=c.report_store,
            )
            s.output_summary = (
                f"심각도={scoping_result.initial_severity},"
                f" 영향범위={scoping_result.blast_radius},"
                f" 유사 보고서={len(scoping_result.similar_reports)}건"
            )
            s.metadata = {
                "심각도": scoping_result.initial_severity,
                "영향범위": scoping_result.blast_radius,
                "유사_보고서": len(scoping_result.similar_reports),
            }
        logger.info(
            "Scoping: severity=%s, blast_radius=%s, reports=%d",
            scoping_result.initial_severity,
            scoping_result.blast_radius,
            len(scoping_result.similar_reports),
        )
        return scoping_result

    def _run_hypothesis_generation(self, scoping_result, run: RunContext):
        c = self._container
        trace = run.trace
        c.session_store.update_state(
            run.rca_id,
            RcaSessionState.HYPOTHESIS_GENERATION,
            claim_token=run.claim_token,
        )
        with trace.span(
            SpanType.HYPOTHESIS_GENERATION,
            input_summary=(f"심각도={scoping_result.initial_severity}, 영향범위={scoping_result.blast_radius}"),
        ) as s:
            hypothesis_result = run_hypothesis_generation(
                scoping_result,
                c.hypothesis_agent,
            )
            hypotheses = list(hypothesis_result.hypotheses)
            s.output_summary = f"가설 {len(hypotheses)}개 생성, tree_id={hypothesis_result.tree_id}"
            s.metadata = {
                "가설_수": len(hypotheses),
                "tree_id": hypothesis_result.tree_id,
            }
        if not hypotheses:
            logger.error("No hypotheses generated, aborting RCA")
            return []
        trace.put_hypotheses(hypotheses)
        return hypotheses

    def _run_validation_loop(
        self,
        alarm,
        scoping_result,
        hypotheses,
        run: RunContext,
    ) -> ValidationLoopState:
        trace = run.trace
        state = ValidationLoopState(hypotheses=hypotheses)
        self._seed_precollected_evidence(state, hypotheses)
        state.timeline.append(f"Alarm received: {alarm.alarm_name}")
        state.timeline.append(f"Scoping complete: severity={scoping_result.initial_severity}")
        state.timeline.append(f"Initial hypotheses: {len(hypotheses)}")

        while True:
            self._check_shutdown()
            state.loop_count += 1
            logger.info(
                "Validation loop %d, hypotheses=%d",
                state.loop_count,
                len(state.hypotheses),
            )
            loop_span = trace.start_span(
                SpanType.VALIDATION_LOOP,
                loop_index=state.loop_count,
                input_summary=f"가설={len(state.hypotheses)}개",
            )

            gate = self._apply_review_gate(state, run, loop_span)
            if gate.early_exit:
                break

            prioritization_result = self._loop_prioritization(
                state,
                scoping_result,
                run,
                loop_span,
            )
            active_hypotheses = select_beam(state.hypotheses, prioritization_result, RCA_BEAM_WIDTH)
            logger.info(
                "Beam selection: %d/%d hypotheses",
                len(active_hypotheses),
                len(state.hypotheses),
            )

            self._loop_evidence(
                state,
                scoping_result,
                active_hypotheses,
                run,
                loop_span,
            )
            validation_result = self._loop_validation(
                state,
                active_hypotheses,
                scoping_result,
                run,
                loop_span,
            )
            self._apply_judgments(state, trace)
            self._loop_termination_check(state, run, loop_span)

            if state.termination and state.termination.should_terminate:
                logger.info("Termination: %s", state.termination.reason)
                state.timeline.append(f"Terminated: {state.termination.reason}")
                trace.end_span(
                    loop_span,
                    output_summary=f"종료: {state.termination.reason}",
                    metadata={"루프_번호": state.loop_count},
                )
                break

            regen_action = self._maybe_regenerate(
                state,
                scoping_result,
                validation_result,
                gate,
                run,
                loop_span,
            )
            if regen_action == _LoopAction.CONTINUE:
                continue
            if regen_action == _LoopAction.BREAK:
                break

            if not self._loop_branching(state, gate, trace, loop_span):
                break

        return state

    def _apply_review_gate(
        self,
        state: ValidationLoopState,
        run: RunContext,
        loop_span,
    ) -> ReviewGateResult:
        trace = run.trace
        gate = run_review_gate(
            state.hypotheses,
            state.all_judgments,
            consecutive_blocked_loops=state.consecutive_blocked_loops,
        )
        if gate.auto_rejected_ids:
            for hid in gate.auto_rejected_ids:
                trace.update_hypothesis_status(
                    hid,
                    status=HypothesisStatus.REJECTED.value,
                    judgment_reasoning=("Review gate: 이미 채택된 가설과 동일 원인 영역으로 자동 기각"),
                )
        if gate.early_exit:
            accepted = next(
                (
                    h
                    for h in state.hypotheses
                    if h.hypothesis_id == gate.accepted_hypothesis_id and h.status == HypothesisStatus.CONFIRMED
                ),
                None,
            )
            if accepted is None:
                logger.error(
                    "Review gate refused early exit without a current accepted hypothesis: %s",
                    gate.reason,
                )
                return ReviewGateResult(
                    early_exit=False,
                    expansion_blocked=False,
                    reason="invalid_accepted_selection",
                    accepted_max_confidence=0.0,
                    accepted_hypothesis_id=None,
                    auto_rejected_ids=gate.auto_rejected_ids,
                )
            logger.info("Review gate early exit: %s", gate.reason)
            state.timeline.append(f"Loop {state.loop_count}: review gate early exit ({gate.reason})")
            state.termination = TerminationDecision(
                should_terminate=True,
                reason=TerminationReason.CONFIRMED,
                best_hypothesis=accepted,
            )
            trace.end_span(
                loop_span,
                output_summary=f"review gate early exit: {gate.reason}",
                metadata={"루프_번호": state.loop_count, "review_gate": gate.reason},
            )
            return gate
        if gate.expansion_blocked:
            state.consecutive_blocked_loops += 1
            logger.info(
                "Review gate expansion blocked (loop=%d, streak=%d)",
                state.loop_count,
                state.consecutive_blocked_loops,
            )
            state.timeline.append(f"Loop {state.loop_count}: expansion blocked ({gate.reason})")
        else:
            state.consecutive_blocked_loops = 0
        return gate

    def _loop_prioritization(
        self,
        state: ValidationLoopState,
        scoping_result,
        run: RunContext,
        loop_span,
    ):
        c = self._container
        trace = run.trace
        c.session_store.update_state(
            run.rca_id,
            RcaSessionState.HYPOTHESIS_PRIORITIZATION,
            claim_token=run.claim_token,
        )
        with trace.span(
            SpanType.PRIORITIZATION,
            parent_span_id=loop_span.span_id,
            input_summary=f"가설={len(state.hypotheses)}개",
        ) as s:
            prioritization_result = run_prioritization(
                scoping_result,
                state.hypotheses,
                c.prioritization_agent,
            )
            s.output_summary = f"가설 {len(state.hypotheses)}개 우선순위 결정"
        return prioritization_result

    def _loop_evidence(
        self,
        state: ValidationLoopState,
        scoping_result,
        active_hypotheses,
        run: RunContext,
        loop_span,
    ) -> None:
        c = self._container
        trace = run.trace
        self._seed_precollected_evidence(state, active_hypotheses)
        c.session_store.update_state(
            run.rca_id,
            RcaSessionState.EVIDENCE_COLLECTION,
            claim_token=run.claim_token,
        )
        new_hypotheses = [h for h in active_hypotheses if h.hypothesis_id not in state.evidence_map]
        with trace.span(
            SpanType.EVIDENCE_COLLECTION,
            parent_span_id=loop_span.span_id,
            input_summary=(f"beam={len(active_hypotheses)}개, 신규={len(new_hypotheses)}개"),
        ) as s:
            if new_hypotheses:
                ev_summary = run_evidence_collection(
                    new_hypotheses,
                    scoping_result,
                    mcp_clients=c.evidence_mcp_clients,
                    rca_id=run.rca_id,
                    trace=trace,
                    s3_client=c.s3_client,
                    existing_evidence_map=state.evidence_map,
                    all_hypotheses=state.hypotheses,
                    cancel_checker=self._check_shutdown,
                    save_lease=lambda effect_name: self._side_effect_lease(
                        run.rca_id,
                        run.claim_token,
                        effect_name,
                    ),
                )
                state.evidence_map.update(ev_summary.evidence_map)
                state.evidence_failed_ids.update(ev_summary.failed_ids)
            s.output_summary = f"가설 {len(new_hypotheses)}개에 대한 증거 수집 완료"
            s.metadata = {
                "신규_가설_수": len(new_hypotheses),
                "beam_width": RCA_BEAM_WIDTH,
            }
        state.timeline.append(
            f"Loop {state.loop_count}: evidence for {len(new_hypotheses)} hypotheses (beam={len(active_hypotheses)})"
        )

    def _seed_precollected_evidence(
        self,
        state: ValidationLoopState,
        hypotheses: list[Hypothesis],
    ) -> None:
        if self._precollected_evidence is None:
            return
        for hypothesis in hypotheses:
            state.evidence_map[hypothesis.hypothesis_id] = self._precollected_evidence

    def _loop_validation(
        self,
        state: ValidationLoopState,
        active_hypotheses,
        scoping_result,
        run: RunContext,
        loop_span,
    ) -> ValidationResult:
        c = self._container
        trace = run.trace
        c.session_store.update_state(
            run.rca_id,
            RcaSessionState.HYPOTHESIS_VALIDATION,
            claim_token=run.claim_token,
        )
        with trace.span(
            SpanType.VALIDATION,
            parent_span_id=loop_span.span_id,
            input_summary=(f"beam={len(active_hypotheses)}개, 증거={len(state.evidence_map)}건"),
        ) as s:
            validation_result = run_validation(
                active_hypotheses,
                state.evidence_map,
                c.validation_agent,
                evidence_failed_ids=state.evidence_failed_ids,
                scoping_result=scoping_result,
            )
            state.all_judgments = validation_result.judgments
            confirmed_count = sum(1 for j in state.all_judgments if j.status == HypothesisStatus.CONFIRMED)
            rejected_count = sum(1 for j in state.all_judgments if j.status == HypothesisStatus.REJECTED)
            s.output_summary = (
                f"판정={len(state.all_judgments)}건,"
                f" 확정={confirmed_count},"
                f" 기각={rejected_count},"
                f" 전체기각={validation_result.all_rejected}"
            )
            s.metadata = {
                "판정_수": len(state.all_judgments),
                "확정": confirmed_count,
                "기각": rejected_count,
                "전체기각": validation_result.all_rejected,
                "beam_width": RCA_BEAM_WIDTH,
            }
        state.timeline.append(
            f"Loop {state.loop_count}: validated {len(state.all_judgments)} hypotheses (beam={len(active_hypotheses)})"
        )
        return validation_result

    def _apply_judgments(self, state: ValidationLoopState, trace) -> None:
        for j in state.all_judgments:
            trace.update_hypothesis_status(
                j.hypothesis_id,
                status=j.status.value,
                confidence=j.confidence_score,
                judgment_reasoning=j.reasoning[:500],
                validated_fault_type=j.validated_fault_type.value,
                validation_evidence_summary="\n".join(j.evidence_summary),
            )
            h = next(
                (h for h in state.hypotheses if h.hypothesis_id == j.hypothesis_id),
                None,
            )
            if h:
                h.status = j.status
                h.confidence_score = j.confidence_score
                h.validated_fault_type = j.validated_fault_type
                h.judgment_reasoning = j.reasoning

        for j in state.all_judgments:
            if j.status != HypothesisStatus.REJECTED:
                continue
            h = next(
                (h for h in state.hypotheses if h.hypothesis_id == j.hypothesis_id),
                None,
            )
            if h and h.description not in state.rejected_descriptions:
                state.rejected_descriptions.append(h.description)
            pruned = prune_subtree(j.hypothesis_id, state.hypotheses)
            if pruned:
                logger.info(
                    "Pruned %d descendant hypotheses of %s",
                    len(pruned),
                    j.hypothesis_id,
                )
                for pid in pruned:
                    trace.update_hypothesis_status(
                        pid,
                        status=HypothesisStatus.REJECTED.value,
                    )

    def _loop_termination_check(
        self,
        state: ValidationLoopState,
        run: RunContext,
        loop_span,
    ) -> None:
        trace = run.trace
        with trace.span(
            SpanType.TERMINATION,
            parent_span_id=loop_span.span_id,
            input_summary=(f"루프={state.loop_count}, 판정={len(state.all_judgments)}건"),
        ) as s:
            state.termination = check_termination(
                judgments=state.all_judgments,
                hypotheses=state.hypotheses,
                start_time=run.start_time,
                validation_loop_count=state.loop_count,
            )
            s.output_summary = f"종료={state.termination.should_terminate}, 사유={state.termination.reason}"
            s.metadata = {"종료여부": state.termination.should_terminate}
            if state.termination.reason:
                s.metadata["사유"] = state.termination.reason.value

    def _maybe_regenerate(
        self,
        state: ValidationLoopState,
        scoping_result,
        validation_result: ValidationResult,
        gate: ReviewGateResult,
        run: RunContext,
        loop_span,
    ) -> _LoopAction:
        trace = run.trace
        if not validation_result.all_rejected:
            return _LoopAction.PROCEED

        if gate.expansion_blocked:
            logger.info(
                "All rejected but expansion blocked → relying on accepted hypothesis, skipping regeneration",
            )
            state.timeline.append(f"Loop {state.loop_count}: regeneration skipped (expansion blocked)")
            trace.end_span(
                loop_span,
                output_summary="expansion blocked, regeneration skipped",
                metadata={"루프_번호": state.loop_count, "review_gate": gate.reason},
            )
            return _LoopAction.CONTINUE

        state.regeneration_count += 1
        if state.regeneration_count > RCA_MAX_REGENERATION_ROUNDS:
            logger.warning("Max regeneration rounds exceeded")
            state.timeline.append("Max regeneration rounds exceeded")
            trace.end_span(
                loop_span,
                output_summary="최대 재생성 라운드 초과",
                status=SpanStatus.FAILED,
            )
            return _LoopAction.BREAK

        logger.info(
            "All rejected, regenerating hypotheses (round %d)",
            state.regeneration_count,
        )
        for h in state.hypotheses:
            if h.status in (HypothesisStatus.PENDING, HypothesisStatus.NEEDS_INVESTIGATION):
                h.status = HypothesisStatus.REJECTED
                trace.update_hypothesis_status(
                    h.hypothesis_id,
                    status=HypothesisStatus.REJECTED.value,
                    judgment_reasoning=("전체 기각으로 가설 재생성 — 이전 라운드 자동 기각"),
                )

        c = self._container
        c.session_store.update_state(
            run.rca_id,
            RcaSessionState.HYPOTHESIS_GENERATION,
            claim_token=run.claim_token,
        )
        with trace.span(
            SpanType.HYPOTHESIS_GENERATION,
            parent_span_id=loop_span.span_id,
            input_summary=f"재생성 라운드 {state.regeneration_count}",
        ) as s:
            hypothesis_result = run_hypothesis_generation(
                scoping_result,
                c.hypothesis_agent,
            )
            new_hypotheses = list(hypothesis_result.hypotheses)
            s.output_summary = f"가설 {len(new_hypotheses)}개 재생성"
            s.metadata = {
                "재생성_라운드": state.regeneration_count,
                "가설_수": len(new_hypotheses),
            }
        if not new_hypotheses:
            logger.error("Regeneration produced no hypotheses")
            trace.end_span(
                loop_span,
                output_summary="재생성 결과 가설 없음",
                status=SpanStatus.FAILED,
            )
            return _LoopAction.BREAK

        state.hypotheses = new_hypotheses
        trace.put_hypotheses(new_hypotheses)
        state.timeline.append(f"Regenerated hypotheses: {len(new_hypotheses)}")
        trace.end_span(
            loop_span,
            output_summary=f"가설 {len(new_hypotheses)}개 재생성",
            metadata={
                "루프_번호": state.loop_count,
                "재생성_라운드": state.regeneration_count,
            },
        )
        return _LoopAction.CONTINUE

    def _loop_branching(
        self,
        state: ValidationLoopState,
        gate: ReviewGateResult,
        trace,
        loop_span,
    ) -> bool:
        """Returns True to continue the loop, False to break."""
        c = self._container
        ni_count = sum(1 for j in state.all_judgments if j.status == HypothesisStatus.NEEDS_INVESTIGATION)

        if gate.expansion_blocked:
            logger.info(
                "Branching skipped: expansion blocked by review gate (ni=%d)",
                ni_count,
            )
            state.timeline.append(f"Loop {state.loop_count}: branching skipped (expansion blocked)")
            trace.end_span(
                loop_span,
                output_summary=f"expansion blocked, branching skipped (ni={ni_count})",
                metadata={
                    "루프_번호": state.loop_count,
                    "review_gate": gate.reason,
                    "ni_count": ni_count,
                },
            )
            return True

        new_children: list[Hypothesis] = []
        with trace.span(
            SpanType.BRANCHING,
            parent_span_id=loop_span.span_id,
            input_summary=f"추가조사필요={ni_count}건",
        ) as s:
            for j in state.all_judgments:
                if j.status != HypothesisStatus.NEEDS_INVESTIGATION:
                    continue
                parent = next(
                    (h for h in state.hypotheses if h.hypothesis_id == j.hypothesis_id),
                    None,
                )
                if parent is None:
                    continue
                evidence_text = state.evidence_map.get(parent.hypothesis_id, "")
                branching_result = run_branching(
                    parent,
                    evidence_text,
                    state.rejected_descriptions,
                    c.branching_agent,
                )
                new_children.extend(branching_result.children)
            s.output_summary = f"신규_하위가설={len(new_children)}개"
            s.metadata = {"신규_하위가설_수": len(new_children)}

        if not new_children:
            logger.info("No new child hypotheses, terminating")
            state.timeline.append("No new child hypotheses")
            trace.end_span(
                loop_span,
                output_summary="신규 하위가설 없음, 종료",
            )
            return False

        trace.put_hypotheses(new_children)
        state.hypotheses.extend(new_children)
        logger.info(
            "Added %d child hypotheses, total=%d",
            len(new_children),
            len(state.hypotheses),
        )
        trace.end_span(
            loop_span,
            output_summary=(f"하위가설 {len(new_children)}개 추가, 총 {len(state.hypotheses)}개"),
            metadata={
                "루프_번호": state.loop_count,
                "신규_하위가설": len(new_children),
            },
        )
        return True

    def _finalize_hypotheses(self, hypotheses, termination, all_judgments, *, trace):
        close_reason = (
            _CLOSE_REASON_MAP.get(termination.reason, "분석 종료")
            if termination and termination.reason
            else "분석 종료"
        )
        best_hid = termination.best_hypothesis.hypothesis_id if termination and termination.best_hypothesis else None
        terminated_by_confirmed = termination and termination.reason == TerminationReason.CONFIRMED
        judgment_scores = {j.hypothesis_id: j.confidence_score for j in all_judgments}
        for h in hypotheses:
            if h.status not in (
                HypothesisStatus.PENDING,
                HypothesisStatus.NEEDS_INVESTIGATION,
            ):
                continue
            if h.hypothesis_id == best_hid:
                continue
            score = judgment_scores.get(h.hypothesis_id)
            should_reject = terminated_by_confirmed or (score is not None and score <= REJECTION_THRESHOLD)
            new_status = HypothesisStatus.REJECTED if should_reject else HypothesisStatus.CLOSED
            h.status = new_status
            trace.update_hypothesis_status(
                h.hypothesis_id,
                status=new_status.value,
                judgment_reasoning=close_reason,
            )

        best_hypothesis = None
        confirmed = False
        if termination and termination.should_terminate and termination.best_hypothesis:
            best_hypothesis = termination.best_hypothesis
            confirmed = termination.reason and termination.reason.value == "CONFIRMED"
        elif all_judgments:
            best_j = max(
                all_judgments,
                key=lambda j: j.confidence_score,
            )
            best_hypothesis = next(
                (h for h in hypotheses if h.hypothesis_id == best_j.hypothesis_id),
                None,
            )

        return best_hypothesis, confirmed

    def _run_report_and_notify(
        self,
        scoping_result,
        best_hypothesis,
        confirmed,
        run: RunContext,
        *,
        alarm=None,
        hypothesis_path,
        evidence_texts,
        rejected_descriptions,
        timeline,
    ) -> bool:
        c = self._container
        store = c.session_store
        trace = run.trace
        elapsed = int(time.monotonic() - run.start_time)

        store.update_state(
            run.rca_id,
            RcaSessionState.REPORT_GENERATION,
            claim_token=run.claim_token,
        )
        with trace.span(
            SpanType.REPORT,
            input_summary=(f"최적가설={'있음' if best_hypothesis else '없음'}, 확정={confirmed}"),
        ) as s:
            rca_report = run_report_generation(
                scoping_result,
                best_hypothesis,
                confirmed,
                hypothesis_path,
                evidence_texts,
                rejected_descriptions,
                timeline,
                c.report_agent,
            )
            if run.rca_id:
                rca_report.rca_id = run.rca_id
            s.output_summary = f"rca_id={rca_report.rca_id}, 신뢰도={rca_report.confidence_score}"
        logger.info("RCA report generated: %s", rca_report.rca_id)

        trace.check_cancelled()
        # 플레이북이 리포트의 한 섹션이므로 리포트보다 먼저 만든다. 순서가 반대면
        # 리포트는 자신이 담아야 할 절차를 모르는 상태로 확정된다.
        playbook, playbook_span_id = self._run_playbook(
            rca_report,
            scoping_result,
            run,
        )
        trace.check_cancelled()

        report_s3_key = c.report_store.save(
            rca_report,
            playbook=playbook,
            claim_token=run.claim_token,
            attempt=run.attempt,
        )
        if settings.S3_REPORT_BUCKET and not report_s3_key:
            logger.error(
                "Report persistence failed for RCA %s; leaving session retryable",
                rca_report.rca_id,
            )
            return False

        with trace.span(
            SpanType.NOTIFICATION,
            input_summary=f"rca_id={rca_report.rca_id}",
        ) as s:
            # 검증이 확정한 원인 유형은 리포트·플레이북의 입력이고 세션 상태로 남지만,
            # 알림 payload 에는 담지 않는다 — 알림에 기계 소비자가 없다.
            validated_fault_type = best_hypothesis.validated_fault_type if best_hypothesis else FaultType.UNSUPPORTED
            notification = build_notification(
                rca_report,
                report_s3_key,
                elapsed,
                playbook=playbook,
                alarm=alarm,
                selected_hypothesis_id=(best_hypothesis.hypothesis_id if best_hypothesis else ""),
            )

            completed = store.mark_completed(
                rca_report.rca_id,
                root_cause=rca_report.root_cause,
                confirmed=confirmed,
                selected_hypothesis_id=(best_hypothesis.hypothesis_id if best_hypothesis else ""),
                fault_type=validated_fault_type,
                completion_notification=notification,
                report_s3_key=report_s3_key,
                playbook_span_id=playbook_span_id or "",
                playbook_id=playbook.playbook_id if playbook else "",
                claim_token=run.claim_token,
            )
            if not completed:
                s.output_summary = "완료 상태 및 알림 저장 실패"
                return False
            c.report_store.save_vectors(rca_report, scoping_result=scoping_result)
            if not c.notification.send(notification):
                s.output_summary = "완료 알림 전송 대기"
                return False
            if not store.mark_completion_notified(
                rca_report.rca_id,
                claim_token=run.claim_token,
            ):
                s.output_summary = "완료 알림 전송 상태 저장 실패"
                return False
            s.output_summary = f"소요시간={elapsed}초"

        logger.info(
            "RCA complete: rca_id=%s, elapsed=%ds",
            rca_report.rca_id,
            elapsed,
        )
        return True

    def _run_playbook(
        self,
        rca_report,
        scoping_result,
        run: RunContext,
    ) -> tuple[Playbook | None, str | None]:
        c = self._container
        trace = run.trace
        playbook_span = trace.start_span(
            SpanType.PLAYBOOK,
            input_summary=f"rca_id={rca_report.rca_id}",
        )
        try:
            playbook = run_playbook_generation(
                rca_report,
                c.playbook_agent,
                playbook_store=c.playbook_store,
                scoping_result=scoping_result,
            )
            with self._side_effect_lease(
                rca_report.rca_id,
                run.claim_token,
                "playbook",
            ):
                c.playbook_store.save(playbook, scoping_result=scoping_result)
            trace.end_span(
                playbook_span,
                output_summary=(f"playbook_id={playbook.playbook_id}, 장애유형={playbook.failure_type}"),
                # 실행 주체가 이 메타데이터에서 절차를 읽으므로 두 엔진이 같은 모양을
                # 써야 한다. cc-headless 의 PLAYBOOK 스팬 메타데이터가 기준이다.
                metadata={
                    "playbook_id": playbook.playbook_id,
                    "failure_type": playbook.failure_type,
                    "symptom_pattern": playbook.symptom_pattern,
                    "severity_criteria": playbook.severity_criteria,
                    "verification_steps": playbook.verification_steps,
                    "execution_steps": [step.model_dump() for step in playbook.execution_steps],
                    "temporary_mitigation": playbook.temporary_mitigation,
                    "permanent_remediation": playbook.permanent_remediation,
                    "escalation_criteria": playbook.escalation_criteria,
                    "prevention_measures": playbook.prevention_measures,
                    "related_metrics": playbook.related_metrics,
                    "tags": playbook.tags,
                    "verification_status": playbook.verification_status.value,
                },
            )
            logger.info(
                "Playbook %s: %s",
                playbook.playbook_id,
                playbook.failure_type,
            )
            return playbook, playbook_span.span_id
        except (SessionCancelledError, SideEffectLeaseUnavailableError):
            raise
        except Exception:
            logger.exception("Playbook generation failed, continuing pipeline")
            trace.end_span(
                playbook_span,
                status=SpanStatus.FAILED,
                error="Playbook generation failed",
            )
            return None, None
