from __future__ import annotations

import json
import re
import signal
import sys
import time
import uuid
from pathlib import Path

import boto3
import structlog

from cc_headless.alarm_parser import parse_alarm
from cc_headless.cc_runner import run_claude
from cc_headless.config import SQS_POLL_WAIT_SECONDS, SQS_QUEUE_URL
from cc_headless.healthz import start_health_server
from cc_headless.logging import setup_logging
from cc_headless.prompt_builder import build_prompt
from cc_headless.report_store import save_report, send_notification
from cc_headless.session_store import (
    check_duplicate,
    create_session,
    mark_completed,
    mark_failed,
    write_idempotency_key,
)

logger = structlog.get_logger()

_running = True
_SESSION_ID_PATH = Path("/tmp/rca-session-id")


def _write_session_id(rca_id: str) -> None:
    _SESSION_ID_PATH.write_text(rca_id)


def _handle_signal(signum, _frame):
    global _running  # noqa: PLW0603
    logger.info("shutdown_signal_received", signal=signum)
    _running = False


def _parse_sns_envelope(body: str) -> dict:
    parsed = json.loads(body)
    if isinstance(parsed.get("Message"), str):
        return json.loads(parsed["Message"])
    return parsed


def _run_rca(
    rca_id: str,
    alarm_data: dict,
    idempotency_key: str,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    start_time = time.time()
    alarm = parse_alarm(alarm_data)

    try:
        _write_session_id(rca_id)
        prompt = build_prompt(alarm)
        log.info("cc_analysis_started")

        cc_result = run_claude(prompt)
        elapsed_seconds = int(time.time() - start_time)

        if not cc_result.success:
            log.error("cc_analysis_failed", error=cc_result.result, raw_output=cc_result.raw_output[:3000])
            mark_failed(rca_id, cc_result.result)
            return False

        log.info("cc_analysis_completed", elapsed_seconds=elapsed_seconds)

        report_markdown = cc_result.result
        report_key = save_report(rca_id, report_markdown)

        match = re.search(r"## Root Cause\n+(.+)", report_markdown)
        root_cause_line = match.group(1) if match else report_markdown[:200]

        mark_completed(rca_id, root_cause_line)
        write_idempotency_key(idempotency_key, rca_id)
        send_notification(rca_id, alarm.alarm_name, root_cause_line, report_key, elapsed_seconds)

        log.info("rca_complete", elapsed_seconds=elapsed_seconds, root_cause=root_cause_line[:200])
        return True
    except Exception:
        log.exception("pipeline_failed")
        mark_failed(rca_id, "Unhandled pipeline exception")
        return False


def _process_message(message_body: str) -> bool:
    alarm_data = _parse_sns_envelope(message_body)
    alarm = parse_alarm(alarm_data)
    idempotency_key = f"{alarm.alarm_name}#{alarm.state_change_time or 'unknown'}"

    log = logger.bind(alarm_name=alarm.alarm_name, idempotency_key=idempotency_key)
    log.info("alarm_received")

    if check_duplicate(idempotency_key):
        log.info("duplicate_alarm_skipped")
        return True

    rca_id = str(uuid.uuid4())
    log = log.bind(rca_id=rca_id)

    if not create_session(rca_id, alarm.alarm_name, idempotency_key, alarm_data=alarm_data):
        log.info("session_already_exists")
        return True

    return _run_rca(rca_id, alarm_data, idempotency_key, log)


def main() -> None:
    setup_logging()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not SQS_QUEUE_URL:
        logger.error("sqs_queue_url_missing")
        sys.exit(1)

    start_health_server()
    logger.info("health_server_started", port=8080)

    sqs = boto3.client("sqs")
    logger.info("sqs_polling_started", queue_url=SQS_QUEUE_URL)

    while _running:
        try:
            resp = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=SQS_POLL_WAIT_SECONDS,
            )
        except Exception:
            logger.exception("sqs_receive_failed")
            time.sleep(5)
            continue

        messages = resp.get("Messages", [])
        if not messages:
            continue

        for msg in messages:
            try:
                success = _process_message(msg.get("Body", "{}"))
            except Exception:
                logger.exception("message_processing_failed")
                success = False

            if success:
                sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

    logger.info("shutdown_complete")


if __name__ == "__main__":
    main()
