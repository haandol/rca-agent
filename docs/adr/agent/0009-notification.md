# ADR 0009: 알림 전송 — SNS 기반 RCA 완료 알림

Date: 2026-04-21

## Status

Accepted

## Context

RCA가 완료되면 SRE 팀에 즉시 알려야 한다. 알림이 지연되면 조치가 늦어지고, 알림이 실패해도 RCA 자체가 블로킹되면 안 된다.

## Decision

**SNS Topic 기반 알림 전송 + 비블로킹 처리** 전략을 채택한다.

### 핵심 결정사항

1. **알림 메시지 구조**: `NotificationMessage` Pydantic 모델로 RCA ID, 근본 원인 요약(최대 200자), 심각도, 보고서 S3 키, 대시보드 URL, 소요 시간, 확정 여부를 포함한다. `build_notification()` 함수가 `RcaReport`에서 메시지를 구성한다.

2. **SNS Topic 발행**: `send_notification()`이 `SNS_NOTIFICATION_TOPIC_ARN`으로 JSON 메시지를 발행한다. Presigned URL 생성에 실패하면 대시보드 URL로 대체한다.

3. **비블로킹 + exponential backoff**: `send_notification()`은 `bool`을 반환하며 예외를 전파하지 않는다. 최대 3회 재시도(`base_delay * 2^attempt`)하며, 모든 시도 실패 시 `False`를 반환하고 로그만 기록한다. SNS 미설정 시에도 파이프라인은 정상 완료된다.

4. **근본 원인 미확정 시**: `root_cause_confirmed=False`이면 `build_notification()`이 "Root cause unconfirmed — manual review needed" 요약을 생성하고, severity를 "medium"으로 설정한다.

5. **Presigned URL**: `_generate_presigned_url()`이 S3 보고서에 대해 24시간(86400초) 만료 Presigned URL을 생성한다. 실패 시 대시보드 URL로 fallback한다.

## Consequences

### Positive

- RCA 완료 즉시 SRE 팀 알림으로 조치 시간 단축
- 비블로킹 처리로 알림 실패가 RCA 흐름에 영향을 주지 않음
- SNS 구독 방식으로 다양한 알림 채널(이메일, Slack, PagerDuty) 확장 용이

### Negative

- MVP에서는 SNS(이메일) 기반으로 Slack/PagerDuty 직접 연동은 미지원

### Risks

- SNS 발행 지연으로 알림이 늦어질 수 있으나, RCA 보고서는 이미 S3에 저장되어 있으므로 대시보드에서 직접 확인 가능하다.

## Implementation Status

구현 완료. 알림 메시지에 플레이북 데이터(playbook_id, failure_type, symptom_pattern, severity_criteria, verification_steps, temporary_mitigation, permanent_remediation, escalation_criteria)를 포함하여 SNS에 발행한다. 다만 이 알림을 구독하여 자동 복구를 수행할 Remediation Agent는 아직 미구현(ADR agent/0012 참조).

## Related

- [ADR agent/0007: RCA 보고서 생성](0007-rca-report-generation.md) — 알림에 포함할 보고서를 생성하는 단계
- [ADR agent/0008: 플레이북 생성](0008-playbook-generation.md) — 알림에 포함할 플레이북을 생성하는 단계
- [ADR agent/0012: 자동 복구](0012-automated-remediation.md) — 알림을 구독하여 복구를 수행하는 에이전트 (미구현)
