"""플레이북 실행과 회고의 오케스트레이션.

승인 요청을 소비해 실행하고, 해결이 확정되면 회고를 이어서 수행한다. 실행 상태는
분석 세션과 별도 생명주기를 가지며, 실행 실패가 분석 리포트를 변경하지 않는다.
"""

from __future__ import annotations

from threading import Event

import structlog

from headless_codex.config.settings import EXECUTION_CLAIM_SECONDS
from headless_codex.di.execution_container import ExecutionContainer
from headless_codex.ports.interfaces.execution_store import (
    ExecutionClaimDisposition,
    ExecutionClaimLostError,
    ExecutionTarget,
)
from headless_codex.services.execution_evidence import ExecutionEvidence
from headless_codex.services.execution_outcome import assemble_evidence, judge_resolution
from headless_codex.services.execution_prompt import (
    build_execution_prompt,
    build_retrospective_prompt,
)
from headless_codex.services.execution_request import (
    ExecutionRequest,
    InvalidExecutionRequestError,
    parse_execution_request,
)
from headless_codex.services.execution_state import ExecutionState, enters_retrospective
from headless_codex.services.execution_workspace import ExecutionWorkspace
from headless_codex.services.playbook_merge import (
    PLAYBOOK_DRAFT,
    VERIFICATION_STATUS_FIELD,
    merge_playbook_update,
    promote_to_verified,
)

logger = structlog.get_logger()

_MISSING_EVIDENCE_RETROSPECTIVE_REASON = "durable execution evidence is unavailable; retrospective was not started"


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
            approved_success_criteria: dict[str, str] = {}
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
                success_criteria = step.get("success_criteria")
                if not isinstance(success_criteria, str) or not success_criteria.strip():
                    reason = "approved playbook has a missing or invalid success criterion"
                    store.update_state(
                        execution_id,
                        rca_id=request.rca_id,
                        state=ExecutionState.FAILED,
                        claim_token=claim_token,
                        error_reason=reason,
                    )
                    return True
                normalized_step_id = step_id.strip()
                approved_step_ids.append(normalized_step_id)
                approved_success_criteria[normalized_step_id] = success_criteria

            prompt = build_execution_prompt(target, execution_id=execution_id)

            def _should_cancel() -> bool:
                if self._shutdown_event.is_set():
                    return True
                state = store.load_state(execution_id, rca_id=request.rca_id)
                return state is ExecutionState.CANCELLED

            codex_result = self._c.execution_runner.run_execution(
                prompt,
                execution_token=workspace.token,
                execution_id=execution_id,
                approved_step_ids=tuple(approved_step_ids),
                approved_success_criteria=approved_success_criteria,
                cancel_checker=_should_cancel,
            )
            # 에이전트가 실패를 보고하지 않고 아무것도 하지 않은 채 성공 종료할 수 있다.
            # 그 경우 기록된 관측이 없으므로 판정은 미해결로 떨어지지만, 왜 수행하지
            # 않았는지는 이 응답에만 남아 있다 — 남기지 않으면 사후에 읽을 방법이 없다.
            log.info(
                "execution_agent_returned",
                succeeded=codex_result.success,
                cancelled=codex_result.cancelled,
                detail=codex_result.result[:2000],
            )

            evidence = assemble_evidence(
                workspace.read_records(),
                execution_id=execution_id,
                rca_id=request.rca_id,
                engine=request.engine,
                playbook=target.playbook,
            )

            if codex_result.cancelled:
                self._finish(
                    execution_id,
                    request,
                    evidence,
                    claim_token,
                    ExecutionState.CANCELLED,
                    "execution was cancelled",
                    log,
                )
                return True

            store.update_state(
                execution_id,
                rca_id=request.rca_id,
                state=ExecutionState.VERIFYING,
                claim_token=claim_token,
                summary=evidence.summary(),
            )

            verdict = judge_resolution(evidence, agent_succeeded=codex_result.success)
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
            evidence_key = self._finish(
                execution_id,
                request,
                evidence,
                claim_token,
                verdict.state,
                verdict.reason,
                log,
            )

            if enters_retrospective(verdict.state) and evidence_key:
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
    ) -> str:
        """증거를 먼저 보존한 뒤 상태를 확정한다.

        실행이 실패·미해결로 끝나도 증거는 지우지 않는다. 자동 회고의 입력은 아니지만
        사람이 원인을 읽는 유일한 자료다. 해결된 실행의 증거를 보존하지 못하면 실행
        결과는 유지하되 회고를 실패로 확정해 갱신 입력이 없는 게시를 막는다.
        """
        evidence.final_state = str(state)
        if state is not ExecutionState.RESOLVED:
            evidence.error_reason = reason

        evidence_key = ""
        try:
            saved_key = self._c.evidence_store.save_execution_evidence(
                execution_id,
                rca_id=request.rca_id,
                evidence=evidence.to_dict(),
            )
            if isinstance(saved_key, str):
                evidence_key = saved_key.strip()
            if not evidence_key:
                log.error("execution_evidence_save_returned_empty_key")
        except Exception:
            # 증거 저장 실패를 실행 실패로 처리하지 않는다. 실행은 이미 일어났고 그
            # 사실을 상태로는 남겨야 한다.
            log.exception("execution_evidence_save_failed")

        retrospective_failure_reason = ""
        if state is ExecutionState.RESOLVED and not evidence_key:
            retrospective_failure_reason = _MISSING_EVIDENCE_RETROSPECTIVE_REASON

        self._c.execution_store.update_state(
            execution_id,
            rca_id=request.rca_id,
            state=state,
            claim_token=claim_token,
            summary=evidence.summary(),
            error_reason="" if state is ExecutionState.RESOLVED else reason,
            evidence_s3_key=evidence_key,
            retrospective_failure_reason=retrospective_failure_reason,
        )
        return evidence_key

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
            if (
                saved is None
                or not isinstance(saved.get("update"), dict)
                or not isinstance(saved.get("rationale"), str)
                or not saved["rationale"].strip()
            ):
                store.record_retrospective(
                    execution_id,
                    rca_id=request.rca_id,
                    claim_token=claim_token,
                    status="FAILED",
                    summary="retrospective exited without a persisted attestation",
                    playbook_snapshot_s3_key=snapshot_key,
                )
                log.error("retrospective_attestation_missing")
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
            if diff.corrected_steps or diff.added_steps:
                published = {**merged, VERIFICATION_STATUS_FIELD: PLAYBOOK_DRAFT}
            else:
                published = promote_to_verified(merged)
            self._publish_playbook(request, target, published, execution_id)
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

        DynamoDB 준비본이 먼저 내구성 있게 저장되고, 벡터 게시 뒤 정식 개정본으로
        확정된다. 벡터의 게시 식별자와 정식 개정본이 일치하기 전에는 검색 어댑터가
        VERIFIED 를 노출하지 않는다.
        """
        try:
            self._c.execution_store.save_playbook_revision(
                request.rca_id,
                request.engine,
                playbook,
                execution_id=execution_id,
            )
        except Exception as exc:
            raise RuntimeError("playbook revision staging failed") from exc

        try:
            indexed = self._c.playbook_store.save_to_s3_vectors(
                playbook,
                request.rca_id,
                metric_name=target.metric_name,
                publication_id=execution_id,
            )
        except Exception as exc:
            raise RuntimeError("playbook vector publication failed") from exc
        if not indexed:
            raise RuntimeError("playbook vector publication did not persist the update")

        try:
            self._c.execution_store.publish_playbook_revision(
                request.rca_id,
                request.engine,
                playbook,
                execution_id=execution_id,
            )
        except Exception as exc:
            raise RuntimeError("playbook revision commit failed") from exc
