from __future__ import annotations

from abc import ABC, abstractmethod

from cc_headless.ports.interfaces.cc_runner import CcRunnerPort
from cc_headless.ports.interfaces.embedding import EmbeddingPort
from cc_headless.ports.interfaces.playbook_store import PlaybookStorePort
from cc_headless.ports.interfaces.report_store import ReportStorePort
from cc_headless.ports.interfaces.session_store import SessionStorePort


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
    def cc_runner(self) -> CcRunnerPort: ...

    @property
    @abstractmethod
    def dynamodb_client(self): ...

    @abstractmethod
    def cleanup(self) -> None: ...
