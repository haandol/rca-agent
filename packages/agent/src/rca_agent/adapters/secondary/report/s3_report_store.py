from __future__ import annotations

import logging

from rca_agent.config.settings import (
    ENGINE,
    REPORT_SIMILARITY_THRESHOLD,
    REPORT_TOP_K,
    S3_REPORT_BUCKET,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_REPORT_INDEX,
)
from rca_agent.ports.dto.models import RcaReport, ReportMatch, ScopingResult
from rca_agent.ports.interfaces.embedding import EmbeddingPort
from rca_agent.ports.interfaces.report_store import ReportStorePort
from rca_agent.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

_SEARCH_MAX_RETRIES = 3
_EMBED_FIELD_MAX = 80
_HYPOTHESIS_PATH_MAX = 200


def _truncate(text: str, max_len: int = _EMBED_FIELD_MAX) -> str:
    return text[:max_len].strip() if text else ""


class S3ReportStore(ReportStorePort):
    def __init__(self, s3_client=None, s3_vectors_client=None, embedding: EmbeddingPort | None = None):
        self._s3 = s3_client
        self._s3v = s3_vectors_client
        self._embedding = embedding

    @property
    def _vectors_enabled(self) -> bool:
        return bool(S3_VECTOR_BUCKET_NAME and self._s3v)

    def save(self, report: RcaReport) -> str:
        if not S3_REPORT_BUCKET or self._s3 is None:
            logger.info("S3 report bucket not configured, skipping upload")
            return ""
        key = f"reports/{ENGINE}/{report.rca_id}.md"
        body = _render_markdown(report)
        try:
            self._s3.put_object(Bucket=S3_REPORT_BUCKET, Key=key, Body=body, ContentType="text/markdown")
            logger.info("Report saved to s3://%s/%s", S3_REPORT_BUCKET, key)
            return key
        except Exception:
            logger.exception("Failed to save report to S3")
            return ""

    def search_similar(self, query_text: str) -> list[ReportMatch]:
        if not self._vectors_enabled or self._embedding is None:
            return []
        try:
            query_vector = self._embedding.embed_query(query_text)
        except Exception:
            logger.exception("Failed to embed query text, skipping report search")
            return []

        def query() -> dict:
            return self._s3v.query_vectors(
                vectorBucketName=S3_VECTOR_BUCKET_NAME,
                indexName=S3_VECTOR_REPORT_INDEX,
                queryVector={"float32": query_vector},
                topK=REPORT_TOP_K,
            )

        response = retry_with_backoff(
            query,
            max_retries=_SEARCH_MAX_RETRIES,
            operation="report search",
        )
        if response is None:
            return []

        matches = []
        for item in response.get("vectors", []):
            similarity = item.get("distance", 0.0)
            if similarity < REPORT_SIMILARITY_THRESHOLD:
                continue
            metadata = item.get("metadata", {})
            matches.append(
                ReportMatch(
                    rca_id=item.get("key", ""),
                    similarity=similarity,
                    incident_summary=metadata.get("incident_summary", ""),
                    root_cause=metadata.get("root_cause", ""),
                    hypothesis_path=metadata.get("hypothesis_path", ""),
                    confirmed=metadata.get("confirmed", "false") == "true",
                )
            )
        return matches

    def save_vectors(self, report: RcaReport, *, scoping_result: ScopingResult | None = None) -> bool:
        if not self._vectors_enabled or self._embedding is None:
            logger.info("S3 Vectors not configured, skipping report indexing")
            return False

        metric_name = ""
        if scoping_result and scoping_result.raw_alarm and scoping_result.raw_alarm.trigger:
            metric_name = scoping_result.raw_alarm.trigger.metric_name

        parts = {
            "장애유형": _truncate(report.root_cause),
            "증상": _truncate(report.incident_summary),
            "메트릭": _truncate(metric_name),
        }
        embed_text = " | ".join(f"{k}: {v}" for k, v in parts.items() if v)
        try:
            vector = self._embedding.embed_document(embed_text)
        except Exception:
            logger.exception("Failed to embed report text")
            return False

        hypothesis_path_str = report.hypothesis_path[0] if report.hypothesis_path else ""
        metadata = {
            "incident_summary": _truncate(report.incident_summary),
            "root_cause": _truncate(report.root_cause),
            "hypothesis_path": hypothesis_path_str[:_HYPOTHESIS_PATH_MAX],
            "confirmed": "true" if report.root_cause_confirmed else "false",
            "rca_id": report.rca_id,
        }
        try:
            self._s3v.put_vectors(
                vectorBucketName=S3_VECTOR_BUCKET_NAME,
                indexName=S3_VECTOR_REPORT_INDEX,
                vectors=[
                    {
                        "key": report.rca_id,
                        "data": {"float32": vector},
                        "metadata": metadata,
                    }
                ],
            )
            logger.info("Report %s indexed in S3 Vectors", report.rca_id)
            return True
        except Exception:
            logger.exception("Failed to index report in S3 Vectors")
            return False


def _render_markdown(report: RcaReport) -> str:
    confirmed_label = "Confirmed" if report.root_cause_confirmed else "Unconfirmed (most likely candidate)"
    lines = [
        f"# RCA Report: {report.rca_id}",
        "",
        "## Incident Summary",
        report.incident_summary,
        "",
        f"- **Severity**: {report.severity}",
    ]
    if report.detection_method:
        lines.append(f"- **Detection**: {report.detection_method}")
    lines.append("")

    if report.impact_summary:
        lines.extend(["## Impact Assessment", report.impact_summary, ""])

    lines.extend(
        [
            "## Root Cause",
            f"**Status**: {confirmed_label}",
            f"**Confidence**: {report.confidence_score:.2f}",
            "",
            report.root_cause,
            "",
        ]
    )
    if report.five_whys:
        lines.append("## 5 Whys")
        for step in report.five_whys:
            lines.append(f"- {step}")
        lines.append("")
    if report.hypothesis_path:
        lines.append("## Hypothesis Path")
        for p in report.hypothesis_path:
            lines.append(f"- {p}")
        lines.append("")
    if report.evidence_list:
        lines.append("## Evidence")
        for e in report.evidence_list:
            lines.append(f"- {e}")
        lines.append("")
    if report.timeline:
        lines.append("## Timeline")
        for t in report.timeline:
            lines.append(f"- {t}")
        lines.append("")
    if report.temporary_mitigation:
        lines.extend(["## Temporary Mitigation", report.temporary_mitigation, ""])
    if report.permanent_remediation:
        lines.extend(["## Permanent Remediation", report.permanent_remediation, ""])
    if report.action_items:
        lines.append("## Action Items")
        for item in report.action_items:
            lines.append(f"- {item}")
        lines.append("")
    if report.lessons_learned:
        lines.extend(["## Lessons Learned", report.lessons_learned, ""])
    if report.rejected_hypotheses:
        lines.append("## Rejected Hypotheses")
        for r in report.rejected_hypotheses:
            lines.append(f"- {r}")
        lines.append("")
    return "\n".join(lines)
