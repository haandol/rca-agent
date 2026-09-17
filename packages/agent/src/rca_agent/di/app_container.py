from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from threading import Lock

import boto3

from rca_agent.config.aws_sdk import SIDE_EFFECT_AWS_CLIENT_CONFIG
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
from rca_agent.ports.interfaces.analysis_parts import AnalysisPartStorePort
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
        self._cloudwatch_clients = {}
        self._logs_clients = {}
        self._ecs_clients = {}

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
        self._recovery_agent = None
        self._code_preview_agent = None
        self._operations_agent = None
        self._analysis_part_store = None
        self._scoping_mcp_clients = None
        self._evidence_mcp_clients = None
        self._agent_baselines = {}
        self._analysis_lock = Lock()

    def _remember_initial_agent(self, agent) -> None:
        """Capture empty conversation/session state once without copying model clients or tool connections."""
        self._agent_baselines[id(agent)] = agent.take_snapshot(preset="session", include=["model_state"])

    @contextmanager
    def analysis_context(self):
        """Reset only between claimed incidents, preserving context within an active analysis.

        Check every cached agent and provider before changing any state. Shared
        MCP clients and model instances stay connected; only completed local
        conversation state, received records and the prior incident's taint reset.
        """
        if not self._analysis_lock.acquire(blocking=False):
            raise RuntimeError("cannot reset stage context during an active analysis")
        try:
            agents = [
                agent
                for name in (
                    "_scoping_agent",
                    "_hypothesis_agent",
                    "_prioritization_agent",
                    "_validation_agent",
                    "_branching_agent",
                    "_report_agent",
                    "_playbook_agent",
                    "_recovery_agent",
                    "_code_preview_agent",
                    "_operations_agent",
                )
                if (agent := getattr(self, name)) is not None
            ]
            with ExitStack() as locks:
                for agent in agents:
                    if not agent._concurrency.try_acquire_lock():
                        raise RuntimeError("cannot reset an actively invoked stage agent")
                    locks.callback(agent._concurrency.release_lock)
                    if any(provider.active for provider in getattr(agent, "_rca_read_tools", [])):
                        raise RuntimeError("cannot reset an active tool provider")
                    if id(agent) not in self._agent_baselines:
                        raise RuntimeError("stage agent has no initial session snapshot")
                for agent in agents:
                    agent.load_snapshot(self._agent_baselines[id(agent)])
                    for provider in getattr(agent, "_rca_read_tools", []):
                        provider.results = []
                        provider.receipts = []
                        provider.termination_uncertain = False
            yield
        finally:
            self._analysis_lock.release()

    # ── AWS Clients (lazy) ─────────────────────────────────────────

    @property
    def dynamodb_client(self):
        if self._dynamodb_client is None and DYNAMODB_TABLE_NAME:
            self._dynamodb_client = boto3.client(
                "dynamodb",
                config=SIDE_EFFECT_AWS_CLIENT_CONFIG,
            )
        return self._dynamodb_client

    @property
    def s3_client(self):
        if self._s3_client is None and (S3_REPORT_BUCKET or S3_EVIDENCE_BUCKET):
            self._s3_client = boto3.client(
                "s3",
                config=SIDE_EFFECT_AWS_CLIENT_CONFIG,
            )
        return self._s3_client

    @property
    def s3_vectors_client(self):
        if self._s3_vectors_client is None and S3_VECTOR_BUCKET_NAME:
            self._s3_vectors_client = boto3.client(
                "s3vectors",
                region_name=S3_VECTOR_REGION,
                config=SIDE_EFFECT_AWS_CLIENT_CONFIG,
            )
        return self._s3_vectors_client

    @property
    def sns_client(self):
        if self._sns_client is None and SNS_NOTIFICATION_TOPIC_ARN:
            self._sns_client = boto3.client(
                "sns",
                config=SIDE_EFFECT_AWS_CLIENT_CONFIG,
            )
        return self._sns_client

    @property
    def cloudwatch_client(self):
        return self.cloudwatch_client_for_region("")

    def cloudwatch_client_for_region(self, region: str):
        region_key = region.strip()
        if region_key not in self._cloudwatch_clients:
            kwargs = {"config": SIDE_EFFECT_AWS_CLIENT_CONFIG}
            if region_key:
                kwargs["region_name"] = region_key
            self._cloudwatch_clients[region_key] = boto3.client("cloudwatch", **kwargs)
        return self._cloudwatch_clients[region_key]

    def logs_client_for_region(self, region: str):
        """Reuse the application's credential provider and bounded read configuration."""
        if region not in self._logs_clients:
            self._logs_clients[region] = boto3.client("logs", region_name=region, config=SIDE_EFFECT_AWS_CLIENT_CONFIG)
        return self._logs_clients[region]

    def ecs_client_for_region(self, region: str):
        """Observe deployment identity without introducing a second credential chain."""
        if region not in self._ecs_clients:
            self._ecs_clients[region] = boto3.client("ecs", region_name=region, config=SIDE_EFFECT_AWS_CLIENT_CONFIG)
        return self._ecs_clients[region]

    @property
    def incident_observer(self):
        from rca_agent.adapters.secondary.evidence.incident_observation import AwsIncidentObservation

        return AwsIncidentObservation(
            s3_client=self.s3_client,
            logs_client_for_region=self.logs_client_for_region,
            ecs_client_for_region=self.ecs_client_for_region,
            evidence_bucket=S3_EVIDENCE_BUCKET,
        )

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

            self._report_store = S3ReportStore(self.s3_client, self.s3_vectors_client, self.embedding)
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

            self._playbook_store = S3VectorsPlaybookStore(
                self.s3_vectors_client,
                self.embedding,
                self.dynamodb_client,
            )
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
            self._remember_initial_agent(self._scoping_agent)
        return self._scoping_agent

    @property
    def hypothesis_agent(self):
        if self._hypothesis_agent is None:
            from rca_agent.agent_factory import create_hypothesis_generation_agent

            self._hypothesis_agent = create_hypothesis_generation_agent()
            self._remember_initial_agent(self._hypothesis_agent)
        return self._hypothesis_agent

    @property
    def prioritization_agent(self):
        if self._prioritization_agent is None:
            from rca_agent.agent_factory import create_prioritization_agent

            self._prioritization_agent = create_prioritization_agent()
            self._remember_initial_agent(self._prioritization_agent)
        return self._prioritization_agent

    @property
    def validation_agent(self):
        if self._validation_agent is None:
            from rca_agent.agent_factory import create_validation_agent

            self._validation_agent = create_validation_agent()
            self._remember_initial_agent(self._validation_agent)
        return self._validation_agent

    @property
    def branching_agent(self):
        if self._branching_agent is None:
            from rca_agent.agent_factory import create_branching_agent

            self._branching_agent = create_branching_agent()
            self._remember_initial_agent(self._branching_agent)
        return self._branching_agent

    @property
    def report_agent(self):
        if self._report_agent is None:
            from rca_agent.agent_factory import create_report_agent

            self._report_agent = create_report_agent()
            self._remember_initial_agent(self._report_agent)
        return self._report_agent

    @property
    def playbook_agent(self):
        if self._playbook_agent is None:
            from rca_agent.agent_factory import create_playbook_agent

            self._playbook_agent = create_playbook_agent()
            self._remember_initial_agent(self._playbook_agent)
        return self._playbook_agent

    def cleanup(self) -> None:
        pass

    @property
    def analysis_part_store(self) -> AnalysisPartStorePort:
        """Keep private stage authority separate from the public library while using the same claim/table."""
        from rca_agent.services.analysis_parts import AnalysisPartStore

        if self._analysis_part_store is None:
            self._analysis_part_store = AnalysisPartStore(
                self.dynamodb_client,
                self.s3_client,
                table_name=DYNAMODB_TABLE_NAME,
                bucket=S3_EVIDENCE_BUCKET,
                engine="strands",
            )
        return self._analysis_part_store

    @property
    def recovery_agent(self):
        """Cache the recovery role with the same incident-reset and stream ownership policy."""
        from rca_agent.agent_factory import create_recovery_agent

        if self._recovery_agent is None:
            self._recovery_agent = create_recovery_agent()
            self._remember_initial_agent(self._recovery_agent)
        return self._recovery_agent

    @property
    def code_preview_agent(self):
        """Cache a tool-free code-preview role; it cannot publish a branch or PR."""
        from rca_agent.agent_factory import create_code_preview_agent

        if self._code_preview_agent is None:
            self._code_preview_agent = create_code_preview_agent()
            self._remember_initial_agent(self._code_preview_agent)
        return self._code_preview_agent

    @property
    def operations_agent(self):
        """Cache prevention generation without connecting it to execution success."""
        from rca_agent.agent_factory import create_operations_agent

        if self._operations_agent is None:
            self._operations_agent = create_operations_agent()
            self._remember_initial_agent(self._operations_agent)
        return self._operations_agent
