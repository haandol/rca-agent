# RCA System Prompt Sections

`rca-system.md`는 이 디렉토리의 조각들을 `{{include: ...}}` 지시자로 합성한다. `prompt_builder.py`가 빌드 시 치환하므로 CC CLI에는 단일 프롬프트로 전달된다.

## 구조

```
sections/
├── core/
│   ├── artifacts-overview.md   # /tmp/rca-{id}/ 산출물 규칙
│   ├── pipeline-overview.md    # 11단계 개요 표
│   └── principles.md           # 에이전트 공통 원칙
├── artifacts/
│   ├── scoping.md              # scoping.json 스키마
│   ├── hypotheses.md           # hypotheses.json 스키마
│   ├── validation.md           # validation-{N}.json 스키마
│   └── playbook.md             # playbook.json 스키마 + 작성 규칙
└── stages/
    ├── 1-scoping.md
    ├── 2-hypothesis-generation.md
    ├── 3-7-validation-loop.md
    ├── 8-report.md             # 사람 가독성 위주 보고서 템플릿
    ├── 9-playbook.md           # 기계 실행 위주 플레이북 가이드
    ├── 10-remediation.md
    └── 11-verification.md
```

## 편집 원칙

- **단일 파일 500줄 미만, 논리 경계 유지**: 새 단계·스키마는 별도 파일로 추가하고 `rca-system.md`에서 include한다.
- **상호 참조 금지**: 섹션 파일은 다른 섹션을 include 하지 않는다. 중첩 include는 기술적으로는 허용(깊이 8)되지만, 플랫한 구조를 유지한다.
- **안전 경로**: include 경로는 `prompts/` 하위로 제한된다 (경로 이탈 시 RuntimeError).
- **테스트 무결성**: `tests/test_prompt_builder.py`가 최종 결합 프롬프트를 검증하므로 include 대상이 빠지거나 오타 나면 바로 실패한다.
