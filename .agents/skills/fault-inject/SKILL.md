---
name: fault-inject
description: RCA Agent의 단일 SQL 저장 컬럼 오류 데모를 계획하거나, 사용자가 요청한 장애 주입·상태 확인·복원을 진행한다. fault inject, 장애 주입, RCA 데모, 장애 리셋 요청에 사용한다. 일반 코드 수정에는 사용하지 않는다.
---

# 단일 저장 컬럼 장애 데모

현재 실행 진입점은 `scripts/run_realistic_demo.py`다. 저장소 루트의 AGENTS.md,
`docs/adr/infra/0007-demo-symptom-alarm-and-deployment-fault-injection.md`와
`packages/healthcare-sensor-app/AGENTS.md`를 읽고 현재 시나리오와 승인 범위를 확인한다.
이전 db-leak·red-herring·CPU·메모리 주입 명령은 실행하지 않는다.

## 준비와 실행

1. 기존 대화와 실행 기록에서 계정·리전·클러스터·서비스·컨테이너, 실패 알람,
   정상 이미지와 결함 이미지 지문, 증거 버킷, run ID와 journal 경로를 확인한다.
   정상 리비전을 최신 또는 직전 번호로 추정하지 않는다. 필요한 값이 없으면
   읽기 전용 조회로 확인하고, 대상이 여럿이면 실제 변경 전에 대상을 확정한다.
2. `uv run --project packages/agent --no-sync python scripts/run_realistic_demo.py --help`로 인자를 확인한다.
   `plan`은 정상 기준과 변경 계획을 보존하며, `apply`는 같은 run ID와 journal의
   계획을 사용한다. apply/status/restore에서 계획의 입력을 바꾸지 않는다.
   모든 실제 명령도 같은 Agent 환경으로 실행한다. runner는 첫 AWS 호출 전에 실행
   interpreter의 botocore 버전과 전체 ECS 모델 지문이 Agent lock/환경과 같은지 검사한다.
   불일치하면 중단한다. `AWS_DATA_PATH`만 설정해 구형 interpreter를 우회하지 않는다.
   검증한 모델을 해당 CLI 자식 프로세스에만 비압축 JSON으로 제공하며 전역 CLI는 바꾸지 않는다.
   stderr의 `ECS_MODEL_PREFLIGHT`에는 interpreter·버전·모델 지문만 기록된다.
3. 사용자가 승인한 범위에서만 `apply`로 결함 이미지를 배포한다. 계획 검토만
   요청했으면 주입하지 않는다. 이미 승인한 동일 범위는 반복 확인하지 않는다.
4. `status`와 실제 알람·세션 기록으로 진행 상태를 확인한다. 장기 실행 명령은
   완료 또는 실패까지 확인한다. 두 분석 엔진은 공용 큐를 경쟁 소비한다.
   Strands 전용 시연을 요청했다면 Headless 분석 워커가 큐를 받지 않는지 확인한다.
5. 정상화 런북이 먼저 공개되고 근본원인·코드 PR 미리보기·운영 개선이 이어지는지
   확인한다. 런북 실행은 별도 사용자 승인 경로만 사용한다. 직접 복원과 실행
   에이전트의 복구를 섞어 에이전트 성공으로 기록하지 않는다.
6. 사용자 요청 또는 기존 시연 정리 범위에 따라 `restore`를 사용한다. 이 명령은
   journal에 기록된 소유 변경을 기준으로 동작한다. 다른 작업의 배포·기록을
   임의로 지우거나 전체 RCA 이력을 초기화하지 않는다.

## 결과 기록

실제 오류와 저장 회복, 배포 수렴, 알람·분석·승인·실행 ID, 각 파트 상태와
확인하지 못한 항목을 구분한다. `plan`의 성공은 시연 성공이 아니며 API 성공만으로
복구를 판정하지 않는다. 로컬 테스트, 모델 평가, 배포 환경 시연도 따로 기록한다.
전체 JSON과 실행 원문은 지정된 증거 저장소·journal에 보관하고 요약만 보고한다.

`docs/demo/single-scenario-review.md`의 Vital 이벤트 v2·매초 생성·지속 재시도는
검토 시나리오다. 해당 앱 동작을 실제 코드와 관측에서 확인하기 전에는 이미
구현되었거나 시연에 성공했다고 주장하지 않는다.
