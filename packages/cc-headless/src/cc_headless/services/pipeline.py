from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from threading import Event

import structlog

from cc_headless.config.settings import ALARM_STALENESS_SECONDS, SIDE_EFFECT_LEASE_SECONDS
from cc_headless.di.container import Container
from cc_headless.ports.dto.models import AlarmContext, parse_alarm
from cc_headless.ports.interfaces.session_store import (
    ClaimDisposition,
    SessionCancelledError,
    SessionOwnershipCheckError,
)
from cc_headless.services.artifact_validation import ArtifactValidationError, validate_completion_artifacts
from cc_headless.services.artifact_watcher import start_watcher
from cc_headless.services.execution_context import ExecutionContext
from cc_headless.services.prompt_builder import build_prompt

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
        from cc_headless.adapters.secondary.session.dynamodb_session_store import build_rca_id

        alarm_data = parse_sns_envelope(message_body)

        if not should_process(alarm_data):
            logger.info(
                "skipping_non_alarm_message",
                alarm_name=alarm_data.get("AlarmName"),
                new_state_value=alarm_data.get("NewStateValue"),
            )
            return True

        alarm = parse_alarm(alarm_data)

        ts_raw = alarm.state_change_time
        if ts_raw:
            dt = datetime.fromisoformat(ts_raw.replace("+0000", "+00:00"))
            ts = dt.isoformat()
        else:
            ts = "unknown"
        idempotency_key = f"{alarm.alarm_name}#{ts}"

        rca_id = build_rca_id(idempotency_key)
        log = logger.bind(alarm_name=alarm.alarm_name, idempotency_key=idempotency_key, rca_id=rca_id)
        log.info("alarm_received")

        store = self._c.session_store

        effective_receive_count = max(receive_count, 1)
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

        if ts_raw and effective_receive_count == 1:
            dt = datetime.fromisoformat(ts_raw.replace("+0000", "+00:00"))
            age_seconds = (datetime.now(UTC) - dt).total_seconds()
            if age_seconds > ALARM_STALENESS_SECONDS:
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
        from cc_headless.adapters.secondary.session.dynamodb_session_store import (
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

            cc_result = c.cc_runner.run(
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

            if not cc_result.success:
                log.error("cc_analysis_failed", error=cc_result.result, raw_output=cc_result.raw_output[:3000])
                store.mark_failed(rca_id, cc_result.result, claim_token=claim_token)
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
        try:
            metric_name = alarm.metric_name or ""
            self._c.playbook_store.save_to_s3_vectors(playbook, rca_id, metric_name=metric_name)
            log.info("playbook_saved", playbook_id=playbook.get("playbook_id"))
        except Exception:
            log.exception("playbook_processing_failed")
