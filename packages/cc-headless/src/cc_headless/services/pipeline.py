from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog

from cc_headless.config.settings import ALARM_STALENESS_SECONDS
from cc_headless.di.container import Container
from cc_headless.ports.dto.models import AlarmContext, parse_alarm
from cc_headless.services.artifact_watcher import start_watcher
from cc_headless.services.prompt_builder import build_prompt

logger = structlog.get_logger()

_SESSION_ID_PATH = Path("/tmp/rca-session-id")


def _write_session_id(rca_id: str) -> None:
    _SESSION_ID_PATH.write_text(rca_id)


def _prepare_artifact_dir(rca_id: str) -> Path:
    d = Path(f"/tmp/rca-{rca_id}")
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    def __init__(self, container: Container):
        self._c = container

    def process_message(self, message_body: str) -> bool:
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

        if store.check_duplicate(rca_id):
            log.info("duplicate_alarm_skipped")
            return True

        if ts_raw:
            dt = datetime.fromisoformat(ts_raw.replace("+0000", "+00:00"))
            age_seconds = (datetime.now(UTC) - dt).total_seconds()
            if age_seconds > ALARM_STALENESS_SECONDS:
                log.info(
                    "stale_alarm_skipped",
                    age_seconds=int(age_seconds),
                    threshold=ALARM_STALENESS_SECONDS,
                )
                if store.create_session(rca_id, alarm.alarm_name, idempotency_key, alarm_data=alarm_data):
                    store.mark_outdated(
                        rca_id,
                        f"Alarm age {int(age_seconds)}s exceeds {ALARM_STALENESS_SECONDS}s threshold",
                    )
                return True

        if not store.create_session(rca_id, alarm.alarm_name, idempotency_key, alarm_data=alarm_data):
            log.info("session_already_exists")
            return True

        return self._run_rca(rca_id, alarm_data, log)

    def _run_rca(
        self,
        rca_id: str,
        alarm_data: dict,
        log: structlog.stdlib.BoundLogger,
    ) -> bool:
        from cc_headless.adapters.secondary.session.dynamodb_session_store import (
            InvalidStateTransitionError,
            SessionCancelledError,
        )

        c = self._c
        store = c.session_store
        start_time = time.time()
        alarm = parse_alarm(alarm_data)
        artifact_dir = _prepare_artifact_dir(rca_id)

        try:
            _write_session_id(rca_id)
            store.update_state(rca_id, "ANALYZING")
            prompt = build_prompt(alarm)
            log.info("cc_analysis_started")

            watcher_thread, watcher_stop = start_watcher(artifact_dir, rca_id, c.dynamodb_client)

            cc_result = c.cc_runner.run(prompt, cancel_checker=lambda: store.is_terminated(rca_id))
            elapsed_seconds = int(time.time() - start_time)

            watcher_stop.set()
            watcher_thread.join(timeout=10)

            if store.is_terminated(rca_id):
                log.info("session_terminated_after_cc", elapsed_seconds=elapsed_seconds)
                return True

            if not cc_result.success:
                log.error("cc_analysis_failed", error=cc_result.result, raw_output=cc_result.raw_output[:3000])
                store.mark_failed(rca_id, cc_result.result)
                return False

            log.info("cc_analysis_completed", elapsed_seconds=elapsed_seconds)

            report_path = artifact_dir / "report.md"
            report_markdown = report_path.read_text() if report_path.exists() else cc_result.result

            report_key = c.report_store.save_report(rca_id, report_markdown)

            match = re.search(r"## 근본 원인\n+(.+)", report_markdown)
            if not match:
                match = re.search(r"## Root Cause\n+(.+)", report_markdown)
            root_cause_line = match.group(1) if match else report_markdown[:200]

            playbook = self._process_playbook(artifact_dir, rca_id, alarm, log)

            store.mark_completed(rca_id, root_cause_line)
            c.report_store.send_notification(
                rca_id,
                alarm.alarm_name,
                root_cause_line,
                report_key,
                elapsed_seconds,
                playbook=playbook,
            )

            log.info("rca_complete", elapsed_seconds=elapsed_seconds, root_cause=root_cause_line[:200])
            return True
        except SessionCancelledError:
            log.info("session_cancelled_during_state_update")
            return True
        except InvalidStateTransitionError as e:
            log.error("invalid_state_transition", detail=str(e))
            return False
        except Exception:
            log.exception("pipeline_failed")
            store.mark_failed(rca_id, "Unhandled pipeline exception")
            return False

    def _process_playbook(
        self,
        artifact_dir: Path,
        rca_id: str,
        alarm: AlarmContext,
        log: structlog.stdlib.BoundLogger,
    ) -> dict | None:
        try:
            playbook = self._c.playbook_store.load_playbook(artifact_dir)
            if not playbook:
                log.info("playbook_not_generated")
                return None
            metric_name = alarm.metric_name or ""
            self._c.playbook_store.save_to_s3_vectors(playbook, rca_id, metric_name=metric_name)
            log.info("playbook_saved", playbook_id=playbook.get("playbook_id"))
            return playbook
        except Exception:
            log.exception("playbook_processing_failed")
            return None
