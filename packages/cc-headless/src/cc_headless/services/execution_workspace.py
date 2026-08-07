"""실행별 격리 작업 디렉터리.

분석 실행이 산출물 디렉터리를 토큰으로 격리하는 것과 같은 이유다. 실행 증거는
실행마다 갈라져야 하고, 이전 실행의 증거를 이번 실행의 것으로 오인하면 회고가
엉뚱한 절차를 교정한다.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

EXECUTION_TOKEN_ENV = "PLAYBOOK_EXECUTION_TOKEN"
EXECUTION_ID_ENV = "PLAYBOOK_EXECUTION_ID"
APPROVED_STEP_IDS_ENV = "PLAYBOOK_APPROVED_STEP_IDS"

_WORKSPACE_ROOT = Path(tempfile.gettempdir()) / "cc-headless-executions"
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")
_EVIDENCE_FILE = "evidence.jsonl"
_RETROSPECTIVE_FILE = "retrospective.json"


def workspace_for_token(token: str) -> Path:
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("invalid playbook execution token")
    return _WORKSPACE_ROOT / token


def evidence_path_for_token(token: str) -> Path:
    return workspace_for_token(token) / _EVIDENCE_FILE


def retrospective_path_for_token(token: str) -> Path:
    return workspace_for_token(token) / _RETROSPECTIVE_FILE


@dataclass(frozen=True)
class ExecutionWorkspace:
    execution_id: str
    token: str

    @classmethod
    def create(cls, execution_id: str) -> ExecutionWorkspace:
        if not execution_id:
            raise ValueError("execution_id must not be empty")
        return cls(execution_id=execution_id, token=uuid.uuid4().hex)

    @property
    def path(self) -> Path:
        return workspace_for_token(self.token)

    @property
    def evidence_path(self) -> Path:
        return evidence_path_for_token(self.token)

    @property
    def retrospective_path(self) -> Path:
        return retrospective_path_for_token(self.token)

    def prepare(self) -> Path:
        _WORKSPACE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.mkdir(mode=0o700, exist_ok=False)
        self.evidence_path.touch(mode=0o600)
        return self.path

    def read_records(self) -> list[dict]:
        """서버가 기록한 증거 줄을 읽는다. 깨진 줄은 건너뛴다."""
        try:
            raw = self.evidence_path.read_text(encoding="utf-8")
        except OSError:
            return []
        records: list[dict] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records

    def read_retrospective(self) -> dict | None:
        try:
            parsed = json.loads(self.retrospective_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
