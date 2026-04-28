from __future__ import annotations

import logging
import time

from rca_agent.config.settings import S3_EVIDENCE_BUCKET, S3_EVIDENCE_MAX_RETRIES
from rca_agent.ports.interfaces.evidence_store import EvidenceStorePort

logger = logging.getLogger(__name__)

_S3_BASE_DELAY = 1.0


class S3EvidenceStore(EvidenceStorePort):
    def __init__(self, s3_client=None):
        self._s3 = s3_client

    def save(self, rca_id: str, hypothesis_id: str, evidence_text: str) -> str | None:
        if not S3_EVIDENCE_BUCKET or self._s3 is None:
            return None
        if not evidence_text.strip():
            return None

        key = f"rca/{rca_id}/evidence/{hypothesis_id}/combined.md"
        for attempt in range(S3_EVIDENCE_MAX_RETRIES):
            try:
                self._s3.put_object(
                    Bucket=S3_EVIDENCE_BUCKET,
                    Key=key,
                    Body=evidence_text,
                    ContentType="text/markdown",
                )
                logger.info("Evidence saved: s3://%s/%s", S3_EVIDENCE_BUCKET, key)
                return key
            except Exception:
                if attempt == S3_EVIDENCE_MAX_RETRIES - 1:
                    logger.exception(
                        "Failed to save evidence for %s after %d attempts",
                        hypothesis_id,
                        S3_EVIDENCE_MAX_RETRIES,
                    )
                else:
                    delay = _S3_BASE_DELAY * (2**attempt)
                    logger.warning("Evidence save attempt %d failed, retrying in %.1fs", attempt + 1, delay)
                    time.sleep(delay)
        return None
