from __future__ import annotations

from rca_agent.ports.dto.models import ReportMatch


def build_report_context(reports: list[ReportMatch], *, include_hypothesis_path: bool = False) -> str:
    """Render similar past RCA reports as a prompt section.

    Scoping omits the hypothesis path; hypothesis generation includes it so the
    LLM can reuse the past "symptom → root cause" reasoning path.
    """
    if not reports:
        return "No similar past RCA reports found."
    lines = ["## Similar Past RCA Reports"]
    for i, r in enumerate(reports, 1):
        status = "confirmed" if r.confirmed else "unconfirmed"
        lines.append(f"{i}. **{r.root_cause}** (similarity: {r.similarity:.2f}, {status})")
        if r.incident_summary:
            lines.append(f"   Incident: {r.incident_summary}")
        if include_hypothesis_path and r.hypothesis_path:
            lines.append(f"   Hypothesis path: {r.hypothesis_path}")
    return "\n".join(lines)
