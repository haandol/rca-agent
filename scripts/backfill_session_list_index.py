#!/usr/bin/env python3
"""세션 목록 인덱스 키를 기존 세션에 채운다.

목록 조회가 전체 순회에서 인덱스 조회로 바뀌면, 인덱스 키가 없는 세션은 목록에서
사라진다. DynamoDB는 인덱스 키가 없는 아이템을 인덱스에 넣지 않기 때문이다 — 이
성질이 인덱스를 세션 전용으로 만들어 주는 근거이기도 하다. 그래서 인덱스를 읽기
시작하는 것과 기존 세션에 키를 채우는 것은 하나의 변경이다.

키는 `engine`과 `created_at`을 그대로 복사한다. 두 속성을 인덱스 키로 직접 쓰지 않는
이유는 가설·실행 아이템도 같은 두 속성을 갖고 있어 인덱스가 오염되기 때문이고, 그래서
세션만 쓰는 별도 이름이 필요하다.

**기존 속성을 덮지 않는다.** 조건식으로 키가 없는 아이템만 갱신하므로, 두 번 돌려도
두 번째는 아무것도 쓰지 않는다. 백필이 세션 상태나 claim을 건드리면 진행 중인 분석의
소유권 판정을 깨뜨릴 수 있다.

사용법:
    python scripts/backfill_session_list_index.py --table <이름> [--dry-run]
"""

from __future__ import annotations

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

LIST_PARTITION_KEY = "list_engine"
LIST_SORT_KEY = "list_created_at"


def session_items(client, table: str):
    """세션 아이템만 훑는다.

    아직 인덱스가 없거나 비어 있는 상태에서 도는 스크립트이므로 순회로 읽는다. 이
    순회는 백필 1회에만 쓰이고, 목록 조회가 순회를 그만두게 하려고 존재한다.
    """
    paginator = client.get_paginator("scan")
    pages = paginator.paginate(
        TableName=table,
        FilterExpression="contains(SK, :session) AND begins_with(PK, :prefix)",
        ExpressionAttributeValues={
            ":session": {"S": "SESSION"},
            ":prefix": {"S": "RCA#"},
        },
        ProjectionExpression="PK, SK, engine, created_at, "
        f"{LIST_PARTITION_KEY}, {LIST_SORT_KEY}",
    )
    for page in pages:
        yield from page.get("Items", [])


def engine_of(item: dict) -> str:
    """이 세션을 소유한 엔진.

    엔진 접두사가 없던 시기의 세션은 SK가 그냥 `SESSION`이고 항상 Strands다. 그
    아이템에 다른 엔진을 적으면 목록에서 소유자가 바뀌므로 접두사를 먼저 믿는다.
    """
    engine_attr = item.get("engine", {}).get("S")
    if engine_attr:
        return engine_attr
    sort_key = item["SK"]["S"]
    if sort_key == "SESSION":
        return "strands"
    return sort_key.split("#")[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True, help="DynamoDB 테이블 이름")
    parser.add_argument("--region", default=None, help="AWS 리전")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="쓰지 않고 무엇이 바뀔지만 보고한다",
    )
    args = parser.parse_args()

    client = boto3.client("dynamodb", region_name=args.region)

    scanned = filled = skipped = missing_created = 0

    for item in session_items(client, args.table):
        scanned += 1

        if LIST_PARTITION_KEY in item and LIST_SORT_KEY in item:
            skipped += 1
            continue

        created_at = item.get("created_at", {}).get("S")
        if not created_at:
            # 정렬 키가 없으면 인덱스에 시간순으로 넣을 수 없다. 임의 시각을 만들어
            # 넣으면 목록 순서가 거짓이 되므로 건너뛰고 보고한다.
            missing_created += 1
            print(
                f"  건너뜀(created_at 없음): {item['PK']['S']} / {item['SK']['S']}",
                file=sys.stderr,
            )
            continue

        engine = engine_of(item)

        if args.dry_run:
            filled += 1
            print(f"  채울 대상: {item['PK']['S']} / {item['SK']['S']} → {engine}")
            continue

        try:
            client.update_item(
                TableName=args.table,
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression=(
                    f"SET {LIST_PARTITION_KEY} = :engine, {LIST_SORT_KEY} = :created"
                ),
                # 이미 채워진 아이템은 건드리지 않는다 — 재실행이 안전해야 하고,
                # 백필이 쓰기 경로가 방금 적은 값을 되돌려서는 안 된다.
                ConditionExpression=f"attribute_not_exists({LIST_PARTITION_KEY})",
                ExpressionAttributeValues={
                    ":engine": {"S": engine},
                    ":created": {"S": created_at},
                },
            )
            filled += 1
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # 이 스크립트가 도는 동안 다른 주체가 채웠다. 원하는 상태이므로
                # 실패가 아니다.
                skipped += 1
                continue
            raise

    verb = "채울 예정" if args.dry_run else "채움"
    print(
        f"\n세션 {scanned}건 확인 · {verb} {filled}건 · 이미 있음 {skipped}건"
        + (f" · created_at 없어 건너뜀 {missing_created}건" if missing_created else "")
    )
    # created_at 없는 세션은 목록에서 빠지므로 조용히 성공으로 처리하지 않는다.
    return 1 if missing_created else 0


if __name__ == "__main__":
    raise SystemExit(main())
