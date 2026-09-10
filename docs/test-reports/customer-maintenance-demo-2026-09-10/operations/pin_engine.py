"""Temporarily keep one alarm consumer for reproducible validation, then restore it."""

import argparse, json, os, signal, time, uuid
from pathlib import Path
from datetime import datetime, UTC
import boto3

parser = argparse.ArgumentParser(
    description="Temporarily pin the controlled DEV customer demo to Headless; always restore idle Strands."
)
parser.add_argument(
    "state_dir",
    type=Path,
    help="Fresh operator-owned directory for engine-pin.json and release marker",
)
parser.add_argument("--owner", default="maintenance-customer-" + uuid.uuid4().hex[:12])
args = parser.parse_args()
for variable in (
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
):
    os.environ.pop(variable, None)
out = args.state_dir.resolve()
out.mkdir(parents=True, exist_ok=True)
assert (
    not (out / "engine-pin.json").exists()
    and not (out / "engine-pin-release.json").exists()
), "use a fresh pin state directory"
assert 1 <= len(args.owner) <= 200
session = boto3.Session(profile_name="default", region_name="us-east-1")
ecs = session.client("ecs")
sqs = session.client("sqs")
assert session.client("sts").get_caller_identity()["Account"] == "395271362395"
name = "RcaAgentDevRcaAgent"
selected = "RcaAgentDevCcHeadless"
key = "CustomerValidationOwner"
owner = args.owner
print(json.dumps({"stateDirectory": str(out), "owner": owner}), flush=True)
record = {"owner": owner, "startedAt": datetime.now(UTC).isoformat(), "restored": False}


def save():
    (out / "engine-pin.json").write_text(json.dumps(record, indent=2))


def get(n):
    return ecs.describe_services(cluster=n, services=[n], include=["TAGS"])["services"][
        0
    ]


def interrupt(signum, frame):
    raise KeyboardInterrupt(str(signum))


for sig in [signal.SIGTERM, signal.SIGINT]:
    signal.signal(sig, interrupt)
service = get(name)
other = get(selected)
assert (
    other["runningCount"] == other["desiredCount"] == 1 and other["pendingCount"] == 0
)
assert (
    service["runningCount"] == service["desiredCount"] == 1
    and service["pendingCount"] == 0
)
assert all(
    d.get("rolloutState") == "COMPLETED"
    for d in service["deployments"] + other["deployments"]
)
assert key not in {t["key"] for t in service.get("tags", [])}
td = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])[
    "taskDefinition"
]
env = {
    e["name"]: e["value"]
    for c in td["containerDefinitions"]
    for e in c.get("environment", [])
}
counts = sqs.get_queue_attributes(
    QueueUrl=env["SQS_QUEUE_URL"],
    AttributeNames=[
        "ApproximateNumberOfMessages",
        "ApproximateNumberOfMessagesNotVisible",
        "ApproximateNumberOfMessagesDelayed",
    ],
)["Attributes"]
assert all(int(v) == 0 for v in counts.values())
record.update(
    serviceArn=service["serviceArn"],
    taskDefinition=service["taskDefinition"],
    originalDesired=service["desiredCount"],
    queueCounts=counts,
)
save()
try:
    ecs.tag_resource(
        resourceArn=service["serviceArn"], tags=[{"key": key, "value": owner}]
    )
    record["tagged"] = True
    save()
    ecs.update_service(cluster=name, service=name, desiredCount=0)
    record["pinRequestedAt"] = datetime.now(UTC).isoformat()
    save()
    deadline = time.monotonic() + 7200
    while (
        time.monotonic() < deadline and not (out / "engine-pin-release.json").exists()
    ):
        current = get(name)
        if (
            current["taskDefinition"] != record["taskDefinition"]
            or current["desiredCount"] != 0
        ):
            raise RuntimeError("foreign service change during pin")
        if (
            current["runningCount"] == 0
            and current["pendingCount"] == 0
            and "readyAt" not in record
        ):
            record["readyAt"] = datetime.now(UTC).isoformat()
            save()
        time.sleep(15)
except BaseException as error:
    record["error"] = type(error).__name__ + ": " + str(error)
    save()
finally:
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, signal.SIG_IGN)
    try:
        current = get(name)
        tags = {t["key"]: t["value"] for t in current.get("tags", [])}
        if (
            current["taskDefinition"] != record["taskDefinition"]
            or tags.get(key) != owner
            or current["desiredCount"] not in (0, record["originalDesired"])
        ):
            raise RuntimeError("foreign change; refusing restore overwrite")
        ecs.update_service(
            cluster=name, service=name, desiredCount=record["originalDesired"]
        )
        for _ in range(60):
            current = get(name)
            if (
                current["runningCount"]
                == current["desiredCount"]
                == record["originalDesired"]
                and current["pendingCount"] == 0
            ):
                break
            time.sleep(5)
        else:
            raise TimeoutError("engine restore not yet healthy")
        ecs.untag_resource(resourceArn=service["serviceArn"], tagKeys=[key])
        record["restored"] = True
    except BaseException as error:
        record["restoreError"] = type(error).__name__ + ": " + str(error)
    record["finishedAt"] = datetime.now(UTC).isoformat()
    save()
