# AGENTS.md

> 이 패키지는 RCA Agent 모노레포의 일부입니다. 전체 아키텍처, ADR, 크로스 패키지 계약, 빌드 명령어는 **[루트 AGENTS.md](../../AGENTS.md)** 를 참조하세요.

## Project Overview

RCA 대시보드는 DynamoDB 세션 상태와 S3 보고서를 조회하는 로컬 전용 웹 대시보드다. 인증 없이 로컬 AWS 크레덴셜(`~/.aws`)을 사용하며, 배포 대상이 아닌 개발/데모용 도구이다.

### Core Features

- **세션 목록 조회**: DynamoDB에서 RCA 세션 전체를 스캔하여 상태별 통계 및 목록 표시
- **보고서 조회**: S3에 저장된 Markdown 보고서를 렌더링
- **트레이스 그래프**: DynamoDB 실행 트레이스를 Vue Flow 기반 DAG로 시각화 (가설 노드 + 스팬 노드)
- **세션 취소/삭제**: 진행 중인 세션 취소(CANCELLED) 및 세션 삭제 지원
- **엔진 구분**: Strands / CC Headless 엔진별 세션 필터링
- **상태 뱃지**: COMPLETED, FAILED, CANCELLED, OUTDATED, 진행 중 상태를 시각적으로 구분

### Tech Stack

- **Framework**: Nuxt.js 4 (Vue 3)
- **UI**: TailwindCSS 4 + DaisyUI 5
- **Graph**: @vue-flow/core + @dagrejs/dagre
- **Markdown**: marked
- **Language**: TypeScript
- **Package Manager**: pnpm (Nx workspace)
- **AWS SDK**: @aws-sdk/client-dynamodb, @aws-sdk/client-s3, @aws-sdk/lib-dynamodb

## Quick Start

```bash
pnpm install
pnpm dev   # http://localhost:3100
```

## Project Structure

```
packages/dashboard/
├── app/
│   ├── pages/
│   │   ├── index.vue              # 세션 목록 + 통계 카드
│   │   ├── report/[id].vue        # 보고서 상세 (Markdown)
│   │   └── trace/[id].vue         # 트레이스 그래프 (Vue Flow DAG)
│   ├── layouts/
│   │   └── default.vue            # 다크 테마 레이아웃
│   ├── components/
│   │   └── flow/
│   │       ├── HypoNode.vue       # 가설 노드 커스텀 컴포넌트
│   │       └── SpanNode.vue       # 스팬 노드 커스텀 컴포넌트
│   ├── composables/
│   │   └── useTraceGraph.ts       # 트레이스 데이터 → Vue Flow 그래프 변환
│   ├── assets/css/main.css        # 글로벌 스타일 (DaisyUI + TailwindCSS)
│   └── app.vue                    # 루트 컴포넌트
├── server/
│   ├── api/
│   │   ├── sessions.get.ts        # GET /api/sessions — DynamoDB 세션 스캔
│   │   ├── sessions/
│   │   │   ├── [id].delete.ts     # DELETE /api/sessions/:id — 세션 삭제
│   │   │   └── [id]/
│   │   │       └── cancel.post.ts # POST /api/sessions/:id/cancel — 세션 취소
│   │   ├── reports/[id].get.ts    # GET /api/reports/:id — S3 보고서 조회
│   │   └── traces/[id].get.ts     # GET /api/traces/:id — DynamoDB 트레이스 조회
│   └── utils/
│       └── aws.ts                 # DynamoDB/S3 클라이언트 싱글톤
├── nuxt.config.ts                 # Nuxt 설정 (포트 3100, runtimeConfig)
├── package.json
└── tsconfig.json
```

## Configuration

`nuxt.config.ts`의 `runtimeConfig`으로 관리한다. 환경변수로 오버라이드 가능.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `AWS_REGION` | `us-east-1` | AWS 리전 |
| `DYNAMODB_TABLE_NAME` | `RcaAgentDevRcaSession` | DynamoDB 테이블명 |
| `S3_REPORT_BUCKET` | `rca-agent-dev-evidence` | S3 보고서 버킷 |

## Agent Guidelines

### Safe to Modify

- 페이지 (`app/pages/`)
- 컴포넌트 (`app/components/`)
- 스타일 (`app/assets/css/`)
- API 라우트 (`server/api/`)

### Approach with Caution

- `nuxt.config.ts` — 프레임워크 설정
- `server/utils/aws.ts` — AWS 클라이언트 싱글톤

### Common Mistakes to Avoid

- 서버 사이드 API에서 AWS SDK 호출 시 클라이언트 사이드로 노출하지 않도록 주의 (`server/` 디렉토리 안에서만 AWS SDK 사용)
- DynamoDB Scan 시 `begins_with(PK, 'RCA#')` 필터로 멱등성 키(`IDEMP#`) 레코드를 제외해야 함
