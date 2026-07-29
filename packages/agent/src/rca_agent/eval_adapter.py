"""라이브 평가 어댑터 — 시나리오 하나를 운영 파이프라인으로 실행하고 정규화 결과를 낸다.

시나리오를 CloudWatch 알람 형태로 변환해 운영과 동일한 파이프라인을 한 번 돌리고,
실행이 남긴 세션 결과를 공통 평가 스키마로 옮긴다. 분석 로직을 여기서 다시 구현하지
않으므로 평가 결과가 운영 동작과 갈라지지 않는다.

SQS 소비 루프를 거치지 않고 파이프라인을 직접 호출한다. 큐·구독·재전달 동작은 이
경로로 검증되지 않는다.

표준 출력에는 결과 JSON 한 개만 기록한다. 진단 로그는 표준 오류로 보낸다.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

_SCHEMA_VERSION = 1

# 알람 상태 변경 시각이 rca_id 를 결정하므로, 같은 시나리오를 다시 실행하면 이전
# 세션과 충돌한다. 실행 시각을 넣어 실행마다 새 세션을 만든다.
_STATE_CHANGE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+0000"

_ARTIFACT_STAGES = ("scoping", "hypotheses", "validation", "remediation", "playbook", "report")

logger = logging.getLogger(__name__)


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _load_scenario(argv: list[str]) -> dict[str, Any]:
    if len(argv) > 1 and argv[1]:
        return json.loads(Path(argv[1]).read_text())
    return json.loads(sys.stdin.read())


def _alarm_envelope(scenario: dict[str, Any], *, state_change_time: str) -> dict[str, Any]:
    """시나리오를 CloudWatch 알람 SNS payload 로 변환한다.

    관측 요약을 NewStateReason 에 이어붙여, 파이프라인이 실제 알람에서 얻는 것과
    같은 형태의 초기 컨텍스트를 받게 한다.
    """
    alarm = scenario.get("alarm") or {}
    observations = scenario.get("observations") or []
    context = "\n".join(
        f"- [{item.get('source')}] {item.get('id')}: {item.get('summary')}"
        for item in observations
        if isinstance(item, dict)
    )
    state_reason = alarm.get("stateReason", "")
    if context:
        state_reason = f"{state_reason}\n\n관측된 신호:\n{context}"

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
    """결과가 실제로 인용한 시나리오 관측 식별자만 모은다.

    인용되지 않은 관측은 포함하지 않아 누락이 커버리지 점수에 드러나게 한다.
    """
    cited: list[str] = []
    for observation in scenario.get("observations") or []:
        identifier = observation.get("id") if isinstance(observation, dict) else None
        if isinstance(identifier, str) and identifier and identifier in corpus:
            cited.append(identifier)
    return cited


def _root_cause(notification) -> str:
    playbook = notification.playbook or {}
    parts = [
        notification.root_cause or notification.root_cause_summary,
        playbook.get("failure_type"),
        playbook.get("symptom_pattern"),
    ]
    return " ".join(part for part in parts if isinstance(part, str) and part).strip()


def _remediation(notification) -> dict[str, Any]:
    """Strands 는 복구를 별도 워커에서 수행하므로 분석 단계의 복구는 미실행이다.

    플레이북의 조치 계획을 제안으로 보고하고, 서버가 기록한 검증 상태를 그대로
    옮긴다. 수행하지 않은 복구를 성공으로 기록하지 않는다.
    """
    playbook = notification.playbook or {}
    verification_steps = [step for step in (playbook.get("verification_steps") or []) if isinstance(step, str)]
    return {
        "summary": " ".join(
            part
            for part in (
                str(notification.fault_type),
                playbook.get("temporary_mitigation"),
                playbook.get("permanent_remediation"),
            )
            if isinstance(part, str) and part
        ).strip(),
        # 분석 단계는 쓰기를 수행하지 않는다. 미실행은 안전한 결과로 취급한다.
        "safe": True,
        "safeguards": {
            "preconditions": playbook.get("severity_criteria") or "확정된 근본 원인과 허용된 fault type을 요구한다.",
            "approval": playbook.get("escalation_criteria") or "허용 목록에 없는 원인은 사람의 판단을 요구한다.",
            "rollback": playbook.get("temporary_mitigation") or "복구가 실패하면 수동 조치로 전환한다.",
            "verification": " ".join(
                part for part in (str(notification.verification_status), *verification_steps) if part
            ).strip()
            or "복구 후 원본 알람 상태를 재확인한다.",
        },
    }


def _stages_reached(notification) -> list[str]:
    """세션이 완료된 실행은 필수 분석 단계를 모두 통과했다.

    Strands 는 단계 산출물을 개별 파일로 남기지 않으므로 완료 상태로 도달 단계를
    보고한다. 복구는 별도 워커의 책임이므로 분석 결과에 포함하지 않는다.
    """
    stages = [stage for stage in _ARTIFACT_STAGES if stage != "remediation"]
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
    scenario_id = scenario.get("id")
    if not isinstance(scenario_id, str) or not scenario_id:
        _fail("scenario id is missing")

    from rca_agent.adapters.secondary.session.dynamodb_session_store import (
        build_idempotency_key,
        build_rca_id,
    )
    from rca_agent.config.settings import ENGINE
    from rca_agent.di.app_container import AppContainer
    from rca_agent.ports.dto.models import AlarmPayload, RcaSessionState
    from rca_agent.services.pipeline import PipelineOrchestrator

    state_change_time = datetime.now(UTC).strftime(_STATE_CHANGE_FORMAT)
    envelope = _alarm_envelope(scenario, state_change_time=state_change_time)
    rca_id = build_rca_id(build_idempotency_key(AlarmPayload.from_cloudwatch_sns(envelope)))

    with _stdout_reserved_for_the_result() as result_stream:
        # 평가는 큐를 소비하지 않으므로 queue_url 은 사용되지 않는다.
        container = AppContainer("")
        orchestrator = PipelineOrchestrator(container)

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

        corpus = "\n".join(
            (
                json.dumps(notification.model_dump(mode="json"), ensure_ascii=False),
                _report_markdown(container, notification),
            )
        )
        payload = {
            "schemaVersion": _SCHEMA_VERSION,
            "scenarioId": scenario_id,
            "engine": ENGINE,
            "rootCause": _root_cause(notification),
            "evidenceIds": _evidence_ids(corpus, scenario),
            "artifacts": _stages_reached(notification),
            "remediation": _remediation(notification),
        }
        result_stream.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
