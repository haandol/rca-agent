---
name: progress-reporting
description: rca-progress MCP의 산출물 저장 계약. 파이프라인 단계 결과를 canonical JSON 또는 Markdown 파일로 저장할 때 참조한다.
---

# rca-progress MCP 사용 가이드

## 세션과 기록 방식

Python 래퍼가 현재 세션 ID와 산출물 디렉터리를 준비한다. 에이전트는 세션 파일이나
DynamoDB를 직접 수정하지 않는다. Python watcher가 저장된 산출물을 감지해 span과
가설 상태를 기록한다. 디렉터리는 실행마다 새로 생성되며 다른 실행의 산출물을 읽거나
재사용하지 않는다.

## 사용 가능한 도구

### `save_artifact(filename, content)`

현재 세션의 산출물을 원자적으로 저장한다. 허용되는 파일명은 다음뿐이다.

- `scoping.json`
- `hypotheses.json`
- `validation-{N}.json` (`N`은 1 이상의 정수)
- `playbook.json`
- `report.md`

JSON 산출물은 각 prompt section에 정의된 스키마를 따라야 한다. 경로, 하위
디렉터리, 임의 확장자는 사용할 수 없다.

저장 도구는 산출물의 형태를 저장 시점에 검사한다. 스키마의 필수 키가 없거나 빈
문자열이면 `ok: false`와 함께 누락된 필드명을 반환하고 파일은 저장되지 않는다.
이 응답을 받으면 지적된 필드를 채워 같은 파일을 다시 저장한다. 저장 실패를 무시하고
다음 단계로 넘어가면 세션이 완료되지 못한다.

## 호출 순서

각 단계를 완료한 직후 해당 산출물을 저장한다. 현재 실행에서 같은 파일을 보강해 다시
저장하면 watcher가 변경을 감지해 최신 내용을 다시 처리한다. 최종 보고서는 반드시
`report.md`로 저장해야 세션이 완료될 수 있다.
