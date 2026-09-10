"""Apply the explicitly supplied main/Curie synthesis; model results stay pending."""

from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re

BASE = Path(__file__).resolve().parent.parent
ROOT = BASE.parents[2]
SOURCE = Path("/private/tmp/rca-realistic-review")
CLOSURE = "sufficiency-residual-final-recheck.md"
ROWS = "sufficiency-closed-coverage.json"
closed_rows = json.loads((SOURCE / ROWS).read_text())
assert Counter(row["status"] for row in closed_rows) == {"PROVEN": 21, "UNVERIFIED": 5}
main_verification = json.loads((ROOT / "docs/test-reports/realistic-demo-evidence-2026-09-10/verification.json").read_text())
assert main_verification["unit"]["passed"] == 1771

MAIN_AUTHORITY = (
    "사용자 main FINAL CODE CLOSURE 메시지: 열린 코드 발견사항 없음, narrow PASS; "
    "full evidence INCONCLUSIVE; 21 PROVEN/0 VIOLATED/5 UNVERIFIED. "
    "세 ADR Proposed 유지, AWS를 새 상태 선행조건으로 만들지 않음. "
    "1771 unit PASS; controller56+Node4; infra82; 모델6완료/2실행중."
)


def code(file, start, end):
    text = (ROOT / file).read_text()
    first = text.index(start)
    last = text.index(end, first)
    return dict(kind="excerpt", location=f"{file}:{text.count(chr(10), 0, first) + 1}", content=text[first:last].rstrip())


def component(name, implementation, evidence, tests, explanation):
    return dict(
        name=name, responsibility=implementation, implementation=implementation,
        verification=tests, codeEvidence=[dict(evidence, explanation=explanation, tests=tests)],
    )


record_component = component(
    "기록·진단 출력 실패와 복원 제어의 분리",
    "복원 중 저장 오류와 stderr 출력 오류를 메모리에 남기고 정리를 계속한다. 신규 변경의 사전 기록 실패는 원래 저장 오류를 다시 던진다.",
    code("scripts/run_realistic_demo.py", "    def record(", "\n    def evidence_result("),
    "Curie recheck-03: Python56 PASS/Node4 PASS, 원래 반례의 OSError와 실제 닫힌 stream ValueError 모두 원본:1 복원 PASS.",
    "F2의 마지막 잔여 반례를 닫은 실제 코드다. 기록 오류를 먼저 보존하고 진단 출력 실패를 따로 격리한다.",
)
pin_component = component(
    "배포 이미지의 선택적 digest 고정",
    "imageDigest가 있으면 repository@sha256를 사용하고 없으면 기존 repository:tag를 유지한다. imageTag는 기존 리비전 라벨로 남는다.",
    code("packages/infra/lib/stacks/healthcare-service-stack.ts", "    if (\n      props.imageDigest", "\n    const taskRole"),
    "Curie의 loader→bin→stack focused 48 PASS/2 suites; main 전체 infra 82 PASS. 실제 AWS 배포는 미실행.",
    "정상 배포에서도 CLI가 요구하는 불변 이미지 원본을 선택할 수 있게 하는 경계다. 소스 manifest 검증과 이미지 식별은 별도로 유지한다.",
)

HILL_UPDATES = {
    "infra-0004": [
        dict(
            claim="요청 완료와 분리한 부하 계획, 실제 결함 리비전과 안전한 HTTP 관측 경계가 같은 조건의 로컬 비교를 뒷받침한다.",
            counterexample="수정 전 F1에서는 driver DETAIL의 canary가 로그에 남았다. 수정 후 같은 반례는 일반 500 응답과 canary 비노출로 바뀌었다.",
            assessment="현재 컴파일된 로컬 proof와 이미지 3개는 verified=true이며 소스가 일치한다. verified=false는 UNBUILT 체크아웃 manifest에만 해당한다. 실제 AWS 배포 관측은 U1로 남는다.",
        ),
        dict(assessment="소유 연결 정리는 독립 오류 시험과 보존된 실제 PostgreSQL 증거로 확인했다. 코드 종료 판정은 PASS이며 AWS 실행 증거와 구분한다."),
    ],
    "infra-0007": [
        dict(
            counterexample="주입 전 대기는 최소 관측 시간에 포함하지 않는다. 수정된 경계는 주입 후 검사 실행 149초의 성공 종료를 거부하고 150초를 허용한다.",
            assessment="네 실제 메커니즘과 최소 관측시간의 코드 경계는 확인했다. AWS 정상·결함·복원 알람 관계와 초기 500ms는 U1로 미검증이다.",
        ),
        dict(
            claim="변경 전 원본과 소유 실행 정보를 보존하고, 복원 중의 기록·진단 오류를 정리 제어와 분리한다.",
            workedExample="같은 저장·stderr 동시 오류에서 수정 전에는 결함 정의 :2와 복원 0회가 남았다. 수정 후 OSError와 닫힌 stream ValueError 모두 원래 :1로 돌아가고 복원 1회를 기록했다.",
            counterexample="진단 출력이 실패했다는 이유로 원래 저장 오류를 잃거나 recoveryVerified=true로 표시하면 안 된다. 회귀 시험은 원래 오류·미검증 복원 결과·소유권 유지를 확인했다.",
            assessment="Curie의 마지막 F2 재검토가 PASS다. Python56·Node4와 원래 반례가 통과했고 열린 코드 수정 항목은 없다.",
            interactions="서비스와 자기 정비 작업의 정리를 각각 시도한다. journalErrors에 출력 실패 종류도 남기고, 불완전한 증거는 복원 성공으로 표시하지 않는다.",
        ),
        dict(
            counterexample="수정 전 F1의 HTTP 오류와 F4의 알람 기본값은 관측 경계를 벗어났다. 현재 재검토는 두 경계의 수정을 확인했다.",
            assessment="원본 시각·단위·중립 식별자·코드 차이와 비노출 경계를 확인했다. 최종 모델 회차는 6개 완료·2개 실행 중이며 품질과 승인은 확정하지 않았다.",
        ),
    ],
    "agent-0016": [
        dict(assessment="현재 canonical 관측은 SHA-256 83aac573…의 컴파일된 로컬 proof를 가리킨다. 실제 proof와 이미지 3개는 verified=true이며 소스가 일치한다. 복원·정답의 입력 혼입 거부 시험도 통과했다."),
        dict(
            claim="평가 어댑터는 제공된 메타데이터의 값과 부재를 공용 최종 프롬프트까지 유지하며 운영 기본값은 보존한다.",
            workedExample="region이 없는 현재 네 입력에는 not provided가 표시된다. region만 ap-northeast-2로 주고 ARN을 생략한 입력은 그 리전을 유지한다.",
            counterexample="누락·null·공백은 not provided로 표현하지만 제공된 0이나 빈 차원 집합은 지우지 않는다. 새 세션 시각을 사고의 원본 시각으로 제시하지 않는다.",
            assessment="F4의 Strands113/Headless201 시험과 독립 최종 prompt 재현이 통과했다. 두 엔진의 최종 모델 결과·기준선 승인은 아직 확정하지 않았다.",
        ),
        dict(
            workedExample="F2 저장·진단 동시 오류의 원래 반례가 원본 서비스 복귀와 소유 작업 정리로 끝난다. Python56·Node4는 성공 표시와 소유권의 경계도 확인한다.",
            assessment="코드 복원 경로는 검증됐지만 실제 AWS 태스크 종료·서비스 증상 회복은 수행하지 않았다. R5의 미검증 상태는 이 실행 증거 한계를 표시한다.",
        ),
    ],
}

for target in ("infra-0004", "infra-0007", "agent-0016"):
    dest = BASE / target
    data = json.loads((dest / "findings.json").read_text())
    old_findings = copy.deepcopy(data.get("resolvedFindings", []) + data["findings"])
    old_rows = copy.deepcopy(data["contractCoverage"])
    source_md = (dest / "implementation-review.source.md").read_text()
    diagrams = re.findall(r"```mermaid\n(.*?)\n```\n\nNotice: ([^\n]+)", source_md, re.S)
    assert len(diagrams) == len(data["reviewHike"]["hills"])
    for name in (CLOSURE, ROWS, "sufficiency-final-review.md"):
        (dest / name).write_bytes((SOURCE / name).read_bytes())
    (dest / "main-verification.json").write_text(json.dumps(main_verification, ensure_ascii=False, indent=2) + "\n")
    (dest / "main-final-data.txt").write_text(MAIN_AUTHORITY + "\n")
    data.update(
        verdict="INCONCLUSIVE", artifactState="FINAL_SYNTHESIS_RECEIVED",
        codeReviewVerdict="PASS", modelQualityState="PENDING",
        mainFinalDataSource=str(dest / "main-final-data.txt"),
        codeClosureSource=CLOSURE, coverageSource=ROWS,
    )
    update = data["mainUpdates"]
    update["verification"].update(unitPassed=1771, packages=dict(agent=740, headless=824, healthcare=125, infra=82))
    update["model"].update(finished=6, strandsRunning=2, finalResultReceived=False)
    update["infraIntegration"].update(status="IMPLEMENTED", finalTestsReceived=True, infraTestsPassed=82)
    update["f2Residual"] = dict(status="CLOSED_BY_INDEPENDENT_RECHECK", pythonPassed=56, nodePassed=4, ruff="PASS", originalStorageErrorPreserved=True)
    update["curieFinalRecheck"] = "PASS — all code findings closed"
    update["source"] = MAIN_AUTHORITY
    data["resolvedFindings"] = [
        dict(item, closure="해소 — Curie 최종 코드 재검토 및 main 합성", closureSource=CLOSURE)
        for item in old_findings if item["sourceId"] in ("F1", "F2", "F4", "U3")
    ]
    data["findingHistory"] = dict(
        N1="고정 정비 명령으로 제한하고 기존 실행 복원 유지. necessity-review-final.md 독립 PASS.",
        N2="선택적 private ingest wrapper 정리 제안이며 변경하지 않았다. 코드 차단 항목이 아니다.",
        F1="HTTP 전체 응답 수명의 canary 비노출과 오류·취소·세션 반환 경계 검증으로 해소.",
        F2="journal+stderr OSError/ValueError에서도 원본 복원·소유 작업 정리·원래 오류 보존. 마지막 독립 재검토 PASS.",
        F3="투영 helper의 목적·시간·필드·출처 JSDoc과 projection11 검증으로 해소.",
        F4="eval의 missing metadata는 not provided; 제공 값과 0/{} 보존, production defaults 유지. 113/201 및 독립 prompt 재현 PASS.",
        U3="주입 후 monotonic 경과 149초 거부/150초 허용과 cleanup으로 코드 의무 해소. AWS 실행 주장은 없음.",
    )
    current_rows = []
    for source in closed_rows:
        if source["adr"] != data["adr"]:
            continue
        row = {k: v for k, v in source.items() if k not in ("adr", "hill")}
        row["adrBasis"] = " ".join(line.strip() for line in row["adrBasis"].splitlines()).strip()
        before = next(item for item in old_rows if item["contractId"] == row["contractId"])
        row["requirement"] = before["requirement"]
        row["reviewSnapshot"] = "Curie closed coverage + explicit main synthesis"
        if row["contractId"] == "D0":
            row["decisionObligationsFullText"] = data["adrDecisionFullText"]
        if target == "infra-0004" and row["contractId"] == "R3":
            row["implementation"] = row["implementation"].replace(
                "로컬 원본은 verified=false.",
                "verified=false는 UNBUILT 체크아웃 manifest에만 해당한다. 현재 컴파일된 로컬 proof와 이미지 3개는 verified=true이며 소스가 일치한다.",
            )
            row["evidenceClarificationSource"] = "사용자 main의 verified final data 정정 메시지"
        current_rows.append(row)
    data["contractCoverage"] = current_rows
    for h, changes in zip(data["reviewHike"]["hills"], HILL_UPDATES[target]):
        for key, value in changes.items():
            if key in ("interactions", "outcome", "responsibility"):
                h["container"][key] = value
            else:
                h[key] = value
        for c in h["components"]:
            if c["name"] == "원본 알람 메타데이터의 전달":
                c["implementation"] = "제공된 Region/AlarmArn/Trigger와 명시적 eval source view를 전달한다. 없는 region/statistic/period/time은 최종 프롬프트에서 not provided로 표시하고 운영 기본값을 유지한다."
                c["verification"] = "Strands113/Headless201 PASS 및 Curie의 실제 최종 prompt 재현 PASS. 현재 네 입력·minimal·region-only·null/blank·0/{} 경계를 확인했다."
            elif c["name"] == "HTTP 오류의 안전한 기록":
                c["verification"] = "Curie Healthcare125 PASS 및 원래 canary 반례 PASS. 전체 응답 수명과 오류·취소·세션 반환·다음 정상 저장을 확인했다."
            elif c["name"] == "같은 입력과 세션 수명":
                c["verification"] = "현재 compiled proof83aac573…의 실제 SQL 1/121/1과 같은 응답 hash; main PostgreSQL2 PASS, Healthcare125 PASS. 독립 검토는 보존 증거를 재검사했다."
            for item in c["codeEvidence"]:
                item["tests"] = c["verification"]
        if target == "infra-0004" and h["id"] == "H1":
            h["components"].append(copy.deepcopy(pin_component))
        if (target == "infra-0007" and h["id"] == "H2") or (target == "agent-0016" and h["id"] == "H3"):
            h["components"].append(copy.deepcopy(record_component))
        for index, c in enumerate(h["components"], 1):
            c["id"] = f"C{index}"
    data["reviewHike"]["context"]["scopeAndRisk"] = (
        "전체 ADR 본문과 원래 호출 경로를 검토했다. 독립 코드 재검토는 PASS이며 열린 코드 발견사항은 없다. "
        "남은 미검증 행은 실제 AWS 실행과 모델 품질·승인의 증거 한계이므로 전체 판정은 INCONCLUSIVE다."
    )
    data["atAGlance"] = dict(
        impact="독립 코드 재검토는 PASS이며 F1/F2/F3/F4와 관측시간 U3를 해소했다. 현재 로컬 proof·이미지의 출처가 일치한다.",
        action="진행 중인 최종 모델 회차의 실제 결과와 정리 기록을 반영한다. 새 기준선은 승인하지 않았고 세 ADR은 Proposed로 유지한다.",
        risk="실제 AWS 알람·배포 복원은 미검증이다. 모델은 6개 완료·2개 실행 중으로 품질 수치를 확정하지 않았다. AWS 배포를 새 상태 승격 선행조건으로 추가하지 않는다.",
    )
    data["reviewDiagnostics"] = dict(
        contractCompleteness=dict(status="CLEAR", assessment="26개 원본 계약을 모두 기록했고 코드 위반 0개다. 21 PROVEN/5 UNVERIFIED는 세 보고서 합계다.", evidence=CLOSURE + "; " + ROWS),
        testSufficiency=dict(status="UNVERIFIED", assessment="코드 경계 시험은 통과했으며 실제 AWS 실행·모델 품질·승인 증거는 미완료다.", evidence="main-verification.json; main 최종자료 메시지"),
        necessity=dict(status="CLEAR", assessment="N1 독립 해소, N2 선택적·변경 없음. 열린 필수 코드 수정 없음.", evidence="necessity-review-final.md; " + CLOSURE),
    )
    runtime_ids = [r["contractId"] for r in current_rows if r["status"] == "UNVERIFIED" and not (target == "agent-0016" and r["contractId"] == "D0")]
    runtime = dict(
        sourceId="U1", category="Unverified risk", perspective="sufficiency", confidence="high",
        summary="실제 AWS 알람과 배포 복원의 실행 증거는 미검증이다.",
        whyItMatters="로컬 PostgreSQL과 이미지 검증은 AWS 알람 전이·전달·서비스 회복을 직접 관측한 결과가 아니다.",
        expectedBehavior="실제 AWS 결과를 주장할 때 해당 실행의 이미지·부하·알람·복원 관측이 연결되어야 한다.",
        observedBehavior="코드 및 로컬 검증은 완료했지만 AWS 배포 E2E를 수행하지 않았다.",
        requestedChange="현재 실행 증거의 한계로 기록한다. 새 코드 수정이나 상태 승격 선행조건을 추가하지 않는다.",
        code="packages/infra/lib/stacks/healthcare-service-stack.ts; scripts/run_realistic_demo.py:status/restore",
        evidence="compiled proof SHA83aac573…와 review-fixed-images.json은 로컬 자료; AWS not-run — main-verification.json",
        test="AWS normal→fault→restore 알람·복원 E2E",
        testResult="NOT RUN — 이번 실행에서 AWS 배포/E2E를 수행하지 않았다. 초기 500ms 충족 주장 없음.",
        contractIds=runtime_ids, adrQuote="원본 계약의 실제 배포·증상·회복 관측 의무",
    )
    data["findings"] = [runtime]
    if target != "infra-0004":
        data["findings"].append(dict(
            sourceId="U2", category="Unverified risk", perspective="sufficiency", confidence="high",
            summary="최종 모델 품질과 새 기준선 승인은 아직 확정되지 않았다.",
            whyItMatters="오프라인 코드 시험 통과는 새 관측에 대한 두 모델의 원인·반증·안전성 품질을 증명하지 않는다.",
            expectedBehavior="같은 digest의 모든 엔진·시나리오 실제 결과와 읽을 수 있는 보고서를 검토한 뒤 승인한다.",
            observedBehavior="realistic-final-20260910T025818Z-a57123은 6개 완료·2개 Strands 실행 중이다. 이전 기준선 digest 검사는 실패를 유지한다.",
            requestedChange="진행 중인 두 결과가 완료되면 main의 최종 품질·정리 자료를 반영한다. 기존 실패나 승인 기준을 완화하지 않는다.",
            code="tests/harness/model-cli.mjs; tests/harness/evaluator.mjs:createBaseline; tests/harness/approve-cli.mjs",
            evidence="main 통보; input digest8dc813d320341b5347ed5f299a5c52217a5c84a7d02863e8be79e2672e860cfc",
            test="최종 두 엔진×4개 사례의 실모델 회차; 루트 승인 digest 검사",
            testResult="PENDING — 6 finished, 2 running; 품질 수치 미확정. Root136 PASS/1 old-baseline digest FAIL; 새 승인 없음.",
            contractIds=["R10"] if target == "infra-0007" else ["D0", "R7"],
            adrQuote="새 입력을 사용한 두 엔진 전수 결과가 통과하고 검토된 뒤에만 새 기준선 승인",
        ))
    # Preserve provenance of closed findings without presenting them as active repairs.
    data["notes"] = "작성 전용 최종 코드 종료 합성. 열린 코드 발견사항 없음. 모델 결과의 마지막 갱신 대기. Proposed 유지, 퀴즈/PR 준비 게이트 없음. 원본 검토 사본은 역사 자료이며 현재 판정은 closed coverage와 main 합성이 권위다."
    data["modelRun"] = update["model"]
    if target in ("infra-0004", "infra-0007"):
        data["scope"] = sorted(set(data["scope"]) | {"packages/infra/test/healthcare-image-config.test.ts"})
        data["changeScope"] = sorted(set(data["changeScope"]) | {"packages/infra/test/healthcare-image-config.test.ts"})
    for h in data["reviewHike"]["hills"]:
        for c in h["components"]:
            for item in c["codeEvidence"]:
                p = item["location"].split(":")[0]
                if (ROOT / p).is_file():
                    data["sourceFingerprints"][p] = hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
    # Implementation choices remain read-only and use the latest measured run.
    for choice in data["implementationChoices"]:
        for key in choice:
            choice[key] = choice[key].replace("F1이 전체 경계의 예외다.", "F1의 HTTP 예외 경계도 후속 재검토로 해소됐다.")
            choice[key] = choice[key].replace("F2는 기록 저장소 장애 문제다.", "후속 F2 수정은 저장·진단 오류를 격리하고 소유 복원을 유지한다.")
            choice[key] = choice[key].replace("33.27841707505286ms", "29.73166643641889ms")
    if target == "agent-0016":
        data["implementationChoices"].append(dict(
            choice="eval source view의 None은 운영 경로, dict는 제공값 전용 평가 경로",
            evidence="양 DTO/eval_adapter와 공용 scoping/prompt_builder",
            intentFit="제공되지 않은 사고 metadata를 런타임 기본값으로 채우지 않는다.",
            whyItMatters="누락값은 not provided지만 실제 0/{}는 보존하고 운영 프롬프트 호환성을 유지한다.",
        ))
    data["browser"]["staticSvgValidation"] = "REVALIDATION_PENDING"
    (dest / "findings.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    md = "# ADR implementation review\n\n## At a glance\n\n<!-- generated from findings.json -->\n\n"
    md += "## Review mode\n\nfull. 독립 필요성·충족성 검토와 잔여 반례 재검토를 합성했다. 코드 재검토 PASS와 전체 증거 INCONCLUSIVE를 구분한다.\n\n"
    md += "## Scope\n\n전체 ADR 본문과 원래 호출 경로는 scope에, 실제 관련 변경은 changeScope에 기록했다. 이전 RCA 효율 수정의 재설계는 포함하지 않는다. 원본 검토 사본은 이력이며 현재 판정은 `sufficiency-closed-coverage.json`과 main 합성이다. 세 ADR은 Proposed로 유지한다.\n\n"
    md += "## Context\n\n<!-- generated review context from findings.json -->\n\n"
    for h, (diagram, notice) in zip(data["reviewHike"]["hills"], diagrams):
        diagram = diagram.replace("기록 실패의 격리 필요 F2", "기록과 진단 오류 격리").replace("부재 유지 F4 재검토", "부재를 그대로 전달")
        if h["id"] == "H2" and target == "infra-0007":
            notice = "기록·진단 오류가 정리 제어를 막지 않는 것은 독립 재검토로 확인했다. AWS의 실제 증상 회복은 별도 미검증이다."
        if h["id"] == "H2" and target == "agent-0016":
            notice = "없는 사고 metadata를 기본값으로 채우지 않는 경계는 검증됐다. 새 기준선 화살표는 승인 조건이며 현재 승인 사실이 아니다."
        md += f"## {h['title']}\n\n{h['reviewQuestion']}\n\n<!-- generated container zoom from findings.json -->\n\n"
        md += "화살표는 호출 순서 또는 자료 이동을 나타낸다. 실제 실행 증거의 범위는 도식 아래 문장과 함께 읽는다.\n\n"
        md += f"```mermaid\n{diagram}\n```\n\nNotice: {notice}\n\n"
        md += "<!-- generated component zoom from findings.json -->\n\n<!-- generated hill evidence from findings.json -->\n\n"
    md += "## Findings\n\n<!-- generated review diagnostics from findings.json -->\n\n열린 코드 발견사항은 없다. HTTP 로그 F1, 복원 F2, 함수 문서 F3, 메타데이터 F4와 최소 관측시간 U3는 독립 재검토로 해소됐다. N1도 종료됐고 N2는 선택적 정리로 변경하지 않았다.\n\n"
    for i, finding in enumerate(data["findings"], 1):
        md += f"### F{i}. {finding['sourceId']} — {finding['summary']}\n\n{finding['whyItMatters']}\n\n{finding['observedBehavior']}\n\n{finding['requestedChange']}\n\n"
    md += "## ADR contract coverage\n\n<!-- generated from findings.json -->\n\n"
    md += "## Notable implementation choices\n\n<!-- generated from findings.json -->\n\n"
    md += "## Tests\n\nmain 최신 집계는 단위 1,771 PASS(agent740/headless824/healthcare125/infra82), 루트136 PASS/1 FAIL이다. 루트 실패는 이전 승인 기준선 digest와 새 입력의 차이이며 기준선을 그대로 보존했다. format/lint/build/typecheck PASS, 기존 skip2/xfail1을 구분한다.\n\n"
    md += "F4는 Strands113/Headless201 및 독립 최종 prompt 재현이 통과했다. 마지막 F2는 Python56/Node4와 journal·stderr 동시 오류의 원래 두 반례가 통과했다. 이미지 pin은 독립 focused48/2 suites와 main 전체 infra82를 구분한다. 같은 시험의 재실행 수를 합산하지 않는다.\n\n"
    md += "실제 PostgreSQL 통합2 PASS와 fixed-proof `83aac573c02bd6eb9596ced5e0797460018704b62b955905b68b15707d1b4f2b`, 컴파일 소스와 이미지3개 일치를 보존한다. 이 실제 compiled proof와 이미지의 manifest는 verified=true다. UNBUILT 체크아웃 manifest의 verified=false와 구분한다.\n\n"
    md += "모델 회차 `realistic-final-20260910T025818Z-a57123`은 6개 완료·2개 Strands 실행 중이다. digest `8dc813d320341b5347ed5f299a5c52217a5c84a7d02863e8be79e2672e860cfc`를 유지하며 품질 수치는 확정하지 않는다.\n\n"
    md += "실행 명령과 출처는 다음과 같다.\n\n"
    md += "| 실행 | 관측 결과·출처 |\n| --- | --- |\n"
    md += "| `python3 tests/harness/realistic_demo_cases.py -v` | 최종56 PASS; Curie recheck-03/python56-final.log |\n"
    md += "| `node --test tests/harness/realistic-demo-script.test.mjs` | 4 PASS; recheck-03/node4.log |\n"
    md += "| `pnpm --filter infra exec jest --runInBand --cache=false test/healthcare-service-stack.test.ts test/healthcare-image-config.test.ts` | 독립48 PASS/2 suites; recheck-03/infra-pin.log |\n"
    md += "| Healthcare venv의 `sufficiency-recheck-03/original-repro.py` | OSError와 닫힌 stream ValueError 모두 원본:1 복원 PASS |\n"
    md += "| 양 엔진 `tests/test_eval_alarm_metadata.py` 및 관련 caller/prompt suite | Strands113/Headless201 PASS; sufficiency-final-review.md의 정확한 N2 명령 |\n"
    md += "| 루트·패키지 최종 검증 | main-verification.json과 realistic-demo-evidence-2026-09-10 로그; 단위1771, 루트136/1 |\n\n"
    md += "## Residual risks\n\n코드 종료 판정은 PASS다. 전체 증거는 21 PROVEN/0 VIOLATED/5 UNVERIFIED로 INCONCLUSIVE이며, 미검증은 실제 runtime·알람과 모델 품질·승인 범위를 표시한다. AWS 배포를 ADR 상태 승격의 새로운 선행조건으로 추가하지 않는다. 현재 Proposed를 유지하며 승인 기준선은 갱신하지 않았다.\n\n"
    md += "AWS 배포 E2E·500ms 알람 관계는 실행하지 않았다. 이전 모델 진단 실패와 중단 기록을 보존한다. 마지막 모델 결과 및 정리 기록은 main 통보 뒤 갱신한다.\n\n"
    md += "NOT_OPENED — 이전 자동 승인 검토의 local file:// 차단과 사용자의 HTTP·shell·다른 브라우저 우회 금지에 따른다. open helper·브라우저 시각 검사는 수행하지 않았다. 정적 SVG XML·관계 도형·내부 링크만 검사한다.\n"
    (dest / "implementation-review.md").write_text(md)
    (dest / "implementation-review.source.md").write_text(md)
    print(target, Counter(row["status"] for row in current_rows), "INCONCLUSIVE; code PASS; model pending")
