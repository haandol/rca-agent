from __future__ import annotations

from abc import ABC, abstractmethod

from codex_headless.ports.interfaces.codex_runner import CodexRunnerPort
from codex_headless.ports.interfaces.embedding import EmbeddingPort
from codex_headless.ports.interfaces.playbook_store import PlaybookStorePort
from codex_headless.ports.interfaces.report_store import ReportStorePort
from codex_headless.ports.interfaces.session_store import SessionStorePort


class Container(ABC):
    @property
    @abstractmethod
    def session_store(self) -> SessionStorePort: ...

    @property
    @abstractmethod
    def report_store(self) -> ReportStorePort: ...

    @property
    @abstractmethod
    def playbook_store(self) -> PlaybookStorePort: ...

    @property
    @abstractmethod
    def embedding(self) -> EmbeddingPort: ...

    @property
    @abstractmethod
    def codex_runner(self) -> CodexRunnerPort: ...

    @property
    @abstractmethod
    def dynamodb_client(self): ...

    @abstractmethod
    def cleanup(self) -> None: ...
