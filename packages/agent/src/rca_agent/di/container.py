from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.interfaces.embedding import EmbeddingPort
from rca_agent.ports.interfaces.evidence_store import EvidenceStorePort
from rca_agent.ports.interfaces.notification import NotificationPort
from rca_agent.ports.interfaces.playbook_store import PlaybookStorePort
from rca_agent.ports.interfaces.queue_consumer import QueueConsumerPort
from rca_agent.ports.interfaces.report_store import ReportStorePort
from rca_agent.ports.interfaces.session_store import SessionStorePort


class Container(ABC):
    @property
    @abstractmethod
    def session_store(self) -> SessionStorePort: ...

    @property
    @abstractmethod
    def report_store(self) -> ReportStorePort: ...

    @property
    @abstractmethod
    def notification(self) -> NotificationPort: ...

    @property
    @abstractmethod
    def playbook_store(self) -> PlaybookStorePort: ...

    @property
    @abstractmethod
    def evidence_store(self) -> EvidenceStorePort: ...

    @property
    @abstractmethod
    def embedding(self) -> EmbeddingPort: ...

    @property
    @abstractmethod
    def queue_consumer(self) -> QueueConsumerPort: ...

    @abstractmethod
    def cleanup(self) -> None: ...
