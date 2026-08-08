import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

import { REPOSITORY_ROOT } from './evaluator.mjs';

const scriptPath = path.join(
  REPOSITORY_ROOT,
  'scripts/inject_deployment_fault.py',
);

function runPython(source, args = []) {
  const result = spawnSync('python3', ['-c', source, scriptPath, ...args], {
    cwd: REPOSITORY_ROOT,
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return JSON.parse(result.stdout);
}

test('deployment fault mutations require a caller-owned run id', () => {
  const result = spawnSync('python3', [scriptPath, 'db-leak', '--json'], {
    cwd: REPOSITORY_ROOT,
    encoding: 'utf8',
  });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /--run-id is required for mutating actions/);
});

test('deployment fault status follows the service revision and exposes actual cleanup state', () => {
  const result = runPython(String.raw`
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("fault_script", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

environment = {
    "FAULT_DB_LEAK": "false",
    "FAULT_SLOW_QUERY_MS": "0",
    "FAULT_ERROR_RATE": "0.0",
    "LOG_LEVEL": "INFO",
}
service = {
    "taskDefinition": "arn:service-revision",
    "desiredCount": 1,
    "runningCount": 1,
    "pendingCount": 0,
    "deployments": [{
        "status": "PRIMARY",
        "rolloutState": "COMPLETED",
        "taskDefinition": "arn:service-revision",
    }],
}

module.describe_service = lambda: service
module.aws = lambda *args: {
    "taskDefinition": {
        "taskDefinitionArn": args[3],
        "containerDefinitions": [{
            "name": module.CONTAINER,
            "environment": [
                {"name": key, "value": value}
                for key, value in environment.items()
            ],
        }],
    },
}
module.describe_db_instance = lambda: {
    "DBInstanceStatus": "available",
    "DBParameterGroups": [{
        "DBParameterGroupName": "original",
        "ParameterApplyStatus": "in-sync",
    }],
}
module.parameter_group_names = lambda: ["default.postgres17", "original"]
module.alarm_states = lambda: {
    name: "OK" for name in module.ALARM_NAMES
}

status = module.status_snapshot()
print(json.dumps(status))
`);

  assert.equal(result.taskDefinitionArn, 'arn:service-revision');
  assert.deepEqual(result.logLevel, { present: true, value: 'INFO' });
  assert.equal(result.database.parameterGroup, 'original');
  assert.equal(result.database.parameterApplyStatus, 'in-sync');
  assert.equal(result.service.stable, true);
  assert.equal(result.checks.noActiveRun, true);
  assert.equal(result.clean, true);
});

test('deployment fault status cannot hide an active flag, alarm, or absent LOG_LEVEL', () => {
  const result = runPython(String.raw`
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("fault_script", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

module.describe_service = lambda: {
    "taskDefinition": "arn:active-fault",
    "desiredCount": 1,
    "runningCount": 1,
    "pendingCount": 0,
    "deployments": [{
        "status": "PRIMARY",
        "rolloutState": "COMPLETED",
        "taskDefinition": "arn:active-fault",
    }],
}
module.aws = lambda *args: {
    "taskDefinition": {
        "taskDefinitionArn": "arn:active-fault",
        "containerDefinitions": [{
            "name": module.CONTAINER,
            "environment": [{"name": "FAULT_DB_LEAK", "value": "true"}],
        }],
    },
}
module.describe_db_instance = lambda: {
    "DBInstanceStatus": "available",
    "DBParameterGroups": [{
        "DBParameterGroupName": "original",
        "ParameterApplyStatus": "pending-reboot",
    }],
}
module.parameter_group_names = lambda: ["original"]
module.alarm_states = lambda: {
    module.ALARM_NAMES[0]: "ALARM",
    module.ALARM_NAMES[1]: "OK",
}

print(json.dumps(module.status_snapshot()))
`);

  assert.deepEqual(result.logLevel, { present: false, value: null });
  assert.equal(result.checks.faultFlagsClear, false);
  assert.equal(result.checks.databaseParameterGroupInSync, false);
  assert.equal(result.checks.alarmsOk, false);
  assert.equal(result.clean, false);
});

test('DB restore reboots pending-reboot state and polls until in-sync', () => {
  const result = runPython(String.raw`
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("fault_script", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
calls = []
states = iter([
    {
        "DBInstanceStatus": "available",
        "DBParameterGroups": [{
            "DBParameterGroupName": "target",
            "ParameterApplyStatus": "pending-reboot",
        }],
    },
    {
        "DBInstanceStatus": "available",
        "DBParameterGroups": [{
            "DBParameterGroupName": "target",
            "ParameterApplyStatus": "pending-reboot",
        }],
    },
    {
        "DBInstanceStatus": "available",
        "DBParameterGroups": [{
            "DBParameterGroupName": "target",
            "ParameterApplyStatus": "in-sync",
        }],
    },
])

module.describe_db_instance = lambda: next(states)
module.aws = lambda *args: calls.append(list(args)) or {}

restored = module.restore_parameter_group("target", timeout_seconds=5)
print(json.dumps({"restored": restored, "calls": calls}))
`);

  assert.equal(result.restored.parameterApplyStatus, 'in-sync');
  assert.equal(result.restored.rebooted, true);
  assert.equal(
    result.calls.filter((call) => call.includes('reboot-db-instance')).length,
    1,
  );
});

test('DB parameter group deletion requires run-scoped proof and matching live tags', () => {
  const result = runPython(String.raw`
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("fault_script", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
run_id = "run-1"
name = module.parameter_group_name_prefix(run_id) + "temp"
proof = {
    "name": name,
    "runId": run_id,
    "provenance": module.PARAMETER_GROUP_PROVENANCE,
    "tags": {
        module.PARAMETER_GROUP_RUN_TAG: run_id,
        module.PARAMETER_GROUP_PROVENANCE_TAG: module.PARAMETER_GROUP_PROVENANCE,
    },
}
calls = []

module.describe_db_instance = lambda: {
    "DBParameterGroups": [{
        "DBParameterGroupName": "original",
        "ParameterApplyStatus": "in-sync",
    }],
}

def fake_aws(*args):
    calls.append(list(args))
    if "describe-db-parameter-groups" in args:
        return {"DBParameterGroups": [{"DBParameterGroupArn": "arn:owned"}]}
    if "list-tags-for-resource" in args:
        return {"TagList": [
            {"Key": key, "Value": value}
            for key, value in proof["tags"].items()
        ]}
    return {}

module.aws = fake_aws
module.delete_parameter_group(proof, run_id=run_id, restored_group="original")
foreign = dict(proof, runId="other-run")
try:
    module.delete_parameter_group(
        foreign,
        run_id=run_id,
        restored_group="original",
    )
except RuntimeError as error:
    rejected = str(error)
else:
    rejected = ""
print(json.dumps({"calls": calls, "name": name, "rejected": rejected}))
`);

  assert.match(result.name, /^rca-e2e-run-1-[a-f0-9]{12}-temp$/);
  assert.equal(
    result.calls.filter((call) => call.includes('delete-db-parameter-group'))
      .length,
    1,
  );
  assert.match(result.rejected, /runId mismatch/);
});

test('cleanup restores LOG_LEVEL and attempts ECS, DB, deletion, alarms, and verification after failures', () => {
  const result = runPython(String.raw`
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("fault_script", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
calls = []

def fail_apply(overrides, label, **kwargs):
    calls.append({
        "step": "ecs",
        "logLevel": overrides.get("LOG_LEVEL"),
        "removals": sorted(kwargs.get("removals") or []),
    })
    raise RuntimeError("ecs failed")

def fail_restore(group_name, **kwargs):
    calls.append({"step": "restore", "group": group_name})
    raise RuntimeError("restore failed")

def delete_group(proof, **kwargs):
    candidate = proof["name"]
    calls.append({"step": "delete", "group": candidate})
    if candidate == "bad-group":
        raise RuntimeError("delete failed")

def fail_alarms(**kwargs):
    calls.append({"step": "alarms"})
    raise TimeoutError("alarms failed")

def final_status():
    calls.append({"step": "verify"})
    return {
        "checks": {
            "faultFlagsClear": False,
            "serviceStable": False,
            "databaseAvailable": True,
            "databaseParameterGroupInSync": False,
            "alarmsOk": False,
            "noActiveRun": False,
        },
        "database": {"parameterGroup": "wrong-group"},
        "environment": {
            "LOG_LEVEL": {"present": True, "value": "INFO"},
        },
    }

module.apply = fail_apply
module.restore_parameter_group = fail_restore
module.delete_parameter_group = delete_group
module.wait_for_alarms_ok = fail_alarms
module.status_snapshot = final_status

cleanup, code = module.cleanup(
    run_id="run-1",
    wait=True,
    timeout_seconds=1,
    restore_db_parameter_group_name="original",
    delete_db_parameter_groups=[
        {"name": "bad-group"},
        {"name": "good-group"},
    ],
    restore_log_level="INFO",
    remove_log_level=False,
)
print(json.dumps({"cleanup": cleanup, "code": code, "calls": calls}))
`);

  assert.deepEqual(
    result.calls.map(({ step }) => step),
    ['ecs', 'restore', 'delete', 'delete', 'alarms', 'verify'],
  );
  assert.equal(result.calls[0].logLevel, 'INFO');
  assert.deepEqual(result.calls[0].removals, []);
  assert.deepEqual(result.cleanup.deletedParameterGroups, ['good-group']);
  assert.deepEqual(
    result.cleanup.errors.map(({ step }) => step),
    ['ecs', 'database.restore', 'database.delete:bad-group', 'alarms'],
  );
  assert.equal(result.cleanup.clean, false);
  assert.equal(result.code, 1);
});

test('cleanup defaults to removing red-herring LOG_LEVEL and refuses unsafe deletion contracts', async () => {
  const source = await readFile(scriptPath, 'utf8');

  assert.match(source, /--restore-log-level/);
  assert.match(source, /--remove-log-level/);
  assert.match(source, /refusing to delete the restored DB parameter group/);
  assert.match(source, /refusing to delete attached DB parameter group/);
  assert.match(
    source,
    /--delete-db-parameter-group requires --restore-db-parameter-group/,
  );
});
