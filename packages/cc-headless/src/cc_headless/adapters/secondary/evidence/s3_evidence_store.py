from __future__ import annotations

import json

import structlog

from cc_headless.config.settings import S3_EVIDENCE_BUCKET
from cc_headless.ports.interfaces.evidence_store import EvidenceStorePort

logger = structlog.get_logger()

# 실행 단위로 경로가 갈라진다. 같은 리포트를 여러 번 실행할 수 있으므로 리포트
# 단위로 묶으면 재실행이 앞선 증거를 덮어쓴다.
_EVIDENCE_KEY = "executions/{rca_id}/{execution_id}/evidence.json"
_SNAPSHOT_KEY = "executions/{rca_id}/{execution_id}/playbook-before.json"
_DIFF_KEY = "executions/{rca_id}/{execution_id}/retrospective-diff.json"


class S3EvidenceStore(EvidenceStorePort):
    def __init__(self, s3_client=None):
        self._s3 = s3_client

    def _put_json(self, key: str, payload: dict) -> str:
        if not S3_EVIDENCE_BUCKET or not self._s3:
            logger.info("evidence_bucket_not_configured", key=key)
            return key
        self._s3.put_object(
            Bucket=S3_EVIDENCE_BUCKET,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False, indent=2).encode(),
            ContentType="application/json",
        )
        return key

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
