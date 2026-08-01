# 루프 라이브 재실측 인계 — 새 세션이 그대로 실행하는 절차

- 작성일: 2026-08-01
- 기준 커밋: `1adef24` (브랜치 `fix/close-the-retrospective-loop`, main 미머지)
- 목적: **회고가 교정한 절차가 다음 분석의 보강 대상이 되는가**를 라이브로 확인한다

## 왜 이 문서가 있는가

코드 수정은 끝났고 오프라인 계약 테스트도 전부 통과했다(Strands 522, CC 536). 하지만
**루프가 실제로 닫히는지는 아직 라이브에서 확인되지 않았다.** 1차 실측에서 승인 이후
구간은 전부 성립했지만, 2차 주입에서 검색이 빈 결과로 돌아와 개정본 조회 경로가 실행되지
않았다. 그 원인 두 개를 고친 것이 `30fea48`이고, **그 수정이 유효한지가 이 실측의 대상**이다.

배경과 지금까지의 실측 결과는
[루프 현황 점검](./rca-remediation-loop-audit-2026-08-01.md)이 보유한다. 이 문서는
**무엇을 어떤 순서로 실행하는가**만 담는다.

---

## 확인해야 하는 것 — 딱 하나

**같은 증상을 두 번 주입했을 때, 두 번째 분석이 첫 회고가 승격한 절차를 보강 대상으로
삼는가.**

성립 조건은 세 단계가 모두 통과할 때다.

1. 검색이 1차 플레이북을 **후보로 찾는다** (임계값 0.80 통과)
2. 상세 조회가 **회고 개정본**을 반환한다 (분석 원본이 아니라)
3. 병합이 그 개정본을 기준으로 일어나고 `verification_status`가 `VERIFIED`로 유지된다

1단계에서 막히면 2·3단계는 실행되지 않는다. 1차 실측에서 정확히 그렇게 됐다.

---

## 0. 시작 전 — 반드시 확인

### 0.1 브랜치와 커밋

```bash
git branch --show-current   # fix/close-the-retrospective-loop
git log --oneline -6        # 1adef24 가 HEAD
git status --short          # 비어 있어야 배포 가능
```

> 배포 스크립트는 **미커밋 변경이 있으면 거부한다.** 배포된 하네스를 커밋으로 재현할 수
> 없기 때문이다.

### 0.2 배포된 이미지가 이 코드인가

```bash
for c in RcaAgentDevRcaAgent RcaAgentDevCcHeadless RcaAgentDevPlaybookExecution; do
  TD=$(aws ecs describe-services --cluster $c --services $c \
        --query 'services[0].taskDefinition' --output text)
  IMG=$(aws ecs describe-task-definition --task-definition "$TD" \
        --query 'taskDefinition.containerDefinitions[?name!=`otel-collector`].image | [0]' --output text)
  echo "$c -> ${IMG##*:}"
done
git rev-parse --short=12 HEAD
```

2026-08-01 18:00 시점 배포 태그는 `cbf6fd8dec80`이다. **`30fea48`(검색 결함 수정)이 빠져
있으므로 재배포가 필요하다.**

### 0.3 CDK 잠금 — 1차 실측에서 실제로 막힌 지점

```bash
ls packages/infra/cdk.out/*.lock 2>/dev/null
```

잠금 파일이 있으면 PID를 읽어 프로세스 생존을 확인한다.

```bash
ps -p <PID> -o pid,etime,command
aws cloudformation describe-stacks --stack-name <스택> --query 'Stacks[0].StackStatus'
```

**스택이 `UPDATE_COMPLETE`인데 프로세스가 남아 있으면 잔여 프로세스다** — 1차 실측에서
6시간 붙들고 있었다. 종료하고 잠금 파일을 제거한다. 이 상태에서 다음 배포는 이미지 푸시까지
성공하고 스택 갱신만 실패해, 배포가 절반만 반영된 것처럼 보인다.

```bash
kill <PID>; rm -f packages/infra/cdk.out/read.<PID>.*.lock
```

---

## 1. 재배포 — 세 워커 순차

CDK는 `cdk.out`을 잠그므로 **두 배포를 동시에 돌릴 수 없다.** 각 배포는 5~6분이다.

```bash
cd packages/infra
pnpm run deploy:service -- cc-headless execution   # 같은 이미지, 두 진입점
pnpm run deploy:service -- agent
```

`infra` 스택 자체는 변경이 없으므로 CDK 전체 배포는 필요하지 않다. 확인:

```bash
git diff --stat cbf6fd8..HEAD -- packages/infra   # 비어 있어야 함
```

배포 후 검증:

```bash
for c in RcaAgentDevRcaAgent RcaAgentDevCcHeadless RcaAgentDevPlaybookExecution; do
  TD=$(aws ecs describe-services --cluster $c --services $c --query 'services[0].taskDefinition' --output text)
  IMG=$(aws ecs describe-task-definition --task-definition "$TD" \
        --query 'taskDefinition.containerDefinitions[?name!=`otel-collector`].image | [0]' --output text)
  RUN=$(aws ecs describe-services --cluster $c --services $c \
        --query 'services[0].[runningCount,desiredCount]' --output text)
  echo "$c -> ${IMG##*:}  ($RUN)"
done
```

세 워커 모두 `1adef24947e9`, `1 1`이어야 한다.

---

## 2. 인덱스 처리 — 판단이 필요한 지점

임베딩 입력 필드가 바뀌었으므로 **기존 인덱스 레코드는 옛 텍스트로 임베딩된 상태**다. 새
쿼리와 공간이 어긋나 검색되지 않는다.

현재 상태(2026-08-01 측정):

| 항목 | 값 |
|------|-----|
| `playbook` 인덱스 총 항목 | 49 |
| `verification_status` 보유 항목 | 4 |

**재적재 스크립트는 없다.** 세 가지 선택이 있고, 이 실측의 목적에 따라 답이 다르다.

### 권장: 재적재하지 않고 새 플레이북으로 검증한다

이 실측이 확인하려는 것은 **개정본 우선 조회와 병합이 동작하는가**이며, 그것은 이 실측
안에서 새로 만드는 1차 플레이북으로 확인할 수 있다. 1차 분석이 새 코드로 플레이북을 쓰면 새
텍스트로 임베딩되므로, 2차 분석의 검색과 공간이 맞는다.

**옛 49개 항목은 검색되지 않은 채 남는다.** 그것은 이 실측의 대상이 아니고, 데모 환경에서
과거 축적을 잇는 가치도 크지 않다.

### 대안 A: 옛 항목을 지우고 시작한다

검색 결과에 매칭되지 않을 레코드가 섞여 있는 것이 혼란스럽다면 정리할 수 있다. 다만 **삭제는
되돌릴 수 없으므로** 데모 이력을 버려도 되는지 먼저 판단한다.

### 대안 B: 재적재 스크립트를 만든다

상태 저장소의 PLAYBOOK 스팬과 개정본을 읽어 새 텍스트로 다시 임베딩한다. 프로덕션에서 축적을
이어야 한다면 이것이 맞는 답이지만, **이 실측을 위해서는 불필요하다** — 실측 결과가 나온 뒤에
결정해도 된다.

---

## 3. 1차 주입 — 승격된 플레이북 만들기

### 3.1 시작 상태 확인

```bash
python3 scripts/inject_deployment_fault.py status   # 세 플래그 전부 false/0
aws cloudwatch describe-alarms --alarm-name-prefix RcaAgentDev-Healthcare \
  --query 'MetricAlarms[?contains(AlarmName,`Rds`) || contains(AlarmName,`Vital`)].[AlarmName,StateValue]' \
  --output text                                     # 둘 다 OK
```

> **스크립트의 `status`는 최신 태스크 정의를 읽는다.** 실행 에이전트가 롤백한 뒤에는 실제
> 실행 중인 revision과 어긋날 수 있다. 실제 상태는 서비스의 태스크 정의로 확인한다.
>
> ```bash
> aws ecs describe-services --cluster RcaAgentDevHealthcare --services RcaAgentDevHealthcare \
>   --query 'services[0].deployments[].[status,taskDefinition,runningCount,rolloutState]' --output text
> ```

### 3.2 주입

```bash
python3 scripts/inject_deployment_fault.py red-herring   # 무해한 배포를 먼저
sleep 150
python3 scripts/inject_deployment_fault.py db-leak       # 실제 원인
```

알람은 주입 후 **약 4분**에 뜬다. 커넥션 알람이 먼저, 증상 알람이 그 다음(순서 계약 —
ADR infra/0007).

### 3.3 분석 대기

```bash
RCA=<새 rca_id>   # 아래로 찾는다
aws dynamodb scan --table-name RcaAgentDevRcaSession \
  --filter-expression "begins_with(PK, :p) AND contains(SK, :s)" \
  --expression-attribute-values '{":p":{"S":"RCA#"},":s":{"S":"SESSION"}}' \
  --query 'Items[].[PK.S,SK.S,state.S,created_at.S]' --output text | sort -k4 -r | head -4
```

Strands는 약 10분, CC는 최대 30분(한도 초과 시 `FAILED`).

> **CC는 1차 실측에서 두 회차 모두 CLI 30분 한도로 `FAILED`했다.** 알려진 병목이고 이번
> 변경과 무관하다. **어느 엔진이든 `COMPLETED` + 절차 있는 리포트 하나면 실측이 된다.**
> 다만 CC 경로의 검색·병합은 CC가 완료해야 확인된다 — 그것까지 보려면 CC 완료를 기다려야
> 한다.

### 3.4 확인 — 갭 A가 유지되는가

```bash
KEY=$(aws dynamodb query --table-name RcaAgentDevRcaSession \
  --key-condition-expression "PK = :p AND SK = :s" \
  --expression-attribute-values "{\":p\":{\"S\":\"RCA#$RCA\"},\":s\":{\"S\":\"strands#SESSION\"}}" \
  --query 'Items[0].report_s3_key.S' --output text)
aws s3 cp "s3://rca-agent-dev-evidence/$KEY" - | grep -n "^## "
```

> **리포트 버킷은 `rca-agent-dev-evidence`다** (`S3_REPORT_BUCKET`이 evidence 버킷을
> 가리킨다). `s3-reports-general-purpose-bucket-...`은 이 시스템과 무관하다.

`## 대응 플레이북` 섹션이 있어야 하고, 서술의 `step_id` 순서가 구조와 같아야 한다.

```bash
aws dynamodb query --table-name RcaAgentDevRcaSession \
  --key-condition-expression "PK = :p" \
  --expression-attribute-values "{\":p\":{\"S\":\"RCA#$RCA\"}}" \
  --query 'Items[?span_type.S==`PLAYBOOK` && starts_with(SK.S,`strands`)].metadata.M.execution_steps.L[].M.step_id.S' \
  --output text
```

### 3.5 승인 → 실행 → 승격

```bash
cd packages/dashboard
EXECUTION_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/395271362395/RcaAgentDevPlaybookExecutionQueue \
  pnpm run dev   # http://localhost:3100
```

> `EXECUTION_QUEUE_URL`이 없으면 승인이 **503으로 실패한다.**

```bash
# 승인 전: 초안 + 절차 있음 확인
curl -s "http://localhost:3100/api/playbooks/$RCA?engine=strands" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('playbook_id:', d.get('playbook_id'))
print('status:', d.get('verification_status'), '| steps:', len(d.get('execution_steps') or []))"

# 승인
curl -s -X POST http://localhost:3100/api/executions -H 'Content-Type: application/json' \
  -d "{\"rcaId\":\"$RCA\",\"engine\":\"strands\"}"
```

실행은 약 9분. 추적:

```bash
aws dynamodb query --table-name RcaAgentDevRcaSession \
  --key-condition-expression "PK = :p AND begins_with(SK, :s)" \
  --expression-attribute-values "{\":p\":{\"S\":\"RCA#$RCA\"},\":s\":{\"S\":\"EXEC#\"}}" \
  --query 'Items[].[execution_state.S,retrospective_status.S]' --output text

aws logs tail /ecs/RcaAgentDev/playbook-execution --since 10m --format short | grep execution_judged
```

`RESOLVED` + 회고 `UPDATED`/`NO_CHANGE` 뒤 **승격을 확인한다** — 이것이 2차 실측의 입력이다.

```bash
aws dynamodb query --table-name RcaAgentDevRcaSession \
  --key-condition-expression "PK = :p AND SK = :s" \
  --expression-attribute-values "{\":p\":{\"S\":\"RCA#$RCA\"},\":s\":{\"S\":\"strands#PLAYBOOK_REVISION\"}}" \
  --query 'Items[0].playbook.S' --output text | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('verification_status:', d.get('verification_status'))
print('playbook_id:', d.get('playbook_id'))
print('steps:', [s['step_id'] for s in d.get('execution_steps',[])])"
```

**`VERIFIED`가 아니면 2차 실측을 진행하지 않는다** — 확인할 대상이 없다.

여기서 1차 플레이북의 `failure_type`과 `symptom_pattern`을 기록해 둔다. 2차 검색이 이것과
얼마나 닮았는지가 성립 조건이다.

---

## 4. 2차 주입 — 여기가 실제 검증

### 4.1 1차 복구 후 상태 정리

실행 에이전트가 롤백했으므로 장애는 이미 해소되어 있다. **리셋을 다시 돌리지 말고** 알람이
OK인 것만 확인한 뒤 바로 재주입한다.

```bash
aws cloudwatch describe-alarms --alarm-name-prefix RcaAgentDev-Healthcare \
  --query 'MetricAlarms[?contains(AlarmName,`Rds`) || contains(AlarmName,`Vital`)].[AlarmName,StateValue]' --output text
python3 scripts/inject_deployment_fault.py db-leak
```

> **1차 실측이 여기서 걸렸다.** 2차 분석이 1차 복구 흔적을 보고 원인을 "잔존·간헐적
> 재발(미확정)"로 서술해, `failure_type`이 1차와 크게 달라졌다. 확정 실패 자체는 분석의
> 판단이므로 강제할 수 없지만, red-herring을 다시 심지 않고 곧바로 db-leak만 주입하면
> 배포 이력이 단순해져 확정 가능성이 올라간다.

### 4.2 검색이 후보를 찾았는지 — 첫 관문

2차 분석이 `COMPLETED`된 뒤:

```bash
aws logs tail /ecs/RcaAgentDev/rca-agent --since 20m --format short \
  | grep -iE "Checking update for playbook|Updating playbook|up-to-date|detail unavailable|Generating playbook draft"
```

| 로그 | 의미 |
|------|------|
| `Checking update for playbook <id> (similarity=0.xx)` | **검색 성립** — 후보를 찾았다 |
| `Updating playbook <id> with new RCA findings` | **병합 성립** |
| `All N existing playbooks are up-to-date` | 후보는 찾았고 보강 불필요로 판단 |
| `Skipping playbook <id> — detail unavailable` | 상세 로드 실패 — 후보 제외 |
| 위 어느 것도 없이 `Generating playbook draft`만 | **검색이 빈 결과** — 1차 실측과 같은 실패 |

### 4.3 개정본을 읽었는지 — 핵심

검색이 성립했다면 상세 조회가 개정본을 읽었는지 확인한다.

```bash
aws logs tail /ecs/RcaAgentDev/rca-agent --since 20m --format short \
  | grep -i "Loaded playbook .* from its retrospective revision"
```

이 로그가 **갭 C가 라이브에서 동작한 증거**다. 없으면 분석 스팬(원본)을 읽었다는 뜻이다.

### 4.4 결과 판정

```bash
RCA2=<2차 rca_id>
# 2차가 1차 식별자를 유지했는가
aws dynamodb query --table-name RcaAgentDevRcaSession \
  --key-condition-expression "PK = :p" \
  --expression-attribute-values "{\":p\":{\"S\":\"RCA#$RCA2\"}}" \
  --query 'Items[?span_type.S==`PLAYBOOK` && starts_with(SK.S,`strands`)].metadata.M.[playbook_id.S,verification_status.S]' \
  --output text
```

| 결과 | 판정 |
|------|------|
| `playbook_id`가 1차와 **같고** `verification_status`가 `VERIFIED` | **루프가 닫혔다** ✅ |
| `playbook_id`가 다름 | 병합 미성립 — 4.2 로그로 어느 관문에서 막혔는지 확인 |
| 같은 id인데 `DRAFT`로 떨어짐 | 병합이 승격을 취소했다 — 보존 규칙 결함 |

---

## 5. 검색이 또 빈 결과였다면

임계값을 더 내리기 전에 **실제 유사도를 측정한다.** 추측으로 값을 옮기면 무관한 병합이 늘어난다.

```bash
cd packages/agent && .venv/bin/python - <<'PY'
import boto3, json, sys
sys.path.insert(0, 'src')
from rca_agent.utils.embed_key import build_embed_key
from rca_agent.config.settings import PLAYBOOK_UPDATE_THRESHOLD
br = boto3.client('bedrock-runtime', region_name='us-east-1')
def embed(t, it):
    r = br.invoke_model(modelId="cohere.embed-v4:0", contentType="application/json",
        accept="application/json",
        body=json.dumps({"texts":[t],"input_type":it,"embedding_types":["float"]}))
    return json.loads(r['body'].read())['embeddings']['float'][0]
def cos(a,b):
    return sum(x*y for x,y in zip(a,b))/((sum(x*x for x in a)**0.5)*(sum(x*x for x in b)**0.5))

# 1차 플레이북 (3.5에서 기록한 값)
stored = build_embed_key(failure_type="<1차 failure_type>",
                        symptom="<1차 symptom_pattern>",
                        metric_name="DatabaseConnections")
# 2차 플레이북
query  = build_embed_key(failure_type="<2차 failure_type>",
                        symptom="<2차 symptom_pattern>",
                        metric_name="DatabaseConnections")
s = cos(embed(query,'search_query'), embed(stored,'search_document'))
print(f"similarity={s:.4f}  threshold={PLAYBOOK_UPDATE_THRESHOLD}  → {'병합' if s>=PLAYBOOK_UPDATE_THRESHOLD else '신규'}")
PY
```

2026-08-01 실측 기준선:

| 시나리오 | 유사도 |
|----------|--------|
| 재발: 같은 유형·표현만 다름 | 0.8332 |
| 재발: 동일 텍스트 | 0.9646 |
| 무관: CPU 폭증 | 0.5242 |
| 무관: 메모리 릭 | 0.4385 |

**측정값이 0.80~0.83 사이라면** 임계값 자체는 맞고 그 회차의 서술 편차가 컸다는 뜻이다.
재시도로 확인한다.

**측정값이 0.6 미만이라면** 2차 분석이 원인을 다르게 판단한 것이다 — 임계값 문제가 아니므로
내리지 말고, 왜 다르게 판단했는지를 본다(1차 복구 흔적, 확정 실패 등).

> 임계값을 바꾸려면 **ADR 0008을 먼저 고친다.** 그 값은 근거와 함께 기록된 요구사항이며,
> 코드만 바꾸면 ADR과 갈라진다. decision-log에도 한 줄 남긴다.

---

## 6. 마무리

```bash
python3 scripts/inject_deployment_fault.py reset
# 롤아웃 완료까지 약 3분, 알람 OK 전환까지 추가로 2분
pkill -f "nuxt.*dev"
```

성립했다면 [루프 현황 점검](./rca-remediation-loop-audit-2026-08-01.md) §6의 "아직
라이브로 확인되지 않은 것"을 결과로 갱신하고, 브랜치를 main에 머지할지 판단한다.

---

## 7. 알려진 함정 모음

| 함정 | 증상 | 대응 |
|------|------|------|
| CDK 잔여 프로세스 | 이미지는 푸시되고 스택 갱신만 실패 | 스택 상태 확인 후 프로세스 종료 + 잠금 제거 (0.3) |
| CDK 동시 배포 | `Other CLIs are currently reading from cdk.out` | 순차 실행 (1장) |
| 미커밋 변경 | 배포 스크립트가 거부 | 먼저 커밋 |
| `infra` 테스트 | gitignore된 `lib/**/*.js`를 Jest가 `.ts`보다 먼저 해석 | `pnpm --filter infra build && test` |
| 리포트 버킷 오인 | `s3-reports-general-purpose-bucket-...`에서 404 | 실제는 `rca-agent-dev-evidence` (3.4) |
| `inject...py status`가 stale | 롤백 후에도 `FAULT_DB_LEAK: true` | 서비스 태스크 정의로 확인 (3.1) |
| CC 30분 한도 | CC 세션이 `FAILED` | 알려진 병목. Strands만으로도 갭 C 확인 가능 |
| 대시보드 큐 URL 누락 | 승인이 503 | `EXECUTION_QUEUE_URL` 설정 (3.5) |

## 8. 관련 문서

| 문서 | 내용 |
|------|------|
| [루프 현황 점검](./rca-remediation-loop-audit-2026-08-01.md) | 갭 A·B·C와 검색 결함 두 개, 지금까지의 실측 결과 |
| [실행 E2E 런북](./execution-live-e2e-runbook.md) | 승인 이후 구간의 일반 절차 |
| [RCA에서 플레이북 실행까지](./rca-to-remediation-flow.md) | 흐름의 각 경계와 근거 |
| [ADR agent/0008](./adr/agent/0008-playbook-generation.md) | Search-First, 임계값 0.80, 개정본 우선, 대칭 규정 |
| [ADR agent/0018](./adr/agent/0018-playbook-retrospective.md) | 회고의 교정 범위와 단방향 승격 |
