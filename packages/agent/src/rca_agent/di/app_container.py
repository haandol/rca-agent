from __future__ import annotations

import logging

import boto3

from rca_agent.config.settings import (
    DYNAMODB_TABLE_NAME,
    GITHUB_PERSONAL_ACCESS_TOKEN,
    S3_EVIDENCE_BUCKET,
    S3_REPORT_BUCKET,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_REGION,
    SNS_NOTIFICATION_TOPIC_ARN,
)
from rca_agent.di.container import Container
from rca_agent.ports.interfaces.embedding import EmbeddingPort
from rca_agent.ports.interfaces.evidence_store import EvidenceStorePort
from rca_agent.ports.interfaces.notification import NotificationPort
from rca_agent.ports.interfaces.playbook_store import PlaybookStorePort
from rca_agent.ports.interfaces.queue_consumer import QueueConsumerPort
from rca_agent.ports.interfaces.report_store import ReportStorePort
from rca_agent.ports.interfaces.session_store import SessionStorePort

logger = logging.getLogger(__name__)


class AppContainer(Container):
    def __init__(self, queue_url: str, *, poll_wait_seconds: int = 20):
        self._queue_url = queue_url
        self._poll_wait_seconds = poll_wait_seconds

        self._dynamodb_client = None
        self._s3_client = None
        self._s3_vectors_client = None
        self._sns_client = None

        self._session_store: SessionStorePort | None = None
        self._report_store: ReportStorePort | None = None
        self._notification: NotificationPort | None = None
        self._playbook_store: PlaybookStorePort | None = None
        self._evidence_store: EvidenceStorePort | None = None
        self._embedding: EmbeddingPort | None = None
        self._queue_consumer: QueueConsumerPort | None = None

        self._scoping_agent = None
        self._hypothesis_agent = None
        self._prioritization_agent = None
        self._validation_agent = None
        self._branching_agent = None
        self._report_agent = None
        self._playbook_agent = None
        self._scoping_mcp_clients = None
        self._evidence_mcp_clients = None

    # ── AWS Clients (lazy) ─────────────────────────────────────────

    @property
    def dynamodb_client(self):
        if self._dynamodb_client is None and DYNAMODB_TABLE_NAME:
            self._dynamodb_client = boto3.client("dynamodb")
        return self._dynamodb_client

    @property
    def s3_client(self):
        if self._s3_client is None and (S3_REPORT_BUCKET or S3_EVIDENCE_BUCKET):
            self._s3_client = boto3.client("s3")
        return self._s3_client

    @property
    def s3_vectors_client(self):
        if self._s3_vectors_client is None and S3_VECTOR_BUCKET_NAME:
            self._s3_vectors_client = boto3.client("s3vectors", region_name=S3_VECTOR_REGION)
        return self._s3_vectors_client

    @property
    def sns_client(self):
        if self._sns_client is None and SNS_NOTIFICATION_TOPIC_ARN:
            self._sns_client = boto3.client("sns")
        return self._sns_client

    # ── Port implementations (lazy) ────────────────────────────────

    @property
    def session_store(self) -> SessionStorePort:
        if self._session_store is None:
            from rca_agent.adapters.secondary.session.dynamodb_session_store import DynamoDbSessionStore

            self._session_store = DynamoDbSessionStore(self.dynamodb_client)
        return self._session_store

    @property
    def report_store(self) -> ReportStorePort:
        if self._report_store is None:
            from rca_agent.adapters.secondary.report.s3_report_store import S3ReportStore

            self._report_store = S3ReportStore(self.s3_client)
        return self._report_store

    @property
    def notification(self) -> NotificationPort:
        if self._notification is None:
            from rca_agent.adapters.secondary.notification.sns_notification import SnsNotificationAdapter

            self._notification = SnsNotificationAdapter(self.sns_client, self.s3_client)
        return self._notification

    @property
    def playbook_store(self) -> PlaybookStorePort:
        if self._playbook_store is None:
            from rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store import S3VectorsPlaybookStore

            self._playbook_store = S3VectorsPlaybookStore(self.s3_vectors_client, self.embedding)
        return self._playbook_store

    @property
    def evidence_store(self) -> EvidenceStorePort:
        if self._evidence_store is None:
            from rca_agent.adapters.secondary.evidence.s3_evidence_store import S3EvidenceStore

            self._evidence_store = S3EvidenceStore(self.s3_client)
        return self._evidence_store

    @property
    def embedding(self) -> EmbeddingPort:
        if self._embedding is None:
            from rca_agent.adapters.secondary.embedding.bedrock_embedding import BedrockEmbeddingAdapter

            self._embedding = BedrockEmbeddingAdapter()
        return self._embedding

    @property
    def queue_consumer(self) -> QueueConsumerPort:
        if self._queue_consumer is None:
            from rca_agent.adapters.secondary.queue.sqs_consumer import SqsConsumer

            self._queue_consumer = SqsConsumer(self._queue_url, poll_wait_seconds=self._poll_wait_seconds)
        return self._queue_consumer

    # ── Strands Agents (lazy) ──────────────────────────────────────

    @property
    def scoping_mcp_clients(self):
        if self._scoping_mcp_clients is None:
            from rca_agent.agent_factory import (
                create_aws_knowledge_mcp_client,
                create_cloudtrail_mcp_client,
                create_cloudwatch_mcp_client,
            )

            self._scoping_mcp_clients = [
                create_aws_knowledge_mcp_client(),
                create_cloudwatch_mcp_client(),
                create_cloudtrail_mcp_client(),
            ]
        return self._scoping_mcp_clients

    @property
    def evidence_mcp_clients(self):
        if self._evidence_mcp_clients is None:
            self._evidence_mcp_clients = list(self.scoping_mcp_clients)
            if GITHUB_PERSONAL_ACCESS_TOKEN:
                from rca_agent.agent_factory import create_github_mcp_client

                self._evidence_mcp_clients.append(create_github_mcp_client())
                logger.info("GitHub MCP client enabled for evidence collection")
        return self._evidence_mcp_clients

    @property
    def scoping_agent(self):
        if self._scoping_agent is None:
            from rca_agent.agent_factory import create_scoping_agent

            self._scoping_agent = create_scoping_agent(mcp_clients=self.scoping_mcp_clients)
        return self._scoping_agent

    @property
    def hypothesis_agent(self):
        if self._hypothesis_agent is None:
            from rca_agent.agent_factory import create_hypothesis_generation_agent

            self._hypothesis_agent = create_hypothesis_generation_agent()
        return self._hypothesis_agent

    @property
    def prioritization_agent(self):
        if self._prioritization_agent is None:
            from rca_agent.agent_factory import create_prioritization_agent

            self._prioritization_agent = create_prioritization_agent()
        return self._prioritization_agent

    @property
    def validation_agent(self):
        if self._validation_agent is None:
            from rca_agent.agent_factory import create_validation_agent

            self._validation_agent = create_validation_agent()
        return self._validation_agent

    @property
    def branching_agent(self):
        if self._branching_agent is None:
            from rca_agent.agent_factory import create_branching_agent

            self._branching_agent = create_branching_agent()
        return self._branching_agent

    @property
    def report_agent(self):
        if self._report_agent is None:
            from rca_agent.agent_factory import create_report_agent

            self._report_agent = create_report_agent()
        return self._report_agent

    @property
    def playbook_agent(self):
        if self._playbook_agent is None:
            from rca_agent.agent_factory import create_playbook_agent

            self._playbook_agent = create_playbook_agent()
        return self._playbook_agent

    def cleanup(self) -> None:
        pass
