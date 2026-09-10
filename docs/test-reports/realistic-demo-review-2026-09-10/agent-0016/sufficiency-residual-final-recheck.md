## ADR Impl Review — F2 residual final narrow recheck

### Verdict
**PASS — 요청된 잔여 코드 재검토. F2 해소, 열린 코드 발견사항 없음.**

F1/F3/F4/U3의 기존 종료 판정도 유지한다. 전체 계약 증거의 판정은 **INCONCLUSIVE**다:26행 중 PROVEN21 / VIOLATED0 / UNVERIFIED5. 남은5행은 이미 구분한 U1 실제runtime/알람과 U2 모델품질·승인 증거 한계이며 새로운 코드 결함이나 ADR Status 승격을 위한 추가배포 요구가 아니다. 최종 모델회차가 아직 진행 중이므로 모델 품질 PASS나 baseline 승인을 주장하지 않는다.

### Scope
- 원래 infra/0007 R8의 잔여 반례만 재실행했다: 사전snapshot/update intent→성공한소유변경→journal오류+stderr오류→원상복원.
- 함께 요청된 선택적 imageDigest는 정상 deployment구성에서 CLI의 immutable baseline을 만들 수 있는 loader→bin→stack 연결만 읽고 기존 focused test로 검증했다.
- 새로운 failure hypothesis를 추가하거나 다른코드로 확대하지 않았다. 코드/ADR/mapping/모델입력 수정, AWS/API/모델 호출, DB쓰기, 이미지빌드, 브라우저 열기 없음. 이전 원본/full/narrow보고서와 재현출력을 보존하고 새파일만 작성했다.

### F2 result
`Demo.record()`는 진단 방출 실패를 다음 코드로 격리한다.

```python
try:
    print(json.dumps(failure | {"recoveryVerified": False}), file=sys.stderr)
except (OSError, ValueError) as diagnostic_error:
    failure["diagnosticErrorType"] = type(diagnostic_error).__name__
if not recovery:
    raise
```

- basis: infra/0007 R8의 “실패·중단에도 모든 복원 단계를 시도”. 새로운결정없이 원래계약 충족을 확인했다.
- evidence: `scripts/run_realistic_demo.py:337–360`. `failure`를 먼저 `journal_errors`에 넣어 진단출력 자체가 실패해도 종류를 메모리에 유지한다. 바깥 저장오류 처리의 bare raise가 strict intent의 원래오류를 보존한다. 기존 original/foreign owner·이미지·durable intent 검증은 바뀌지 않았다.
- test: 독립 기존반례를 `sufficiency-recheck-03/original-repro.py`로 재실행. 이전OSError stderr와 실제로 close한 StringIO의ValueError를 각각 사용했다. 두 경우 모두 original/current=`arn:task-definition/healthcare:1`, restore_intents=1, 원래 `synthetic journal storage failure` 유지, recoveryVerified=false, diagnosticErrorType 일치. 예전결과 :2/restore0과 구별된다.
- repository regression: pool/maintenance × OSError/ValueError 네 경우를 검사한다. 서비스원본복귀/소유task stop, foreign task미변경, owner유지, released기록없음, pending/recoveryfalse 및 diagnosticErrorType 모두 확인한다.
- testResult: PASS. 원래 journal-only 반례와 이 마지막 residual을 모두 해소했다. 새코드수정요청 없음.

### Optional image pin integration
- `config/loader.ts:ImageDigestSchema`는 `sha256:`+64개 소문자16진수를 검사한다. `healthcareImageDigest()`는 ENV의 nullish 우선순위를 사용하므로 명시한빈/잘못된값을 태그로 조용히 대체하지 않는다.
- `bin/infra.ts:64`가 `Config.healthcare.imageDigest`를 stack에 전달한다.
- `healthcare-service-stack.ts:75–87,122`는 pin있으면 `repository@sha256:…`, 없으면 기존 `repository:tag`를 구성한다. `DEPLOYED_REVISION`은기존imageTag label이며 소스증명으로 대체하지 않는다.
- 두 focused suite는 real bin→loader→stack 합성, ENV/TOML 우선순위·오류거부, image외다른resource/property불변을 검사했다. **직접관측한결과는48 PASS/2 suites**다. caller가이전에보고한49개를 reviewer실행수로복사하지않았다.
- 실제배포/500ms 알람관계는 실행하지않았으며 이합성성공으로추론하지않는다.

### Tests executed

모든시험출력은 `sufficiency-recheck-03/`에 보존했다. Python은 PYTHONDONTWRITEBYTECODE=1, TMPDIR는동일디렉터리다.

| Command | Result / log |
| --- | --- |
| `python3 tests/harness/realistic_demo_cases.py -v` |56 PASS. 첫실행후 caller의Ruff with정리가감지되어 최종파일을읽고56개를다시실행했다. `python56-final.log` |
| `node --test tests/harness/realistic-demo-script.test.mjs` |4 PASS. wrapper의Python lifecycle도통과. `node4.log` |
| `pnpm --filter infra exec jest --runInBand --cache=false test/healthcare-service-stack.test.ts test/healthcare-image-config.test.ts` |48 PASS,2 suites. `infra-pin.log` |
| Healthcarevenv로 `sufficiency-recheck-03/original-repro.py` |기존residual2경우 PASS. `original-repro-results.json` |
| `computeInputDigest()` 읽기전용호출 |`sha256:8dc813d320341b5347ed5f299a5c52217a5c84a7d02863e8be79e2672e860cfc` 유지 |

### Coverage and remaining limits
- 최신26행: `sufficiency-closed-coverage.json`. 원래D0/Rn ID/문구/행수 유지.
- 이번확정변경: infra/0007 R8 **VIOLATED→PROVEN**. image pin 연결의구현증거를 infra/0004 R6에추가했지만 실제deployment/runtime증거부재를PASS로위장하지않았다.
- UNVERIFIED5: infra/0004 R6, infra/0007 D0/R6, agent/0016 D0/R5. 모두기존U1/U2 증거범위다. **코드재검토PASS와전체외부검증완료는구별한다.**
- F1/F3/F4/U3 종료는이전검토그대로다. 제품/ADR 상태변경이나배포선행조건을새로요구하지않는다. 최종8모델회차/기준선승인은이검토밖의미완료품질단계다.
- PG fixture는reviewer에게더이상필요없다. 기존실측83aac573…와세이미지검증기록은보존된증거이고이번에는DB를다시접근하지않았다.

### Notes
열린코드finding/수정target/결정요청 없음. 사용자지시대로 speculative expansion 없이기존반례를닫았다. 이전보고서는이력으로보존하며이파일과closed-coverage가최신코드종료판정이다.
