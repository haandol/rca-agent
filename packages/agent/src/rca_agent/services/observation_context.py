"""스코핑 관측을 프롬프트 텍스트로 옮기는 단일 경로.

가설 생성과 증거 수집이 각자 문자열을 조립하면 한쪽만 추세나 시퀀스를 빠뜨려도
오류가 나지 않고, 두 단계가 같은 관측을 다르게 읽는다. 렌더링을 한 곳에 둔다.
"""

from __future__ import annotations

from rca_agent.ports.dto.models import ConcurrentAlarm, MetricObservation, MetricTrend

# 시퀀스를 그대로 넘기면 프롬프트가 비대해지므로 양 끝을 남긴다. 추세 판정은 서버가
# 전체 시퀀스로 이미 수행했으므로, 프롬프트의 시퀀스는 그 판정을 사람과 모델이
# 확인하는 근거다.
_MAX_RENDERED_POINTS = 12

_TREND_LABEL = {
    MetricTrend.RISING: "read as rising",
    MetricTrend.FALLING: "read as falling",
    MetricTrend.FLAT: "read as flat",
    MetricTrend.SPIKE: "read as a spike that returned",
    MetricTrend.UNKNOWN: "trend undetermined (too few datapoints)",
}


def _render_datapoints(datapoints: list[float]) -> str:
    if not datapoints:
        return "no datapoints"
    if len(datapoints) <= _MAX_RENDERED_POINTS:
        shown = ", ".join(f"{value:g}" for value in datapoints)
        return f"[{shown}]"
    head = ", ".join(f"{value:g}" for value in datapoints[: _MAX_RENDERED_POINTS // 2])
    tail = ", ".join(f"{value:g}" for value in datapoints[-(_MAX_RENDERED_POINTS // 2) :])
    return f"[{head}, … , {tail}] ({len(datapoints)} points)"


def render_observation(observation: MetricObservation) -> str:
    # 시퀀스가 근거이고 추세는 그것을 읽은 요약이다. 순서를 이렇게 두어 하류 단계가
    # 요약을 근거로 착각하지 않게 한다 — 요약과 다르게 읽을 여지가 남아야 한다.
    parts = [_render_datapoints(observation.datapoints), _TREND_LABEL[observation.trend]]
    if observation.shape_note:
        parts.append(observation.shape_note)
    if observation.unit:
        parts.append(observation.unit)
    if observation.baseline is not None:
        parts.append(f"baseline={observation.baseline:g}")
    if observation.window_start and observation.window_end:
        parts.append(f"window {observation.window_start.isoformat()} ~ {observation.window_end.isoformat()}")
    return " | ".join(parts)


def render_observations(observations: list[MetricObservation]) -> str:
    if not observations:
        return "No metric data available."
    return "\n".join(f"- **{obs.metric_name}**: {render_observation(obs)}" for obs in observations)


def render_concurrent_alarms(alarms: list[ConcurrentAlarm]) -> str:
    """동시 발생 알람을 렌더링한다.

    빈 목록과 "확인하지 않음"을 구별해 표기한다. 둘을 같은 문장으로 내보내면 확인되지
    않은 것을 근거로 "다른 알람은 없었다"는 단정이 성립한다.
    """
    if not alarms:
        return "No concurrent alarms were reported for this scope."
    return "\n".join(f"- {alarm.alarm_name}: {alarm.state or 'state unknown'}" for alarm in alarms)
