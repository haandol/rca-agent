from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time

import boto3

from rca_agent.agent_factory import (
    create_aws_knowledge_mcp_client,
    create_branching_agent,
    create_cloudtrail_mcp_client,
    create_cloudwatch_mcp_client,
    create_evidence_collection_agent,
    create_github_mcp_client,
    create_hypothesis_generation_agent,
    create_playbook_agent,
    create_prioritization_agent,
    create_report_agent,
    create_scoping_agent,
    create_validation_agent,
    create_verification_agent,
)
from rca_agent.branching import run_branching
from rca_agent.config import (
    DYNAMODB_TABLE_NAME,
    GITHUB_PERSONAL_ACCESS_TOKEN,
    HEALTHCARE_ECS_CLUSTER,
    HEALTHCARE_ECS_SERVICE,
    HEALTHCARE_SERVICE_HOST,
    RCA_MAX_REGENERATION_ROUNDS,
    REMEDIATION_ENABLED,
    S3_REPORT_BUCKET,
    S3_VECTOR_BUCKET_NAME,
    SNS_NOTIFICATION_TOPIC_ARN,
)
from rca_agent.evidence import run_evidence_collection, save_evidence_to_s3
from rca_agent.healthz import start_health_server
from rca_agent.hypothesis import run_hypothesis_generation
from rca_agent.models import AlarmPayload, HypothesisStatus, RcaSessionState
from rca_agent.notification import build_notification, send_notification
from rca_agent.playbook_gen import run_playbook_generation, save_playbook_to_s3_vectors
from rca_agent.prioritization import run_prioritization
from rca_agent.remediation import execute_remediation
from rca_agent.report import run_report_generation, save_report_to_s3
from rca_agent.scoping import run_scoping
from rca_agent.session_store import (
    check_duplicate,
    create_session,
    mark_completed,
    mark_failed,
    update_state,
)
from rca_agent.termination import check_termination
from rca_agent.validation import run_validation
from rca_agent.verification import run_verification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
POLL_WAIT_SECONDS = int(os.environ.get("SQS_POLL_WAIT_SECONDS", "20"))

_running = True


def _handle_signal(signum, _frame):
    global _running  # noqa: PLW0603
    logger.info("Received signal %s, shutting down", signum)
    _running = False


def _parse_sns_envelope(body: dict) -> dict:
    if "Message" in body and isinstance(body["Message"], str):
        return json.loads(body["Message"])
    return body


def _create_s3_vectors_client():
    if not S3_VECTOR_BUCKET_NAME:
        return None
    return boto3.client("s3vectors")


def _create_s3_client():
    if not S3_REPORT_BUCKET:
        return None
    return boto3.client("s3")


def _create_sns_client():
    if not SNS_NOTIFICATION_TOPIC_ARN:
        return None
    return boto3.client("sns")


def _create_dynamodb_client():
    if not DYNAMODB_TABLE_NAME:
        return None
    return boto3.client("dynamodb")


class _Agents:
    def __init__(self, mcp_clients=None, evidence_mcp_clients=None):
        self._mcp_clients = mcp_clients
        self._evidence_mcp_clients = evidence_mcp_clients or mcp_clients
        self._scoping = None
        self._hypothesis = None
        self._prioritization = None
        self._evidence = None
        self._validation = None
        self._branching = None
        self._report = None
        self._playbook = None
        self._verification = None

    @property
    def scoping(self):
        if self._scoping is None:
            self._scoping = create_scoping_agent(mcp_clients=self._mcp_clients)
        return self._scoping

    @property
    def hypothesis(self):
        if self._hypothesis is None:
            self._hypothesis = create_hypothesis_generation_agent()
        return self._hypothesis

    @property
    def prioritization(self):
        if self._prioritization is None:
            self._prioritization = create_prioritization_agent()
        return self._prioritization

    @property
    def evidence(self):
        if self._evidence is None:
            self._evidence = create_evidence_collection_agent(mcp_clients=self._evidence_mcp_clients)
        return self._evidence

    @property
    def validation(self):
        if self._validation is None:
            self._validation = create_validation_agent()
        return self._validation

    @property
    def branching(self):
        if self._branching is None:
            self._branching = create_branching_agent()
        return self._branching

    @property
    def report(self):
        if self._report is None:
            self._report = create_report_agent()
        return self._report

    @property
    def playbook(self):
        if self._playbook is None:
            self._playbook = create_playbook_agent()
        return self._playbook

    @property
    def post_remediation_verification(self):
        if self._verification is None:
            self._verification = create_verification_agent(mcp_clients=self._mcp_clients)
        return self._verification


def _process_alarm(
    body: dict,
    agents: _Agents,
    *,
    s3_vectors_client=None,
    s3_client=None,
    sns_client=None,
    dynamodb_client=None,
) -> None:
    start_time = time.monotonic()
    alarm_data = _parse_sns_envelope(body)
    alarm = AlarmPayload.from_cloudwatch_sns(alarm_data)
    logger.info(
        "Parsed alarm: name=%s, resource=%s, service=%s",
        alarm.alarm_name,
        alarm.resource_id,
        alarm.service_name,
    )

    # Idempotency check
    if check_duplicate(alarm, dynamodb_client=dynamodb_client):
        logger.info("Skipping duplicate alarm: %s", alarm.alarm_name)
        return

    # Create DynamoDB session
    session = create_session(alarm, dynamodb_client=dynamodb_client)
    rca_id = session.rca_id if session else ""

    try:
        _run_pipeline(
            alarm,
            agents,
            rca_id=rca_id,
            start_time=start_time,
            s3_vectors_client=s3_vectors_client,
            s3_client=s3_client,
            sns_client=sns_client,
            dynamodb_client=dynamodb_client,
        )
    except Exception:
        logger.exception("Pipeline failed for alarm %s", alarm.alarm_name)
        if rca_id:
            mark_failed(rca_id, error_reason="Unhandled pipeline exception", dynamodb_client=dynamodb_client)


def _run_pipeline(
    alarm: AlarmPayload,
    agents: _Agents,
    *,
    rca_id: str,
    start_time: float,
    s3_vectors_client=None,
    s3_client=None,
    sns_client=None,
    dynamodb_client=None,
) -> None:
    # F1: Scoping
    update_state(rca_id, RcaSessionState.SCOPING, dynamodb_client=dynamodb_client)
    scoping_result = run_scoping(
        alarm,
        agents.scoping,
        s3_vectors_client=s3_vectors_client,
    )
    logger.info(
        "Scoping: severity=%s, blast_radius=%s, playbooks=%d",
        scoping_result.initial_severity,
        scoping_result.blast_radius,
        len(scoping_result.similar_playbooks),
    )

    # F2: Hypothesis generation (with regeneration loop on all-rejected)
    update_state(rca_id, RcaSessionState.HYPOTHESIS_GENERATION, dynamodb_client=dynamodb_client)
    hypothesis_result = run_hypothesis_generation(
        scoping_result,
        agents.hypothesis,
    )
    hypotheses = list(hypothesis_result.hypotheses)
    if not hypotheses:
        logger.error("No hypotheses generated, aborting RCA")
        mark_failed(rca_id, error_reason="No hypotheses generated", dynamodb_client=dynamodb_client)
        return

    all_judgments = []
    rejected_descriptions: list[str] = []
    validation_loop_count = 0
    regeneration_count = 0
    timeline: list[str] = []
    evidence_map: dict[str, str] = {}

    timeline.append(f"Alarm received: {alarm.alarm_name}")
    timeline.append(f"Scoping complete: severity={scoping_result.initial_severity}")
    timeline.append(f"Initial hypotheses: {len(hypotheses)}")

    termination = None

    while True:
        validation_loop_count += 1
        logger.info("Validation loop %d, hypotheses=%d", validation_loop_count, len(hypotheses))

        # F3: Prioritization
        update_state(rca_id, RcaSessionState.HYPOTHESIS_PRIORITIZATION, dynamodb_client=dynamodb_client)
        run_prioritization(
            scoping_result,
            hypotheses,
            agents.prioritization,
        )

        # F4: Evidence collection
        update_state(rca_id, RcaSessionState.EVIDENCE_COLLECTION, dynamodb_client=dynamodb_client)
        new_hypotheses = [h for h in hypotheses if h.hypothesis_id not in evidence_map]
        if new_hypotheses:
            new_evidence = run_evidence_collection(
                new_hypotheses,
                scoping_result,
                agents.evidence,
            )
            evidence_map.update(new_evidence)
            save_evidence_to_s3(rca_id, new_evidence, s3_client=s3_client)
        timeline.append(
            f"Loop {validation_loop_count}: collected evidence for {len(new_hypotheses)} hypotheses",
        )

        # F5: Validation
        update_state(rca_id, RcaSessionState.HYPOTHESIS_VALIDATION, dynamodb_client=dynamodb_client)
        validation_result = run_validation(
            hypotheses,
            evidence_map,
            agents.validation,
        )
        all_judgments = validation_result.judgments
        timeline.append(
            f"Loop {validation_loop_count}: validated {len(all_judgments)} hypotheses",
        )

        # Track rejected descriptions for branching dedup
        for j in all_judgments:
            if j.status == HypothesisStatus.REJECTED:
                h = next((h for h in hypotheses if h.hypothesis_id == j.hypothesis_id), None)
                if h and h.description not in rejected_descriptions:
                    rejected_descriptions.append(h.description)

        # F6: Termination check
        termination = check_termination(
            judgments=all_judgments,
            hypotheses=hypotheses,
            start_time=start_time,
            validation_loop_count=validation_loop_count,
        )
        if termination.should_terminate:
            logger.info("Termination: %s", termination.reason)
            timeline.append(f"Terminated: {termination.reason}")
            break

        # All rejected → regeneration loop (ADR 0004)
        if validation_result.all_rejected:
            regeneration_count += 1
            if regeneration_count > RCA_MAX_REGENERATION_ROUNDS:
                logger.warning("Max regeneration rounds exceeded")
                timeline.append("Max regeneration rounds exceeded")
                break
            logger.info("All rejected, regenerating hypotheses (round %d)", regeneration_count)
            update_state(rca_id, RcaSessionState.HYPOTHESIS_GENERATION, dynamodb_client=dynamodb_client)
            hypothesis_result = run_hypothesis_generation(
                scoping_result,
                agents.hypothesis,
            )
            hypotheses = list(hypothesis_result.hypotheses)
            if not hypotheses:
                logger.error("Regeneration produced no hypotheses")
                break
            timeline.append(f"Regenerated hypotheses: {len(hypotheses)}")
            continue

        # F5: Branching for NEEDS_INVESTIGATION hypotheses
        new_children = []
        for j in all_judgments:
            if j.status != HypothesisStatus.NEEDS_INVESTIGATION:
                continue
            parent = next((h for h in hypotheses if h.hypothesis_id == j.hypothesis_id), None)
            if parent is None:
                continue
            evidence_text = evidence_map.get(parent.hypothesis_id, "")
            branching_result = run_branching(
                parent,
                evidence_text,
                rejected_descriptions,
                agents.branching,
            )
            new_children.extend(branching_result.children)

        if not new_children:
            logger.info("No new child hypotheses, terminating")
            timeline.append("No new child hypotheses")
            break

        hypotheses.extend(new_children)
        logger.info("Added %d child hypotheses, total=%d", len(new_children), len(hypotheses))

    # Determine best hypothesis
    best_hypothesis = None
    confirmed = False
    if termination and termination.should_terminate and termination.best_hypothesis:
        best_hypothesis = termination.best_hypothesis
        confirmed = termination.reason and termination.reason.value == "CONFIRMED"
    elif all_judgments:
        best_j = max(all_judgments, key=lambda j: j.confidence_score)
        best_hypothesis = next(
            (h for h in hypotheses if h.hypothesis_id == best_j.hypothesis_id),
            None,
        )

    # Build hypothesis path
    hypothesis_path = []
    if best_hypothesis:
        hypothesis_path.append(best_hypothesis.description)

    evidence_texts = [e for e in evidence_map.values() if e]
    elapsed = int(time.monotonic() - start_time)

    # F7: Report generation
    update_state(rca_id, RcaSessionState.REPORT_GENERATION, dynamodb_client=dynamodb_client)
    rca_report = run_report_generation(
        scoping_result,
        best_hypothesis,
        confirmed,
        hypothesis_path,
        evidence_texts,
        rejected_descriptions,
        timeline,
        agents.report,
    )
    if rca_id:
        rca_report.rca_id = rca_id
    logger.info("RCA report generated: %s", rca_report.rca_id)

    report_s3_key = save_report_to_s3(rca_report, s3_client=s3_client)

    # F8: Playbook generation (search-first: update existing or create new)
    playbook = run_playbook_generation(
        rca_report,
        agents.playbook,
        scoping_result=scoping_result,
        s3_vectors_client=s3_vectors_client,
    )
    save_playbook_to_s3_vectors(
        playbook,
        scoping_result=scoping_result,
        s3_vectors_client=s3_vectors_client,
    )
    logger.info("Playbook %s: %s", playbook.playbook_id, playbook.failure_type)

    # F9: Remediation
    remediation_result = None
    if REMEDIATION_ENABLED and HEALTHCARE_SERVICE_HOST:
        update_state(rca_id, RcaSessionState.REMEDIATION, dynamodb_client=dynamodb_client)
        remediation_result = execute_remediation(
            report=rca_report,
            playbook=playbook,
            service_host=HEALTHCARE_SERVICE_HOST,
            ecs_cluster=HEALTHCARE_ECS_CLUSTER,
            ecs_service=HEALTHCARE_ECS_SERVICE,
        )
        logger.info(
            "Remediation: success=%s, summary=%s",
            remediation_result.overall_success,
            remediation_result.summary[:200],
        )

    # F10: Verification
    verification_result = None
    if remediation_result and remediation_result.overall_success:
        update_state(rca_id, RcaSessionState.VERIFICATION, dynamodb_client=dynamodb_client)
        verification_result = run_verification(
            agent=agents.post_remediation_verification,
            alarm=alarm,
            remediation=remediation_result,
            remediation_time=time.time(),
        )
        logger.info(
            "Verification: normalized=%s, summary=%s",
            verification_result.metrics_normalized,
            verification_result.verification_summary[:200],
        )

    # F11: Notification
    notification = build_notification(rca_report, report_s3_key, elapsed)
    send_notification(notification, sns_client=sns_client, s3_client=s3_client)

    # Mark session completed
    mark_completed(
        rca_report.rca_id,
        root_cause=rca_report.root_cause,
        confirmed=confirmed,
        dynamodb_client=dynamodb_client,
    )

    logger.info("RCA complete: rca_id=%s, elapsed=%ds", rca_report.rca_id, elapsed)


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not QUEUE_URL:
        logger.error("SQS_QUEUE_URL is not set")
        sys.exit(1)

    start_health_server()
    logger.info("Health server started on port 8000")

    ak_mcp_client = create_aws_knowledge_mcp_client()
    cw_mcp_client = create_cloudwatch_mcp_client()
    ct_mcp_client = create_cloudtrail_mcp_client()
    scoping_mcp_clients = [ak_mcp_client, cw_mcp_client, ct_mcp_client]
    evidence_mcp_clients = list(scoping_mcp_clients)
    if GITHUB_PERSONAL_ACCESS_TOKEN:
        gh_mcp_client = create_github_mcp_client()
        evidence_mcp_clients.append(gh_mcp_client)
        logger.info("GitHub MCP client enabled for evidence collection")
    agents = _Agents(mcp_clients=scoping_mcp_clients, evidence_mcp_clients=evidence_mcp_clients)
    s3_vectors_client = _create_s3_vectors_client()
    s3_client = _create_s3_client()
    sns_client = _create_sns_client()
    dynamodb_client = _create_dynamodb_client()
    logger.info("Pipeline initialized")

    sqs = boto3.client("sqs")
    logger.info("Starting SQS long polling: %s", QUEUE_URL)

    while _running:
        try:
            resp = sqs.receive_message(
                QueueUrl=QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=POLL_WAIT_SECONDS,
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
                body = json.loads(msg["Body"])
                logger.info(
                    "Received alarm: %s",
                    body.get("AlarmName", body.get("Message", "unknown")[:80]),
                )
                _process_alarm(
                    body,
                    agents,
                    s3_vectors_client=s3_vectors_client,
                    s3_client=s3_client,
                    sns_client=sns_client,
                    dynamodb_client=dynamodb_client,
                )
            except Exception:
                logger.exception("Failed to process message")
            finally:
                sqs.delete_message(
                    QueueUrl=QUEUE_URL,
                    ReceiptHandle=msg["ReceiptHandle"],
                )

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
