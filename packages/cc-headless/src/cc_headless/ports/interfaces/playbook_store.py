from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class PlaybookStorePort(ABC):
    @abstractmethod
    def load_playbook(self, artifact_dir: Path) -> dict | None: ...

    @abstractmethod
    def save_to_s3_vectors(self, playbook: dict, rca_id: str, *, metric_name: str = "") -> bool: ...
