from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from threading import Event

import structlog

from codex_headless.config.settings import (
    ACTIVE_INCIDENT_OK_COOLDOWN_SECONDS,
    ALARM_STALENESS_SECONDS,
    PLAYBOOK_UPDATE_THRESHOLD,
    SIDE_EFFECT_LEASE_SECONDS,
)
from codex_headless.di.container import Container
from codex_headless.ports.dto.models import AlarmContext, parse_alarm
from codex_headless.ports.interfaces.session_store import (
    ClaimDisposition,
    IncidentAlarm,
    IncidentClaimDisposition,
    SessionCancelledError,
    SessionOwnershipCheckError,
)
from codex_headless.services.artifact_validation import ArtifactValidationError, validate_completion_artifacts
from codex_headless.services.artifact_watcher import start_watcher
from codex_headless.services.execution_context import ExecutionContext
from codex_headless.services.playbook_merge import (
    PLAYBOOK_DRAFT,
    VERIFICATION_STATUS_FIELD,
    merge_playbook_update,
    normalize_verification_status,
)
from codex_headless.services.prompt_builder import build_prompt
from codex_headless.utils.embed_key import build_embed_key

logger = structlog.get_logger()
_ROOT_CAUSE_SECTION = re.compile(
    r"^## (?:근본 원인|Root Cause)\s*\n(?P<body>.*?)(?=^## |\Z)",
    re.MULTILINE | re.DOTALL,
)
_ROOT_CAUSE_METADATA_PREFIXES = (
    "상태:",
    "status:",
    "신뢰도:",
    "confidence:",
    "확정 가설",
    "confirmed hypothesis",
    "가설 제목:",
    "hypothesis title:",
)


def extract_root_cause(report_markdown: str) -> str:
    match = _ROOT_CAUSE_SECTION.search(report_markdown)
    if not match:
        return report_markdown[:200].strip()

    for raw_line in match.group("body").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("|") or line.startswith("#"):
            continue
        normalized = line.strip("*_`> ").lower()
        if normalized.startswith(_ROOT_CAUSE_METADATA_PREFIXES):
            continue
        return line.removeprefix(">").strip().strip("*_` ")
    return report_markdown[:200].strip()


def parse_sns_envelope(body: str) -> dict:
    parsed = json.loads(body)
    if isinstance(parsed.get("Message"), str):
        return json.loads(parsed["Message"])
    return parsed


def should_process(alarm_data: dict) -> bool:
    if not alarm_data.get("AlarmName"):
        return False
    return alarm_data.get("NewStateValue", "ALARM") == "ALARM"


class PipelineOrchestrator:
    def __init__(self, container: Container, shutdown_event: Event | None = None):
        self._c = container
        self._shutdown_event = shutdown_event or Event()

    def process_message(self, message_body: str, *, receive_count: int = 1) -> bool:
        from codex_headless.adapters.secondary.session.dynamodb_session_store import (
            build_idempotency_key,
            build_rca_id,
        )

        alarm_data = parse_sns_envelope(message_body)

        if not alarm_data.get("AlarmName"):
            logger.info(
                "skipping_non_alarm_message",
                alarm_name=alarm_data.get("AlarmName"),
                new_state_value=alarm_data.get("NewStateValue"),
            )
            return True

        alarm = parse_alarm(alarm_data)

        ts_raw = alarm.state_change_time
        dt = datetime.fromisoformat(ts_raw.replace("+0000", "+00:00")) if ts_raw else None
        incident_alarm = IncidentAlarm(
            alarm_name=alarm.alarm_name,
            alarm_arn=alarm_data.get("AlarmArn") or None,
            region=alarm.region,
            state_change_time=dt,
            new_state=alarm_data.get("NewStateValue", "ALARM"),
        )
        store = self._c.session_store
        if incident_alarm.new_state == "OK":
            try:
                recorded = store.record_recovery(incident_alarm)
            except Exception:
                logger.exception("alarm_recovery_record_failed", alarm_name=alarm.alarm_name)
                return False
            if not recorded:
                logger.error("alarm_recovery_store_unavailable", alarm_name=alarm.alarm_name)
                return False
            logger.info("alarm_recovery_recorded", alarm_name=alarm.alarm_name)
            return True

        if not should_process(alarm_data):
            logger.info(
                "skipping_non_alarm_message",
                alarm_name=alarm.alarm_name,
                new_state_value=alarm_data.get("NewStateValue"),
            )
            return True

        idempotency_key = build_idempotency_key(incident_alarm)

        rca_id = build_rca_id(idempotency_key)
        log = logger.bind(alarm_name=alarm.alarm_name, idempotency_key=idempotency_key, rca_id=rca_id)
        log.info("alarm_received")

        effective_receive_count = max(receive_count, 1)
        if ts_raw and effective_receive_count == 1:
            age_seconds = (datetime.now(UTC) - dt).total_seconds()
            if age_seconds > ALARM_STALENESS_SECONDS:
                claim_token = store.claim_session(
                    rca_id,
                    alarm.alarm_name,
                    idempotency_key,
                    receive_count=effective_receive_count,
                    alarm_data=alarm_data,
                )
                if claim_token.disposition is ClaimDisposition.TERMINAL_DUPLICATE:
                    log.info("terminal_duplicate_acknowledged", receive_count=receive_count)
                    return True
                if not claim_token.acquired:
                    log.info("session_claim_contended", receive_count=receive_count)
                    return False
                log.info(
                    "stale_alarm_skipped",
                    age_seconds=int(age_seconds),
                    threshold=ALARM_STALENESS_SECONDS,
                )
                store.mark_outdated(
                    rca_id,
                    f"Alarm age {int(age_seconds)}s exceeds {ALARM_STALENESS_SECONDS}s threshold",
                    claim_token=claim_token.claim_token,
                )
                return True

        try:
            incident_claim = store.claim_incident(
                incident_alarm,
                cooldown_seconds=ACTIVE_INCIDENT_OK_COOLDOWN_SECONDS,
            )
        except Exception:
            log.exception("active_incident_claim_failed")
            return False
        if incident_claim.disposition is IncidentClaimDisposition.SUPPRESSED:
            log.info(
                "active_incident_suppressed",
                candidate_rca_id=incident_claim.candidate_rca_id,
                reason=incident_claim.reason,
                retryable=incident_claim.retryable,
            )
            return not incident_claim.retryable
        if not incident_claim.acquired or incident_claim.candidate_rca_id != rca_id:
            log.info("active_incident_claim_contended")
            return False

        claim_token = store.claim_session(
            rca_id,
            alarm.alarm_name,
            idempotency_key,
            receive_count=effective_receive_count,
            alarm_data=alarm_data,
        )
        if claim_token.disposition is ClaimDisposition.TERMINAL_DUPLICATE:
            log.info("terminal_duplicate_acknowledged", receive_count=receive_count)
            return True
        if not claim_token.acquired:
            log.info("session_claim_contended", receive_count=receive_count)
            return False

        return self._run_rca(
            rca_id,
            alarm_data,
            log,
            claim_token.claim_token,
            attempt=claim_token.attempt or receive_count,
        )

    def _run_rca(
        self,
        rca_id: str,
        alarm_data: dict,
        log: structlog.stdlib.BoundLogger,
        claim_token: str,
        *,
        attempt: int = 1,
    ) -> bool:
        from codex_headless.adapters.secondary.session.dynamodb_session_store import (
            InvalidStateTransitionError,
        )

        c = self._c
        store = c.session_store
        start_time = time.time()
        alarm = parse_alarm(alarm_data)
        execution = ExecutionContext.create(rca_id)
        artifact_dir = execution.prepare()
        watcher_thread = None
        watcher_stop = None
        side_effect_lease_token = None
        ownership_check_failed = Event()

        try:
            store.update_state(rca_id, "ANALYZING", claim_token=claim_token)
            prompt = build_prompt(alarm)
            log.info("cc_analysis_started")

            watcher_thread, watcher_stop = start_watcher(
                artifact_dir,
                rca_id,
                claim_token,
                c.dynamodb_client,
            )

            def _should_cancel() -> bool:
                if self._shutdown_event.is_set():
                    return True
                try:
                    return store.is_terminated(rca_id, claim_token=claim_token)
                except SessionOwnershipCheckError:
                    ownership_check_failed.set()
                    return True

            codex_result = c.codex_runner.run(
                prompt,
                execution_token=execution.token,
                cancel_checker=_should_cancel,
                rca_id=rca_id,
                claim_token=claim_token,
                attempt=attempt,
            )
            elapsed_seconds = int(time.time() - start_time)

            watcher_stop.set()
            watcher_thread.join(timeout=10)
            watcher_stop = None
            watcher_thread = None

            if ownership_check_failed.is_set():
                log.error("ownership_check_failed_during_execution")
                return False

            if self._shutdown_event.is_set():
                log.info("session_aborted_on_shutdown", elapsed_seconds=elapsed_seconds)
                store.mark_failed(
                    rca_id,
                    "Aborted due to SIGTERM shutdown",
                    claim_token=claim_token,
                )
                return False

            if store.is_terminated(rca_id, claim_token=claim_token):
                log.info("session_terminated_after_cc", elapsed_seconds=elapsed_seconds)
                return False

            if not codex_result.success:
                log.error(
                    "codex_analysis_failed",
                    error=codex_result.result,
                    raw_output=codex_result.raw_output[:3000],
                )
                store.mark_failed(rca_id, codex_result.result, claim_token=claim_token)
                return False

            log.info("cc_analysis_completed", elapsed_seconds=elapsed_seconds)

            try:
                artifacts = validate_completion_artifacts(artifact_dir)
            except ArtifactValidationError as exc:
                log.error("completion_artifact_validation_failed", detail=str(exc))
                store.mark_failed(
                    rca_id,
                    f"Completion artifact validation failed: {exc}",
                    claim_token=claim_token,
                )
                return False

            root_cause_line = extract_root_cause(artifacts.report_markdown)
            side_effect_lease_token = store.acquire_side_effect_lease(
                rca_id,
                claim_token=claim_token,
                effect_name="final-publication",
                lease_seconds=SIDE_EFFECT_LEASE_SECONDS,
            )
            self._process_playbook(artifacts.playbook, rca_id, alarm, log)
            report_key = c.report_store.save_report(
                rca_id,
                artifacts.report_markdown,
                claim_token=claim_token,
                attempt=attempt,
            )
            if not isinstance(report_key, str) or not report_key.strip():
                raise RuntimeError("Report persistence returned no S3 key")
            c.report_store.send_notification(
                rca_id,
                alarm.alarm_name,
                root_cause_line,
                report_key,
                elapsed_seconds,
                playbook=artifacts.playbook,
                confirmed=artifacts.confirmed,
                alarm_context=alarm,
            )
            store.mark_completed(
                rca_id,
                root_cause_line,
                report_key,
                playbook=artifacts.playbook,
                confirmed=artifacts.confirmed,
                claim_token=claim_token,
                side_effect_lease_token=side_effect_lease_token,
            )
            side_effect_lease_token = None

            log.info("rca_complete", elapsed_seconds=elapsed_seconds, root_cause=root_cause_line[:200])
            return True
        except SessionCancelledError:
            log.info("session_cancelled_during_state_update")
            return False
        except SessionOwnershipCheckError:
            log.exception("session_ownership_check_failed")
            return False
        except InvalidStateTransitionError as e:
            log.error("invalid_state_transition", detail=str(e))
            return False
        except Exception:
            log.exception("pipeline_failed")
            if side_effect_lease_token is not None:
                try:
                    store.release_side_effect_lease(
                        rca_id,
                        claim_token=claim_token,
                        lease_token=side_effect_lease_token,
                    )
                except Exception:
                    log.exception("side_effect_lease_release_failed")
                side_effect_lease_token = None
            try:
                store.mark_failed(
                    rca_id,
                    "Unhandled pipeline exception",
                    claim_token=claim_token,
                )
            except Exception:
                log.exception("mark_failed_after_pipeline_error_failed")
            return False
        finally:
            if watcher_stop is not None:
                watcher_stop.set()
            if watcher_thread is not None:
                watcher_thread.join(timeout=10)
            execution.cleanup()

    def _process_playbook(
        self,
        playbook: dict,
        rca_id: str,
        alarm: AlarmContext,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        metric_name = alarm.metric_name or ""
        recorded = self._merge_into_existing(playbook, metric_name, log)
        saved = self._c.playbook_store.save_to_s3_vectors(recorded, rca_id, metric_name=metric_name)
        if not saved:
            raise RuntimeError("Playbook persistence failed")
        log.info("playbook_saved", playbook_id=recorded.get("playbook_id"))

    def _merge_into_existing(
        self,
        playbook: dict,
        metric_name: str,
        log: structlog.stdlib.BoundLogger,
    ) -> dict:
        """같은 유형의 기존 플레이북이 있으면 그것을 보강한다.

        새 식별자로 분기하면 같은 증상의 플레이북이 여럿이 되어 어느 것이 최신인지 알 수
        없고, 회고가 쌓아 온 검증된 절차가 다음 실행의 근거가 되지 못한다. 그래서 충분히
        닮은 플레이북을 찾으면 그 식별자를 유지한 채 병합한다.

        검색·병합 실패는 분석을 중단시키지 않는다. 플레이북은 미래를 위한 자산이고 이번
        RCA 의 결과물은 리포트이므로, 자산 축적 실패가 결과 전달을 막아서는 안 된다.
        """
        store = self._c.playbook_store
        query = build_embed_key(
            failure_type=str(playbook.get("failure_type", "")),
            symptom=str(playbook.get("symptom_pattern", "")),
            metric_name=metric_name,
        )
        if not query:
            return playbook

        try:
            hits = store.search_similar(query, threshold=PLAYBOOK_UPDATE_THRESHOLD)
        except Exception:
            log.exception("playbook_search_failed")
            return playbook

        for hit in hits:
            try:
                existing = store.load_detail(hit)
            except Exception:
                log.exception("playbook_detail_load_failed", playbook_id=hit.playbook_id)
                continue
            if existing is None:
                # 기존 절차를 보지 못한 상태의 "보강"은 같은 식별자로 과거 내용을 덮어써
                # 축적을 되돌린다. 그 후보는 건너뛰고 신규 생성으로 떨어지는 편이 안전하다.
                log.info("playbook_merge_skipped_no_detail", playbook_id=hit.playbook_id)
                continue

            merged, diff = merge_playbook_update(existing, playbook)
            merged["playbook_id"] = hit.playbook_id
            procedures_unchanged = merged.get("execution_steps") == existing.get("execution_steps")
            merged[VERIFICATION_STATUS_FIELD] = (
                normalize_verification_status(existing.get(VERIFICATION_STATUS_FIELD))
                if procedures_unchanged
                else PLAYBOOK_DRAFT
            )
            log.info(
                "playbook_merged_into_existing",
                playbook_id=hit.playbook_id,
                similarity=round(hit.similarity, 3),
                changed_fields=len(diff.changed_fields),
                corrected_steps=len(diff.corrected_steps),
                added_steps=len(diff.added_steps),
                preserved_steps=len(diff.preserved_steps),
                procedures_unchanged=procedures_unchanged,
            )
            return merged

        return playbook
