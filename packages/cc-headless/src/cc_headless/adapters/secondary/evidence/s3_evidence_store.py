from __future__ import annotations

import hashlib
import json

from cc_headless.config.settings import S3_EVIDENCE_BUCKET
from cc_headless.ports.interfaces.evidence_store import EvidenceStorePort

# 실행 단위로 경로가 갈라진다. 같은 리포트를 여러 번 실행할 수 있으므로 리포트
# 단위로 묶으면 재실행이 앞선 증거를 덮어쓴다.
_EVIDENCE_KEY = "executions/{rca_id}/{execution_id}/evidence.json"
_SNAPSHOT_KEY = "executions/{rca_id}/{execution_id}/playbook-before.json"
_DIFF_KEY = "executions/{rca_id}/{execution_id}/retrospective-diff.json"


class S3EvidenceStore(EvidenceStorePort):
    def __init__(self, s3_client=None):
        self._s3 = s3_client

    def _put_json(self, key: str, payload: dict) -> str:
        if not S3_EVIDENCE_BUCKET:
            raise RuntimeError("evidence bucket is not configured")
        if self._s3 is None:
            raise RuntimeError("S3 evidence client is unavailable")
        self._s3.put_object(
            Bucket=S3_EVIDENCE_BUCKET,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False, indent=2).encode(),
            ContentType="application/json",
        )
        return key

    def load_approved_playbook(self, approved_playbook_s3_key: str, *, playbook_digest: str) -> dict:
        """승인 시점의 바이트를 검증한 뒤 플레이북 객체로 해석한다."""
        if not S3_EVIDENCE_BUCKET or self._s3 is None:
            raise RuntimeError("approved playbook store is unavailable")
        response = self._s3.get_object(
            Bucket=S3_EVIDENCE_BUCKET,
            Key=approved_playbook_s3_key,
        )
        body = response.get("Body")
        if body is None:
            raise RuntimeError("approved playbook snapshot has no body")
        raw = body.read()
        actual_digest = hashlib.sha256(raw).hexdigest()
        if actual_digest != playbook_digest:
            raise RuntimeError("approved playbook digest does not match the stored snapshot")
        try:
            playbook = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("approved playbook snapshot is not valid JSON") from exc
        if not isinstance(playbook, dict):
            raise RuntimeError("approved playbook snapshot must be a JSON object")
        return playbook

    def save_execution_evidence(self, execution_id: str, *, rca_id: str, evidence: dict) -> str:
        return self._put_json(
            _EVIDENCE_KEY.format(rca_id=rca_id, execution_id=execution_id),
            evidence,
        )

    def save_playbook_snapshot(self, execution_id: str, *, rca_id: str, playbook: dict) -> str:
        return self._put_json(
            _SNAPSHOT_KEY.format(rca_id=rca_id, execution_id=execution_id),
            playbook,
        )

    def save_retrospective_diff(self, execution_id: str, *, rca_id: str, diff: dict) -> str:
        return self._put_json(
            _DIFF_KEY.format(rca_id=rca_id, execution_id=execution_id),
            diff,
        )
