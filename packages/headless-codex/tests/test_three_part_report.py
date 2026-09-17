"""The final report explains stored facts without serializing role payloads into the main text."""

from copy import deepcopy

from headless_codex.services.three_part_analysis import render_parts_report


def outcomes():
    values = {
        "recovery": {
            "title": "정상 버전 복원",
            "summary": "호환성을 확인한 롤백 제안",
            "reason": "실제 정상 쓰기 근거",
            "recommendation": "ROLLBACK",
            "evidence_refs": ["s3://proof"],
            "limitations": ["아직 미실행"],
            "playbook": {
                "execution_steps": [
                    {
                        "step_id": "rollback",
                        "action": "정상 정의로 변경",
                        "intent": "쓰기 복원",
                        "commands": ["aws ecs update-service --service service"],
                        "success_criteria": "첫 두 구간 실패 0",
                        "deployment_wait": {"max_wait_seconds": 900},
                    }
                ],
                "verification_steps": ["알람 OK 및 완료 쓰기 확인"],
            },
        },
        "root_cause": {
            "report_markdown": "# 전체 원인\n\n5 Whys detail\n\n```python\n# preserve code\n```\n",
            "code_proposal": {
                "status": "PROPOSED",
                "title": "입력 검증 수정",
                "repository": "team/repo",
                "base_revision": "commit",
                "tests_status": "NOT_RUN",
                "test_plan": ["입력 호환 테스트"],
                "limitations": ["테스트 미실행"],
                "files": [
                    {
                        "path": "app.py",
                        "start_line": 2,
                        "end_line": 3,
                        "unified_diff": "--- a/app.py\n+++ b/app.py\n-old\n+new\n",
                        "evidence_refs": ["github://source"],
                    }
                ],
            },
        },
        "operations": {
            "title": "운영 예방",
            "summary": "관측과 제안 구분",
            "limitations": ["CI 실행 미확인"],
            "findings": [{"statement": "검증 설정 미확인", "status": "UNVERIFIED", "evidence_refs": []}],
            "recommendations": [
                {
                    "title": "호환성 CI 추가",
                    "description": "배포 전 계약 확인",
                    "stage": "CI",
                    "priority": "high",
                    "owner": "운영팀",
                    "check": "입력 계약",
                    "failure_condition": "계약 불일치",
                    "verification_plan": "불일치 입력 검사",
                    "validation_status": "NOT_RUN",
                    "evidence_refs": ["ci://source"],
                }
            ],
        },
    }
    return {
        name: {
            "record": {
                "revision": name,
                "payload_s3_key": f"{name}.json",
                "payload_sha256": name,
                "approval_status": "READY",
            },
            "payload": {"status": "COMPLETED", "result": result},
        }
        for name, result in values.items()
    }


def test_three_parts_readable_and_lossless_root_with_collapsed_manifest():
    values = outcomes()
    original = deepcopy(values)
    report = render_parts_report(values)
    main, details = report.split("<details>")
    assert [line for line in main.splitlines() if line.startswith("## ")] == [
        "## 정상화",
        "## 근본원인·코드 수정",
        "## 운영 개선",
    ]
    for required in [
        "실제 정상 쓰기 근거",
        "성공 기준: 첫 두 구간 실패 0",
        "알람 OK 및 완료 쓰기 확인",
        "aws ecs update-service --service service",
        "최대 900초",
        "app.py · 2–3행",
        "```diff",
        "입력 호환 테스트",
        "[UNVERIFIED] 검증 설정 미확인",
        "실패 조건: 계약 불일치",
        "검증 계획: 불일치 입력 검사",
        "검증 상태: NOT_RUN",
        "테스트 상태: NOT_RUN",
        "5 Whys detail",
        "```python\n# preserve code\n```",
        "github://source",
        "ci://source",
    ]:
        assert required in main
    assert "```json" not in main
    assert '"recommendations"' not in main
    assert "Analysis parts manifest" in details and "recovery.json" in details
    assert values == original


def test_failed_parts_and_unavailable_preview_do_not_imply_execution_or_tests():
    values = outcomes()
    values["recovery"]["payload"] = {"status": "FAILED", "error": "관측 근거 없음", "result": {}}
    values["root_cause"]["payload"]["result"]["code_proposal"] = {
        "status": "UNAVAILABLE",
        "title": "소스 없음",
        "files": [],
        "tests_status": "NOT_RUN",
        "limitations": ["정확한 소스를 읽지 못함"],
        "test_plan": [],
    }
    report = render_parts_report(values, root_report="# Final full root\n\nall comparison details")
    assert "제한 사유: 관측 근거 없음" in report
    assert "제안 상태: UNAVAILABLE" in report and "정확한 소스를 읽지 못함" in report
    assert "all comparison details" in report
    assert "aws ecs update-service" not in report
    assert "제안 상태: PROPOSED" not in report
