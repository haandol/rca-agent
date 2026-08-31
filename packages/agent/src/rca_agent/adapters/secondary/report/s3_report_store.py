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
from rca_agent.ports.dto.models import Playbook, RcaReport, ReportMatch, ScopingResult
from rca_agent.ports.interfaces.embedding import EmbeddingPort
from rca_agent.ports.interfaces.report_store import ReportStorePort
from rca_agent.utils.embed_key import EMBED_FIELD_MAX, build_embed_key
from rca_agent.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

_SEARCH_MAX_RETRIES = 3
_HYPOTHESIS_PATH_MAX = 200


def _truncate(text: str, max_len: int = EMBED_FIELD_MAX) -> str:
    return text[:max_len].strip() if text else ""


class S3ReportStore(ReportStorePort):
    def __init__(self, s3_client=None, s3_vectors_client=None, embedding: EmbeddingPort | None = None):
        self._s3 = s3_client
        self._s3v = s3_vectors_client
        self._embedding = embedding

    @property
    def _vectors_enabled(self) -> bool:
        return bool(S3_VECTOR_BUCKET_NAME and self._s3v)

    def save(
        self,
        report: RcaReport,
        *,
        playbook: Playbook | None,
        claim_token: str | None = None,
        attempt: int | None = None,
    ) -> str:
        if not S3_REPORT_BUCKET or self._s3 is None:
            logger.info("S3 report bucket not configured, skipping upload")
            return ""
        if claim_token:
            attempt_segment = f"attempt-{attempt or 1}-{claim_token}"
            key = f"reports/{ENGINE}/{report.rca_id}/{attempt_segment}/report.md"
        else:
            key = f"reports/{ENGINE}/{report.rca_id}.md"

        body = _render_markdown(report, playbook)
        # 사람은 서술을 읽고 승인하는데 실행은 구조를 따라가므로, 둘이 어긋난 리포트는
        # 저장하지 않는다. 이 엔진에서 절차 섹션은 모델이 쓰지 않고 실행 주체가 읽는
        # 것과 같은 플레이북에서 렌더링되므로, 이 검사가 막는 것은 모델의 발산이 아니라
        # 렌더러가 절차를 빠뜨리거나 재배열하는 회귀다.
        mismatch = _step_mismatch(body, playbook)
        if mismatch:
            logger.error("Refusing to save report %s: %s", report.rca_id, mismatch)
            return ""

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
                returnMetadata=True,
                # 거리를 요청하지 않으면 응답에 그 필드가 없다. 없는 값을 최대 거리로
                # 읽으면 모든 후보의 유사도가 0이 되어 임계값에서 전부 탈락하고,
                # 검색은 오류 없이 빈 결과만 돌려준다.
                returnDistance=True,
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
            similarity = 1.0 - float(item.get("distance", 1.0))
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

        embed_text = build_embed_key(
            failure_type=report.root_cause,
            symptom=report.incident_summary,
            metric_name=metric_name,
        )
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


_PLAYBOOK_SECTION = "## 대응 플레이북"


def _render_playbook_section(playbook: Playbook | None) -> list[str]:
    """Render the procedure a person reads before approving it.

    A draft label is fixed into the body because analysis has never run any of
    these steps. The status shown on the approval screen is the playbook's current
    value, not this label — a prior retrospective may have promoted the procedure
    since, and the body is fixed at analysis time.
    """
    lines = [_PLAYBOOK_SECTION, ""]
    if playbook is None:
        lines.extend(
            [
                "플레이북 생성이 실패해 이 리포트에는 실행 절차가 없다. 원인 분석 결과는 "
                "위 내용으로 완결이며, 승인할 절차가 없으므로 실행 대상이 아니다.",
                "",
            ]
        )
        return lines
    if not playbook.execution_steps:
        lines.extend(
            [
                "확정된 근본 원인이 없어 실행 절차를 만들지 않았다. 추측 절차가 승인 버튼 뒤에 "
                "놓이면 사람이 검증된 절차로 오인하기 때문이다. 이 리포트의 조치 항목은 사람이 "
                "판단해 수행할 권고이며 실행 대상이 아니다.",
                "",
            ]
        )
        return lines

    lines.extend(
        [
            "이 플레이북은 **초안(DRAFT)**이며 아직 실행으로 검증되지 않았다. 실행과 회고를 "
            "거친 뒤에야 검증된 절차가 된다.",
            "",
            "각 절차의 작업은 자연어다. 대상 리소스 식별자와 리전은 실행 시점의 알람 컨텍스트에서 결정된다.",
            "",
        ]
    )
    for index, step in enumerate(playbook.execution_steps, start=1):
        lines.append(f"### {index}. {step.step_id}")
        lines.append("")
        lines.append(f"- **의도**: {step.intent or 'N/A'}")
        lines.append(f"- **수행할 작업**: {step.action}")
        lines.append(f"- **성공 판정 기준**: {step.success_criteria}")
        lines.append("")

    if playbook.permanent_remediation:
        lines.extend(
            [
                "되돌릴 수 없는 조치(삭제·종료·자격 증명 회수)는 위 절차에 담기지 않는다. "
                "실행 계층이 거부하므로 그런 조치는 영구 조치 권고로만 남는다.",
                "",
            ]
        )
    return lines


def _step_mismatch(body: str, playbook: Playbook | None) -> str:
    """Return why the narrative and the structure disagree, or an empty string.

    Checks identifiers and their order, not prose: the narrative is free to
    describe a step differently, but it must describe the same steps in the same
    sequence the execution agent will follow.
    """
    step_ids = [step.step_id for step in playbook.execution_steps] if playbook else []
    if not step_ids:
        return ""

    section = body.split(_PLAYBOOK_SECTION, 1)
    if len(section) == 1:
        return "report has no playbook section to approve"
    rendered = section[1]

    missing = [step_id for step_id in step_ids if step_id not in rendered]
    if missing:
        return f"playbook steps missing from the report narrative: {', '.join(missing)}"

    positions = [rendered.index(step_id) for step_id in step_ids]
    if positions != sorted(positions):
        return "playbook steps appear in a different order in the report narrative"
    return ""


def _render_markdown(report: RcaReport, playbook: Playbook | None) -> str:
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
        ]
    )
    if report.selected_hypothesis_id:
        lines.append(f"**Selected hypothesis ID**: `{report.selected_hypothesis_id}`")
    if report.selected_hypothesis_title:
        lines.append(f"**Hypothesis title**: {report.selected_hypothesis_title}")
    lines.extend(["", report.root_cause, ""])
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
    lines.extend(_render_playbook_section(playbook))
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
