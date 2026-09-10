"""Apply supplied final model/cleanup facts without changing closed code coverage."""

import copy
import hashlib
import json
from pathlib import Path
import re

BASE = Path(__file__).resolve().parent.parent
ROOT = BASE.parents[2]
RUN = "tests/results/model/realistic-final-20260910T025818Z-a57123"
REPORT = RUN + "/final-report.json"
CLEANUP = RUN + "/cleanup-verification.json"
LOCAL = "docs/test-reports/realistic-demo-evidence-2026-09-10/local-cleanup.json"
STOPS = [
    "tests/results/model/realistic-20260910T022808Z-fa3ab0/cleanup-verification.json",
    "tests/results/model/realistic-20260910T023723Z-6308b4/cleanup-verification.json",
]
report = json.loads((ROOT / REPORT).read_text())
cleanup = json.loads((ROOT / CLEANUP).read_text())
local = json.loads((ROOT / LOCAL).read_text())
stops = [json.loads((ROOT / file).read_text()) for file in STOPS]
assert report["actualResults"] == 8 and report["passed"] == 0 and report["failed"] == 8
assert report["inputDigestUnchanged"]
assert cleanup["allAbsent"] and all(item["allAbsent"] for item in stops)
assert local["remainingProofSchemasBeforeStop"] == 0 and local["containerRemoved"]
assert all(local["anonymousVolumesRemoved"].values())
dimensions = {
    key: sum(row["dimensions"][key] for row in report["rows"])
    for key in report["rows"][0]["dimensions"]
}
assert dimensions == dict(
    rootCauseIdentified=4, evidenceLinked=7, artifactsComplete=8,
    remediationSafe=7, competingCausesRejected=0,
)
assert sum(row["confirmed"] for row in report["rows"]) == 8
locks = [row for row in report["rows"] if row["scenario"] == "maintenance-transaction-lock"]
assert len(locks) == 2 and all(
    row["reportedFaultType"] == "slow-query" and row["acceptedFaultTypes"] == ["unsupported"]
    for row in locks
)
missing = [
    dict(scenario=row["scenario"], missingRootEvidence=row["missingRootEvidence"])
    for row in report["rows"]
    if row["engine"] == "headless-codex" and row["missingRootEvidence"]
]
assert len(missing) == 3

MODEL = (
    "최종 실모델 회차 realistic-final-20260910T025818Z-a57123은 정규화 결과 8개를 모두 남겼고 "
    "평가는 0 PASS/8 FAIL이다. 입력 digest는 "
    "8dc813d320341b5347ed5f299a5c52217a5c84a7d02863e8be79e2672e860cfc로 유지됐다. "
    "산출물 완비 8/8, 증거 연결 7/8, 제안 안전성 정적 검사 7/8, 원인 식별 4/8이며 "
    "경쟁 원인 기각은 모두 false(0/8)다. 모델의 자체 확정 8/8은 모델이 내린 표시이며 "
    "검토자의 정답 판정이 아니다. 기존 기준선은 그대로이고 승인된 새 fixture는 없다."
)
CLEANUP_TEXT = (
    "평가용 자원 정리는 완료됐다. 최종 AWS 회차의 DynamoDB 테이블 4개, S3 버킷 1개, "
    "벡터 버킷 1개와 인덱스 8개 삭제가 main에 의해 확인됐고, cleanup-verification.json의 "
    "자원 부재 검사는 allAbsent=true다. 중단한 이전 두 회차도 각각 allAbsent=true다. "
    "로컬 PostgreSQL은 잔여 proof schema 0개를 확인한 뒤 소유 컨테이너와 익명 볼륨을 제거했다. "
    "이 정리 결과는 평가용 자원의 종료 증거이며 Healthcare AWS 배포 E2E나 500ms 알람 관계의 "
    "성공을 의미하지 않는다."
)
DETAILS = (
    "두 엔진의 락 사례는 기대 유형 unsupported 대신 slow-query로 분류됐다. "
    "Headless의 세션 정리·정비 락·풀 설정 사례에는 확정 원인에 필요한 인용이 각각 누락됐다. "
    "Headless 세션 정리 사례의 step-1은 안전성 정적 검사에 걸렸지만, main 검토에서는 "
    "메모리상의 소유 집합 제거와 rollback 표현에 따른 오탐 가능성을 남겼다. "
    "실제로 위험한 작업을 실행했다는 증거로 해석하지 않는다."
)
PRECISION = (
    "Strands의 세션 정리 설명은 예외 위치를 commit 이후로 서술했다. main이 대조한 probe의 "
    "SQL 오류는 commit 이전에 발생하므로 이 설명에는 사건 순서의 정밀도 한계가 있다. "
    "이는 제공된 정성 검토 결과이며 새로운 코드 결함 판정을 추가하지 않는다."
)
AUTHORITY = (
    "사용자 main FINAL MODEL/cleanup 통보와 보존된 final-report.json, "
    "최종/중단 두 회차 cleanup-verification.json, local-cleanup.json을 적용했다. "
    "전체 INCONCLUSIVE/Proposed와 closed coverage를 유지한다. 모델0PASS/8FAIL, "
    "기준선 미변경·새 fixture 미승인, 자원 정리 완료. "
    "안전성 정적 검출의 오탐 가능성과 commit 서술 정밀도 한계는 main 제공 판단이다."
)

for target in ("infra-0004", "infra-0007", "agent-0016"):
    dest = BASE / target
    file = dest / "findings.json"
    data = json.loads(file.read_text())
    coverage_before = copy.deepcopy(data["contractCoverage"])
    diagram_before = copy.deepcopy(data["diagramRequirements"])
    data["modelQualityState"] = "COMPLETED_FAILED"
    model = dict(
        runId=report["runId"], inputDigest=report["frozenInputDigest"].removeprefix("sha256:"),
        inputDigestUnchanged=True, status="COMPLETED", finished=8, strandsRunning=0,
        finalResultReceived=True, passed=0, failed=8, source=REPORT,
        dimensionsPassed=dimensions, modelSelfConfirmed=8,
        selfConfirmationIsReviewerTruth=False,
    )
    data["modelRun"] = model
    data["mainUpdates"]["model"] = copy.deepcopy(model)
    data["mainUpdates"]["source"] = AUTHORITY
    data["finalModelContext"] = dict(
        summary=MODEL, classificationAndCitationLimits=DETAILS,
        qualitativePrecisionLimit=PRECISION,
        headlessMissingRootCitations=missing,
        unsafeStepInterpretation="Static flag only; possible false positive in memory-owned-set removal/rollback wording; no executed dangerous action claimed.",
        baselineChanged=False, approvedNewFixture=False, source=REPORT,
    )
    data["cleanupContext"] = dict(
        summary=CLEANUP_TEXT, finalAwsAllAbsent=True,
        stoppedRunAllAbsent=[True, True],
        deletedByMain=dict(dynamodbTables=4, s3Buckets=1, vectorBuckets=1, vectorIndexes=8),
        indexDeletionAuthority="사용자 main",
        finalAbsenceCheckSource=CLEANUP, stoppedAbsenceCheckSources=STOPS,
        localSource=LOCAL, remainingLocalProofSchemas=0,
        localContainerRemoved=True, localAnonymousVolumesRemoved=True,
        awsDeploymentE2e="NOT_RUN",
    )
    data["atAGlance"]["action"] = (
        "최종 모델 평가 0 PASS/8 FAIL과 새 fixture 미승인을 기록했다. 평가용 AWS·로컬 자원은 "
        "정리와 부재 확인을 마쳤으며 기존 기준선과 Proposed를 유지한다."
    )
    data["atAGlance"]["risk"] = (
        "실제 Healthcare AWS 알람·배포 복원은 미검증이다. 모델 자체 확정 8/8을 정답으로 "
        "취급하지 않으며 안전성 정적 검출은 실제 위험 작업 실행을 뜻하지 않는다. "
        "AWS를 새 상태 승격 선행조건으로 추가하지 않는다."
    )
    data["reviewDiagnostics"]["testSufficiency"]["assessment"] = (
        "코드 경계 시험은 통과했다. 최종 모델 평가 8개는 모두 실패했고 새 기준선은 승인하지 "
        "않았다. AWS 배포 E2E는 미실행이며 평가용 자원 정리는 확인됐다."
    )
    data["reviewDiagnostics"]["testSufficiency"]["evidence"] = (
        "main-verification.json; " + REPORT + "; " + CLEANUP + "; " + LOCAL
    )
    if target == "infra-0007":
        data["reviewHike"]["hills"][2]["assessment"] = (
            "관측 경계의 코드 검사는 통과했다. 최종 모델 8개 평가는 모두 실패했고 입력 digest를 "
            "유지했다. 평가용 자원 정리는 완료됐으며 새 fixture는 승인하지 않았다."
        )
    if target == "agent-0016":
        data["reviewHike"]["hills"][1]["assessment"] = (
            "F4 관련 시험과 독립 prompt 재현은 통과했다. 최종 실모델 결과 8개는 모두 필수 평가에 "
            "실패했고 경쟁 원인 기각은 0/8이다. 기준선 변경이나 새 fixture 승인은 없다."
        )
    for finding in data["findings"]:
        if finding["sourceId"] == "U1":
            finding["observedBehavior"] = (
                "Healthcare AWS 배포 E2E는 수행하지 않았다. 별도 평가용 AWS 자원과 로컬 "
                "PostgreSQL 자원은 정리 후 부재 확인을 마쳤다."
            )
            finding["evidence"] += "; 정리 결과: " + CLEANUP + "; " + LOCAL
        if finding["sourceId"] == "U2":
            finding["summary"] = "최종 모델 평가 8개가 모두 실패했고 새 기준선은 미승인이다."
            finding["observedBehavior"] = (
                "정규화 결과 8개가 완료됐고 필수 평가는0 PASS/8 FAIL이다. 입력 digest는 "
                "유지됐으며 기존 기준선과 승인되지 않은 새 fixture 상태를 보존한다."
            )
            finding["requestedChange"] = (
                "최종 실패 결과를 그대로 보존하고 모델 자체 확정, 정적 안전성 검출과 실제 "
                "실행 사실을 구분해 읽는다. 이 문서에서 코드 종료 판정이나 승인 정책을 바꾸지 않는다."
            )
            finding["evidence"] = REPORT + "; 동일 입력 digest 유지; 사용자 main 최종 품질 검토"
            finding["testResult"] = (
                "COMPLETED — normalized8, 0 PASS/8 FAIL; competingCausesRejected0/8. "
                "Root136 PASS/1 old-baseline digest FAIL; 기준선 변경·새 fixture 승인 없음."
            )
    data["notes"] = (
        "작성 전용 최종 모델·정리 문맥 갱신. 열린 코드 발견사항 없음. INCONCLUSIVE/Proposed와 "
        "closed coverage21PROVEN/5UNVERIFIED 유지. coverage의 실행 설명은 Curie 코드 종료 "
        "시점 스냅샷이며 이후 최종 모델 결과는 별도 문맥에서 갱신했다. 부모 index/model summary는 main 담당."
    )
    data["finalModelCleanupAuthority"] = AUTHORITY
    data["finalModelCleanupSourceHashes"] = {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
        for p in [REPORT, CLEANUP, LOCAL, *STOPS]
    }
    assert data["contractCoverage"] == coverage_before
    assert data["diagramRequirements"] == diagram_before
    assert data["verdict"] == "INCONCLUSIVE" and data["status"] == "Proposed"
    file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    md = (dest / "implementation-review.source.md").read_text()
    findings_text = "## Findings\n\n<!-- generated review diagnostics from findings.json -->\n\n"
    findings_text += (
        "열린 코드 발견사항은 없다. HTTP 로그 F1, 복원 F2, 함수 문서 F3, 메타데이터 F4와 "
        "최소 관측시간 U3는 독립 재검토로 해소됐다. N1도 종료됐고 N2는 선택적 정리로 변경하지 않았다.\n\n"
    )
    for i, finding in enumerate(data["findings"], 1):
        findings_text += f"### F{i}. {finding['sourceId']} — {finding['summary']}\n\n"
        findings_text += finding["whyItMatters"] + "\n\n" + finding["observedBehavior"] + "\n\n" + finding["requestedChange"] + "\n\n"
    md = re.sub(r"## Findings\n.*?(?=## ADR contract coverage)", lambda _: findings_text, md, flags=re.S)
    md = re.sub(
        r"모델 회차 `realistic-final-20260910T025818Z-a57123`은 6개 완료.*?(?=\n\n)",
        lambda _: MODEL, md, count=1, flags=re.S,
    )
    md = md.replace(
        "실행 명령과 출처는 다음과 같다.",
        (DETAILS + "\n\n" + PRECISION + "\n\n" if target == "agent-0016" else "")
        + CLEANUP_TEXT + "\n\n"
        + "모델 결과 출처: `" + REPORT + "`. 정리 출처: `" + CLEANUP + "`, `" + LOCAL + "`.\n\n"
        + "실행 명령과 출처는 다음과 같다.",
    )
    md = md.replace(
        "AWS 배포 E2E·500ms 알람 관계는 실행하지 않았다. 이전 모델 진단 실패와 중단 기록을 보존한다. 마지막 모델 결과 및 정리 기록은 main 통보 뒤 갱신한다.",
        "AWS 배포 E2E·500ms 알람 관계는 실행하지 않았다. 최종 모델 평가는 0 PASS/8 FAIL이며 이전 "
        "진단 실패와 중단 기록을 보존한다. 최종 회차와 중단 두 회차, 로컬 PostgreSQL 자원의 정리를 "
        "확인했다. 코드 종료 시점의 closed coverage는 유지하고 이후 모델 결과를 이 절에 기록했다.",
    )
    (dest / "implementation-review.source.md").write_text(md)
    (dest / "implementation-review.md").write_text(md)
    with (dest / "main-final-data.txt").open("a") as stream:
        stream.write("\n" + AUTHORITY + "\n")
    print(f"{target}: model0/8 PASS; cleanup verified; coverage and diagrams unchanged")
