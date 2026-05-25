---
name: hypothesis-generation
description: 가설 생성 서브에이전트 가이드 — 스코핑 결과로부터 3-5개 근본원인 가설을 생성하고, rca-progress MCP로 DDB에 저장하고 /tmp에 산출물을 남긴다. Agent tool로 가설 생성 서브에이전트를 스폰할 때 이 스킬을 따른다.
---

# 가설 생성 서브에이전트

## 서브에이전트 역할

메인 에이전트가 Agent tool로 이 서브에이전트를 스폰한다. 서브에이전트는:

1. 스코핑 결과를 입력받아 **3-5개** 근본원인 가설을 생성한다
2. 각 가설에 UUID를 부여한다
3. rca-progress MCP의 `save_artifact`로 `hypotheses.json`을 저장한다 (자동으로 `/tmp/rca-{세션ID}/` 아래에 저장되고, Python watcher가 파일을 감지해 DDB에 스팬·HYPO 아이템을 기록한다)

## 가설 구조

각 가설은 다음 필드를 포함한다:

- `hypothesis_id`: UUID (직접 생성)
- `tree_id`: 이 생성 라운드의 공유 UUID
- `title`: 짧은 한 줄 제목 (≤60자, 한글). 대시보드 카드·그래프 노드에 표시되므로 명사구로 간결히 작성한다 (예: "Healthcare 앱 커넥션 누수").
- `description`: 상세 설명 — 이 가설을 세운 근거와 어떤 증거로 검증할 계획인지 2~4문장으로 기술
- `category`: `DEPLOYMENT`, `INFRASTRUCTURE`, `TRAFFIC`, `DEPENDENCY`, `CONFIGURATION` 중 하나
- `confidence_score`: 0.0-1.0 (초기 추정)
- `required_evidence`: 검증에 필요한 증거 목록
- `status`: `PENDING`
- `parent_id`: null (루트 가설)
- `depth`: 0

## 5 Whys 사고 프레임 (필수)

각 가설은 "증상에 대한 1차 '왜?'의 답 후보" 로 구성한다. 이후 검증 루프에서 같은 가설에 대해 다시 "왜 그게 발생했는가?" 로 한 단계 더 내려갈 수 있어야 한다.

- **"휴먼 에러"·"운영자 실수" 가설은 종착점이 아니다.** 그 방향이 의심되면 "왜 그 행동이 가능했는가? (검증 부재? 권한 과다? 런북 결함?)" 까지 풀어 시스템·프로세스 결함으로 표현한다.
- **단일 원인을 가정하지 않는다.** 카테고리 분포가 다양하도록(예: 한 가지에 5개 몰지 않기) 다요인 후보를 제시한다 — 인시던트는 보통 multi-causal.
- **`description`에는 "어떤 증거로 어떻게 검증할 것인가"를 함께 적는다.** 검증 불가능한 추상적·심리적 원인은 금지.
- **Blameless 톤** — 사람·팀을 특정하지 않고 시스템 관점으로 작성.

## 가설 카테고리 기준

| 카테고리 | 설명 | 대표적 근본원인 |
|---------|------|--------------|
| `DEPLOYMENT` | 최근 코드/설정 배포 | 비효율 코드, 리소스 누수, 설정 오류 |
| `INFRASTRUCTURE` | AWS 인프라 이슈 | 호스트 열화, AZ 장애, 네트워크 파티션 |
| `TRAFFIC` | 트래픽 패턴 변화 | DDoS, 트래픽 급증, 봇 크롤링 |
| `DEPENDENCY` | 외부/내부 의존 서비스 | DB 지연, 외부 API 타임아웃, DNS 실패 |
| `CONFIGURATION` | 런타임 설정 문제 | 잘못된 환경변수, 리소스 한도, IAM 정책 |

## 재생성 시 지침

전체 기각 후 재생성 요청을 받으면:

1. 기각된 가설 목록을 참고한다
2. 기각된 방향과 **다른 관점**에서 새 가설을 생성한다
3. 새 `tree_id`를 발급한다
4. 동일하게 `save_artifact("hypotheses.json", ...)` 호출

## MCP 호출 예시

`title`은 대시보드 카드·그래프 노드에 그대로 노출되므로 **반드시 명사구로 채운다**. 생략 시 DDB에 빈 값으로 기록되어 노드가 description 첫 줄로 fallback된다.

```
save_artifact("hypotheses.json", '{
  "stage": "HYPOTHESIS_GENERATION",
  "tree_id": "tree-uuid",
  "hypotheses": [
    {
      "hypothesis_id": "uuid-1",
      "tree_id": "tree-uuid",
      "title": "Healthcare 앱 커넥션 누수",
      "description": "최근 배포 이후 ActiveConnections 지표가 선형 증가하고 있어 커넥션 누수가 의심된다. DB 커넥션 풀 지표와 배포 diff로 검증한다.",
      "category": "DEPLOYMENT",
      "confidence_score": 0.6,
      "required_evidence": ["recent deployments", "DB connection metrics"],
      "status": "PENDING",
      "parent_id": null,
      "depth": 0
    }
  ],
  "summary": "가설 N개 생성",
  "output_summary": "가설 3개 생성: 커넥션 누수, CPU 스트레스, ..."
}')
```
