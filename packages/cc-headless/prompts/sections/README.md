# Prompt Sections

`rca-system.md`는 include 지시자로 다음 조각을 조립한다.

```text
core/
  artifacts-overview.md
  pipeline-overview.md
  principles.md
artifacts/
  scoping.md
  hypotheses.md
  validation.md
  playbook.md
stages/
  1-rca-agent.md
  2-report-agent.md
```

메인 프롬프트는 오케스트레이션만 정의한다. 세부 역할과 도구 권한은
`.claude/agents/`, 반복 가능한 절차는 `.claude/skills/`에서 관리한다.
