"""Materialize writing-only review drafts from the supplied independent evidence."""

import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path("/Users/dongkyl/git/rca-agent")
OUT = ROOT / "docs/test-reports/realistic-demo-review-2026-09-10"
SOURCE = Path("/private/tmp/rca-realistic-review")
TOOLS = Path("/private/tmp/rca-adr-tools-0.8.19")
HS = "packages/healthcare-sensor-app/"
ledger = json.loads((SOURCE / "contract-ledger.json").read_text())
coverage = json.loads((SOURCE / "sufficiency-contract-coverage.json").read_text())
sufficiency = (SOURCE / "sufficiency-review.md").read_text()
necessity = (SOURCE / "necessity-review.md").read_text()
mapping = json.loads((ROOT / "docs/adr/.mapping.json").read_text())
original_changes = [
    line[3:].strip() for line in (SOURCE / "change-scope.txt").read_text().splitlines()
    if line.strip()
]
f4_files = list(json.loads((SOURCE / "f4-applied.json").read_text())["files"])
MAIN_UPDATE = {
    "source": "사용자 main 최종자료 갱신 메시지 2026-09-10; 아직 최종 closure/model 결과 대기",
    "f4": {
        "applied": True,
        "strandsTargetedPassed": 113,
        "headlessTargetedPassed": 201,
        "behavior": "eval의 missing region/statistic/period/time은 not provided; production defaults unchanged",
        "testPlan": "/private/tmp/rca-realistic-review/f4-test-plan.md",
    },
    "verification": {
        "unitPassed": 1733,
        "packages": {"agent": 740, "headless": 824, "healthcare": 125, "infra": 44},
        "rootPassed": 136,
        "rootFailed": 1,
        "rootFailure": "old approved-baseline digest mismatch",
        "format": "PASS", "lint": "PASS", "build": "PASS", "typecheck": "PASS",
    },
    "model": {
        "runId": "realistic-final-20260910T025818Z-a57123",
        "inputDigest": "8dc813d320341b5347ed5f299a5c52217a5c84a7d02863e8be79e2672e860cfc",
        "status": "RUNNING", "finalResultReceived": False,
    },
    "images": {
        "count": 3, "matchesLatestProof": True,
        "record": "docs/test-reports/realistic-demo-evidence-2026-09-10/review-fixed-images.json",
    },
    "infraIntegration": {
        "status": "IN_PROGRESS",
        "change": "optional healthcare imageDigest; legacy imageTag default preserved",
        "reason": "CLI baseline requires a digest-pinned image",
        "finalTestsReceived": False,
    },
    "curieFinalRecheck": "PENDING",
    "awsDeployment": "NOT_RUN",
}


def excerpt(file, start, end):
    text = (ROOT / file).read_text()
    begin = text.index(start)
    finish = text.index(end, begin) if end else len(text)
    line = text.count("\n", 0, begin) + 1
    return {"kind": "excerpt", "location": f"{file}:{line}", "content": text[begin:finish].rstrip()}


def changed_hunk(file, needle):
    diff = subprocess.check_output(
        ["git", "diff", "--unified=3", "--", file], cwd=ROOT, text=True
    )
    hunks = re.split(r"(?=^@@ )", diff, flags=re.M)
    hunk = next(h for h in hunks[1:] if needle in h)
    return {"kind": "diff", "location": file, "content": hunk.rstrip()}


def component(name, responsibility, implementation, verification, code, explanation):
    return dict(
        name=name, responsibility=responsibility, implementation=implementation,
        verification=verification,
        codeEvidence=[dict(code, explanation=explanation, tests=verification)],
    )


repo_diff = changed_hunk(
    HS + "src/test_service/adapters/secondary/sensor_repository/sqlalchemy_sensor_repository.py",
    "async with self._database.session_context()",
)
adapter_diff = changed_hunk(
    "packages/agent/src/rca_agent/eval_adapter.py", 'EvalSourceMetadata'
)
workload_code = excerpt(
    HS + "src/test_service/services/traffic_generator.py",
    "            missed = max(", "            slot += 1",
)
maintenance_code = excerpt(
    HS + "src/test_service/maintenance.py",
    "        async def release():", "        emit(",
)
fixed_command_code = excerpt(
    "scripts/run_realistic_demo.py", "def maintenance_command(", "\ndef atomic_create(",
)
projection_code = excerpt(
    "tests/harness/scenario-capture-boundary.mjs",
    "export function projectIncidentCaptures(", "  const ordinal",
)
http_diff = changed_hunk(HS + "src/test_service/middleware/logging.py", "error_type = type(exc).__name__")

MECHANISM = component(
    "같은 입력과 세션 수명",
    "저장·조회가 같은 요청 조건과 세션 정리 경계를 사용한다.",
    "데이터 접근부는 session_context로 요청 본문의 예외를 정리 경로에 전달한다. 빌드 리비전 r2는 실제 행별 SQL을, r3는 예외 경로 반환 누락을 선택한다.",
    "원본 T1 서비스 117 PASS/2 SKIP/1 XFAIL; T9 실제 PostgreSQL 2 PASS는 main 제공 로그. 당시 proof03에서 SQL 1/121/1과 동일 응답 hash를 확인했다.",
    repo_diff,
    "generator 순회에서 context manager로 바꾼 실제 diff다. 세션의 종료 책임이 요청 본문 실패와 연결된다.",
)
WORKLOAD = component(
    "완료 지연과 독립적인 요청 시작",
    "처리 중인 요청 상한과 실행하지 못한 요청을 구별한다.",
    "정해진 슬롯을 놓치면 skipped로 세고, active 상한이면 새 작업을 만들지 않는다. 다음 시각은 현재 시각과 간격으로 잡아 몰아 보내기를 피한다.",
    "원본 T1의 stalled_requests 및 event_loop_delay 경계 시험 PASS; 반복 실행 결과를 새 측정으로 세지 않는다.",
    workload_code,
    "missed와 active 상한이 서로 다른 생략 사유를 계수한다. 이 발췌는 실제 함수의 연속 구간이다.",
)
CLEANUP = component(
    "소유 트랜잭션 종료",
    "정비 작업 자신의 트랜잭션과 연결을 종료한다.",
    "rollback이 실패해도 finally에서 connection을 닫는다. cleanup task를 shield하고 취소 중에도 종료를 기다린다.",
    "원본 T8의 start/LOCK/rollback 오류 close 3종 PASS; T9는 실제 PostgreSQL 별도 제공 실행 2 PASS.",
    maintenance_code,
    "소유 연결 정리가 rollback의 성공 여부와 독립적으로 실행되는 실제 코드다.",
)
OWNER = component(
    "고정 정비 명령과 기존 실행 복원",
    "소유된 정비 모듈만 시작하고 과거 journal의 임의 프로그램 실행을 거부한다.",
    "명령은 MAINTENANCE_COMMAND에서 만들고 run ID·유한 유지 시간·schema를 검사한다. 기존 journal의 복원은 launch 검증과 분리된다.",
    "necessity-review-final.md: 독립 N1 반례 재검사 PASS, Python 40 PASS, 관련 Node 15 PASS. 실제 AWS 호출 없음.",
    fixed_command_code,
    "N1 해소를 확인한 고정 명령 경계다. 임의 command를 포맷하여 실행하는 기존 선택 기능이 없다.",
)
PROJECTION = component(
    "사고 전후 관측의 선택",
    "모델 입력에서 미래 복원 정보와 평가자의 정답을 제외한다.",
    "같은 원본의 normal과 fault만 pair로 구성하고 원본 포인터 매핑을 따로 둔다. 관측 식별자는 중립적인 obs/capture 값이다.",
    "원본 T10 15 PASS, T12 projection 함수 문서 보완 후 11 PASS. 입력 변경 불변성은 catalog 회귀로 검사했다.",
    projection_code,
    "모델로 들어갈 두 구간을 직접 선택하는 연속 코드다. 복원·cleanup 구간을 pair에 추가하지 않는다.",
)
METADATA = component(
    "원본 알람 메타데이터의 전달",
    "평가 어댑터가 제공 값과 부재를 공용 분석 경계로 옮긴다.",
    "현재 diff는 제공된 Region/AlarmArn/Trigger와 명시적 EvalSourceMetadata를 전달한다. 최종 프롬프트의 F4 해소 여부는 별도 독립 재검토를 기다린다.",
    "원본 T11 최종 prompt 반례 FAIL 뒤 F4 반영. main 통보의 Strands 113/Headless 201 targeted PASS; 최종 Curie 재검토는 대기한다.",
    adapter_diff,
    "현재 실제 diff는 어댑터 경계의 변경 증거다. 기존 좁은 테스트의 통과를 최종 프롬프트 비노출 보장으로 확대하지 않는다.",
)
HTTP_BOUNDARY = component(
    "HTTP 오류의 안전한 기록",
    "요청 처리 중 드라이버 오류의 원문 값이 앱·서버 로그로 넘어가는 것을 막는다.",
    "전체 응답 수명을 감싸는 ASGI 경계에서 오류 종류만 추출한다. 응답 전 오류는 일반 500 응답으로 처리하고, 응답 시작 뒤 오류는 원래 예외가 연결되지 않은 안전한 오류로 종료한다.",
    "main의 healthcare 125 PASS 및 F1 수정 완료 통보. 원본 canary 반례와 후속 독립 재검토는 최종 합성에서 구분한다.",
    http_diff,
    "원래 logger.exception과 재전파를 바꾼 실제 diff다. driver DETAIL을 그대로 기록하지 않는 HTTP 경계를 보여준다.",
)


def hill(title, question, contracts, claim, example, counter, assessment, responsibility,
         interactions, outcome, components, diagram, notice, reason):
    return dict(
        title=title, reviewQuestion=question, sliceType="logical-capability", sliceName=title,
        contractIds=contracts, claim=claim, workedExample=example,
        counterexample=counter, assessment=assessment,
        container=dict(responsibility=responsibility, interactions=interactions, outcome=outcome),
        components=copy.deepcopy(components), diagram=diagram, notice=notice, reason=reason,
    )


SAME_INPUT = """sequenceDiagram
  participant Runner as 로컬 비교 실행
  participant Service as 고정 리비전 서비스
  participant DB as PostgreSQL
  Runner->>Service: 정상 구성과 요청 조건
  Service->>DB: 업무 SQL
  DB-->>Service: 응답과 연결 반환
  Runner->>Service: 같은 요청과 결함 구성
  Service->>DB: 변경된 SQL 또는 연결 수명
  DB-->>Service: 증폭 또는 대기와 실패
  Runner->>Service: 정상 리비전 복원과 같은 요청
  Service->>DB: 업무 SQL 재실행
  DB-->>Service: 응답과 연결 반환
  Runner->>Runner: 원본 시각과 동일 입력 비교"""
CLEANUP_FLOW = """sequenceDiagram
  participant Owner as 소유 정비 작업
  participant DB as PostgreSQL
  participant Stop as 종료 또는 유지 시간 만료
  Owner->>DB: 자기 트랜잭션 시작과 쓰기 충돌 락
  Stop->>Owner: 종료 요청
  Owner->>DB: 자기 트랜잭션 rollback
  alt rollback 성공
    DB-->>Owner: 종료 확인
    Owner->>DB: 자기 연결 닫기
  else rollback 실패
    DB-->>Owner: 오류
    Owner->>DB: finally에서 자기 연결 닫기
  end"""
RESTORE_FLOW = """flowchart TD
  Original[원본과 소유 실행 영속 기록] --> Apply[결함 적용]
  Apply --> Failure[실패 또는 중단]
  Failure --> Recover[소유권 확인과 복원 경계]
  Recover --> Service[원래 서비스 정의 복귀 시도]
  Recover --> Job[자기 정비 작업 중지 시도]
  Service --> Check[새 서비스 증상 확인]
  Job --> Check
  Check --> Result[기록과 증상으로 복원 판정]
  Failure --> Journal[기록 실패의 격리 필요 F2]
  Journal --> Recover"""
PROJECTION_FLOW = """flowchart LR
  Raw[시각이 있는 원본 관측] --> Cut[normal과 fault만 선택]
  Raw --> Archive[운영자용 복원 증거 보존]
  Cut --> Model[모델 입력]
  Model --> Result[실제 분석 산출물]
  Result --> Eval[공통 평가]
  Expected[평가자 전용 정답과 필수 증거] --> Eval"""
MODEL_FLOW = """flowchart LR
  Input[동결한 시나리오 입력] --> Adapter[두 엔진의 번역 어댑터]
  Adapter --> Prompt[공용 최종 프롬프트]
  Prompt --> Model[실모델 분석]
  Model --> Result[정규화 결과와 보고서]
  Result --> Gate[모든 엔진과 시나리오 검사]
  Gate --> Review[결과 검토와 명시 승인]
  Review --> Baseline[새 기준선]
  Missing[누락된 메타데이터] --> Boundary[부재 유지 F4 재검토]
  Boundary --> Prompt"""

ADRS = list(ledger)
CONFIGS = {
    "infra-0004": dict(
        adr=ADRS[0], title="같은 부하에서 결함과 복원을 관측하는 Healthcare 서비스",
        intent="실제 데이터베이스를 사용하는 사설 데모 서비스를 두고, 코드·설정 차이가 사용자 증상으로 나타나는지 비교한다.",
        contracts="사설 네트워크와 비밀 참조, 정상 이미지 기본값, 제한된 부하, SQL 값 비노출, 소유 연결 정리와 동일 부하 비교를 보존한다.",
        impact="초기 검토는 HTTP 실패 로그에서 SQL 값 노출(F1)을 확인했다. 수정과 테스트 완료 통보를 받았으며 독립 최종 재검토를 기다린다.",
        findings=["F1", "U1"], notes=["N2"],
        hills=[
            hill("같은 부하에서 실제 결함과 안전한 관측을 비교한다",
                 "처리가 지연되어도 같은 요청 계획을 유지하고 값 노출 없이 실제 증상을 관측하는가?",
                 ["D0", "R1", "R2", "R3", "R4", "R6"],
                 "완료를 기다리지 않는 요청 계획과 명시적인 결함 리비전이 로컬 비교를 가능하게 한다.",
                 "원본 검토의 120개 응답 비교에서는 정상·복원의 SQL 1회와 결함의 121회가 같은 응답 hash를 유지했다.",
                 "원본 F1 재현은 hide_parameters=True여도 driver DETAIL의 canary가 HTTP traceback에 남는 것을 보였다.",
                 "로컬 메커니즘 증거와 AWS 배포 증거는 분리한다. F1 수정의 최종 판정은 아직 반영하지 않았다.",
                 "정상·결함·복원에 같은 요청 조건을 적용한다.",
                 "내장 부하가 서비스와 DB를 호출하고, 독립 관측 경로가 요청 상태와 SQL 시간을 남긴다.",
                 "요청 시작량과 완료량을 구별하며 실제 SQL·연결 차이를 비교할 수 있다.",
                 [WORKLOAD, MECHANISM, HTTP_BOUNDARY], SAME_INPUT,
                 "이 순서도는 로컬 비교를 설명하며 실제 AWS 알람 전이를 확인했다는 뜻이 아니다.",
                 "세 구간의 동일 요청과 DB 응답 순서를 비교해야 하므로 요청 순서도를 사용한다."),
            hill("종료와 실패에서 자기 트랜잭션을 정리한다",
                 "종료 신호나 rollback 실패가 발생해도 자기 연결 정리를 마치는가?",
                 ["R5"], "정비 작업은 자신이 연 트랜잭션과 연결만 정리한다.",
                 "원본 T8은 트랜잭션 시작·LOCK·rollback 오류 각각에서 close 호출을 확인했다.",
                 "다른 실행의 연결을 닫는 방식은 이 계약을 충족하지 않는다. 소유권 거부 시험을 별도로 유지한다.",
                 "독립 오류 시험과 제공된 PostgreSQL 실행은 로컬 종료 경로를 뒷받침한다.",
                 "유한한 락 유지와 종료 처리를 하나의 작업이 소유한다.",
                 "종료 요청은 cleanup 작업을 기다리고, rollback 성공 여부와 무관하게 연결을 닫는다.",
                 "서비스 종료나 유지 한도 만료 뒤 자기 DB 자원을 남기지 않는 경로를 검사한다.",
                 [CLEANUP], CLEANUP_FLOW,
                 "rollback 오류 뒤에도 연결 닫기 화살표가 이어지는지가 검토 지점이다.",
                 "종료 요청과 DB 응답, 오류 후 정리 순서가 핵심이므로 요청 순서도를 사용한다."),
        ],
        choice_indexes=[0, 2, 4, 5],
        related=ADRS[1],
        similarity="정상·결함·복원을 같은 요청 조건으로 비교하고 소유 연결을 정리한다.",
        difference="infra/0004는 서비스 배포와 관측 기반을, infra/0007은 결함 적용과 알람·복원 판단을 정한다.",
        reviewImpact="따라서 서비스의 제한된 부하와 로그 비노출은 여기서 읽고, 실제 알람 성공 여부는 별도 실행 증거로 구분한다.",
    ),
    "infra-0007": dict(
        adr=ADRS[1], title="실제 결함 적용부터 소유 변경 복원까지의 검토",
        intent="같은 입력에 대한 정상 성공, 실제 결함의 증상, 복원 후 회복을 연결해 데모의 인과관계를 확인한다.",
        contracts="실제 네 원인, 변경 전 원본 보존, 독립 복원, 관측의 출처·비노출과 레거시 수치 계약을 보존한다.",
        impact="초기 F1 로그 노출과 F2 기록 오류에 따른 복원 누락을 수정 대상으로 기록했다. 두 수정과 최소 150초 보강은 최종 독립 재검토 대기다.",
        findings=["F1", "F2", "U1", "U2", "U3"], notes=["N1", "N2"],
        hills=[
            hill("같은 요청으로 네 결함의 증상을 구별한다",
                 "풀·SQL·연결 수명의 차이를 같은 요청으로 비교하고 알람 의무를 지키는가?",
                 ["D0", "R1", "R2", "R4", "R5", "R6", "R9"],
                 "풀 축소, SQL 증폭, 정비 락, 예외 뒤 반환 누락은 서로 다른 실제 메커니즘이다.",
                 "원본 검토에서 pool 8/1/8 비교와 SQL 1/121/1 비교, r3의 1/2/3 checkout 잔류를 확인했다.",
                 "레거시 주입 전 대기 150초는 주입 후 최소 관측 시간을 대신하지 않는다. U3 보강은 최종 재검토 대기다.",
                 "실제 PostgreSQL의 메커니즘 증거는 있으나 AWS의 정상·결함·복원 알람 관계는 미검증이다.",
                 "실제 원인의 변화와 요청 결과를 연결한다.",
                 "고정 소스 서비스와 제한된 부하가 PostgreSQL에 같은 업무를 보내고 원본 관측을 남긴다.",
                 "저장 실패와 조회 지연을 장애 플래그 이름에 의존하지 않고 비교한다.",
                 [MECHANISM, WORKLOAD], SAME_INPUT,
                 "로컬 조회 표본의 시간과 CloudWatch 1분 집계를 혼동하지 않으며 500ms 충족을 주장하지 않는다.",
                 "세 구간에서 입력과 응답이 어떻게 비교되는지 요청 순서로 드러낸다."),
            hill("기록 장애가 나도 소유 변경의 복원을 시도한다",
                 "변경 뒤 journal 저장에 실패해도 원래 서비스와 자기 정비 작업을 각각 정리하는가?",
                 ["R3", "R8"],
                 "journal은 변경 전에 저장한 원본과 소유 실행 정보를 담는 복원 기록이다.",
                 "원본 F2 시험은 서비스 변경 뒤 기록 오류를 주입해 원래 정의 :1 대신 :2가 남고 복원 시도가 0회인 것을 확인했다.",
                 "apply 오류 기록만 보호해도 restore_intent 기록이 먼저 실패하면 복원은 계속 막힐 수 있다.",
                 "F2 수정·시험 완료는 main 통보이며 최종 독립 판정은 대기한다. N1의 임의 명령 제거는 독립 재검토로 해소됐다.",
                 "보존된 원본과 소유권에 따라 각 정리를 시도하고 증상 회복을 확인한다.",
                 "복원 CLI는 서비스 정의와 소유 정비 작업을 다루며 기록 손실은 성공 판정과 별도로 취급해야 한다.",
                 "복원 성공은 API 응답만으로 정하지 않고 실제 서비스 증상과 기록 조건으로 판정한다.",
                 [OWNER, CLEANUP], RESTORE_FLOW,
                 "도식은 승인된 복원 계약과 F2의 수정 지점을 설명한다. 수정 후 AWS 실행 성공을 나타내지 않는다.",
                 "독립적인 두 정리와 기록 실패의 관계를 드러내기 위해 분기 구조도를 사용한다."),
            hill("원본 관측과 모델 평가의 증거 수준을 분리한다",
                 "사고 관측의 출처를 보존하면서 복원 결과나 정답이 모델 입력으로 들어가는 것을 막는가?",
                 ["R7", "R10"],
                 "시각·단위·원본 로그와 코드 차이는 관측에 남기고 정답과 복원 정보는 별도로 보존한다.",
                 "projectIncidentCaptures는 normal과 fault만 선택하며 복원·cleanup 변경이 입력을 바꾸지 않는 시험이 통과했다.",
                 "원본 F1은 서비스 로그 비노출 의무의 예외였으므로 투영 함수 시험만으로 R7 전체를 충족했다고 할 수 없었다.",
                 "새 모델 결과는 별도 회차로 검토한다. 이전 진단 실패·중단 기록과 승인 전 기준선을 보존한다.",
                 "관측·평가 기대값·운영자 복원 증거의 용도를 구별한다.",
                 "원본에서 사고 입력을 선택하고, 분석 결과는 별도의 공통 평가 기준과 비교한다.",
                 "로컬 DB 실측을 AWS 수집 결과나 모델 합격으로 잘못 해석하지 않는다.",
                 [PROJECTION, HTTP_BOUNDARY], PROJECTION_FLOW,
                 "정답과 복원 증거에서 모델 입력으로 향하는 연결은 없다.",
                 "입력·운영 기록·채점 데이터의 경계를 보여주기 위해 데이터 흐름도를 사용한다."),
        ],
        choice_indexes=[5, 6, 7, 8, 9],
        related=ADRS[0],
        similarity="서비스가 실제 DB 증상을 만들고 소유 작업을 정리한다는 경계를 공유한다.",
        difference="infra/0007은 변경 원본 보존과 전체 복원 판단까지 요구하므로 서비스 자체 cleanup보다 범위가 넓다.",
        reviewImpact="서비스 단위 종료 시험만으로 journal 장애 후 배포 복원이나 AWS 알람 관계를 충족 처리하지 않는다.",
    ),
    "agent-0016": dict(
        adr=ADRS[2], title="실제 관측으로 두 RCA 엔진을 평가하는 경계 검토",
        intent="두 분석 엔진을 같은 관측·원인·반증·안전성 계약으로 비교하며 일반 CI와 실모델·배포 검증을 분리한다.",
        contracts="네 평가 계층, 공용 분석 경로, 관측의 부재·출처, 엔진 공통 기준과 두 엔진 전수 결과의 명시 승인 경계를 보존한다.",
        impact="F4는 평가에서 없는 region/statistic/period/time을 not provided로 표시하도록 수정됐고 운영 기본값은 유지한다. 관련 113/201개 시험이 통과했으며 최종 재검토와 모델 회차 결과를 기다린다.",
        findings=["F4", "U1", "U2"], notes=["F3"],
        hills=[
            hill("실제 결함 관측에서 사고 입력을 만든다",
                 "동일 요청에서 생긴 네 원인을 구별할 관측만 모델에 제공하는가?",
                 ["R1", "R2"],
                 "모델 입력은 실제 PostgreSQL의 정상·결함 구간에서 선택하며 원인 정답은 평가자 측에 둔다.",
                 "원본 검토는 중립 obs/capture 식별자와 4개 source snippet의 hash·줄 일치를 확인했다.",
                 "미래 복원 결과나 정리 성공을 입력에 넣으면 사고 시점에 알 수 없던 정보를 원인 근거로 쓰게 된다.",
                 "T10/T12가 관측 선택과 함수 문서를 검사했다. 최신 proof 교체의 최종 결과는 별도 전달 자료를 따른다.",
                 "원본 시각·단위·리소스와 코드 차이가 있는 사고 입력을 만든다.",
                 "원본 보존과 모델 투영을 분리하고 동일 원본의 normal과 fault를 선택한다.",
                 "실제 원인 관측과 평가 기대값의 혼입을 막는 경계를 읽을 수 있다.",
                 [PROJECTION, MECHANISM], PROJECTION_FLOW,
                 "복원 원본은 운영자에게 남고 모델은 사고 전후 두 구간만 받는다.",
                 "관측과 정답의 경계가 핵심이므로 데이터 흐름도를 사용한다."),
            hill("제공된 메타데이터와 같은 기준으로 두 엔진을 평가한다",
                 "없는 알람 정보를 만들어내지 않고 두 엔진의 실제 결과가 모두 모여야 승인되는가?",
                 ["D0", "R3", "R4", "R6", "R7"],
                 "평가 어댑터는 운영과 같은 분석 경로를 호출하고 관측 식별자를 model-eval에서만 전달한다.",
                 "원본 F4는 region이 없는 네 입력에서도 us-east-1이 최종 prompt에 표시됨을 양쪽 엔진에서 재현했다.",
                 "메타데이터 JSON이 올바르더라도 별도의 알람 상세 기본값이 충돌하면 부재 유지 계약을 충족하지 못한다.",
                 "F4 반영 파일이 존재하지만 최종 독립 통보 전에는 해소를 확정하지 않는다. 새 결과와 기준선 승인도 미확정이다.",
                 "두 엔진의 원인·반증·산출물·안전성 기준을 같은 규칙으로 검사한다.",
                 "입력 어댑터, 공용 프롬프트, 실제 분석, 정규화 결과와 공통 평가가 이어지고 전수 결과 뒤에 검토·승인이 온다.",
                 "오프라인 통과나 한 엔진의 부분 결과만으로 새 모델 품질을 승인하지 않는다.",
                 [METADATA], MODEL_FLOW,
                 "실행 리전과 사고 리소스의 관측 리전은 구별하며, 새 기준선 화살표는 승인 조건이지 현재 승인 사실이 아니다.",
                 "부재 유지와 전체 승인 경계를 함께 보여주기 위해 데이터 흐름도를 사용한다."),
            hill("배포 검증을 실제 복원 증거로 제한한다",
                 "deployed-e2e를 선언한 실행이 실제 소유 자원과 서비스 회복으로 끝나는가?",
                 ["R5"],
                 "현재 네 사례는 model-eval만 선언하며 배포 E2E 성공을 선언하지 않는다.",
                 "소유 CLI의 정상·부분 실패 복원은 상태형 double에서 검사했고 로컬 PG의 cleanup은 별도 관측으로 보존했다.",
                 "reset 응답이나 준비된 관측만으로 실제 배포와 서비스 회복을 통과시킬 수 없다.",
                 "AWS에서 최신 태스크 종료·증상 회복을 확인하지 않았으므로 이 계약은 UNVERIFIED로 유지한다.",
                 "실행 가능한 제어와 실제 복원 관측이 있는 배포 사례만 허용한다.",
                 "소유 원본 기록에서 서비스와 정비 작업의 복원을 각각 시도하고 후속 증상을 확인한다.",
                 "제공 관측 평가와 실제 증거 탐색의 검증 범위를 분리한다.",
                 [OWNER], RESTORE_FLOW,
                 "이 도식은 검증해야 할 배포 경계이며 AWS 성공 증거를 대신하지 않는다.",
                 "두 복원 대상과 최종 증상 검사의 관계를 설명하기 위해 분기 구조도를 사용한다."),
        ],
        choice_indexes=[10, 11],
        related=ADRS[1],
        similarity="관측의 원본·시간을 보존하고 실제 결함·복원 증거와 합성 입력을 구별한다.",
        difference="agent/0016의 model-eval은 제공 관측의 해석·인용을 평가하며 infra/0007의 실제 배포 알람·회복 검증을 대신하지 않는다.",
        reviewImpact="같은 원본을 읽어도 모델 품질, 로컬 DB 메커니즘, AWS 증거 탐색의 결과를 서로 다른 증거 수준으로 기록한다.",
    ),
}

FINDING_MAP = {
    "F1": {"infra-0004": ["R4"], "infra-0007": ["R7"]},
    "F2": {"infra-0007": ["R8"]},
    "F4": {"agent-0016": ["R3"]},
    "U1": {"infra-0004": ["R6"], "infra-0007": ["D0", "R6"], "agent-0016": ["R5"]},
    "U2": {"infra-0007": ["R10"], "agent-0016": ["D0", "R7"]},
    "U3": {"infra-0007": ["R9"]},
}


def original_finding(fid, target):
    block = re.search(rf"^#### {fid} \[(.*?)\] (.*?)(?=^#### |^### |\Z)", sufficiency, re.S | re.M)
    category, body = block.groups()
    title, _, detail = body.partition("\n")
    fields = {}
    for line in detail.splitlines():
        match = re.match(r"- ([^:]+): (.*)", line)
        if match:
            fields[match[1]] = match[2]
    evidence = fields.get("evidence", detail)
    result = fields.get("testResult", fields.get("test / testResult", "원본 검토 참조"))
    update = {
        "F1": "수정·테스트 완료 main 통보. 최종 독립 재검토 대기.",
        "F2": "수정·테스트 완료 main 통보. 최종 독립 재검토 대기.",
        "F4": "수정 반영 및 Strands 113/Headless 201 targeted PASS main 통보. 평가의 누락 정보는 not provided, 운영 기본값 유지. 최종 Curie 재검토 대기.",
        "U3": "최소 150초 보강과 테스트 완료 main 통보. 최종 독립 재검토 대기.",
        "U1": "최신 PG proof와 이미지 기록은 추가되었으나 AWS 배포 E2E는 미실행.",
        "U2": "이전 진단 모델 실행은 실패/중단으로 보존. realistic-final-20260910T025818Z-a57123 실행 중이며 최종 성공 결과 미통보.",
    }[fid]
    return dict(
        sourceId=fid, category=category, perspective="sufficiency",
        summary=f"{fid} — {title} (원본 검토; 최종 갱신 대기)",
        confidence="medium" if fid in ["U1", "U3"] else "high",
        adrQuote=fields.get("basis", fields.get("perspective", "")),
        code=evidence, evidence=evidence,
        test=fields.get("test", fields.get("test / testResult", "원본 T6/T7/T9; AWS NOT RUN")),
        testResult=result + " / 갱신: " + update,
        whyItMatters=fields.get("whyItMatters", fields.get("whyItMatters / verifiable premise", "")),
        expectedBehavior=fields.get("expectedBehavior", ""),
        observedBehavior=fields.get("observedBehavior", ""),
        requestedChange=fields.get("requestedChange", fields.get("requestedChange / fix", "")),
        editTargets=fields.get("editTargets", ""),
        completionCriteria=fields.get("completionCriteria", ""),
        fix="main과 Curie의 최종 결과를 반영한 뒤 해당 계약의 상태를 확정한다.",
        contractIds=FINDING_MAP[fid][target], remediationUpdate=update,
        originalFindingMarkdown=block.group(0),
    )


choices_section = sufficiency.split("### Notable implementation choices\n", 1)[1].split("### Verifiable premises", 1)[0]
choices = []
for line in choices_section.splitlines():
    if line.startswith("| ") and not line.startswith("| Selected") and not line.startswith("| ---"):
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) == 4:
            choices.append(dict(zip(["choice", "evidence", "intentFit", "whyItMatters"], cols)))

TESTS = sufficiency.split("### Tests executed\n", 1)[1].split("### Notes", 1)[0]
proof_path = "tests/fixtures/observations/realistic-local-20260910/proof-review-fixed-01.json.txt"
proof_sha = hashlib.sha256((ROOT / proof_path).read_bytes()).hexdigest()
assert proof_sha == "83aac573c02bd6eb9596ced5e0797460018704b62b955905b68b15707d1b4f2b"

for target, config in CONFIGS.items():
    dest = OUT / target
    dest.mkdir(parents=True, exist_ok=True)
    for name in [
        "review-baseline.md", "contract-ledger.json", "necessity-review.md",
        "necessity-review-final.md", "sufficiency-review.md",
        "sufficiency-contract-coverage.json", "sufficiency-reviewed-sources.json",
        "f4-test-plan.md", "change-scope.txt",
    ]:
        (dest / name).write_bytes((SOURCE / name).read_bytes())
    adr_text = (ROOT / config["adr"]).read_text()
    (dest / "adr-source.md").write_text(adr_text)
    adr_decision = adr_text.split("## Decision\n", 1)[1].split("\n## ", 1)[0].strip()
    category = config["adr"].split("/")[2]
    entry = next(x for x in mapping["categories"][category]["adrs"] if x["path"] == config["adr"])
    rows = []
    for original in coverage:
        if original["adr"] != config["adr"]:
            continue
        row = {k: v for k, v in original.items() if k not in ["adr", "hill"]}
        row["originalAdrBasis"] = row["adrBasis"]
        row["adrBasis"] = " ".join(line.strip() for line in row["adrBasis"].splitlines()).strip()
        row["requirement"] = row["adrBasis"] if row["contractId"] != "D0" else (
            " ".join(adr_decision.split("\n\n", 1)[0].splitlines())
        )
        if row["contractId"] == "D0":
            row["decisionObligationsFullText"] = adr_decision
        row["reviewSnapshot"] = "original-independent-review; final recheck pending"
        rows.append(row)
    ids = [x["contractId"] for x in rows]
    assert ids == [x["contractId"] for x in ledger[config["adr"]]]
    findings = [original_finding(fid, target) for fid in config["findings"]]
    hills = copy.deepcopy(config["hills"])
    diagrams = []
    for index, h in enumerate(hills, 1):
        h["id"] = f"H{index}"
        h["diagramIds"] = [f"V{index}"]
        for ci, c in enumerate(h["components"], 1):
            c["id"] = f"C{ci}"
        diagrams.append(dict(
            id=f"V{index}", question=h["reviewQuestion"],
            diagramType=h["diagram"].splitlines()[0].split()[0],
            section=h["title"], reason=h["reason"],
            evidence="sufficiency-review.md의 원본 수직 기능 및 계약 근거; " +
                     "; ".join(c["codeEvidence"][0]["location"] for c in h["components"]),
        ))
    scope = sorted(set(
        [c["codeEvidence"][0]["location"].split(":")[0] for h in hills for c in h["components"]]
        + [
            HS + "src/test_service/main.py",
            HS + "src/test_service/adapters/secondary/database_adapter.py",
            HS + "demo/local_runner.py", HS + "demo/local_worker.py",
            "packages/infra/lib/stacks/healthcare-service-stack.ts",
            "packages/infra/lib/stacks/rds-stack.ts",
            "scripts/run_realistic_demo.py", "scripts/run_deployed_e2e.py",
            "tests/harness/evaluator.mjs", "tests/harness/model-cli.mjs",
            "tests/harness/approve-cli.mjs", "tests/scenarios",
            "packages/agent/src/rca_agent/eval_adapter.py",
            "packages/headless-codex/src/headless_codex/eval_adapter.py",
        ]
    ))
    target_changes = set()
    for file in original_changes:
        if target in ("infra-0004", "infra-0007") and file.startswith((HS, "packages/infra/")):
            target_changes.add(file)
        if target in ("infra-0007", "agent-0016") and file.startswith((
            "tests/scenarios/", "tests/fixtures/", "tests/harness/realistic",
            "tests/harness/scenario-capture", "tests/harness/evaluator.test",
            "tests/harness/model-and-approval.test",
        )):
            target_changes.add(file)
    if target in ("infra-0004", "infra-0007"):
        target_changes.add(HS + "src/test_service/middleware/logging.py")
        target_changes.add(HS + "tests/test_http_exception_logging.py")
    if target in ("infra-0007", "agent-0016"):
        target_changes.update((
            "scripts/run_realistic_demo.py", "scripts/run_deployed_e2e.py",
            "tests/harness/deployed-e2e-driver.test.mjs", "docs/execution-live-e2e-runbook.md",
        ))
    if target == "agent-0016":
        target_changes.update(f4_files)
    scope = sorted(set(scope) | target_changes)
    source_hashes = {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
        for p in scope if (ROOT / p).is_file()
    }
    context = dict(
        intent=config["intent"],
        preconditions="사설 서비스 또는 전용 로컬 PostgreSQL, 소유 실행 ID, 불변 소스와 같은 부하 조건을 전제로 한다.",
        contracts=config["contracts"],
        scopeAndRisk="전체 ADR 본문과 원래 호출 경로를 포함하는 full 검토다. 계약 상태는 원본 독립 검토 시점이며, 수정 후 재검토·최종 모델 결과는 아직 반영하지 않았다.",
    )
    data = dict(
        language="ko", reviewMode="full", title=config["title"], adr=config["adr"],
        status="Proposed", verdict="FIX_REQUIRED", artifactState="DRAFT_AWAITING_MAIN_FINAL_DATA",
        mainUpdates=MAIN_UPDATE,
        atAGlance=dict(
            impact=config["impact"],
            action="Curie의 수정 후 독립 재검토와 main의 최종 실행 자료를 반영한 뒤 공식 최종 검증을 수행한다. ADR은 Proposed로 유지한다.",
            risk="AWS 배포 E2E와 실제 알람 관계는 미검증이다. 새 모델 품질·기준선 승인은 미확정이며 이전 실패/중단 자료를 보존한다.",
        ),
        relatedAdrComparisons=[dict(
            adr=config["related"], title=(ROOT / config["related"]).read_text().splitlines()[0].lstrip("# "),
            similarity=config["similarity"], difference=config["difference"],
            reviewImpact=config["reviewImpact"], evidence="비교한 두 ADR의 전체 Decision 및 Requirement contract",
        )],
        diagramRequirements=diagrams,
        reviewHike=dict(context=context, hills=[
            {k: v for k, v in h.items() if k not in ["diagram", "notice", "reason"]} for h in hills
        ]),
        reviewDiagnostics=dict(
            contractCompleteness=dict(status="ISSUE", assessment="원본 검토는 위반 및 미검증 행을 확인했다. 수정 후 상태 갱신은 대기한다.", evidence="원본 sufficiency-contract-coverage.json"),
            testSufficiency=dict(status="UNVERIFIED", assessment="원본 재현·테스트 결과를 보존하며 최종 재검토와 모델/AWS 증거는 별도로 구분한다.", evidence="sufficiency-review.md Tests executed; main 최종 자료 대기"),
            necessity=dict(status="CLEAR", assessment="N1 독립 재검토 PASS. N2는 선택적 정리이며 변경하지 않았다.", evidence="necessity-review-final.md"),
        ),
        report=str(dest / "implementation-review.md"), scope=scope,
        changeScope=sorted(target_changes | set(
            c["codeEvidence"][0]["location"].split(":")[0]
            for h in hills for c in h["components"] if c["codeEvidence"][0]["kind"] == "diff"
        )),
        changeScopeBasis="원본 change-scope.txt의 해당 기능 경로 및 main이 통보한 후속 F1/F4/U3 변경. 혼합 작업 트리의 기존 효율 수정 전체를 새 데모 변경으로 귀속하지 않는다.",
        conventions="AGENTS.md; docs/agent-protocol.md; 사용자 지정 writing-only 소유 범위",
        implementationChoices=[choices[i] for i in config["choice_indexes"]],
        contractCoverage=rows, findings=findings, adrFullBody=adr_text,
        adrDecisionFullText=adr_decision, mappingEntry=entry,
        sourceFingerprints=source_hashes,
        suppliedLatestProof=dict(path=proof_path, sha256=proof_sha, evidenceLevel="actual-local-PostgreSQL; not AWS E2E"),
        findingHistory=dict(
            N1="necessity-review-final.md에서 독립 해소 확인. 고정 명령과 기존 실행 복원 유지.",
            N2="원본 Refactor 제안. 선택적·변경 없음. sensor.py의 private wrapper 정리이며 blocker 아님.",
            F3="원본 sufficiency-review.md T12에서 함수별 JSDoc 보완 확인. main 최종 자료와 함께 이력으로 보존.",
            pending="F1/F2/F4/U3의 최종 독립 재검토 결과 및 새 모델 회차 결과",
        ),
        browser=dict(
            status="NOT_OPENED", reason="사용자가 전달한 이전 자동 승인 검토가 local file:// 열기를 거부했고 HTTP·shell·다른 브라우저 우회도 금지했다.",
            openHelperExecuted=False, visualInspection="NOT_PERFORMED", staticSvgValidation="PENDING",
        ),
        notes="작성 전용 초안. 신규 기술 결론을 만들지 않았다. 원본 PROVEN/VIOLATED/UNVERIFIED와 새 수정 통보를 구분한다. 자동 승격·퀴즈·PR 준비 게이트 없음.",
    )
    (dest / "findings.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    text = "# ADR implementation review\n\n## At a glance\n\n<!-- generated from findings.json -->\n\n"
    text += "## Review mode\n\nfull. 두 독립 관점의 원본 판단을 보존한 작성 전용 초안이며, 최종 재검토 데이터 대기 상태다.\n\n"
    text += "## Scope\n\n전체 구현 범위는 원본 necessity/sufficiency 문서의 호출 경로를 포함한다. 변경 범위는 별도의 changeScope와 실제 diff로 기록한다. 기존 RCA 효율 변경의 재설계는 포함하지 않는다. 세 ADR은 Proposed이며 이 보고서는 상태를 변경하지 않는다.\n\n"
    text += "## Context\n\n<!-- generated review context from findings.json -->\n\n"
    for h in hills:
        text += f"## {h['title']}\n\n{h['reviewQuestion']}\n\n"
        text += "<!-- generated container zoom from findings.json -->\n\n"
        text += "도식의 화살표는 호출 순서 또는 자료 이동을 나타낸다. 검증의 경계는 아래 주의 문장과 함께 읽는다.\n\n"
        text += f"```mermaid\n%% requirement: {h['diagramIds'][0]}\n{h['diagram']}\n```\n\nNotice: {h['notice']}\n\n"
        text += "<!-- generated component zoom from findings.json -->\n\n<!-- generated hill evidence from findings.json -->\n\n"
    text += "## Findings\n\n<!-- generated review diagnostics from findings.json -->\n\n아래 항목은 원본 독립 검토의 판정이다. 수정 완료 통보는 각 항목의 결과에 별도로 적었으며, 최종 독립 재검토 전에는 원본 계약 상태를 올리지 않았다.\n\n"
    for i, f in enumerate(findings, 1):
        text += f"### F{i}. {f['summary']}\n\n{f['whyItMatters']}\n\n{f['remediationUpdate']}\n\n"
    text += "N1은 독립 재검토로 해소됐다. N2는 선택적 정리이며 변경하지 않았다. F3 함수 문서 보완은 원본 검토의 T12에 기록되어 있다.\n\n"
    text += "## ADR contract coverage\n\nContract compliance: 원본 Decision D0와 R1..Rn을 빠짐없이 한 번씩 배정했다. 각 행은 원문 값·집합·단위·순서·실패 의무를 유지하며 수정 후 판정 대기 상태를 명시한다.\n\n<!-- generated from findings.json -->\n\n"
    text += "## Notable implementation choices\n\n<!-- generated from findings.json -->\n\n"
    text += "## Tests\n\n다음은 독립 검토자가 실제 실행했거나 출처를 구분해 인용한 원본 시험 목록이다. 보고서 작성자는 제품 시험을 새로 실행하지 않았다. 원본 proof03 수치와 다음 최신 proof는 서로 다른 실행으로 보존한다.\n\n"
    text += TESTS + "\n\n"
    text += f"main이 지정한 최신 로컬 PostgreSQL 증거는 `{proof_path}`이며 SHA-256은 `{proof_sha}`다. 이 작성 단계에서는 파일 해시만 대조했고 DB 재실행 또는 새 기술 판정을 하지 않았다.\n\n"
    text += "main 최신 통보: 단위 테스트 1,733 PASS(agent 740, headless 824, healthcare 125, infra 44), 루트 테스트 136 PASS/1 FAIL(이전 승인 기준선 digest 불일치). format/lint/build/typecheck는 PASS다. F4 관련 Strands 113/Headless 201 시험도 PASS이며 운영 기본값을 유지한다. 이 수치를 과거 원본 검토의 실행 수에 더해 중복 집계하지 않는다.\n\n"
    text += "최신 로컬 이미지 3개는 `review-fixed-images.json`에서 위 fixed-proof와 소스가 일치한다고 main이 확인했다. 정상 배포의 optional healthcare imageDigest 연결은 진행 중이며 기존 imageTag 기본값을 유지할 예정이다. 최종 코드·시험 결과 전에는 이 통합 수정을 완료로 표시하지 않는다.\n\n"
    text += "최종 실모델 회차 `realistic-final-20260910T025818Z-a57123`은 실행 중이다. 입력 digest는 `8dc813d320341b5347ed5f299a5c52217a5c84a7d02863e8be79e2672e860cfc`이며 아직 최종 결과를 받지 않았다.\n\n"
    text += "## Residual risks\n\nAWS 배포 E2E, 실제 증상 알람 관계, 새 두 엔진 전수 모델 결과와 기준선 승인은 미검증으로 둔다. 로컬 실측에서 500ms 알람 충족을 추정하지 않는다. 이전 진단 모델 실행의 실패와 중단 기록을 보존한다.\n\n"
    text += "NOT_OPENED — 이전 자동 승인 검토가 local file:// 열기를 거부했고 사용자가 HTTP·shell·다른 브라우저 우회도 금지했다. open helper를 실행하지 않는다. 브라우저 시각 검사는 수행하지 않으며 최종 단계에서 정적 SVG 검사만 기록한다.\n\n"
    text += "## Repair guide\n\n"
    for f in findings:
        text += f"### {f['sourceId']}의 후속 확인\n\n"
        text += f"Files and symbols to change: {f['editTargets']}\n\n"
        text += "Scope not to touch: 모델·기각·안전·승인 정책, ADR의 의무, 기존 실패 이력 및 다른 에이전트의 코드. 작성자는 제품 코드를 바꾸지 않는다.\n\n"
        text += f"{f['requestedChange']}\n\n"
        text += f"예상 동작: {f['expectedBehavior']}\n\n"
        text += f"검증: {f['test']} — {f['testResult']}\n\n"
        text += f"Completion criteria: {f['completionCriteria']}\n\n"
        text += f"Needs confirmation: {f['remediationUpdate']} 신뢰도 {f['confidence']}는 원본 판정의 신뢰도다.\n\n"
    (dest / "implementation-review.md").write_text(text)
    (dest / "implementation-review.source.md").write_text(text)
    (dest / "draft-status.json").write_text(json.dumps(dict(
        artifactState=data["artifactState"], contractIds=ids,
        mainFinalDataReceived=False, finalValidationCompleted=False,
        browser=data["browser"],
    ), ensure_ascii=False, indent=2) + "\n")
    print(f"{target}: draft {len(rows)} contracts, {len(hills)} hills, {len(findings)} original findings")
