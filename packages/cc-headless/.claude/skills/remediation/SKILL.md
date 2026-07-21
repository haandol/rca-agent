---
name: remediation
description: 확정 RCA에 한해 서버 검증형 Healthcare reset 도구를 사용하는 fail-closed 복구 절차
---

# 제한된 Healthcare Remediation

## 책임 경계

Remediation 전문 에이전트는 임의 HTTP, Bash, ECS `UpdateService`, 배포, 재시작,
롤백을 실행하지 않는다. 사용할 수 있는 쓰기 도구는
`execute_healthcare_reset(fault_type)` 하나뿐이다.

도구는 모델이 전달한 확정 여부를 신뢰하지 않는다. 현재 실행 디렉터리에서 가장 큰
인덱스의 `validation-{N}.json`과 `hypotheses.json`을 직접 읽고 다음을 모두 검증한다.

- 최신 validation이 valid JSON object인지
- `confirmed`가 비어 있지 않은지
- 각 confirmed 항목이 기존 hypothesis ID를 참조하고 confidence가 0.8 이상인지
- confirmed validation의 fault type enum이 참조 hypothesis의 enum과 일치하는지
- 모든 validation loop를 반영한 최신 hypothesis 상태에서, 확정 fault와 다른 allowlisted
  fault type의 경쟁 가설이 `unclassified` 또는 `needs_investigation`으로 남아 있지 않은지
- 요청한 fault type이 검증된 enum과 일치하는지
- Healthcare 호스트가 서버 환경에 고정되어 있는지

하나라도 실패하면 `BLOCKED`로 종료하고 변경하지 않는다.
`rejected` 또는 `closed`인 경쟁 가설은 해소된 것으로 본다. 복수 가설이 확정되더라도
모두 같은 reset action으로 수렴하면 충돌로 보지 않는다.

## 허용 액션

| fault type | 고정 endpoint |
|------------|---------------|
| `db-leak` | `/fault/db-leak/reset` |
| `high-cpu` | `/fault/high-cpu/reset` |
| `high-memory` | `/fault/high-memory/reset` |
| `slow-query` | `/fault/slow-query/reset` |

근본원인 자유 텍스트에서 fault type을 추론하지 않는다. confirmed validation에
기록된 구조화 enum만 사용한다. URL이나 endpoint path를 인자로 전달하지 않는다.
unsupported 원인에
대체 액션은 없다. confirmed 원인이 네 allowlist 유형에 매칭되지 않으면
`execute_healthcare_reset("unsupported")`를 호출한다. 서버는 최신 validation과
confirmed hypothesis ID를 확인한 뒤 `endpoint_path=null`인 `BLOCKED` 결과를
저장한다. 미확정이면 도구를 호출하지 않으며 `remediation.json`도 만들지 않는다.

## 결과

도구 호출은 성공 여부와 무관하게 `remediation.json`을 저장하고 다음 상태 중 하나를
반환한다.

- `SUCCEEDED`: 허용 reset이 2xx로 완료됨
- `FAILED`: 확정·허용 게이트는 통과했지만 서버 호출이 실패함
- `BLOCKED`: 미확정, malformed, unsupported, ambiguous, action mismatch

결과를 재해석하지 말고 Report 전문 에이전트에 그대로 전달한다.

reset 성공 후 도구는 원본 알람 메트릭을 CloudWatch에서 제한된 횟수로 재조회하고
`verification.status`를 기록한다.

- `NORMALIZED`: reset 이후 관측값이 알람 임계치를 더 이상 위반하지 않음
- `FAILED`: 제한된 검증 동안 관측값이 계속 임계치를 위반함
- `PENDING`: reset 이후 신뢰할 수 있는 새 관측값이 아직 없거나 조회 불가

이 상태를 모델 판단으로 변경하거나 `SUCCEEDED`를 `NORMALIZED`로 간주하지 않는다.
