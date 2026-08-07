"""플레이북 실행과 회고의 오케스트레이션.

승인 요청을 소비해 실행하고, 해결이 확정되면 회고를 이어서 수행한다. 실행 상태는
분석 세션과 별도 생명주기를 가지며, 실행 실패가 분석 리포트를 변경하지 않는다.
"""

from __future__ import annotations

from threading import Event

import structlog

from cc_headless.config.settings import EXECUTION_CLAIM_SECONDS
from cc_headless.di.execution_container import ExecutionContainer
from cc_headless.ports.interfaces.execution_store import (
    ExecutionClaimDisposition,
    ExecutionClaimLostError,
    ExecutionTarget,
)
from cc_headless.services.execution_evidence import ExecutionEvidence
from cc_headless.services.execution_outcome import assemble_evidence, judge_resolution
from cc_headless.services.execution_prompt import (
    build_execution_prompt,
    build_retrospective_prompt,
)
from cc_headless.services.execution_request import (
    ExecutionRequest,
    InvalidExecutionRequestError,
    parse_execution_request,
)
from cc_headless.services.execution_state import ExecutionState, enters_retrospective
from cc_headless.services.execution_workspace import ExecutionWorkspace
from cc_headless.services.playbook_merge import merge_playbook_update, promote_to_verified

logger = structlog.get_logger()


class ExecutionOrchestrator:
    def __init__(self, container: ExecutionContainer, shutdown_event: Event | None = None):
        self._c = container
        self._shutdown_event = shutdown_event or Event()

    def process_message(self, message_body: str) -> bool:
        """실행 요청 하나를 처리한다. True 면 큐에서 지운다."""
        try:
            request = parse_execution_request(message_body)
        except InvalidExecutionRequestError as exc:
            # 승인으로 해석할 수 없는 메시지는 실행하지 않는다. 재전달해도 같은 판정이
            # 나오므로 큐에서 지운다.
            logger.error("execution_request_rejected", detail=str(exc))
            return True

        execution_id = request.execution_id
        log = logger.bind(
            execution_id=execution_id,
            rca_id=request.rca_id,
            engine=request.engine,
            approval_id=request.approval_id,
        )
        log.info("execution_request_received")

        store = self._c.execution_store
        claim = store.claim_execution(
            execution_id,
            rca_id=request.rca_id,
            engine=request.engine,
            approval_id=request.approval_id,
            requested_by=request.requested_by,
            report_s3_key=request.report_s3_key,
            approved_playbook_s3_key=request.approved_playbook_s3_key,
            playbook_digest=request.playbook_digest,
            claim_seconds=EXECUTION_CLAIM_SECONDS,
        )
        if claim.disposition is ExecutionClaimDisposition.TERMINAL_DUPLICATE:
            log.info("execution_terminal_duplicate_acknowledged")
            return True
        if claim.disposition is ExecutionClaimDisposition.REJECTED:
            log.error("execution_reservation_rejected")
            return True
        if claim.disposition is ExecutionClaimDisposition.EXPIRED_FAILED:
            log.error("execution_expired_and_failed_reapproval_required")
            return True
        if not claim.acquired:
            log.info("execution_claim_contended")
            return False

        return self._run(request, execution_id, claim.claim_token, log)

    def _run(
        self,
        request: ExecutionRequest,
        execution_id: str,
        claim_token: str,
        log: structlog.stdlib.BoundLogger,
    ) -> bool:
        store = self._c.execution_store
        workspace = ExecutionWorkspace.create(execution_id)
        workspace.prepare()

        try:
            try:
                playbook = self._c.evidence_store.load_approved_playbook(
                    request.approved_playbook_s3_key,
                    playbook_digest=request.playbook_digest,
                )
                target = store.load_target(
                    request.rca_id,
                    request.engine,
                    report_s3_key=request.report_s3_key,
                    playbook=playbook,
                )
            except Exception as exc:
                log.error("execution_target_unavailable", detail=str(exc))
                store.update_state(
                    execution_id,
                    rca_id=request.rca_id,
                    state=ExecutionState.FAILED,
                    claim_token=claim_token,
                    error_reason=str(exc),
                )
                return True

            steps = target.playbook.get("execution_steps")
            if not isinstance(steps, list) or not steps:
                reason = "approved playbook declares no execution steps"
                log.info("execution_has_no_steps")
                store.update_state(
                    execution_id,
                    rca_id=request.rca_id,
                    state=ExecutionState.FAILED,
                    claim_token=claim_token,
                    error_reason=reason,
                )
                return True
            approved_step_ids: list[str] = []
            for step in steps:
                step_id = step.get("step_id") if isinstance(step, dict) else None
                if not isinstance(step_id, str) or not step_id.strip() or step_id.strip() in approved_step_ids:
                    reason = "approved playbook has invalid or duplicate execution step IDs"
                    store.update_state(
                        execution_id,
                        rca_id=request.rca_id,
                        state=ExecutionState.FAILED,
                        claim_token=claim_token,
                        error_reason=reason,
                    )
                    return True
                approved_step_ids.append(step_id.strip())

            prompt = build_execution_prompt(target, execution_id=execution_id)

            def _should_cancel() -> bool:
                if self._shutdown_event.is_set():
                    return True
                state = store.load_state(execution_id, rca_id=request.rca_id)
                return state is ExecutionState.CANCELLED

            cc_result = self._c.execution_runner.run_execution(
                prompt,
                execution_token=workspace.token,
                execution_id=execution_id,
                approved_step_ids=tuple(approved_step_ids),
                cancel_checker=_should_cancel,
            )
            # 에이전트가 실패를 보고하지 않고 아무것도 하지 않은 채 성공 종료할 수 있다.
            # 그 경우 기록된 관측이 없으므로 판정은 미해결로 떨어지지만, 왜 수행하지
            # 않았는지는 이 응답에만 남아 있다 — 남기지 않으면 사후에 읽을 방법이 없다.
            log.info(
                "execution_agent_returned",
                succeeded=cc_result.success,
                cancelled=cc_result.cancelled,
                detail=cc_result.result[:2000],
            )

            evidence = assemble_evidence(
                workspace.read_records(),
                execution_id=execution_id,
                rca_id=request.rca_id,
                engine=request.engine,
                playbook=target.playbook,
            )

            if cc_result.cancelled:
                return self._finish(
                    execution_id,
                    request,
                    evidence,
                    claim_token,
                    ExecutionState.CANCELLED,
                    "execution was cancelled",
                    log,
                )

            store.update_state(
                execution_id,
                rca_id=request.rca_id,
                state=ExecutionState.VERIFYING,
                claim_token=claim_token,
                summary=evidence.summary(),
            )

            verdict = judge_resolution(evidence, agent_succeeded=cc_result.success)
            log.info(
                "execution_judged",
                state=str(verdict.state),
                blocked=evidence.blocked_count,
                failed=evidence.failed_step_count,
                # 절차를 수행했는데 해소 기록이 없는 조합은 에이전트가 마지막 기록에
                # 도달하지 못했다는 뜻이다. 판정은 미해결로 정확하지만 원인이 절차
                # 실패인지 조기 종료인지 구별되지 않으므로, 그 구별을 로그가 보유한다.
                resolution_recorded=evidence.resolution_confirmed is not None,
                attempted=evidence.attempted_step_count,
            )
            self._finish(
                execution_id,
                request,
                evidence,
                claim_token,
                verdict.state,
                verdict.reason,
                log,
            )

            if enters_retrospective(verdict.state):
                self._retrospect(
                    execution_id,
                    request,
                    target,
                    evidence,
                    claim_token,
                    workspace,
                    request.approved_playbook_s3_key,
                    log,
                )
            return True
        except ExecutionClaimLostError:
            # 다른 워커가 같은 실행을 이어받았다. 이 워커의 쓰기는 더 이상 유효하지 않다.
            log.info("execution_claim_lost")
            return False
        except Exception:
            log.exception("execution_pipeline_failed")
            try:
                store.update_state(
                    execution_id,
                    rca_id=request.rca_id,
                    state=ExecutionState.FAILED,
                    claim_token=claim_token,
                    error_reason="Unhandled execution exception",
                )
            except Exception:
                log.exception("execution_mark_failed_failed")
            return False
        finally:
            workspace.cleanup()

    def _finish(
        self,
        execution_id: str,
        request: ExecutionRequest,
        evidence: ExecutionEvidence,
        claim_token: str,
        state: ExecutionState,
        reason: str,
        log: structlog.stdlib.BoundLogger,
    ) -> bool:
        """증거를 먼저 보존한 뒤 상태를 확정한다.

        실행이 실패·미해결로 끝나도 증거는 지우지 않는다. 자동 회고의 입력은 아니지만
        사람이 원인을 읽는 유일한 자료다.
        """
        evidence.final_state = str(state)
        if state is not ExecutionState.RESOLVED:
            evidence.error_reason = reason

        evidence_key = ""
        try:
            evidence_key = self._c.evidence_store.save_execution_evidence(
                execution_id,
                rca_id=request.rca_id,
                evidence=evidence.to_dict(),
            )
        except Exception:
            # 증거 저장 실패를 실행 실패로 처리하지 않는다. 실행은 이미 일어났고 그
            # 사실을 상태로는 남겨야 한다.
            log.exception("execution_evidence_save_failed")

        self._c.execution_store.update_state(
            execution_id,
            rca_id=request.rca_id,
            state=state,
            claim_token=claim_token,
            summary=evidence.summary(),
            error_reason="" if state is ExecutionState.RESOLVED else reason,
            evidence_s3_key=evidence_key,
        )
        return True

    def _retrospect(
        self,
        execution_id: str,
        request: ExecutionRequest,
        target: ExecutionTarget,
        evidence: ExecutionEvidence,
        claim_token: str,
        workspace: ExecutionWorkspace,
        snapshot_key: str,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        """해결된 실행 직후의 회고. 실패해도 실행 결과를 되돌리지 않는다."""
        store = self._c.execution_store
        if not store.claim_retrospective(execution_id, rca_id=request.rca_id, claim_token=claim_token):
            log.info("retrospective_already_claimed")
            return

        try:
            prompt = build_retrospective_prompt(target, evidence, execution_id=execution_id)
            result = self._c.execution_runner.run_retrospective(
                prompt,
                execution_token=workspace.token,
                execution_id=execution_id,
            )
            if not result.success:
                store.record_retrospective(
                    execution_id,
                    rca_id=request.rca_id,
                    claim_token=claim_token,
                    status="FAILED",
                    summary=result.result[:500],
                    playbook_snapshot_s3_key=snapshot_key,
                )
                log.error("retrospective_agent_failed", detail=result.result[:500])
                return

            saved = workspace.read_retrospective()
            if saved is None:
                # 교정할 결함이 없었다는 것은 절차가 그대로 이슈를 해소했다는 뜻이므로
                # 승격은 일어난다. 갱신 없는 회고도 절차를 확인한 회고다.
                self._publish_playbook(request, target, promote_to_verified(target.playbook), execution_id)
                store.record_retrospective(
                    execution_id,
                    rca_id=request.rca_id,
                    claim_token=claim_token,
                    status="NO_CHANGE",
                    summary="retrospective found no procedure defect to correct",
                    playbook_snapshot_s3_key=snapshot_key,
                )
                log.info("retrospective_no_change", promoted=True)
                return

            merged, diff = merge_playbook_update(target.playbook, saved.get("update"))
            if diff.is_empty:
                self._publish_playbook(request, target, promote_to_verified(merged), execution_id)
                store.record_retrospective(
                    execution_id,
                    rca_id=request.rca_id,
                    claim_token=claim_token,
                    status="NO_CHANGE",
                    summary="proposed update changed nothing",
                    playbook_snapshot_s3_key=snapshot_key,
                )
                log.info("retrospective_update_changed_nothing", promoted=True)
                return

            diff_key = self._c.evidence_store.save_retrospective_diff(
                execution_id,
                rca_id=request.rca_id,
                diff={
                    "rationale": str(saved.get("rationale", ""))[:4000],
                    **diff.to_dict(),
                },
            )
            self._publish_playbook(request, target, promote_to_verified(merged), execution_id)
            store.record_retrospective(
                execution_id,
                rca_id=request.rca_id,
                claim_token=claim_token,
                status="UPDATED",
                summary=str(saved.get("rationale", ""))[:500],
                playbook_snapshot_s3_key=snapshot_key,
                diff_s3_key=diff_key,
            )
            log.info(
                "retrospective_updated_playbook",
                corrected=len(diff.corrected_steps),
                added=len(diff.added_steps),
            )
        except Exception:
            # 회고 실패는 이미 확정된 실행 결과를 되돌리지 않는다.
            log.exception("retrospective_failed")
            try:
                store.record_retrospective(
                    execution_id,
                    rca_id=request.rca_id,
                    claim_token=claim_token,
                    status="FAILED",
                    summary="unhandled retrospective exception",
                    playbook_snapshot_s3_key=snapshot_key,
                )
            except Exception:
                log.exception("retrospective_record_failed")

    def _publish_playbook(
        self,
        request: ExecutionRequest,
        target: ExecutionTarget,
        playbook: dict,
        execution_id: str,
    ) -> None:
        """회고를 통과한 플레이북을 개정본과 검색 인덱스 양쪽에 반영한다.

        두 저장소가 같은 내용을 보아야 한다 — 다음 실행은 개정본을 읽고 다음 RCA 의
        보강은 인덱스를 읽으므로, 한쪽만 갱신하면 승격이 한 경로에서만 보인다.
        """
        self._c.execution_store.save_playbook_revision(
            request.rca_id,
            request.engine,
            playbook,
            execution_id=execution_id,
        )
        # 갱신된 절차가 다음 유사 장애의 검색 결과에 반영되도록 인덱스도 갱신한다.
        self._c.playbook_store.save_to_s3_vectors(
            playbook,
            request.rca_id,
            metric_name=target.metric_name,
        )
