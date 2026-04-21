# ADR 0009: 알림 전송 — SNS 기반 RCA 완료 알림

Date: 2026-04-21

## Status

Proposed

## Context

RCA가 완료되면 SRE 팀에 즉시 알려야 한다. 알림이 지연되면 조치가 늦어지고, 알림이 실패해도 RCA 자체가 블로킹되면 안 된다.

## Decision

**SNS Topic 기반 알림 전송 + 비블로킹 처리** 전략을 채택한다.

### 핵심 결정사항

1. **알림 메시지 구조**: RCA ID, 근본 원인 요약(1~2줄), 심각도, 보고서 링크(S3 Presigned URL 또는 대시보드 URL), 소요 시간을 포함한다.

2. **SNS Topic 발행**: RCA 전용 SNS Topic으로 발행하며, SRE 팀이 이메일/Slack/PagerDuty 등으로 구독한다.

3. **비블로킹**: 알림 전송 실패가 RCA 전체 흐름을 블로킹하지 않는다. 실패해도 `COMPLETED`로 전이한다. 최대 3회 재시도 후 실패 기록만 남긴다.

4. **근본 원인 미확정 시**: "근본 원인 미확정 — 수동 검토 필요" 메시지로 알림하여 SRE가 즉시 인지하도록 한다.

5. **Presigned URL 만료 대비**: 대시보드 URL을 병행 제공하여 Presigned URL 만료 후에도 보고서 접근이 가능하도록 한다.

## Consequences

### Positive

- RCA 완료 즉시 SRE 팀 알림으로 조치 시간 단축
- 비블로킹 처리로 알림 실패가 RCA 흐름에 영향을 주지 않음
- SNS 구독 방식으로 다양한 알림 채널(이메일, Slack, PagerDuty) 확장 용이

### Negative

- MVP에서는 SNS(이메일) 기반으로 Slack/PagerDuty 직접 연동은 미지원

### Risks

- SNS 발행 지연으로 알림이 늦어질 수 있으나, RCA 보고서는 이미 S3에 저장되어 있으므로 대시보드에서 직접 확인 가능하다.

## Related

- [ADR agent/0007: RCA 보고서 생성](0007-rca-report-generation.md) — 알림에 포함할 보고서를 생성하는 단계
