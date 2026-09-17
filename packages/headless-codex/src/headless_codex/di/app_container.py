from __future__ import annotations

import boto3
from botocore.config import Config

from headless_codex.config.settings import AWS_REGION, DYNAMODB_TABLE_NAME, S3_EVIDENCE_BUCKET, S3_VECTOR_REGION
from headless_codex.di.container import Container
from headless_codex.ports.interfaces.codex_runner import CodexRunnerPort
from headless_codex.ports.interfaces.embedding import EmbeddingPort
from headless_codex.ports.interfaces.playbook_store import PlaybookStorePort
from headless_codex.ports.interfaces.report_store import ReportStorePort
from headless_codex.ports.interfaces.session_store import SessionStorePort


class AppContainer(Container):
    def __init__(self):
        self._dynamodb_client = None
        self._s3_client = None
        self._sns_client = None
        self._s3_vectors_client = None
        self._bedrock_client = None
        self._session_store = None
        self._report_store = None
        self._playbook_store = None
        self._embedding = None
        self._codex_runner = None
        self._analysis_part_store = None
        self._recovery_clients = {}

    @property
    def dynamodb_client(self):
        if self._dynamodb_client is None and DYNAMODB_TABLE_NAME:
            self._dynamodb_client = boto3.client("dynamodb")
        return self._dynamodb_client

    @property
    def s3_client(self):
        if self._s3_client is None:
            self._s3_client = boto3.client("s3", config=Config(signature_version="s3v4"))
        return self._s3_client

    @property
    def sns_client(self):
        if self._sns_client is None:
            self._sns_client = boto3.client("sns")
        return self._sns_client

    @property
    def s3_vectors_client(self):
        if self._s3_vectors_client is None:
            self._s3_vectors_client = boto3.client("s3vectors", region_name=S3_VECTOR_REGION)
        return self._s3_vectors_client

    @property
    def bedrock_client(self):
        if self._bedrock_client is None:
            self._bedrock_client = boto3.client("bedrock-runtime", region_name=S3_VECTOR_REGION)
        return self._bedrock_client

    @property
    def session_store(self) -> SessionStorePort:
        if self._session_store is None:
            from headless_codex.adapters.secondary.session.dynamodb_session_store import DynamoDbSessionStore

            self._session_store = DynamoDbSessionStore(self.dynamodb_client)
        return self._session_store

    @property
    def report_store(self) -> ReportStorePort:
        if self._report_store is None:
            from headless_codex.adapters.secondary.report.s3_report_store import S3ReportStore

            self._report_store = S3ReportStore(self.s3_client, self.sns_client)
        return self._report_store

    @property
    def embedding(self) -> EmbeddingPort:
        if self._embedding is None:
            from headless_codex.adapters.secondary.embedding.bedrock_embedding import BedrockEmbeddingAdapter

            self._embedding = BedrockEmbeddingAdapter(self.bedrock_client)
        return self._embedding

    @property
    def playbook_store(self) -> PlaybookStorePort:
        if self._playbook_store is None:
            from headless_codex.adapters.secondary.playbook.s3_vectors_playbook_store import S3VectorsPlaybookStore

            # 상세 절차는 인덱스가 아니라 상태 저장소에 있으므로 두 클라이언트를 함께
            # 받는다. 검색만 되고 상세를 못 읽으면 병합이 성립하지 않는다.
            self._playbook_store = S3VectorsPlaybookStore(
                self.s3_vectors_client,
                self.embedding,
                dynamodb_client=self.dynamodb_client,
            )
        return self._playbook_store

    @property
    def codex_runner(self) -> CodexRunnerPort:
        if self._codex_runner is None:
            from headless_codex.adapters.secondary.codex.codex_subprocess_runner import CodexSubprocessRunner

            self._codex_runner = CodexSubprocessRunner()
        return self._codex_runner

    def _recovery_client(self, service: str, region: str):
        """Bound each native observation/storage call to one SDK attempt under the analysis budget."""
        key = (service, region)
        if key not in self._recovery_clients:
            self._recovery_clients[key] = boto3.client(
                service,
                region_name=region,
                config=Config(
                    connect_timeout=5, read_timeout=60, retries={"total_max_attempts": 1, "mode": "standard"}
                ),
            )
        return self._recovery_clients[key]

    @property
    def analysis_part_store(self):
        """Publish private stage records through the shared claim-fenced, create-only storage contract."""
        if self._analysis_part_store is None:
            from headless_codex.services.analysis_parts import AnalysisPartStore

            self._analysis_part_store = AnalysisPartStore(
                self._recovery_client("dynamodb", AWS_REGION),
                self._recovery_client("s3", AWS_REGION),
                table_name=DYNAMODB_TABLE_NAME,
                bucket=S3_EVIDENCE_BUCKET,
                engine="headless-codex",
            )
        return self._analysis_part_store

    def observe_recovery(self, alarm_data: dict, *, timeout_seconds: float) -> dict:
        """Give the server observer original alarm data and bounded clients, never model baseline claims."""
        from headless_codex.services.recovery_observation import observe_recovery_evidence

        return observe_recovery_evidence(
            alarm_data,
            s3_client=self._recovery_client("s3", AWS_REGION),
            logs_client_for_region=lambda region: self._recovery_client("logs", region),
            ecs_client_for_region=lambda region: self._recovery_client("ecs", region),
            evidence_bucket=S3_EVIDENCE_BUCKET,
            timeout_seconds=timeout_seconds,
        )

    def cleanup(self) -> None:
        """Release the native observation clients created for this run without altering shared legacy adapters."""
        for client in self._recovery_clients.values():
            client.close()
        self._recovery_clients.clear()
        self._analysis_part_store = None
