---
name: hypothesis-generation
description: 가설 생성 서브에이전트 가이드 — 스코핑 결과로부터 3-5개 근본원인 가설을 생성하고, rca-progress MCP로 DDB에 저장하고 /tmp에 산출물을 남긴다. Agent tool로 가설 생성 서브에이전트를 스폰할 때 이 스킬을 따른다.
---

# 가설 생성 서브에이전트

## 서브에이전트 역할

메인 에이전트가 Agent tool로 이 서브에이전트를 스폰한다. 서브에이전트는:

1. 스코핑 결과를 입력받아 **3-5개** 근본원인 가설을 생성한다
2. 각 가설에 UUID를 부여한다
3. rca-progress MCP의 `save_hypotheses`로 DDB에 저장한다
4. rca-progress MCP의 `save_artifact`로 `hypotheses.md`를 저장한다 (자동으로 `/tmp/rca-{세션ID}/` 아래에 저장됨)

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
4. 동일하게 `save_hypotheses` + `save_artifact` 호출

## MCP 호출 예시

```
save_hypotheses('[
  {"hypothesis_id": "uuid-1", "tree_id": "tree-uuid", "description": "...",
   "category": "DEPLOYMENT", "confidence_score": 0.6,
   "required_evidence": ["recent deployments", "CPU metrics"],
   "status": "PENDING", "parent_id": null, "depth": 0},
  ...
]')

save_artifact("hypotheses.md", "# 가설 목록\n\n## 1. ...")
```
