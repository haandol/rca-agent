from __future__ import annotations

from abc import ABC, abstractmethod

import boto3
from botocore.config import Config

from headless_codex.config.settings import DYNAMODB_TABLE_NAME, S3_VECTOR_REGION
from headless_codex.ports.interfaces.evidence_store import EvidenceStorePort
from headless_codex.ports.interfaces.execution_runner import ExecutionRunnerPort
from headless_codex.ports.interfaces.execution_store import ExecutionStorePort
from headless_codex.ports.interfaces.playbook_store import PlaybookStorePort


class ExecutionContainer(ABC):
    @property
    @abstractmethod
    def execution_store(self) -> ExecutionStorePort: ...

    @property
    @abstractmethod
    def evidence_store(self) -> EvidenceStorePort: ...

    @property
    @abstractmethod
    def playbook_store(self) -> PlaybookStorePort: ...

    @property
    @abstractmethod
    def execution_runner(self) -> ExecutionRunnerPort: ...


class AppExecutionContainer(ExecutionContainer):
    def __init__(self):
        self._dynamodb_client = None
        self._s3_client = None
        self._s3_vectors_client = None
        self._bedrock_client = None
        self._execution_store = None
        self._evidence_store = None
        self._playbook_store = None
        self._execution_runner = None

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
    def execution_store(self) -> ExecutionStorePort:
        if self._execution_store is None:
            from headless_codex.adapters.secondary.execution.dynamodb_execution_store import (
                DynamoDbExecutionStore,
            )

            self._execution_store = DynamoDbExecutionStore(self.dynamodb_client)
        return self._execution_store

    @property
    def evidence_store(self) -> EvidenceStorePort:
        if self._evidence_store is None:
            from headless_codex.adapters.secondary.evidence.s3_evidence_store import S3EvidenceStore

            self._evidence_store = S3EvidenceStore(self.s3_client)
        return self._evidence_store

    @property
    def playbook_store(self) -> PlaybookStorePort:
        if self._playbook_store is None:
            from headless_codex.adapters.secondary.embedding.bedrock_embedding import BedrockEmbeddingAdapter
            from headless_codex.adapters.secondary.playbook.s3_vectors_playbook_store import (
                S3VectorsPlaybookStore,
            )

            self._playbook_store = S3VectorsPlaybookStore(
                self.s3_vectors_client,
                BedrockEmbeddingAdapter(self.bedrock_client),
            )
        return self._playbook_store

    @property
    def execution_runner(self) -> ExecutionRunnerPort:
        if self._execution_runner is None:
            from headless_codex.adapters.secondary.codex.codex_execution_runner import CodexExecutionRunner

            self._execution_runner = CodexExecutionRunner()
        return self._execution_runner
