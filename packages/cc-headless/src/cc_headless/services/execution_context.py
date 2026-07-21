from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

RUN_TOKEN_ENV = "RCA_EXECUTION_TOKEN"
RCA_ID_ENV = "RCA_SESSION_ID"
CLAIM_TOKEN_ENV = "RCA_CLAIM_TOKEN"
ATTEMPT_ENV = "RCA_ATTEMPT"

_ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "cc-headless-artifacts"
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")


def artifact_dir_for_token(token: str) -> Path:
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("invalid RCA execution token")
    return _ARTIFACT_ROOT / token


@dataclass(frozen=True)
class ExecutionContext:
    rca_id: str
    token: str

    @classmethod
    def create(cls, rca_id: str) -> ExecutionContext:
        if not rca_id:
            raise ValueError("rca_id must not be empty")
        return cls(rca_id=rca_id, token=uuid.uuid4().hex)

    @property
    def artifact_dir(self) -> Path:
        return artifact_dir_for_token(self.token)

    def prepare(self) -> Path:
        _ARTIFACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.artifact_dir.mkdir(mode=0o700, exist_ok=False)
        return self.artifact_dir

    def cleanup(self) -> None:
        shutil.rmtree(self.artifact_dir, ignore_errors=True)
