# RCA 효율 점검 증거

초기 점검 기준 커밋: `bc5a7d5`. 초기 점검은 운영 코드·ADR 변경이나 라이브 AWS·유료 모델 호출 없이 수행했다.
합성 입력의 호출 수·문자 수는 운영 발생률이나 과금 토큰 수가 아니다.
별도 표시가 없는 출력은 실행 당시 도구 출력에서 옮긴 기록이며 재실행 결과가 아니다.

## 저장된 재현

- `reproduce_queue.py`: 실제 Headless Codex 파이프라인과 Moto 저장소로 만료 재전달을 재현한다.
  모델 실행은 mock으로 대체한다.
- `queue-output.json`: 두 재현의 핵심 관측값. 매번 달라지는 세션 ID는 생략했다.
- `strands/probe-validation-history.py`: 실제 설치된 Agent와 가짜 모델로 검증 대화 누적을 재현한다.
- `strands/probe-pipeline.py`: 모의 응답으로 재생성·중복 분기를 재현한다.
  원래 실행에 포함된 스코핑·직렬 처리 관측도 보존되어 있으나 이번 보고서의 확정 발견사항에는 포함하지 않았다.
- `strands/*output.txt`: 해당 실행 출력 기록.
- `strands/test-command.sh`: 네트워크 연결을 거부한 159개 관련 테스트의 정확한 명령.
- `headless-test-command.sh`: 네트워크 연결을 거부한 178개 관련 테스트의 정확한 명령.
- `headless-probe-output.json`: 실제 러너의 Popen을 mock으로 대체해 관측한 두 단계 timeout과 프롬프트 크기.
  원본 probe는 stdin으로 실행되어 파일로 보존되지 않았다. 경과 시간 측정 결과가 아니다.

큐 재현은 `packages/headless-codex`에서 다음과 같이 실행한다.

```sh
uv run --locked python ../../docs/test-reports/rca-efficiency-evidence-2026-09-09/reproduce_queue.py
```

Strands 재현 명령과 환경 제한은 `strands/README.md`에 있다.

## 기존 테스트 결과

| 작업 디렉터리 | 명령 | 결과 |
|---|---|---|
| `packages/agent` | `uv run --locked pytest tests/test_agent_main.py tests/test_main.py tests/test_session_store.py -q` | 127 passed in 13.01s |
| `packages/headless-codex` | `uv run --locked pytest tests/test_main.py tests/test_pipeline.py tests/test_session_store.py -q` | 79 passed in 11.21s |
| 저장소 루트 | `node --test tests/harness/shared-alarm-queue-contract.test.mjs tests/harness/engine-parity.test.mjs` | 9 passed |
| `packages/agent` | `strands/test-command.sh`의 Python 실행 | 159 passed in 5.58s |
| `packages/headless-codex` | `headless-test-command.sh`의 Python 실행 | 178 passed in 6.08s |
| `packages/infra` | `pnpm exec jest --runInBand --no-cache test/event-bus-stack.test.ts test/rca-agent-service-stack.test.ts test/headless-codex-stack.test.ts` | 15 passed |

서로 겹치는 테스트가 있으므로 위 실행 결과를 고유 테스트 총수로 합산하지 않는다.
전체 모노레포 build/verify, fixture 평가, 라이브 모델 평가, 배포 E2E는 이번 범위에서 실행하지 않았다.
보고서 HTML은 Chrome에서 본문·두 Mermaid 다이어그램·원문 표시를 확인했다.

## Strands 수신 오류 probe

`packages/agent`에서 아래 코드를 `uv run --locked python`으로 실행했다.
실제 SQS 호출 대신 즉시 예외를 발생시키고 로깅은 mock으로 바꾼다.

```python
import json
from unittest.mock import Mock, patch
from rca_agent.adapters.secondary.queue.sqs_consumer import SqsConsumer
sqs = Mock()
sqs.receive_message.side_effect = RuntimeError("immediate receive failure")
with (
    patch("rca_agent.adapters.secondary.queue.sqs_consumer.boto3.client", return_value=sqs),
    patch("rca_agent.adapters.secondary.queue.sqs_consumer.logger"),
    patch("time.sleep") as sleep,
):
    consumer = SqsConsumer("offline", poll_wait_seconds=20)
    for _ in range(100):
        assert list(consumer.poll()) == []
    print(json.dumps({
        "immediate_receive_failures": sqs.receive_message.call_count,
        "application_sleep_calls": sleep.call_count,
    }))
```

결과: `{"immediate_receive_failures": 100, "application_sleep_calls": 0}`.
SDK 자체 대기나 네트워크 왕복시간은 이 probe가 측정하지 않는다.

## 인프라 추가 확인

기존 TypeScript 스택을 `ts-node/register/transpile-only`로 로컬 합성하고 CDK Template으로 검사했다.
운영 AWS 상태를 조회하지 않았다. 관측값:

- 각 분석 서비스 desired count 1, task CPU 1024, memory 2048 MiB.
- 자동 확장 대상 없음.
- 추적을 켠 Strands의 `otel-collector`에 별도 CPU·memory·memoryReservation 없음.
- 분석 컨테이너 로깅 mode가 명시되지 않음. 계정 기본값은 확인하지 않음.
- 공용 알람 큐 visibility timeout 3,900초와 SNS의 SQS 구독 1개.

이 검사는 현재 구성의 관측이며 제안한 개선의 통과 기준이 아니다.

## 후속 구현 검증

같은 날짜에 사용자의 수정 요청을 받아 운영 코드를 수정했다. 아래 파일은 후속 구현의 검증 결과다.
초기 probe와 그 출력은 수정 전 기록으로 보존하며 현재 동작의 통과 증거로 사용하지 않는다.

- `verify-after.log`: 최종 `pnpm verify` 출력. 포맷·린트·단위 테스트는 통과하고 공통 계약 120/121개 통과.
  승인된 입력 지문과 파일 집합이 달라 최종 종료 코드는 1이다.
- `typecheck-after.log`: 별도로 실행한 `pnpm run typecheck` 통과 기록.
- `verification-after.json`: 단위 테스트 1,502개 통과를 포함한 검증 요약.
- `offline-after.json`: 기존 fixture의 두 엔진 × 네 시나리오 구조 게이트는 모두 통과하나 지문 게이트는 실패.
- `measure_prompt_sizes.py`, `prompt-sizes-after.json`: 같은 합성 알람의 수정 전후 기본 프롬프트 비교.
  원본 프롬프트는 `bc5a7d5`에서 읽고, 수정 후 입력은 현재 코드로 조립한다.

프롬프트 측정은 `packages/headless-codex`에서 다음과 같이 실행한다.

```sh
uv run --no-sync python ../../docs/test-reports/rca-efficiency-evidence-2026-09-09/measure_prompt_sizes.py
```

최종 회귀 테스트는 각 패키지의 현재 `tests/`에 있다.
실모델 평가·기준선 재승인·배포·커밋은 하지 않았다.
