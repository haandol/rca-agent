import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { REPOSITORY_ROOT } from './evaluator.mjs';

const driverPath = path.join(REPOSITORY_ROOT, 'scripts/run_deployed_e2e.py');

const fakeFaultScript = String.raw`
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

action = sys.argv[1]
arguments = sys.argv[1:]
log_path = Path(os.environ["FAKE_FAULT_LOG"])
state_path = Path(os.environ["FAKE_FAULT_STATE"])
child_pid_path = Path(os.environ["FAKE_FAULT_CHILD_PID"])

def append_log(entry):
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\n")

append_log(arguments)

if action == "db-leak":
    state_path.write_text("changed", encoding="utf-8")

should_interrupt = (
    action == os.environ.get("FAKE_INTERRUPT_ACTION")
    and (action != "status" or state_path.exists())
)
if should_interrupt:
    interrupt_signal = getattr(signal, os.environ["FAKE_INTERRUPT_SIGNAL"])
    child_pid_path.write_text(f"{os.getpid()} {action}", encoding="utf-8")

    def terminate_child(_signum, _frame):
        append_log(["child-terminated", action])
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate_child)
    append_log(["child-sleeping", action])
    os.kill(os.getppid(), interrupt_signal)
    time.sleep(30)

if action == "status":
    changed = state_path.exists()
    issue = os.environ.get("FAKE_INITIAL_ISSUE", "") if not changed else ""
    log_present = os.environ.get("FAKE_LOG_PRESENT", "1") == "1"
    groups = ["default.postgres17", "original", "legacy-custom"]
    if changed:
        groups.append("current-run-temp")
    checks = {
        "faultFlagsClear": not changed and issue != "fault",
        "serviceStable": issue != "service",
        "databaseAvailable": issue != "database",
        "databaseParameterGroupInSync": issue != "apply",
        "alarmsOk": issue != "alarm",
        "noActiveRun": issue != "foreign-run" and not changed,
    }
    environment = {
        "LOG_LEVEL": {
            "present": log_present,
            "value": "INFO" if log_present else None,
        },
        "FAULT_DB_LEAK": {"present": changed, "value": "true" if changed else None},
    }
    active_run_id = (
        "foreign-run" if issue == "foreign-run"
        else "caller-run-1" if changed
        else None
    )
    print(json.dumps({
        "action": "status",
        "taskDefinitionArn": "arn:changed" if changed else "arn:original",
        "environment": environment,
        "database": {
            "instanceStatus": "unavailable" if issue == "database" else "available",
            "parameterGroup": "current-run-temp" if changed else "original",
            "parameterApplyStatus": "pending-reboot" if issue == "apply" else "in-sync",
            "parameterGroups": groups,
        },
        "activeRunId": active_run_id,
        "checks": checks,
        "clean": all(checks.values()),
    }))
elif action == "db-leak":
    run_id = sys.argv[sys.argv.index("--run-id") + 1]
    slug = run_id.lower()
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    proof = {
        "name": f"rca-e2e-{slug}-{digest}-temp",
        "runId": run_id,
        "provenance": "inject_deployment_fault.py:db-leak",
        "tags": {
            "RCA_TEST_RUN_ID": run_id,
            "RCA_TEST_PROVENANCE": "inject_deployment_fault.py:db-leak",
        },
    }
    ownership = os.environ.get("FAKE_OWNERSHIP", "valid")
    if ownership == "foreign":
        proof["runId"] = "foreign-run"
    print(json.dumps({
        "action": action,
        "runId": run_id,
        "startedAt": "2026-08-08T00:00:00+00:00",
        "completedAt": "2026-08-08T00:01:00+00:00",
        "ownedResources": {
            "dbParameterGroups": [] if ownership == "none" else [proof],
        },
    }))
elif action == "cleanup":
    cleanup_signal_name = os.environ.get("FAKE_CLEANUP_SIGNAL")
    if cleanup_signal_name:
        os.kill(os.getppid(), getattr(signal, cleanup_signal_name))
        time.sleep(0.2)
        append_log(["cleanup-completed-after-signal", cleanup_signal_name])
    if child_pid_path.exists():
        child_pid, child_action = child_pid_path.read_text(encoding="utf-8").split()
        try:
            os.kill(int(child_pid), 0)
        except ProcessLookupError:
            append_log(["cleanup-observed-child-reaped", child_action])
        else:
            append_log(["cleanup-observed-child-running", child_action])
            raise SystemExit(12)
    print(json.dumps({
        "action": "cleanup",
        "runId": sys.argv[sys.argv.index("--run-id") + 1],
        "clean": True,
    }))
else:
    print(json.dumps({"action": action, "runId": sys.argv[sys.argv.index("--run-id") + 1]}))
`;

async function runDriver(validationCommand, options = {}) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'rca-deployed-e2e-'));
  const fakeScript = path.join(directory, 'fake_fault.py');
  const manifestPath = path.join(directory, 'manifest.json');
  const logPath = path.join(directory, 'calls.jsonl');
  const statePath = path.join(directory, 'state');
  const childPidPath = path.join(directory, 'child.pid');
  await writeFile(fakeScript, fakeFaultScript, 'utf8');

  const result = spawnSync(
    'python3',
    [
      driverPath,
      '--run-id',
      'caller-run-1',
      '--manifest',
      manifestPath,
      '--fault-script',
      fakeScript,
      '--red-herring-delay-seconds',
      '0',
      '--timeout-seconds',
      '1',
      '--',
      ...validationCommand,
    ],
    {
      cwd: REPOSITORY_ROOT,
      encoding: 'utf8',
      env: {
        ...process.env,
        FAKE_FAULT_LOG: logPath,
        FAKE_FAULT_STATE: statePath,
        FAKE_FAULT_CHILD_PID: childPidPath,
        FAKE_LOG_PRESENT: options.logPresent === false ? '0' : '1',
        FAKE_INTERRUPT_ACTION: options.interruptAction ?? '',
        FAKE_INTERRUPT_SIGNAL: options.interruptSignal ?? '',
        FAKE_CLEANUP_SIGNAL: options.cleanupSignal ?? '',
        FAKE_INITIAL_ISSUE: options.initialIssue ?? '',
        FAKE_OWNERSHIP: options.ownership ?? 'valid',
      },
      timeout: 15_000,
    },
  );

  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  const calls = (await readFile(logPath, 'utf8'))
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line));
  await rm(directory, { recursive: true, force: true });
  return { calls, manifest, result };
}

function cleanupCall(calls) {
  return calls.find(([action]) => action === 'cleanup');
}

test('deployed E2E driver preserves state and cleans up after validation success', async () => {
  const { calls, manifest, result } = await runDriver([
    'python3',
    '-c',
    [
      'import os,sys',
      'valid = os.environ.get("RCA_E2E_RUN_ID") == "caller-run-1"',
      'valid = valid and os.path.exists(os.environ["RCA_E2E_MANIFEST"])',
      'valid = valid and bool(os.environ.get("RCA_E2E_STARTED_AT"))',
      'valid = valid and os.path.isdir(os.environ["RCA_E2E_EVIDENCE_DIR"])',
      'sys.exit(0 if valid else 9)',
    ].join(';'),
  ]);

  assert.equal(result.status, 0, result.stderr || result.stdout);
  assert.equal(manifest.runId, 'caller-run-1');
  assert.equal(manifest.original.dbParameterGroup, 'original');
  assert.deepEqual(manifest.original.environment.LOG_LEVEL, {
    present: true,
    value: 'INFO',
  });
  assert.deepEqual(manifest.preCleanup.ownedDbParameterGroups, [
    'rca-e2e-caller-run-1-6974c7493482-temp',
  ]);
  assert.equal(manifest.cleanup.result.clean, true);

  const cleanup = cleanupCall(calls);
  assert.ok(cleanup);
  assert.deepEqual(cleanup.slice(0, 5), [
    'cleanup',
    '--json',
    '--run-id',
    'caller-run-1',
    '--timeout-seconds',
  ]);
  assert.ok(cleanup.includes('--restore-log-level'));
  assert.ok(cleanup.includes('INFO'));
  assert.ok(cleanup.includes('--restore-db-parameter-group'));
  assert.ok(cleanup.includes('original'));
  assert.ok(cleanup.includes('--delete-db-parameter-group'));
  assert.ok(
    cleanup.some(
      (argument) =>
        argument.includes('rca-e2e-caller-run-1-6974c7493482-temp') &&
        argument.includes('"provenance":"inject_deployment_fault.py:db-leak"'),
    ),
  );
  assert.ok(!cleanup.includes('legacy-custom'));
});

test('deployed E2E driver fails closed on every dirty initial state', async (t) => {
  for (const issue of [
    'fault',
    'service',
    'database',
    'apply',
    'alarm',
    'foreign-run',
  ]) {
    await t.test(issue, async () => {
      const { calls, manifest, result } = await runDriver(
        ['python3', '-c', 'raise SystemExit(99)'],
        { initialIssue: issue },
      );

      assert.equal(result.status, 1, result.stderr || result.stdout);
      assert.match(manifest.preflightError.message, /initial status/);
      assert.equal(manifest.validation, undefined);
      assert.equal(manifest.cleanup, undefined);
      assert.deepEqual(
        calls.map(([action]) => action),
        ['status'],
      );
    });
  }
});

test('deployed E2E driver never deletes inventory-difference groups without current-run proof', async () => {
  const { calls, manifest, result } = await runDriver(
    ['python3', '-c', 'raise SystemExit(0)'],
    { ownership: 'none' },
  );

  assert.equal(result.status, 0, result.stderr || result.stdout);
  assert.deepEqual(manifest.preCleanup.ownedDbParameterGroups, []);
  const cleanup = cleanupCall(calls);
  assert.equal(cleanup.includes('--delete-db-parameter-group'), false);
  assert.equal(
    cleanup.some((argument) => argument === 'legacy-custom'),
    false,
  );
});

test('deployed E2E driver rejects foreign ownership proof after mutation without deleting it', async () => {
  const { calls, manifest, result } = await runDriver(
    ['python3', '-c', 'raise SystemExit(0)'],
    { ownership: 'foreign' },
  );

  assert.equal(result.status, 1, result.stderr || result.stdout);
  assert.match(manifest.orchestrationError.message, /runId mismatch/);
  assert.equal(manifest.validation, undefined);
  assert.deepEqual(manifest.preCleanup.ownedDbParameterGroups, []);
  assert.equal(
    cleanupCall(calls).includes('--delete-db-parameter-group'),
    false,
  );
});

test('deployed E2E driver cleans up after a validation failure and restores absent LOG_LEVEL', async () => {
  const { calls, manifest, result } = await runDriver(
    ['python3', '-c', 'raise SystemExit(7)'],
    { logPresent: false },
  );

  assert.equal(result.status, 7, result.stderr || result.stdout);
  assert.equal(manifest.validation.exitCode, 7);
  assert.equal(manifest.exitCode, 7);
  assert.equal(manifest.cleanup.result.clean, true);
  assert.ok(cleanupCall(calls).includes('--remove-log-level'));
});

test('deployed E2E driver defers SIGINT and SIGTERM until cleanup completes', async (t) => {
  for (const [signalName, expectedStatus] of [
    ['SIGINT', 130],
    ['SIGTERM', 143],
  ]) {
    await t.test(signalName, async () => {
      const validation = [
        'import os,signal,time',
        `os.kill(os.getppid(), signal.${signalName})`,
        'time.sleep(30)',
      ].join(';');
      const { calls, manifest, result } = await runDriver([
        'python3',
        '-c',
        validation,
      ]);

      assert.equal(
        result.status,
        expectedStatus,
        result.stderr || result.stdout,
      );
      assert.equal(manifest.interrupted.signal, signalName);
      assert.equal(manifest.cleanup.result.clean, true);
      assert.equal(manifest.exitCode, expectedStatus);
      assert.ok(cleanupCall(calls));
    });
  }
});

test('deployed E2E driver reaps interrupted fault and status children before cleanup', async (t) => {
  for (const [action, signalName, expectedStatus] of [
    ['red-herring', 'SIGINT', 130],
    ['red-herring', 'SIGTERM', 143],
    ['db-leak', 'SIGINT', 130],
    ['db-leak', 'SIGTERM', 143],
    ['status', 'SIGINT', 130],
    ['status', 'SIGTERM', 143],
  ]) {
    await t.test(`${action} ${signalName}`, async () => {
      const { calls, manifest, result } = await runDriver(
        ['python3', '-c', 'raise SystemExit(99)'],
        {
          interruptAction: action,
          interruptSignal: signalName,
        },
      );

      assert.equal(
        result.status,
        expectedStatus,
        result.stderr || result.stdout,
      );
      assert.equal(manifest.interrupted.signal, signalName);
      assert.equal(manifest.exitCode, expectedStatus);
      assert.equal(manifest.cleanup.result.clean, true);
      if (action === 'status') {
        assert.equal(manifest.validation.exitCode, 99);
      } else {
        assert.equal(manifest.validation, undefined);
      }

      const terminatedIndex = calls.findIndex(
        ([event, childAction]) =>
          event === 'child-terminated' && childAction === action,
      );
      const cleanupIndexes = calls
        .map(([event], index) => (event === 'cleanup' ? index : -1))
        .filter((index) => index >= 0);
      const cleanupObservedReaped = calls.some(
        ([event, childAction]) =>
          event === 'cleanup-observed-child-reaped' && childAction === action,
      );
      assert.notEqual(terminatedIndex, -1);
      assert.equal(cleanupIndexes.length, 1);
      assert.ok(terminatedIndex < cleanupIndexes[0]);
      assert.equal(cleanupObservedReaped, true);
    });
  }
});

test('deployed E2E driver defers signals raised by the cleanup child', async (t) => {
  for (const [signalName, expectedStatus] of [
    ['SIGINT', 130],
    ['SIGTERM', 143],
  ]) {
    await t.test(signalName, async () => {
      const { calls, manifest, result } = await runDriver(
        ['python3', '-c', 'raise SystemExit(0)'],
        { cleanupSignal: signalName },
      );

      assert.equal(
        result.status,
        expectedStatus,
        result.stderr || result.stdout,
      );
      assert.equal(manifest.cleanup.result.clean, true);
      assert.equal(manifest.exitCode, expectedStatus);
      assert.equal(
        calls.some(
          ([event, observedSignal]) =>
            event === 'cleanup-completed-after-signal' &&
            observedSignal === signalName,
        ),
        true,
      );
      assert.equal(calls.filter(([event]) => event === 'cleanup').length, 1);
    });
  }
});
