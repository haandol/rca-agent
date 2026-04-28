from __future__ import annotations

import logging

from rca_agent.config.settings import S3_REPORT_BUCKET
from rca_agent.ports.dto.models import RcaReport
from rca_agent.ports.interfaces.report_store import ReportStorePort

logger = logging.getLogger(__name__)


class S3ReportStore(ReportStorePort):
    def __init__(self, s3_client=None):
        self._s3 = s3_client

    def save(self, report: RcaReport) -> str:
        if not S3_REPORT_BUCKET or self._s3 is None:
            logger.info("S3 report bucket not configured, skipping upload")
            return ""
        key = f"reports/{report.rca_id}.md"
        body = _render_markdown(report)
        try:
            self._s3.put_object(Bucket=S3_REPORT_BUCKET, Key=key, Body=body, ContentType="text/markdown")
            logger.info("Report saved to s3://%s/%s", S3_REPORT_BUCKET, key)
            return key
        except Exception:
            logger.exception("Failed to save report to S3")
            return ""


def _render_markdown(report: RcaReport) -> str:
    confirmed_label = "Confirmed" if report.root_cause_confirmed else "Unconfirmed (most likely candidate)"
    lines = [
        f"# RCA Report: {report.rca_id}",
        "",
        "## Incident Summary",
        report.incident_summary,
        "",
        "## Root Cause",
        f"**Status**: {confirmed_label}",
        f"**Confidence**: {report.confidence_score:.2f}",
        "",
        report.root_cause,
        "",
    ]
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
    if report.temporary_mitigation:
        lines.extend(["## Temporary Mitigation", report.temporary_mitigation, ""])
    if report.permanent_remediation:
        lines.extend(["## Permanent Remediation", report.permanent_remediation, ""])
    if report.timeline:
        lines.append("## Timeline")
        for t in report.timeline:
            lines.append(f"- {t}")
        lines.append("")
    if report.rejected_hypotheses:
        lines.append("## Rejected Hypotheses")
        for r in report.rejected_hypotheses:
            lines.append(f"- {r}")
        lines.append("")
    return "\n".join(lines)
