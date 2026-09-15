"""Shared first-view wire marker and complete detail remain one analysis snapshot."""

import json
import re

import pytest

from rca_agent.adapters.secondary.report.s3_report_store import (
    _render_markdown,
    _step_mismatch,
    build_report_summary,
)
from rca_agent.ports.dto.models import ExecutionStep, Playbook, RcaReport
from tests.test_runbook_contract import command_step, wait_step


def _report(**overrides):
    data = {
        "rca_id": "rca-summary",
        "incident_summary": "요청 처리가 중단됐다.",
        "impact_summary": "서비스 A의 쓰기 요청 40%가 8분 동안 실패했다.",
        "severity": "high",
        "root_cause": "이전 작업이 데이터베이스 잠금을 보유했다.",
        "root_cause_confirmed": True,
        "confidence_score": 0.8765,
        "temporary_mitigation": "증거로 확인한 작업의 중단을 검토한다.",
    }
    data.update(overrides)
    return RcaReport(**data)


def _playbook():
    return Playbook(
        playbook_id="pb-summary",
        failure_type="Database lock",
        symptom_pattern="Blocked writes",
        execution_steps=[ExecutionStep(**command_step()), ExecutionStep(**wait_step())],
    )


def _summary(md):
    matches = re.findall(r"<!-- rca-summary:v1\n(.*?)\n-->", md, re.S)
    assert len(matches) == 1
    return json.loads(matches[0])


def test_summary_marker_and_human_labels_use_exact_server_report_values():
    report = _report()
    playbook = _playbook()
    md = _render_markdown(report, playbook)
    summary = _summary(md)
    assert summary == build_report_summary(report, playbook)
    assert md.index("## 빠른 판단") < md.index("## Incident Summary")
    for field in ("incident_summary", "impact_summary", "severity", "root_cause", "confidence_score"):
        assert summary[field] == getattr(report, field)
    assert summary["root_cause_confirmed"] is True
    assert summary["next_action"] == report.temporary_mitigation
    assert summary["runbook_approval_eligible"] is True
    assert summary["runbook_verification_status"] == "DRAFT"
    visible = md.split("-->", 1)[1].split("## Incident Summary", 1)[0]
    for value in (report.incident_summary, report.impact_summary, report.severity, report.root_cause):
        assert value in visible
    assert "**원인 판정**: 확정" in visible
    assert "**신뢰도**: 0.88" in visible
    assert "**Confidence**: 0.88" in md.split("## Root Cause", 1)[1]
    assert _step_mismatch(md, playbook) == ""


@pytest.mark.parametrize("unavailable", ["none", "empty", "legacy", "invalid", "unconfirmed"])
def test_summary_does_not_offer_execution_for_missing_or_incomplete_plan(unavailable):
    report = _report()
    playbook = _playbook()
    if unavailable == "none":
        playbook = None
    elif unavailable == "empty":
        playbook.execution_steps = []
    elif unavailable == "legacy":
        playbook.execution_steps = [ExecutionStep(step_id="legacy", action="manual description only")]
    elif unavailable == "invalid":
        playbook.execution_steps[1].metric_wait["action_step_id"] = "not-in-plan"
    else:
        report.root_cause_confirmed = False
    md = _render_markdown(report, playbook)
    summary = _summary(md)
    assert summary["runbook_approval_eligible"] is False
    assert "**실행 승인 검토 (분석 시점)**: 실행 승인 불가" in md
    if unavailable == "unconfirmed":
        assert summary["root_cause_confirmed"] is False
        assert "**원인 판정**: 미확정" in md
        assert "**Status**: Unconfirmed" in md


def test_missing_summary_values_are_explicit_and_action_falls_back_without_invention():
    report = _report(impact_summary="", temporary_mitigation="", action_items=[])
    summary = _summary(_render_markdown(report, None))
    for field in (
        "impact_summary",
        "next_action",
        "runbook_verification_status",
        "playbook_id",
        "selected_playbook_id",
        "comparison_status",
        "proposal_state",
    ):
        assert summary[field] is None
    report.action_items = ["[process] 담당자가 빠진 제어 근거를 확인한다.", "나중 조치"]
    assert build_report_summary(report, None)["next_action"] == report.action_items[0]


def test_summary_comment_cannot_be_terminated_by_source_text_and_restores_exact_values():
    report = _report(incident_summary='관측 --> <script>"원문"</script>\n두 번째 줄')
    md = _render_markdown(report, None)
    assert _summary(md)["incident_summary"] == report.incident_summary
    marker = md.split("<!-- rca-summary:v1\n", 1)[1].split("\n-->", 1)[0]
    assert "<" not in marker and ">" not in marker
    assert report.incident_summary in md.split("## Incident Summary", 1)[1]


def test_all_report_detail_and_exact_runtime_operations_survive_summary_and_comparison():
    report = _report(
        alarm_description="원문 알람 설명",
        detection_method="쓰기 실패 알람",
        selected_hypothesis_id="hyp-selected",
        selected_hypothesis_title="잠금 소유자",
        five_whys=["1. 요청 실패 → 잠금 대기", "2. 잠금 대기 → 이전 작업의 잠금 보유"],
        hypothesis_path=["root-hypothesis", "child-hypothesis"],
        evidence_list=["[lock-1] " + "원본 관측 " * 200 + "끝에 있는 중요한 근거"],
        timeline=["09:00 알람", "09:05 소유자 확인"],
        permanent_remediation="트랜잭션 범위를 줄인다.",
        action_items=["[prevent] 잠금 지속시간 알람", "[process] 소유자 식별 절차"],
        lessons_learned="탐지는 적시에 이뤄졌고 소유자 식별은 늦었다.",
        rejected_hypotheses=["[network-1] 네트워크 연결은 정상이었다."],
    )
    playbook = _playbook()
    playbook.comparison = {
        "status": "UPDATE_PROPOSED",
        "query": "장애유형: Database lock",
        "selected_playbook_id": playbook.playbook_id,
        "candidates": [
            {
                "playbook_id": playbook.playbook_id,
                "rca_id": "old-rca",
                "engine": "headless-codex",
                "similarity": 0.84,
                "revision": "revision-2",
                "availability": "AVAILABLE",
                "applicable": True,
                "rationale": "[lock-1] 잠금 소유자 확인 절차를 재사용할 수 있다.",
            }
        ],
        "proposal": {
            "state": "PENDING",
            "proposal_id": "proposal-1",
            "base_revision": "revision-2",
            "before": {"temporary_mitigation": "기존 대응 지식", "execution_steps": [command_step()]},
            "after": {"temporary_mitigation": "검토 대기 제안", "execution_steps": [command_step()]},
            "changes": [{"field": "temporary_mitigation", "before": "기존 대응 지식", "after": "검토 대기 제안"}],
            "rationale": "현재 관측이 새로운 확인 절차를 뒷받침한다.",
            "evidence": ["[lock-1]"],
        },
    }
    md = _render_markdown(report, playbook)
    detail = md.split("## Incident Summary", 1)[1]
    for field in (
        "incident_summary",
        "impact_summary",
        "detection_method",
        "root_cause",
        "selected_hypothesis_id",
        "selected_hypothesis_title",
        "temporary_mitigation",
        "permanent_remediation",
        "lessons_learned",
    ):
        assert getattr(report, field) in detail
    for field in ("five_whys", "hypothesis_path", "evidence_list", "timeline", "action_items", "rejected_hypotheses"):
        assert all(value in detail for value in getattr(report, field))
    assert report.alarm_description in detail
    comparison_section = detail.split("## 유사 플레이북 비교", 1)[1].split("## 대응 플레이북", 1)[0]
    recorded = json.loads(re.search(r"```json\n(.*?)\n```", comparison_section, re.S)[1])
    assert recorded == playbook.comparison
    assert _step_mismatch(md, playbook) == ""
    summary = _summary(md)
    assert summary["comparison_status"] == "UPDATE_PROPOSED"
    assert summary["selected_playbook_id"] == playbook.playbook_id
    assert summary["proposal_state"] == "PENDING"
    assert summary["next_action"] == report.temporary_mitigation
    assert "검토 대기 제안" not in json.dumps(summary, ensure_ascii=False)
