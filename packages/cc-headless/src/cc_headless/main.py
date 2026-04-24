from __future__ import annotations

import json
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import boto3
import structlog

from cc_headless.alarm_parser import parse_alarm
from cc_headless.artifact_watcher import start_watcher
from cc_headless.cc_runner import run_claude
from cc_headless.config import DYNAMODB_TABLE_NAME, ENGINE, SQS_POLL_WAIT_SECONDS, SQS_QUEUE_URL
from cc_headless.healthz import start_health_server
from cc_headless.logging import setup_logging
from cc_headless.prompt_builder import build_prompt
from cc_headless.report_store import save_report, send_notification
from cc_headless.session_store import (
    build_rca_id,
    check_duplicate,
    create_session,
    mark_completed,
    mark_failed,
    update_state,
)

logger = structlog.get_logger()

_running = True
_SESSION_ID_PATH = Path("/tmp/rca-session-id")
_CANCEL_CHECK_INTERVAL = 15


def _write_session_id(rca_id: str) -> None:
    _SESSION_ID_PATH.write_text(rca_id)


def _prepare_artifact_dir(rca_id: str) -> Path:
    d = Path(f"/tmp/rca-{rca_id}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _handle_signal(signum, _frame):
    global _running  # noqa: PLW0603
    logger.info("shutdown_signal_received", signal=signum)
    _running = False


def _parse_sns_envelope(body: str) -> dict:
    parsed = json.loads(body)
    if isinstance(parsed.get("Message"), str):
        return json.loads(parsed["Message"])
    return parsed


def _should_process(alarm_data: dict) -> bool:
    if not alarm_data.get("AlarmName"):
        return False
    return alarm_data.get("NewStateValue", "ALARM") == "ALARM"


def _is_cancelled(rca_id: str, ddb) -> bool:
    if not DYNAMODB_TABLE_NAME or not ddb:
        return False
    try:
        resp = ddb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": f"{ENGINE}#SESSION"},
            },
            ProjectionExpression="#st",
            ExpressionAttributeNames={"#st": "state"},
        )
        state = resp.get("Item", {}).get("state", {}).get("S", "")
        return state == "CANCELLED"
    except Exception:
        logger.exception("cancel_check_failed", rca_id=rca_id)
        return False


def _run_rca(
    rca_id: str,
    alarm_data: dict,
    log: structlog.stdlib.BoundLogger,
    ddb,
) -> bool:
    start_time = time.time()
    alarm = parse_alarm(alarm_data)
    artifact_dir = _prepare_artifact_dir(rca_id)

    try:
        _write_session_id(rca_id)
        update_state(rca_id, "ANALYZING")
        prompt = build_prompt(alarm)
        log.info("cc_analysis_started")

        watcher_thread, watcher_stop = start_watcher(artifact_dir, rca_id, ddb)

        cc_result = run_claude(prompt, cancel_checker=lambda: _is_cancelled(rca_id, ddb))
        elapsed_seconds = int(time.time() - start_time)

        watcher_stop.set()
        watcher_thread.join(timeout=5)

        if _is_cancelled(rca_id, ddb):
            log.info("session_cancelled_after_cc", elapsed_seconds=elapsed_seconds)
            return True

        if not cc_result.success:
            log.error("cc_analysis_failed", error=cc_result.result, raw_output=cc_result.raw_output[:3000])
            mark_failed(rca_id, cc_result.result)
            return False

        log.info("cc_analysis_completed", elapsed_seconds=elapsed_seconds)

        report_path = artifact_dir / "report.md"
        report_markdown = report_path.read_text() if report_path.exists() else cc_result.result

        report_key = save_report(rca_id, report_markdown)

        match = re.search(r"## 근본 원인\n+(.+)", report_markdown)
        if not match:
            match = re.search(r"## Root Cause\n+(.+)", report_markdown)
        root_cause_line = match.group(1) if match else report_markdown[:200]

        mark_completed(rca_id, root_cause_line)
        send_notification(rca_id, alarm.alarm_name, root_cause_line, report_key, elapsed_seconds)

        log.info("rca_complete", elapsed_seconds=elapsed_seconds, root_cause=root_cause_line[:200])
        return True
    except Exception:
        log.exception("pipeline_failed")
        mark_failed(rca_id, "Unhandled pipeline exception")
        return False


def _process_message(message_body: str, ddb) -> bool:
    alarm_data = _parse_sns_envelope(message_body)

    if not _should_process(alarm_data):
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

    if check_duplicate(rca_id):
        log.info("duplicate_alarm_skipped")
        return True

    if not create_session(rca_id, alarm.alarm_name, idempotency_key, alarm_data=alarm_data):
        log.info("session_already_exists")
        return True

    return _run_rca(rca_id, alarm_data, log, ddb)


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
    ddb = boto3.client("dynamodb") if DYNAMODB_TABLE_NAME else None
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
                success = _process_message(msg.get("Body", "{}"), ddb)
            except Exception:
                logger.exception("message_processing_failed")
                success = False

            if success:
                sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

    logger.info("shutdown_complete")


if __name__ == "__main__":
    main()
