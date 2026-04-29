from __future__ import annotations

import logging

from rca_agent.config.settings import S3_EVIDENCE_BUCKET, S3_EVIDENCE_MAX_RETRIES
from rca_agent.ports.interfaces.evidence_store import EvidenceStorePort
from rca_agent.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)


class S3EvidenceStore(EvidenceStorePort):
    def __init__(self, s3_client=None):
        self._s3 = s3_client

    def save(self, rca_id: str, hypothesis_id: str, evidence_text: str) -> str | None:
        if not S3_EVIDENCE_BUCKET or self._s3 is None:
            return None
        if not evidence_text.strip():
            return None

        key = f"rca/{rca_id}/evidence/{hypothesis_id}/combined.md"

        def put() -> str:
            self._s3.put_object(
                Bucket=S3_EVIDENCE_BUCKET,
                Key=key,
                Body=evidence_text,
                ContentType="text/markdown",
            )
            logger.info("Evidence saved: s3://%s/%s", S3_EVIDENCE_BUCKET, key)
            return key

        return retry_with_backoff(
            put,
            max_retries=S3_EVIDENCE_MAX_RETRIES,
            operation=f"evidence save for {hypothesis_id}",
        )
