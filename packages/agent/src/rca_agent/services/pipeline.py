from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from rca_agent.adapters.secondary.trace.dynamodb_trace_store import SpanStatus, SpanType, TraceStore
from rca_agent.config.settings import (
    ALARM_STALENESS_SECONDS,
    RCA_BEAM_WIDTH,
    RCA_MAX_REGENERATION_ROUNDS,
    REJECTION_THRESHOLD,
)
from rca_agent.ports.dto.models import (
    AlarmPayload,
    HypothesisStatus,
    Playbook,
    RcaSessionState,
    TerminationReason,
)
from rca_agent.services.branching import run_branching
from rca_agent.services.evidence import run_evidence_collection
from rca_agent.services.hypothesis import run_hypothesis_generation
from rca_agent.services.playbook_gen import run_playbook_generation
from rca_agent.services.prioritization import run_prioritization
from rca_agent.services.report import run_report_generation
from rca_agent.services.scoping import run_scoping
from rca_agent.services.termination import check_termination
from rca_agent.services.validation import run_validation

logger = logging.getLogger(__name__)


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


class PipelineOrchestrator:
    def __init__(self, container):
        self._container = container

    def process_alarm(self, body: dict) -> None:
        from rca_agent.adapters.secondary.session.dynamodb_session_store import (
            InvalidStateTransitionError,
            SessionCancelledError,
        )

        start_time = time.monotonic()
        alarm_data = parse_sns_envelope(body)

        if not should_process(alarm_data):
            logger.info(
                "Skipping non-alarm message: AlarmName=%s, NewStateValue=%s",
                alarm_data.get("AlarmName"),
                alarm_data.get("NewStateValue"),
            )
            return

        alarm = AlarmPayload.from_cloudwatch_sns(alarm_data)
        logger.info(
            "Parsed alarm: name=%s, resource=%s, service=%s",
            alarm.alarm_name,
            alarm.resource_id,
            alarm.service_name,
        )

        store = self._container.session_store
        if store.check_duplicate(alarm):
            logger.info("Skipping duplicate alarm: %s", alarm.alarm_name)
            return

        if alarm.state_change_time:
            age_seconds = (datetime.now(UTC) - alarm.state_change_time).total_seconds()
            if age_seconds > ALARM_STALENESS_SECONDS:
                logger.info(
                    "Skipping stale alarm: %s (age=%.0fs > %ds)",
                    alarm.alarm_name,
                    age_seconds,
                    ALARM_STALENESS_SECONDS,
                )
                session = store.create_session(alarm)
                if session:
                    store.mark_outdated(
                        session.rca_id,
                        reason=(f"Alarm age {int(age_seconds)}s exceeds {ALARM_STALENESS_SECONDS}s threshold"),
                    )
                return

        session = store.create_session(alarm)
        rca_id = session.rca_id if session else ""
        trace = TraceStore(
            rca_id,
            dynamodb_client=self._container.dynamodb_client,
        )

        try:
            self._run_pipeline(
                alarm,
                rca_id=rca_id,
                start_time=start_time,
                trace=trace,
            )
        except SessionCancelledError:
            logger.info(
                "Pipeline cancelled for alarm %s (rca_id=%s)",
                alarm.alarm_name,
                rca_id,
            )
        except InvalidStateTransitionError:
            logger.exception(
                "Invalid state transition for alarm %s (rca_id=%s)",
                alarm.alarm_name,
                rca_id,
            )
        except Exception:
            logger.exception("Pipeline failed for alarm %s", alarm.alarm_name)
            if rca_id:
                store.mark_failed(
                    rca_id,
                    error_reason="Unhandled pipeline exception",
                )

    def _run_pipeline(self, alarm, *, rca_id, start_time, trace):
        store = self._container.session_store

        scoping_result = self._run_scoping(alarm, rca_id=rca_id, trace=trace)

        hypotheses = self._run_hypothesis_generation(
            scoping_result,
            rca_id=rca_id,
            trace=trace,
        )
        if not hypotheses:
            store.mark_failed(rca_id, error_reason="No hypotheses generated")
            return

        termination, all_judgments, evidence_map, rejected_descriptions, timeline = self._run_validation_loop(
            alarm,
            scoping_result,
            hypotheses,
            rca_id=rca_id,
            start_time=start_time,
            trace=trace,
        )

        best_hypothesis, confirmed = self._finalize_hypotheses(
            hypotheses,
            termination,
            all_judgments,
            trace=trace,
        )

        self._run_report_and_notify(
            scoping_result,
            best_hypothesis,
            confirmed,
            hypothesis_path=[best_hypothesis.description] if best_hypothesis else [],
            evidence_texts=[e for e in evidence_map.values() if e],
            rejected_descriptions=rejected_descriptions,
            timeline=timeline,
            rca_id=rca_id,
            start_time=start_time,
            trace=trace,
        )

    def _run_scoping(self, alarm, *, rca_id, trace):
        c = self._container
        c.session_store.update_state(rca_id, RcaSessionState.SCOPING)
        with trace.span(
            SpanType.SCOPING,
            input_summary=f"알람={alarm.alarm_name}, 리전={alarm.region}",
        ) as s:
            scoping_result = run_scoping(
                alarm,
                c.scoping_agent,
                s3_vectors_client=c.s3_vectors_client,
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

    def _run_hypothesis_generation(self, scoping_result, *, rca_id, trace):
        c = self._container
        c.session_store.update_state(rca_id, RcaSessionState.HYPOTHESIS_GENERATION)
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

    def _run_validation_loop(self, alarm, scoping_result, hypotheses, *, rca_id, start_time, trace):
        c = self._container
        store = c.session_store

        all_judgments = []
        rejected_descriptions: list[str] = []
        validation_loop_count = 0
        regeneration_count = 0
        timeline: list[str] = []
        evidence_map: dict[str, str] = {}
        evidence_failed_ids: set[str] = set()

        timeline.append(f"Alarm received: {alarm.alarm_name}")
        timeline.append(
            f"Scoping complete: severity={scoping_result.initial_severity}",
        )
        timeline.append(f"Initial hypotheses: {len(hypotheses)}")

        termination = None

        while True:
            validation_loop_count += 1
            logger.info(
                "Validation loop %d, hypotheses=%d",
                validation_loop_count,
                len(hypotheses),
            )
            loop_span = trace.start_span(
                SpanType.VALIDATION_LOOP,
                loop_index=validation_loop_count,
                input_summary=f"가설={len(hypotheses)}개",
            )

            # F3: Prioritization
            store.update_state(
                rca_id,
                RcaSessionState.HYPOTHESIS_PRIORITIZATION,
            )
            with trace.span(
                SpanType.PRIORITIZATION,
                parent_span_id=loop_span.span_id,
                input_summary=f"가설={len(hypotheses)}개",
            ) as s:
                prioritization_result = run_prioritization(
                    scoping_result,
                    hypotheses,
                    c.prioritization_agent,
                )
                s.output_summary = f"가설 {len(hypotheses)}개 우선순위 결정"

            active_hypotheses = select_beam(
                hypotheses,
                prioritization_result,
                RCA_BEAM_WIDTH,
            )
            logger.info(
                "Beam selection: %d/%d hypotheses",
                len(active_hypotheses),
                len(hypotheses),
            )

            # F4: Evidence collection
            store.update_state(rca_id, RcaSessionState.EVIDENCE_COLLECTION)
            new_hypotheses = [h for h in active_hypotheses if h.hypothesis_id not in evidence_map]
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
                        rca_id=rca_id,
                        trace=trace,
                        s3_client=c.s3_client,
                        existing_evidence_map=evidence_map,
                        all_hypotheses=hypotheses,
                    )
                    evidence_map.update(ev_summary.evidence_map)
                    evidence_failed_ids.update(ev_summary.failed_ids)
                s.output_summary = f"가설 {len(new_hypotheses)}개에 대한 증거 수집 완료"
                s.metadata = {
                    "신규_가설_수": len(new_hypotheses),
                    "beam_width": RCA_BEAM_WIDTH,
                }
            timeline.append(
                f"Loop {validation_loop_count}:"
                f" evidence for {len(new_hypotheses)} hypotheses"
                f" (beam={len(active_hypotheses)})"
            )

            # F5: Validation
            store.update_state(
                rca_id,
                RcaSessionState.HYPOTHESIS_VALIDATION,
            )
            with trace.span(
                SpanType.VALIDATION,
                parent_span_id=loop_span.span_id,
                input_summary=(f"beam={len(active_hypotheses)}개, 증거={len(evidence_map)}건"),
            ) as s:
                validation_result = run_validation(
                    active_hypotheses,
                    evidence_map,
                    c.validation_agent,
                    evidence_failed_ids=evidence_failed_ids,
                )
                all_judgments = validation_result.judgments
                confirmed_count = sum(1 for j in all_judgments if j.status == HypothesisStatus.CONFIRMED)
                rejected_count = sum(1 for j in all_judgments if j.status == HypothesisStatus.REJECTED)
                s.output_summary = (
                    f"판정={len(all_judgments)}건,"
                    f" 확정={confirmed_count},"
                    f" 기각={rejected_count},"
                    f" 전체기각={validation_result.all_rejected}"
                )
                s.metadata = {
                    "판정_수": len(all_judgments),
                    "확정": confirmed_count,
                    "기각": rejected_count,
                    "전체기각": validation_result.all_rejected,
                    "beam_width": RCA_BEAM_WIDTH,
                }
            timeline.append(
                f"Loop {validation_loop_count}:"
                f" validated {len(all_judgments)} hypotheses"
                f" (beam={len(active_hypotheses)})"
            )

            for j in all_judgments:
                trace.update_hypothesis_status(
                    j.hypothesis_id,
                    status=j.status.value,
                    confidence=j.confidence_score,
                    judgment_reasoning=j.reasoning[:500],
                )
                h = next(
                    (h for h in hypotheses if h.hypothesis_id == j.hypothesis_id),
                    None,
                )
                if h:
                    h.status = j.status

            for j in all_judgments:
                if j.status == HypothesisStatus.REJECTED:
                    h = next(
                        (h for h in hypotheses if h.hypothesis_id == j.hypothesis_id),
                        None,
                    )
                    if h and h.description not in rejected_descriptions:
                        rejected_descriptions.append(h.description)
                    pruned = prune_subtree(j.hypothesis_id, hypotheses)
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

            # Termination check
            with trace.span(
                SpanType.TERMINATION,
                parent_span_id=loop_span.span_id,
                input_summary=(f"루프={validation_loop_count}, 판정={len(all_judgments)}건"),
            ) as s:
                termination = check_termination(
                    judgments=all_judgments,
                    hypotheses=hypotheses,
                    start_time=start_time,
                    validation_loop_count=validation_loop_count,
                )
                s.output_summary = f"종료={termination.should_terminate}, 사유={termination.reason}"
                s.metadata = {"종료여부": termination.should_terminate}
                if termination.reason:
                    s.metadata["사유"] = termination.reason.value

            if termination.should_terminate:
                logger.info("Termination: %s", termination.reason)
                timeline.append(f"Terminated: {termination.reason}")
                trace.end_span(
                    loop_span,
                    output_summary=f"종료: {termination.reason}",
                    metadata={"루프_번호": validation_loop_count},
                )
                break

            # All rejected → regeneration
            if validation_result.all_rejected:
                regeneration_count += 1
                if regeneration_count > RCA_MAX_REGENERATION_ROUNDS:
                    logger.warning("Max regeneration rounds exceeded")
                    timeline.append("Max regeneration rounds exceeded")
                    trace.end_span(
                        loop_span,
                        output_summary="최대 재생성 라운드 초과",
                        status=SpanStatus.FAILED,
                    )
                    break
                logger.info(
                    "All rejected, regenerating hypotheses (round %d)",
                    regeneration_count,
                )
                for h in hypotheses:
                    if h.status in (
                        HypothesisStatus.PENDING,
                        HypothesisStatus.NEEDS_INVESTIGATION,
                    ):
                        h.status = HypothesisStatus.REJECTED
                        trace.update_hypothesis_status(
                            h.hypothesis_id,
                            status=HypothesisStatus.REJECTED.value,
                            judgment_reasoning=("전체 기각으로 가설 재생성 — 이전 라운드 자동 기각"),
                        )
                store.update_state(
                    rca_id,
                    RcaSessionState.HYPOTHESIS_GENERATION,
                )
                with trace.span(
                    SpanType.HYPOTHESIS_GENERATION,
                    parent_span_id=loop_span.span_id,
                    input_summary=f"재생성 라운드 {regeneration_count}",
                ) as s:
                    hypothesis_result = run_hypothesis_generation(
                        scoping_result,
                        c.hypothesis_agent,
                    )
                    hypotheses = list(hypothesis_result.hypotheses)
                    s.output_summary = f"가설 {len(hypotheses)}개 재생성"
                    s.metadata = {
                        "재생성_라운드": regeneration_count,
                        "가설_수": len(hypotheses),
                    }
                if not hypotheses:
                    logger.error("Regeneration produced no hypotheses")
                    trace.end_span(
                        loop_span,
                        output_summary="재생성 결과 가설 없음",
                        status=SpanStatus.FAILED,
                    )
                    break
                trace.put_hypotheses(hypotheses)
                timeline.append(
                    f"Regenerated hypotheses: {len(hypotheses)}",
                )
                trace.end_span(
                    loop_span,
                    output_summary=f"가설 {len(hypotheses)}개 재생성",
                    metadata={
                        "루프_번호": validation_loop_count,
                        "재생성_라운드": regeneration_count,
                    },
                )
                continue

            # Branching
            new_children = []
            ni_count = sum(1 for j in all_judgments if j.status == HypothesisStatus.NEEDS_INVESTIGATION)
            with trace.span(
                SpanType.BRANCHING,
                parent_span_id=loop_span.span_id,
                input_summary=f"추가조사필요={ni_count}건",
            ) as s:
                for j in all_judgments:
                    if j.status != HypothesisStatus.NEEDS_INVESTIGATION:
                        continue
                    parent = next(
                        (h for h in hypotheses if h.hypothesis_id == j.hypothesis_id),
                        None,
                    )
                    if parent is None:
                        continue
                    evidence_text = evidence_map.get(
                        parent.hypothesis_id,
                        "",
                    )
                    branching_result = run_branching(
                        parent,
                        evidence_text,
                        rejected_descriptions,
                        c.branching_agent,
                    )
                    new_children.extend(branching_result.children)
                s.output_summary = f"신규_하위가설={len(new_children)}개"
                s.metadata = {"신규_하위가설_수": len(new_children)}

            if not new_children:
                logger.info("No new child hypotheses, terminating")
                timeline.append("No new child hypotheses")
                trace.end_span(
                    loop_span,
                    output_summary="신규 하위가설 없음, 종료",
                )
                break

            trace.put_hypotheses(new_children)
            hypotheses.extend(new_children)
            logger.info(
                "Added %d child hypotheses, total=%d",
                len(new_children),
                len(hypotheses),
            )
            trace.end_span(
                loop_span,
                output_summary=(f"하위가설 {len(new_children)}개 추가, 총 {len(hypotheses)}개"),
                metadata={
                    "루프_번호": validation_loop_count,
                    "신규_하위가설": len(new_children),
                },
            )

        return termination, all_judgments, evidence_map, rejected_descriptions, timeline

    def _finalize_hypotheses(self, hypotheses, termination, all_judgments, *, trace):
        close_reason_map = {
            TerminationReason.CONFIRMED: "확정된 근본원인 발견으로 기각",
            TerminationReason.TIME_BUDGET: "시간 예산 소진",
            TerminationReason.TOKEN_BUDGET: "토큰 예산 소진",
            TerminationReason.MAX_DEPTH: "최대 트리 깊이 초과",
            TerminationReason.MAX_LOOPS: "최대 검증 루프 초과",
            TerminationReason.ALL_REJECTED: "전체 가설 기각",
        }
        close_reason = (
            close_reason_map.get(termination.reason, "분석 종료") if termination and termination.reason else "분석 종료"
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
        *,
        hypothesis_path,
        evidence_texts,
        rejected_descriptions,
        timeline,
        rca_id,
        start_time,
        trace,
    ):
        c = self._container
        store = c.session_store
        elapsed = int(time.monotonic() - start_time)

        # F7: Report
        store.update_state(rca_id, RcaSessionState.REPORT_GENERATION)
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
            if rca_id:
                rca_report.rca_id = rca_id
            s.output_summary = f"rca_id={rca_report.rca_id}, 신뢰도={rca_report.confidence_score}"
        logger.info("RCA report generated: %s", rca_report.rca_id)

        report_s3_key = c.report_store.save(rca_report)
        c.report_store.save_vectors(rca_report, scoping_result=scoping_result)

        # F8: Playbook
        playbook: Playbook | None = None
        playbook_span = trace.start_span(
            SpanType.PLAYBOOK,
            input_summary=f"rca_id={rca_report.rca_id}",
        )
        try:
            playbook = run_playbook_generation(
                rca_report,
                c.playbook_agent,
                scoping_result=scoping_result,
                s3_vectors_client=c.s3_vectors_client,
            )
            c.playbook_store.save(playbook, scoping_result=scoping_result)
            pb_meta = {
                "playbook_id": playbook.playbook_id,
                "failure_type": playbook.failure_type,
                "symptom_pattern": playbook.symptom_pattern,
                "severity_criteria": playbook.severity_criteria,
                "verification_steps": playbook.verification_steps,
                "temporary_mitigation": playbook.temporary_mitigation,
                "permanent_remediation": playbook.permanent_remediation,
                "escalation_criteria": playbook.escalation_criteria,
                "prevention_measures": playbook.prevention_measures,
                "related_metrics": playbook.related_metrics,
                "tags": playbook.tags,
            }
            trace.end_span(
                playbook_span,
                output_summary=(f"playbook_id={playbook.playbook_id}, 장애유형={playbook.failure_type}"),
                metadata=pb_meta,
            )
            logger.info(
                "Playbook %s: %s",
                playbook.playbook_id,
                playbook.failure_type,
            )
        except Exception:
            logger.exception(
                "Playbook generation failed, continuing pipeline",
            )
            trace.end_span(
                playbook_span,
                status=SpanStatus.FAILED,
                error="Playbook generation failed",
            )

        # F9: Notification
        from rca_agent.services.notification import build_notification

        with trace.span(
            SpanType.NOTIFICATION,
            input_summary=f"rca_id={rca_report.rca_id}",
        ) as s:
            notification = build_notification(
                rca_report,
                report_s3_key,
                elapsed,
                playbook=playbook,
            )
            c.notification.send(notification)
            s.output_summary = f"소요시간={elapsed}초"

        store.mark_completed(
            rca_report.rca_id,
            root_cause=rca_report.root_cause,
            confirmed=confirmed,
        )
        logger.info(
            "RCA complete: rca_id=%s, elapsed=%ds",
            rca_report.rca_id,
            elapsed,
        )
