from __future__ import annotations

import json
import logging
import re
import signal
import sys
import time
import uuid

import boto3

from cc_headless.alarm_parser import parse_alarm
from cc_headless.cc_runner import run_claude
from cc_headless.config import SQS_POLL_WAIT_SECONDS, SQS_QUEUE_URL
from cc_headless.healthz import start_health_server
from cc_headless.prompt_builder import build_prompt
from cc_headless.report_store import save_report, send_notification
from cc_headless.session_store import (
    check_duplicate,
    create_session,
    mark_completed,
    mark_failed,
    update_state,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

_running = True


def _handle_signal(signum, _frame):
    global _running  # noqa: PLW0603
    logger.info("Received signal %s, shutting down", signum)
    _running = False


def _parse_sns_envelope(body: str) -> dict:
    parsed = json.loads(body)
    if isinstance(parsed.get("Message"), str):
        return json.loads(parsed["Message"])
    return parsed


def _process_message(message_body: str) -> None:
    start_time = time.time()
    alarm_data = _parse_sns_envelope(message_body)
    alarm = parse_alarm(alarm_data)
    idempotency_key = f"{alarm.alarm_name}#{alarm.state_change_time or 'unknown'}"

    logger.info("Received alarm: %s, key: %s", alarm.alarm_name, idempotency_key)

    if check_duplicate(idempotency_key):
        logger.info("Duplicate alarm, skipping: %s", idempotency_key)
        return

    rca_id = str(uuid.uuid4())
    if not create_session(rca_id, alarm.alarm_name, idempotency_key):
        logger.info("Session already exists for: %s", idempotency_key)
        return

    try:
        update_state(rca_id, "ANALYZING")

        prompt = build_prompt(alarm)
        logger.info("Starting CC headless analysis for RCA %s", rca_id)

        cc_result = run_claude(prompt)
        elapsed_seconds = int(time.time() - start_time)

        if not cc_result.success:
            logger.error("CC headless failed: %s", cc_result.result)
            mark_failed(rca_id, cc_result.result)
            return

        logger.info("CC headless completed in %ds", elapsed_seconds)
        update_state(rca_id, "REPORT_GENERATION")

        report_markdown = cc_result.result
        report_key = save_report(rca_id, report_markdown)

        match = re.search(r"## Root Cause\n+(.+)", report_markdown)
        root_cause_line = match.group(1) if match else report_markdown[:200]

        mark_completed(rca_id, root_cause_line)
        send_notification(rca_id, alarm.alarm_name, root_cause_line, report_key, elapsed_seconds)

        logger.info("RCA complete: rca_id=%s, elapsed=%ds", rca_id, elapsed_seconds)
    except Exception:
        logger.exception("Pipeline failed for %s", alarm.alarm_name)
        mark_failed(rca_id, "Unhandled pipeline exception")


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not SQS_QUEUE_URL:
        logger.error("SQS_QUEUE_URL is not set")
        sys.exit(1)

    start_health_server()
    logger.info("Health server started on port 8080")

    sqs = boto3.client("sqs")
    logger.info("Starting SQS long polling: %s", SQS_QUEUE_URL)

    while _running:
        try:
            resp = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=SQS_POLL_WAIT_SECONDS,
            )
        except Exception:
            logger.exception("Failed to receive SQS message")
            time.sleep(5)
            continue

        messages = resp.get("Messages", [])
        if not messages:
            continue

        for msg in messages:
            try:
                _process_message(msg.get("Body", "{}"))
            except Exception:
                logger.exception("Failed to process message")
            finally:
                sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
