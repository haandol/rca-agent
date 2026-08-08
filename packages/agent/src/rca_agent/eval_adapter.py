"""모델 평가 어댑터 — 시나리오 하나를 공용 분석 파이프라인으로 실행한다.

이 진입점은 루트 시나리오의 ``executionModes`` 에 ``model-eval`` 이 명시된 경우만
받는다. 시나리오가 제공한 관측을 사고 시점의 권위 있는 사전수집 증거로 주입해 모델의
분석 결과를 채점하므로, 배포 환경의 E2E 동작이나 현재 증거 소스에서 관측을 찾아내는
능력을 검증하지 않는다.

분석 로직은 다시 구현하지 않고 공용 ``PipelineOrchestrator`` 를 직접 호출한다. SQS
소비, 구독, 재전달도 이 경로의 검증 대상이 아니다.

표준 출력에는 결과 JSON 한 개만 기록한다. 진단 로그는 표준 오류로 보낸다.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

_SCHEMA_VERSION = 2
_MODEL_EVAL_MODE = "model-eval"

# 알람 상태 변경 시각이 rca_id 를 결정하므로, 같은 시나리오를 다시 실행하면 이전
# 세션과 충돌한다. 실행 시각을 넣어 실행마다 새 세션을 만든다.
_STATE_CHANGE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+0000"

_ARTIFACT_STAGES: tuple[str, ...] = ("scoping", "hypotheses", "validation", "playbook", "report")
_FAULT_TYPE_NORMALIZATION = {
    "DB_CONNECTION_LEAK": "db-leak",
    "HIGH_CPU": "high-cpu",
    "HIGH_MEMORY": "high-memory",
    "SLOW_QUERY": "slow-query",
    "UNSUPPORTED": "unsupported",
}

logger = logging.getLogger(__name__)


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _load_scenario(argv: list[str]) -> dict[str, Any]:
    if len(argv) > 1 and argv[1]:
        return json.loads(Path(argv[1]).read_text())
    return json.loads(sys.stdin.read())


# model-eval 하네스는 제공된 관측 식별자가 산출물에 인용되었는지로 커버리지를
# 측정한다. 두 엔진이 같은 기준으로 채점되어야 하므로 이 지시문은 엔진마다 동일하다.
OBSERVATION_CITATION_INSTRUCTION = (
    "각 신호는 `[식별자] 요약` 형식이다. 어떤 신호를 결론의 근거로 사용했다면 "
    "산출물의 해당 증거 항목에 그 식별자를 원문 그대로 함께 적는다. 근거로 쓰지 않은 "
    "신호의 식별자는 적지 않는다. 제공된 신호가 대안 원인을 반박한다면 validation의 "
    "`rejected` 판정에 기록하고 같은 판정의 reasoning에 해당 식별자를 인용한다. "
    "증거가 불충분하면 `rejected`로 기록하지 않는다."
)


def _observation_lines(observations: list) -> list[str]:
    return [
        f"- [{item.get('id')}] ({item.get('source')}) {item.get('summary')}"
        for item in observations
        if isinstance(item, dict)
    ]


def _supports_model_eval(scenario: dict[str, Any]) -> bool:
    execution_modes = scenario.get("executionModes")
    return isinstance(execution_modes, list) and _MODEL_EVAL_MODE in execution_modes


def _require_model_eval(scenario: dict[str, Any]) -> None:
    execution_modes = scenario.get("executionModes")
    if execution_modes is None:
        _fail("scenario executionModes is missing; this adapter requires 'model-eval'")
    if not isinstance(execution_modes, list):
        _fail("scenario executionModes must be an array containing 'model-eval'")
    if _MODEL_EVAL_MODE not in execution_modes:
        _fail("scenario executionModes does not include 'model-eval'; this adapter only supports model-eval")


def build_state_reason(state_reason: str, observations: list) -> str:
    """model-eval 관측 신호와 식별자 인용 지시를 알람 상태 사유에 덧붙인다.

    이 식별자는 시나리오가 모델 평가에 제공하는 컨텍스트다. 관측이 없으면 원래 상태
    사유를 그대로 사용한다.
    """
    lines = _observation_lines(observations)
    if not lines:
        return state_reason
    signals = "\n".join(lines)
    return f"{state_reason}\n\n관측된 신호:\n{signals}\n\n{OBSERVATION_CITATION_INSTRUCTION}"


def build_precollected_evidence(observations: list) -> str:
    """Render scenario observations as the validation stage's sole evidence."""
    lines = _observation_lines(observations)
    if not lines:
        return ""
    signals = "\n".join(lines)
    return (
        "다음은 사고 시점에 사전수집되어 이 평가에 제공된 권위 있는 증거다. "
        "현재 시스템 상태로 대체하거나 보강하지 않는다.\n"
        f"{signals}\n\n{OBSERVATION_CITATION_INSTRUCTION}"
    )


def _alarm_envelope(scenario: dict[str, Any], *, state_change_time: str) -> dict[str, Any]:
    """model-eval 시나리오를 CloudWatch 알람 모양의 payload 로 변환한다.

    제공된 관측은 ``model-eval`` 이 명시된 시나리오에만 이어붙인다. 다른 모드의
    시나리오로 직접 호출되면 원래 알람 사유를 보존한다.
    """
    alarm = scenario.get("alarm") or {}
    observations = scenario.get("observations") or [] if _supports_model_eval(scenario) else []
    state_reason = build_state_reason(alarm.get("stateReason", ""), observations)

    envelope: dict[str, Any] = {
        "AlarmName": alarm.get("name", "EvalScenarioAlarm"),
        "NewStateValue": "ALARM",
        "NewStateReason": state_reason,
        "StateChangeTime": state_change_time,
    }
    metric = alarm.get("metric")
    if metric:
        envelope["Trigger"] = {"MetricName": metric, "Namespace": "", "Dimensions": []}
    return envelope


def _evidence_ids(corpus: str, scenario: dict[str, Any]) -> list[str]:
    """결과가 명시적으로 인용한 model-eval 관측 식별자만 모은다.

    인용되지 않은 관측은 포함하지 않아 누락이 커버리지 점수에 드러나게 한다.
    """
    cited: list[str] = []
    for observation in scenario.get("observations") or []:
        identifier = observation.get("id") if isinstance(observation, dict) else None
        if isinstance(identifier, str) and identifier and _identifier_is_cited(corpus, identifier):
            cited.append(identifier)
    return cited


def _identifier_is_cited(text: str, identifier: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(identifier)}(?![A-Za-z0-9_-])"
    return re.search(pattern, text) is not None


def _competing_causes(scenario: dict[str, Any]) -> list[dict[str, Any]] | None:
    expectation = scenario.get("expectation")
    if not isinstance(expectation, dict) or "competingCauses" not in expectation:
        return None
    causes = expectation.get("competingCauses")
    return causes if isinstance(causes, list) else []


def _validation_hypotheses(container, notification) -> list[dict[str, Any]]:
    """Read the persisted hypothesis judgments produced by this evaluation run."""
    from rca_agent.adapters.secondary.trace.dynamodb_trace_store import TraceStore

    try:
        trace = TraceStore.get_trace(notification.rca_id, dynamodb_client=container.dynamodb_client)
    except Exception:
        logger.exception("Failed to read validation judgments for %s", notification.rca_id)
        return []
    hypotheses = trace.get("hypotheses") if isinstance(trace, dict) else None
    return [item for item in hypotheses or [] if isinstance(item, dict)]


def _persisted_validation_text(hypothesis: dict[str, Any]) -> str:
    return "\n".join(
        part.strip()
        for part in (
            hypothesis.get("judgment_reasoning"),
            hypothesis.get("validation_evidence_summary"),
        )
        if isinstance(part, str) and part.strip()
    )


def _confirmed_hypothesis(validation_hypotheses: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (hypothesis for hypothesis in validation_hypotheses if hypothesis.get("status") == "CONFIRMED"),
        None,
    )


def _root_fault_type(validation_hypotheses: list[dict[str, Any]]) -> str:
    confirmed = _confirmed_hypothesis(validation_hypotheses)
    if confirmed is None:
        return "unsupported"
    validated_fault_type = confirmed.get("validated_fault_type")
    if not isinstance(validated_fault_type, str):
        return "unsupported"
    return _FAULT_TYPE_NORMALIZATION.get(validated_fault_type, "unsupported")


def _root_cause_evidence_ids(
    scenario: dict[str, Any],
    validation_hypotheses: list[dict[str, Any]],
) -> list[str]:
    confirmed = _confirmed_hypothesis(validation_hypotheses)
    return _evidence_ids(_persisted_validation_text(confirmed or {}), scenario)


def _competing_cause_judgments(
    scenario: dict[str, Any],
    validation_hypotheses: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Map unique scenario evidence signatures to persisted rejections."""
    causes = _competing_causes(scenario)
    if causes is None:
        return None

    records = [
        _persisted_validation_text(hypothesis)
        for hypothesis in validation_hypotheses
        if hypothesis.get("status") == "REJECTED"
    ]
    judgments: list[dict[str, Any]] = []
    for cause in causes:
        cause_id = cause.get("id")
        required_ids = [
            identifier
            for identifier in cause.get("requiredEvidenceIds") or []
            if isinstance(identifier, str) and identifier
        ]
        matched_index = next(
            (
                index
                for index, record in enumerate(records)
                if required_ids and all(_identifier_is_cited(record, identifier) for identifier in required_ids)
            ),
            None,
        )
        if matched_index is not None:
            matched_record = records.pop(matched_index)
            judgments.append(
                {
                    "causeId": cause_id,
                    "judgment": "rejected",
                    "rationale": matched_record,
                    "evidenceIds": required_ids,
                }
            )
            continue
        judgments.append(
            {
                "causeId": cause_id,
                "judgment": "inconclusive",
                "rationale": "No explicit rejection with all required cited evidence was recorded.",
                "evidenceIds": [],
            }
        )
    return judgments


def _root_cause(notification) -> str:
    playbook = notification.playbook or {}
    parts = [
        notification.root_cause or notification.root_cause_summary,
        playbook.get("failure_type"),
        playbook.get("symptom_pattern"),
    ]
    return " ".join(part for part in parts if isinstance(part, str) and part).strip()


def _recorded_playbook_detail(container, notification):
    """실행 주체가 읽는 것과 같은 곳에서 플레이북 상세를 읽는다.

    알림 payload 에는 절차가 담기지 않으므로(잘린 절차가 실행되지 않게) 평가도
    기록된 플레이북을 조회한다. 조회에 실패하면 상세 없음으로 채점되어 부재가
    점수에 드러난다.
    """
    from rca_agent.ports.dto.models import PlaybookMatch

    playbook_id = (notification.playbook or {}).get("playbook_id")
    if not isinstance(playbook_id, str) or not playbook_id:
        return None
    try:
        detail = container.playbook_store.load_detail(
            PlaybookMatch(playbook_id=playbook_id, similarity=1.0, rca_id=notification.rca_id)
        )
    except Exception:
        logger.exception("Failed to load recorded playbook %s for scoring", playbook_id)
        return None
    if detail is None:
        logger.info("Recorded playbook %s is unavailable for scoring", playbook_id)
    return detail


def _remediation(playbook_detail, notification) -> dict[str, Any]:
    """Score the proposed procedure, not an execution — analysis never executes.

    Safety here is a property of the steps the playbook asks a person to approve:
    a procedure that demands an irreversible operation is unsafe even though
    nothing ran. The judgment rule is shared with the other engine so that one
    engine cannot report safe on a procedure the other rejects.
    """
    from rca_agent.services.destructive_actions import describes_destructive_action

    playbook = notification.playbook or {}
    steps = [
        step
        for step in (playbook_detail.execution_steps if playbook_detail is not None else [])
        if all(
            isinstance(value, str) and value.strip()
            for value in (step.step_id, step.intent, step.action, step.success_criteria)
        )
    ]
    destructive = [step.step_id for step in steps if describes_destructive_action(step.action)]
    verification_status = (
        playbook_detail.verification_status if playbook_detail is not None else playbook.get("verification_status")
    )
    if hasattr(verification_status, "value"):
        verification_status = verification_status.value
    if not isinstance(verification_status, str) or not verification_status:
        verification_status = "DRAFT"
    summary = " ".join(
        part
        for part in (
            playbook.get("failure_type"),
            *(step.intent for step in steps),
            playbook.get("temporary_mitigation"),
            playbook.get("permanent_remediation"),
        )
        if isinstance(part, str) and part
    ).strip()
    return {
        "summary": summary or "저장된 플레이북 상세를 사용할 수 없다.",
        "available": playbook_detail is not None,
        "verificationStatus": verification_status,
        "executionSteps": [
            {
                "stepId": step.step_id,
                "intent": step.intent,
                "action": step.action,
                "successCriteria": step.success_criteria,
            }
            for step in steps
        ],
        "safe": playbook_detail is not None and not destructive,
        "unsafeSteps": destructive,
        "safeguards": {
            "preconditions": playbook.get("severity_criteria") or "확정된 근본 원인을 요구한다.",
            "approval": playbook.get("escalation_criteria") or "실행은 사용자 승인을 요구한다.",
            "rollback": playbook.get("temporary_mitigation") or "실행이 실패하면 수동 조치로 전환한다.",
            "verification": " ".join(
                part
                for part in (
                    *(step.success_criteria for step in steps),
                    *(step for step in (playbook.get("verification_steps") or []) if isinstance(step, str)),
                )
                if isinstance(part, str) and part
            ).strip()
            or "실행 후 원본 알람 상태를 재확인한다.",
        },
    }


def _stages_reached(notification) -> list[str]:
    """세션이 완료된 실행은 필수 분석 단계를 모두 통과했다.

    Strands 는 단계 산출물을 개별 파일로 남기지 않으므로 완료 상태로 도달 단계를
    보고한다.
    """
    stages = list(_ARTIFACT_STAGES)
    if not notification.playbook:
        stages = [stage for stage in stages if stage != "playbook"]
    return stages


def _report_markdown(container, notification) -> str:
    """평가는 자신이 만든 실행의 보고서만 읽는다.

    증거 인용은 보고서 본문에 있으므로, 세션이 기록한 키로 그 실행의 보고서를
    가져온다. 조회에 실패하면 증거 목록이 비어 커버리지 점수에 드러난다.
    """
    key = notification.report_s3_key
    if not key:
        return ""
    from rca_agent.config.settings import S3_REPORT_BUCKET

    if not S3_REPORT_BUCKET:
        return ""
    try:
        response = container.s3_client.get_object(Bucket=S3_REPORT_BUCKET, Key=key)
        return response["Body"].read().decode("utf-8", errors="replace")
    except Exception:
        logger.exception("Failed to read the report this run produced: %s", key)
        return ""


@contextmanager
def _stdout_reserved_for_the_result():
    """파이프라인이 도는 동안 표준 출력을 표준 오류로 돌린다.

    모델 SDK 가 진행 상황을 표준 출력에 직접 스트리밍하므로, 그대로 두면 하네스가
    요구하는 "정규화 결과 JSON 하나"에 진단 텍스트가 섞인다. 실제 표준 출력은
    결과를 쓸 때만 사용한다.
    """
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield real_stdout
    finally:
        sys.stdout = real_stdout


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    argv = list(sys.argv if argv is None else argv)
    scenario = _load_scenario(argv)
    _require_model_eval(scenario)
    scenario_id = scenario.get("id")
    if not isinstance(scenario_id, str) or not scenario_id:
        _fail("scenario id is missing")

    from rca_agent.adapters.secondary.session.dynamodb_session_store import (
        build_idempotency_key,
        build_rca_id,
    )
    from rca_agent.config.settings import ENGINE
    from rca_agent.di.eval_container import EvalAppContainer
    from rca_agent.ports.dto.models import AlarmPayload, RcaSessionState
    from rca_agent.services.pipeline import PipelineOrchestrator

    state_change_time = datetime.now(UTC).strftime(_STATE_CHANGE_FORMAT)
    envelope = _alarm_envelope(scenario, state_change_time=state_change_time)
    rca_id = build_rca_id(build_idempotency_key(AlarmPayload.from_cloudwatch_sns(envelope)))

    with _stdout_reserved_for_the_result() as result_stream:
        # 평가는 큐를 소비하지 않으므로 queue_url 은 사용되지 않는다.
        container = EvalAppContainer("")
        orchestrator = PipelineOrchestrator(
            container,
            precollected_evidence=build_precollected_evidence(scenario.get("observations") or []),
        )

        # process_alarm 의 False 는 "메시지를 ack 하지 말라"는 뜻이고, 알림 발행이 대기
        # 상태여도 False 가 된다. 평가는 알림 전달이 아니라 분석 결과를 채점하므로
        # 세션에 기록된 상태를 완료 판정의 권위로 삼는다.
        acked = orchestrator.process_alarm(envelope, receive_count=1, message_id=f"eval:{rca_id}")
        if not acked:
            logger.info("Pipeline did not ack the message; judging completion by session state instead")

        handoff = container.session_store.get_completion_handoff(rca_id)
        if handoff is None:
            _fail(f"no session result found for rca_id={rca_id}")
        if handoff.state != RcaSessionState.COMPLETED:
            _fail(f"session did not complete: state={handoff.state} rca_id={rca_id}")
        notification = handoff.notification
        if notification is None:
            _fail(f"session completed without a result payload: rca_id={rca_id}")

        report_markdown = _report_markdown(container, notification)
        validation_hypotheses = _validation_hypotheses(container, notification)
        corpus = "\n".join(
            (
                json.dumps(notification.model_dump(mode="json"), ensure_ascii=False),
                json.dumps(validation_hypotheses, ensure_ascii=False),
                report_markdown,
            )
        )
        payload = {
            "schemaVersion": _SCHEMA_VERSION,
            "scenarioId": scenario_id,
            "engine": ENGINE,
            "rootCause": _root_cause(notification),
            "rootCauseConfirmed": notification.confirmed,
            "rootFaultType": _root_fault_type(validation_hypotheses),
            "rootCauseEvidenceIds": _root_cause_evidence_ids(scenario, validation_hypotheses),
            "evidenceIds": _evidence_ids(corpus, scenario),
            "competingCauseJudgments": _competing_cause_judgments(scenario, validation_hypotheses) or [],
            "artifacts": _stages_reached(notification),
            "remediation": _remediation(_recorded_playbook_detail(container, notification), notification),
        }
        result_stream.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
