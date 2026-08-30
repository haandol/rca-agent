from codex_headless.services.playbook_merge import merge_playbook_update, promote_to_verified

EXISTING = {
    "stage": "PLAYBOOK",
    "playbook_id": "pb-1",
    "failure_type": "DB 커넥션 누수",
    "symptom_pattern": "DatabaseConnections 80 초과",
    "temporary_mitigation": "서비스 재배포",
    "permanent_remediation": "커넥션 반환 누락 수정",
    "verification_steps": ["DatabaseConnections 관측"],
    "prevention_measures": ["커넥션 상한 알람 추가"],
    "tags": ["db-leak"],
    "verification_status": "DRAFT",
    "execution_steps": [
        {
            "step_id": "step-1",
            "intent": "커넥션 회수",
            "action": "api 서비스를 강제 재배포",
            "success_criteria": "DatabaseConnections 20 이하",
        },
        {
            "step_id": "step-2",
            "intent": "증상 확인",
            "action": "VitalIngestFailure 조회",
            "success_criteria": "VitalIngestFailure 0",
        },
    ],
}


def test_an_update_that_omits_fields_keeps_the_existing_values():
    merged, diff = merge_playbook_update(EXISTING, {"temporary_mitigation": "재배포 후 커넥션 확인"})

    assert merged["symptom_pattern"] == EXISTING["symptom_pattern"]
    assert merged["prevention_measures"] == EXISTING["prevention_measures"]
    assert merged["temporary_mitigation"] == "재배포 후 커넥션 확인"
    assert diff.changed_fields == ["temporary_mitigation"]


def test_an_update_that_blanks_a_field_does_not_erase_it():
    """모델이 필드를 비워 반환할 때 축적이 조용히 사라지는 것을 코드가 막는다."""
    merged, diff = merge_playbook_update(
        EXISTING,
        {"symptom_pattern": "", "prevention_measures": [], "permanent_remediation": None},
    )

    assert merged["symptom_pattern"] == EXISTING["symptom_pattern"]
    assert merged["prevention_measures"] == EXISTING["prevention_measures"]
    assert merged["permanent_remediation"] == EXISTING["permanent_remediation"]
    assert diff.is_empty


def test_an_update_that_omits_a_step_keeps_that_step():
    merged, diff = merge_playbook_update(
        EXISTING,
        {"execution_steps": [{"step_id": "step-1", "action": "api 서비스를 강제 재배포하고 30초 대기"}]},
    )

    step_ids = [step["step_id"] for step in merged["execution_steps"]]

    assert step_ids == ["step-1", "step-2"]
    assert merged["execution_steps"][1] == EXISTING["execution_steps"][1]
    assert diff.preserved_steps == ["step-2"]
    assert diff.corrected_steps[0]["step_id"] == "step-1"
    assert diff.corrected_steps[0]["changes"]["action"]["before"] == "api 서비스를 강제 재배포"


def test_correcting_a_step_keeps_its_identifier_and_position():
    merged, _ = merge_playbook_update(
        EXISTING,
        {"execution_steps": [{"step_id": "step-2", "success_criteria": "VitalIngestFailure 0 을 5분 유지"}]},
    )

    assert [step["step_id"] for step in merged["execution_steps"]] == ["step-1", "step-2"]
    assert merged["execution_steps"][1]["success_criteria"] == "VitalIngestFailure 0 을 5분 유지"
    assert merged["execution_steps"][1]["intent"] == "증상 확인"


def test_a_new_step_is_appended_after_the_existing_ones():
    merged, diff = merge_playbook_update(
        EXISTING,
        {
            "execution_steps": [
                {
                    "step_id": "step-3",
                    "intent": "커넥션 상한 확인",
                    "action": "RDS max_connections 파라미터 조회",
                    "success_criteria": "max_connections 가 200 이상",
                }
            ]
        },
    )

    assert [step["step_id"] for step in merged["execution_steps"]] == ["step-1", "step-2", "step-3"]
    assert diff.added_steps == ["step-3"]


def test_a_new_step_without_an_observable_criterion_is_rejected():
    merged, diff = merge_playbook_update(
        EXISTING,
        {"execution_steps": [{"step_id": "step-9", "intent": "무언가", "action": "무언가 한다"}]},
    )

    assert [step["step_id"] for step in merged["execution_steps"]] == ["step-1", "step-2"]
    assert diff.added_steps == []


def test_an_update_cannot_reassign_the_playbook_identifier():
    merged, diff = merge_playbook_update(EXISTING, {"playbook_id": "pb-2", "stage": "SOMETHING"})

    assert merged["playbook_id"] == "pb-1"
    assert merged["stage"] == "PLAYBOOK"
    assert diff.is_empty


def test_an_unreadable_update_changes_nothing():
    for update in (None, "not an object", [], 42):
        merged, diff = merge_playbook_update(EXISTING, update)

        assert merged == EXISTING
        assert diff.is_empty


def test_a_step_update_matching_the_existing_value_is_not_reported_as_a_change():
    merged, diff = merge_playbook_update(
        EXISTING,
        {"execution_steps": [{"step_id": "step-1", "action": "api 서비스를 강제 재배포"}]},
    )

    assert merged["execution_steps"] == EXISTING["execution_steps"]
    assert diff.corrected_steps == []
    assert diff.preserved_steps == ["step-1", "step-2"]


def test_a_model_cannot_declare_the_verification_status_through_an_update():
    # 이 값이 모델 출력이 되면 실행되지 않은 절차가 검증됨으로 표기된다.
    merged, diff = merge_playbook_update(EXISTING, {"verification_status": "VERIFIED"})

    assert merged["verification_status"] == "DRAFT"
    assert diff.is_empty


def test_a_model_cannot_demote_a_verified_playbook_through_an_update():
    verified = {**EXISTING, "verification_status": "VERIFIED"}

    merged, diff = merge_playbook_update(verified, {"verification_status": "DRAFT"})

    assert merged["verification_status"] == "VERIFIED"
    assert diff.is_empty


def test_promotion_marks_the_procedure_verified_without_touching_the_content():
    promoted = promote_to_verified(EXISTING)

    assert promoted["verification_status"] == "VERIFIED"
    assert promoted["execution_steps"] == EXISTING["execution_steps"]
    # 원본을 제자리에서 바꾸면 갱신 전 사본이 승격된 값으로 오염된다.
    assert EXISTING["verification_status"] == "DRAFT"


def test_promoting_an_already_verified_playbook_is_a_no_op():
    verified = {**EXISTING, "verification_status": "VERIFIED"}

    assert promote_to_verified(verified) == verified
