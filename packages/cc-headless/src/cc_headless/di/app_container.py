from __future__ import annotations

import boto3
from botocore.config import Config

from cc_headless.config.settings import DYNAMODB_TABLE_NAME, S3_VECTOR_REGION
from cc_headless.di.container import Container
from cc_headless.ports.interfaces.cc_runner import CcRunnerPort
from cc_headless.ports.interfaces.embedding import EmbeddingPort
from cc_headless.ports.interfaces.playbook_store import PlaybookStorePort
from cc_headless.ports.interfaces.report_store import ReportStorePort
from cc_headless.ports.interfaces.session_store import SessionStorePort


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
        self._cc_runner = None

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
            from cc_headless.adapters.secondary.session.dynamodb_session_store import DynamoDbSessionStore

            self._session_store = DynamoDbSessionStore(self.dynamodb_client)
        return self._session_store

    @property
    def report_store(self) -> ReportStorePort:
        if self._report_store is None:
            from cc_headless.adapters.secondary.report.s3_report_store import S3ReportStore

            self._report_store = S3ReportStore(self.s3_client, self.sns_client)
        return self._report_store

    @property
    def embedding(self) -> EmbeddingPort:
        if self._embedding is None:
            from cc_headless.adapters.secondary.embedding.bedrock_embedding import BedrockEmbeddingAdapter

            self._embedding = BedrockEmbeddingAdapter(self.bedrock_client)
        return self._embedding

    @property
    def playbook_store(self) -> PlaybookStorePort:
        if self._playbook_store is None:
            from cc_headless.adapters.secondary.playbook.s3_vectors_playbook_store import S3VectorsPlaybookStore

            self._playbook_store = S3VectorsPlaybookStore(self.s3_vectors_client, self.embedding)
        return self._playbook_store

    @property
    def cc_runner(self) -> CcRunnerPort:
        if self._cc_runner is None:
            from cc_headless.adapters.secondary.cc.cc_subprocess_runner import CcSubprocessRunner

            self._cc_runner = CcSubprocessRunner()
        return self._cc_runner

    def cleanup(self) -> None:
        pass
