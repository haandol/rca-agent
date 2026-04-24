"""Seed sample trace data into an existing RCA session for dashboard testing."""
from __future__ import annotations

import time
import uuid

import boto3

TABLE_NAME = "RcaAgentDevRcaSession"
RCA_ID = "225a5f36-91c0-4e3d-8465-da378c5df210"
TTL = int(time.time()) + 90 * 86400

client = boto3.client("dynamodb", region_name="us-east-1")


def put_span(
    span_id: str,
    span_type: str,
    *,
    parent_span_id: str | None = None,
    loop_index: int | None = None,
    input_summary: str = "",
    output_summary: str = "",
    status: str = "COMPLETED",
    duration_ms: int = 5000,
    error: str | None = None,
    metadata: dict | None = None,
    start_offset_ms: int = 0,
) -> None:
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
    start = base + timedelta(milliseconds=start_offset_ms)
    end = start + timedelta(milliseconds=duration_ms)

    item: dict = {
        "PK": {"S": f"RCA#{RCA_ID}"},
        "SK": {"S": f"strands#SPAN#{span_id}"},
        "engine": {"S": "strands"},
        "span_type": {"S": span_type},
        "span_status": {"S": status},
        "start_time": {"S": start.isoformat()},
        "end_time": {"S": end.isoformat()},
        "duration_ms": {"N": str(duration_ms)},
        "input_summary": {"S": input_summary},
        "output_summary": {"S": output_summary},
        "ttl": {"N": str(TTL)},
    }
    if parent_span_id:
        item["parent_span_id"] = {"S": parent_span_id}
    if loop_index is not None:
        item["loop_index"] = {"N": str(loop_index)}
    if error:
        item["error"] = {"S": error}
    if metadata:
        m = {}
        for k, v in metadata.items():
            if isinstance(v, bool):
                m[k] = {"BOOL": v}
            elif isinstance(v, (int, float)):
                m[k] = {"N": str(v)}
            else:
                m[k] = {"S": str(v)}
        item["metadata"] = {"M": m}

    client.put_item(TableName=TABLE_NAME, Item=item)
    print(f"  SPAN: {span_type} ({span_id[:8]})")


def put_hypothesis(
    hypothesis_id: str,
    description: str,
    category: str,
    *,
    parent_id: str | None = None,
    depth: int = 0,
    status: str = "PENDING",
    confidence: float = 0.5,
    evidence_summary: str = "",
    judgment_reasoning: str = "",
    judgment_confidence: float | None = None,
) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    item: dict = {
        "PK": {"S": f"RCA#{RCA_ID}"},
        "SK": {"S": f"strands#HYPO#{hypothesis_id}"},
        "engine": {"S": "strands"},
        "tree_id": {"S": "tree-demo-001"},
        "depth": {"N": str(depth)},
        "description": {"S": description},
        "category": {"S": category},
        "confidence_score": {"N": str(confidence)},
        "status": {"S": status},
        "required_evidence": {"L": [{"S": "metrics"}, {"S": "logs"}]},
        "evidence_summary": {"S": evidence_summary},
        "judgment_reasoning": {"S": judgment_reasoning},
        "created_at": {"S": now},
        "updated_at": {"S": now},
        "ttl": {"N": str(TTL)},
    }
    if parent_id:
        item["parent_id"] = {"S": parent_id}
    else:
        item["parent_id"] = {"NULL": True}
    if judgment_confidence is not None:
        item["judgment_confidence"] = {"N": str(judgment_confidence)}

    client.put_item(TableName=TABLE_NAME, Item=item)
    print(f"  HYPO: {description[:50]} ({hypothesis_id[:8]})")


def main() -> None:
    print(f"Seeding trace data for RCA: {RCA_ID}")
    print()

    # ── Spans ───────────────────────────────────────────────────────
    print("Writing spans...")

    scoping_id = str(uuid.uuid4())
    put_span(
        scoping_id, "SCOPING",
        input_summary="알람=HealthcareHighCPU-Test, 리전=us-east-1",
        output_summary="심각도=high, 영향범위=single, 유사 플레이북=1건",
        duration_ms=45000,
        start_offset_ms=0,
        metadata={"심각도": "high", "영향범위": "single", "유사_플레이북": 1},
    )

    hypgen_id = str(uuid.uuid4())
    put_span(
        hypgen_id, "HYPOTHESIS_GENERATION",
        input_summary="심각도=high, 영향범위=single",
        output_summary="가설 3개 생성, tree_id=tree-demo-001",
        duration_ms=12000,
        start_offset_ms=45000,
        metadata={"가설_수": 3, "tree_id": "tree-demo-001"},
    )

    # 검증 루프 1
    loop1_id = str(uuid.uuid4())
    put_span(
        loop1_id, "VALIDATION_LOOP",
        loop_index=1,
        input_summary="가설=3개",
        output_summary="종료: CONFIRMED",
        duration_ms=180000,
        start_offset_ms=57000,
        metadata={"루프_번호": 1},
    )

    prio1_id = str(uuid.uuid4())
    put_span(
        prio1_id, "PRIORITIZATION",
        parent_span_id=loop1_id,
        input_summary="가설=3개",
        output_summary="가설 3개 우선순위 결정",
        duration_ms=8000,
        start_offset_ms=57000,
    )

    ev1_id = str(uuid.uuid4())
    put_span(
        ev1_id, "EVIDENCE_COLLECTION",
        parent_span_id=loop1_id,
        input_summary="신규_가설=3개",
        output_summary="가설 3개에 대한 증거 수집 완료",
        duration_ms=95000,
        start_offset_ms=65000,
        metadata={"신규_가설_수": 3},
    )

    val1_id = str(uuid.uuid4())
    put_span(
        val1_id, "VALIDATION",
        parent_span_id=loop1_id,
        input_summary="가설=3개, 증거=3건",
        output_summary="판정=3건, 확정=1, 기각=1, 전체기각=False",
        duration_ms=35000,
        start_offset_ms=160000,
        metadata={"판정_수": 3, "확정": 1, "기각": 1, "전체기각": False},
    )

    term1_id = str(uuid.uuid4())
    put_span(
        term1_id, "TERMINATION",
        parent_span_id=loop1_id,
        input_summary="루프=1, 판정=3건",
        output_summary="종료=True, 사유=CONFIRMED",
        duration_ms=5,
        start_offset_ms=195000,
        metadata={"종료여부": True, "사유": "CONFIRMED"},
    )

    report_id = str(uuid.uuid4())
    put_span(
        report_id, "REPORT",
        input_summary="최적가설=있음, 확정=True",
        output_summary="rca_id=225a5f36, 신뢰도=0.95",
        duration_ms=15000,
        start_offset_ms=237000,
    )

    playbook_id = str(uuid.uuid4())
    put_span(
        playbook_id, "PLAYBOOK",
        input_summary="rca_id=225a5f36",
        output_summary="playbook_id=pb-001, 장애유형=high_cpu_deployment",
        duration_ms=10000,
        start_offset_ms=252000,
    )

    notif_id = str(uuid.uuid4())
    put_span(
        notif_id, "NOTIFICATION",
        input_summary="rca_id=225a5f36",
        output_summary="소요시간=270초",
        duration_ms=2000,
        start_offset_ms=262000,
    )

    # ── Hypotheses ──────────────────────────────────────────────────
    print()
    print("Writing hypotheses...")

    h1_id = str(uuid.uuid4())
    h2_id = str(uuid.uuid4())
    h3_id = str(uuid.uuid4())

    put_hypothesis(
        h1_id,
        "최근 배포에서 헬스체크 엔드포인트에 CPU 집약적 코드 경로가 도입되어 CPU 사용률이 임계치를 초과함",
        "DEPLOYMENT",
        status="CONFIRMED",
        confidence=0.95,
        evidence_summary="CloudWatch 메트릭에서 09:45 UTC부터 CPU 급등 확인, 09:43 ECS 배포와 시간적 상관관계 일치. CloudTrail에서 09:42 RegisterTaskDefinition 확인.",
        judgment_reasoning="배포와 CPU 급등 간 강한 시간적 상관관계. 배포 로그에서 새 태스크 정의와 업데이트된 컨테이너 이미지 확인. 롤백으로 문제 해결됨.",
        judgment_confidence=0.95,
    )

    put_hypothesis(
        h2_id,
        "인프라 수준 이슈: ECS 태스크가 노이지 네이버로 인해 CPU 쓰로틀링이 발생하는 열화된 EC2 호스트에서 실행 중",
        "INFRASTRUCTURE",
        status="REJECTED",
        confidence=0.15,
        evidence_summary="EC2 호스트 수준 이상 징후 미감지. ECS Fargate 태스크는 격리됨. 동일 호스트의 다른 태스크에서 성능 저하 없음.",
        judgment_reasoning="인프라 수준 성능 저하 증거 없음. CPU 급등은 특정 서비스에 국한되며 호스트 전체가 아님. Fargate는 컴퓨팅 격리를 제공.",
        judgment_confidence=0.15,
    )

    put_hypothesis(
        h3_id,
        "외부 부하 테스트 또는 DDoS로 인한 트래픽 급증으로 리소스 고갈 발생",
        "TRAFFIC",
        status="NEEDS_INVESTIGATION",
        confidence=0.4,
        evidence_summary="ALB 요청 수 15% 증가하였으나 일일 정상 변동 범위 내. 비정상 소스 IP 미감지.",
        judgment_reasoning="트래픽 증가폭이 미미하고 예상 범위 내. 다만 신규 배포와 결합 시 서비스의 용량 여유가 줄었을 가능성 있음.",
        judgment_confidence=0.4,
    )

    # 하위 가설 (h3에서 분기)
    h3_child_id = str(uuid.uuid4())
    put_hypothesis(
        h3_child_id,
        "신규 배포로 CPU 여유가 감소하여 정상 트래픽 변동에도 취약해짐",
        "DEPLOYMENT",
        parent_id=h3_id,
        depth=1,
        status="CONFIRMED",
        confidence=0.85,
        evidence_summary="배포 후 CPU 기준선이 45%에서 78%로 상승. 15%의 정상 트래픽 변동이 이제 90% 알람 임계치를 초과하게 됨.",
        judgment_reasoning="배포의 비효율적 코드와 정상 트래픽 패턴의 결합이 알람을 설명. 주요 근본원인과 함께 기여 요인으로 확인.",
        judgment_confidence=0.85,
    )

    print()
    print(f"Done! View the trace at: http://localhost:3100/trace/{RCA_ID}")


if __name__ == "__main__":
    main()
