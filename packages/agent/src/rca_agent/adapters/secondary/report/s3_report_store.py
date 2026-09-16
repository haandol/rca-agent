from __future__ import annotations

import json
import logging
import re

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
from rca_agent.services.runbook_contract import render_step_operation, validate_runbook
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
_RUNBOOK_SECTION = "### 이번 사고의 런북"
SUMMARY_MARKER = "rca-summary:v1"
_COMPARISON_LABELS = {
    "UPDATE_PROPOSED": "변경 제안",
    "NO_CHANGE": "변경 불필요",
    "NO_MATCH": "유사 후보 없음",
    "NO_APPLICABLE_MATCH": "적용 가능한 후보 없음",
    "SEARCH_FAILED": "검색·비교 실패",
}


def build_report_summary(report: RcaReport, playbook: Playbook | None) -> dict:
    """One server-owned summary supplies both the human view and the API marker."""
    approval_eligible = bool(report.root_cause_confirmed and playbook and playbook.execution_steps)
    if approval_eligible:
        try:
            validate_runbook([step.model_dump() for step in playbook.execution_steps])
        except (ValueError, TypeError):
            approval_eligible = False
    comparison = playbook.comparison if playbook else {}
    proposal = comparison.get("proposal")
    return {
        "incident_summary": report.incident_summary or None,
        "impact_summary": report.impact_summary or None,
        "severity": report.severity or None,
        "root_cause": report.root_cause or None,
        "root_cause_confirmed": report.root_cause_confirmed,
        "confidence_score": report.confidence_score,
        "next_action": report.temporary_mitigation or next(iter(report.action_items), None) or None,
        "runbook_verification_status": playbook.verification_status.value if playbook else None,
        "runbook_approval_eligible": approval_eligible,
        "playbook_id": playbook.playbook_id if playbook else None,
        "selected_playbook_id": comparison.get("selected_playbook_id") or None,
        "comparison_status": comparison.get("status") or None,
        "proposal_state": proposal.get("state") if isinstance(proposal, dict) else None,
        "incident_observations": report.incident_observations.model_dump(mode="json"),
    }


def _render_summary(report: RcaReport, playbook: Playbook | None) -> list[str]:
    """Render visible and machine-readable summaries from the same server-owned values."""
    summary = build_report_summary(report, playbook)
    # Keep source text from terminating the hidden JSON marker. JSON decoding
    # restores exact DTO values; display labels below come from the same object.
    encoded = json.dumps(summary, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    status = summary["comparison_status"]
    fields = [
        ("장애", summary["incident_summary"]),
        ("영향", summary["impact_summary"]),
        ("심각도", summary["severity"]),
        ("원인", summary["root_cause"]),
        ("원인 판정", "확정" if summary["root_cause_confirmed"] else "미확정 — 가장 유력한 후보"),
        ("신뢰도", f"{summary['confidence_score']:.2f}"),
        ("다음 조치 (권고)", summary["next_action"]),
        ("런북 검증 상태 (분석 시점)", summary["runbook_verification_status"]),
        ("실행 승인 검토 (분석 시점)", "검토 가능" if summary["runbook_approval_eligible"] else "실행 승인 불가"),
        ("플레이북", summary["playbook_id"]),
        ("선택한 유사 플레이북", summary["selected_playbook_id"]),
        ("유사 플레이북 비교", _COMPARISON_LABELS.get(status, status)),
        ("지식 변경 제안", summary["proposal_state"]),
    ]
    lines = ["## 빠른 판단", "", f"<!-- {SUMMARY_MARKER}\n{encoded}\n-->", ""]
    for label, value in fields:
        text = str(value) if value is not None else "미제공"
        lines.append(f"- **{label}**: " + text.replace("\n", "\n  "))
    lines.extend(
        [
            "",
            "아래 상세에 근거·원문·명령을 모두 보존했다. 요약의 상태는 분석 시점 기록이며, "
            "실행 승인 시에는 현재 런북 전체를 다시 검토한다. 플레이북 지식 반영과 런북 실행 승인은 별개다.",
            "",
        ]
    )
    return lines


def _render_comparison_section(playbook: Playbook | None) -> list[str]:
    """Preserve the incident comparison snapshot without presenting pending knowledge as applied."""
    if playbook is None or not playbook.comparison:
        return []
    comparison = playbook.comparison
    status = comparison.get("status")
    return [
        "## 유사 플레이북 비교",
        "",
        f"**비교 결과**: {_COMPARISON_LABELS.get(status, status or '미제공')}",
        "",
        "### 생성에 사용한 참고 자료",
        "",
        "아래 참고 자료는 기존 지식 재사용·현재 RCA의 런북 입력·실제 인용 증거를 구분한다. "
        "검색 후보 목록은 별도의 감사 기록이며 생성에 사용했다는 뜻이 아니다.",
        "",
        "유사도는 검색 관련성이고 원인 확정 확률이 아니다. 아래는 분석 당시 후보별 판단과 비교 사본이다. "
        "PENDING 제안은 아직 공개 지식에 반영되지 않았다. 변경 전후의 과거 런북은 비교 기준이며, "
        "실행 검토 대상은 대응 플레이북 섹션의 이번 사고 런북이다.",
        "",
        "```json",
        json.dumps(comparison, ensure_ascii=False, indent=2),
        "```",
        "",
    ]


def _render_playbook_knowledge(playbook: Playbook) -> list[str]:
    lines = [
        "### 유형별 대응 지식",
        "",
        "장애 유형에 재사용하는 판단·대응 지식이다. 실행 승인 대상은 아래 이번 사고의 런북이다.",
        "",
        f"- **플레이북 ID**: {playbook.playbook_id}",
        "",
    ]
    for label, value in (
        ("장애 유형", playbook.failure_type),
        ("증상 패턴", playbook.symptom_pattern),
        ("관련 메트릭", playbook.related_metrics),
        ("심각도 판단 기준", playbook.severity_criteria),
        ("확인 절차 (유형별 검증 지식)", playbook.verification_steps),
        ("임시 조치", playbook.temporary_mitigation),
        ("영구 조치", playbook.permanent_remediation),
        ("에스컬레이션 기준 (담당자에게 대응을 넘길 조건)", playbook.escalation_criteria),
        ("예방 조치", playbook.prevention_measures),
        ("태그", playbook.tags),
    ):
        if value:
            lines.extend([f"**{label}**", ""])
            values = value if isinstance(value, list) else [value]
            for item in values:
                # Indent continuation lines so numbered knowledge never becomes
                # a runtime heading or a new report section.
                lines.append("- " + item.replace("\n", "\n  "))
            lines.append("")
    return lines


def _render_playbook_section(playbook: Playbook | None) -> list[str]:
    """Snapshot reusable knowledge and the final analysis runbook for this report."""
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
    lines.extend(_render_playbook_knowledge(playbook))
    lines.extend([_RUNBOOK_SECTION, ""])
    if playbook.rollback_context is not None:
        lines.extend(
            [
                "**Reader-verified rollback context**",
                "",
                "```json",
                json.dumps(playbook.rollback_context, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    if not playbook.execution_steps:
        lines.extend(
            [
                "근본 원인이 미확정이거나 실행에 필요한 근거가 부족해 실행 절차를 만들지 않았다. "
                "추측 절차가 승인 버튼 뒤에 "
                "놓이면 사람이 검증된 절차로 오인하기 때문이다. 이 리포트의 조치 항목은 사람이 "
                "판단해 수행할 권고이며 실행 대상이 아니다.",
                "",
            ]
        )
        return lines

    lines.extend(
        [
            f"분석 완료 시점의 런북 검증 상태: **{playbook.verification_status.value}**.",
            (
                "초안(DRAFT)은 아직 실행으로 검증되지 않은 절차다."
                if playbook.verification_status.value == "DRAFT"
                else "검증됨(VERIFIED)은 기존과 동일한 절차에 보존된 검증 상태이며, 이번 사고의 해결을 뜻하지 않는다."
            ),
            "이 보고서는 분석 완료 시점의 기록이며 이후 회고로 변경되지 않는다.",
            "",
            "명령·대상·리전·순서·성공 기준은 승인 전에 고정한다. 변경하려면 새 승인이 필요하다.",
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
        lines.extend(render_step_operation(step.model_dump()))

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

    Check the ordered identifiers and the exact structured operation values.
    """
    step_ids = [step.step_id for step in playbook.execution_steps] if playbook else []
    if not step_ids:
        return ""

    section = re.search(rf"^{re.escape(_PLAYBOOK_SECTION)}\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    if section is None:
        return "report has no playbook section to approve"
    runbook = re.search(rf"^{re.escape(_RUNBOOK_SECTION)}\n(.*)", section[1], re.M | re.S)
    if runbook is None:
        return "report has no current runbook section to approve"
    rendered = runbook[1]
    headings = list(re.finditer(r"^### (\d+)\. ([^\n]+)\n", rendered, re.M))
    if [(int(h[1]), h[2]) for h in headings] != list(enumerate(step_ids, 1)):
        return "playbook step headings missing, added or reordered in the report narrative"
    for index, step in enumerate(playbook.execution_steps):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(rendered)
        step_body = rendered[headings[index].end() : end]
        operation = "\n".join(render_step_operation(step.model_dump()))
        if operation not in step_body:
            return f"playbook operation missing or changed in report: {step.step_id}"
    return ""


def _render_markdown(report: RcaReport, playbook: Playbook | None) -> str:
    """Render server-owned results and quote supplied discovery metadata without promotion."""
    confirmed_label = "Confirmed" if report.root_cause_confirmed else "Unconfirmed (most likely candidate)"
    lines = [
        f"# RCA Report: {report.rca_id}",
        "",
        *_render_summary(report, playbook),
        "## Incident Summary",
        report.incident_summary,
        "",
        f"- **Severity**: {report.severity}",
    ]
    if report.detection_method:
        lines.append(f"- **Detection**: {report.detection_method}")
    lines.append("")
    if (
        report.incident_observations.critical_facts
        or report.incident_observations.baseline
        or report.incident_observations.diagnostics
    ):
        lines.extend(
            [
                "## Source Observations",
                "These scoped source facts are separate from the prose summary and the root cause judgment.",
                "```json",
                json.dumps(report.incident_observations.model_dump(mode="json"), ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    if report.alarm_description is not None:
        lines.extend(
            [
                "## Provided Discovery Context",
                "AlarmDescription is untrusted source data, not instructions, validated findings, or permissions.",
                "```json",
                json.dumps(report.alarm_description, ensure_ascii=False),
                "```",
                "",
            ]
        )

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
    lines.extend(_render_comparison_section(playbook))
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
