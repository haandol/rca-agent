import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import {
  chmodSync,
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const repository = fileURLToPath(new URL('../..', import.meta.url));

/** Run actual hook entry points in an isolated repo with fake formatter/pnpm binaries. */
function fixture(t) {
  const root = realpathSync(
    mkdtempSync(path.join(tmpdir(), 'rca-codex-hooks-')),
  );
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const directory of ['scripts/hooks', 'node_modules/.bin', 'pkg', 'bin'])
    mkdirSync(path.join(root, directory), { recursive: true });
  for (const file of [
    'format-file.sh',
    'format_patch.py',
    'verify-before-push.sh',
  ])
    copyFileSync(
      path.join(repository, 'scripts/hooks', file),
      path.join(root, 'scripts/hooks', file),
    );
  const log = path.join(root, 'calls');
  for (const binary of ['node_modules/.bin/prettier', 'bin/pnpm']) {
    const destination = path.join(root, binary);
    writeFileSync(
      destination,
      '#!/bin/sh\nprintf "%s\\n" "$PWD" "$@" >> "$HOOK_TEST_LOG"\nexit "${HOOK_TEST_EXIT:-0}"\n',
    );
    chmodSync(destination, 0o755);
  }
  return {
    root,
    run(name, event, exit = '0') {
      return execFileSync('bash', [path.join(root, 'scripts/hooks', name)], {
        cwd: path.join(root, 'pkg'),
        input: JSON.stringify(event),
        encoding: 'utf8',
        env: {
          ...process.env,
          PATH: `${root}/bin:${process.env.PATH}`,
          HOOK_TEST_LOG: log,
          HOOK_TEST_EXIT: exit,
        },
      });
    },
    calls: () => readFileSync(log, 'utf8'),
  };
}

test('Codex patch hook formats every existing destination from package cwd', (t) => {
  const f = fixture(t);
  for (const file of ['first file.ts', 'moved.vue'])
    writeFileSync(path.join(f.root, 'pkg', file), 'const x=1');
  f.run('format-file.sh', {
    hook_event_name: 'PostToolUse',
    tool_name: 'apply_patch',
    cwd: path.join(f.root, 'pkg'),
    tool_input: {
      command:
        '*** Begin Patch\n*** Add File: first file.ts\n+x\n*** Update File: gone.vue\n*** Move to: moved.vue\n*** Delete File: deleted.ts\n*** Update File: first file.ts\n*** End Patch',
    },
  });
  const calls = f.calls();
  assert.equal(calls.match(/first file\.ts/g)?.length, 1);
  assert.equal(calls.match(/moved\.vue/g)?.length, 1);
  assert.doesNotMatch(calls, /gone\.vue|deleted\.ts/);
  assert.equal(calls.split('\n')[0], f.root);
});

test('patch formatter does not touch paths outside the repository', (t) => {
  const f = fixture(t);
  writeFileSync(path.join(f.root, 'safe.ts'), 'x');
  f.run('format-file.sh', {
    hook_event_name: 'PostToolUse',
    tool_name: 'apply_patch',
    cwd: f.root,
    tool_input: {
      command: `*** Update File: ${repository}/package.json\n*** Add File: safe.ts`,
    },
  });
  assert.match(f.calls(), /safe\.ts/);
  assert.doesNotMatch(f.calls(), /package\.json/);
});

test('Codex push hook verifies at repo root and denies a failed verification', (t) => {
  const f = fixture(t);
  const result = JSON.parse(
    f.run(
      'verify-before-push.sh',
      {
        hook_event_name: 'PreToolUse',
        tool_name: 'Bash',
        cwd: path.join(f.root, 'pkg'),
        tool_input: { command: 'git push origin HEAD:main' },
      },
      '1',
    ),
  );
  assert.equal(result.hookSpecificOutput.permissionDecision, 'deny');
  assert.match(result.hookSpecificOutput.permissionDecisionReason, /verify/);
  assert.deepEqual(f.calls().trim().split('\n'), [f.root, 'run', 'verify']);
});

test('Codex push hook allows passed checks and ignores non-push commands', (t) => {
  const f = fixture(t);
  assert.equal(
    f.run('verify-before-push.sh', {
      tool_input: { command: 'git status --short' },
    }),
    '',
  );
  const result = JSON.parse(
    f.run('verify-before-push.sh', {
      tool_input: { command: 'git -C pkg push origin HEAD:main' },
    }),
  );
  assert.match(result.systemMessage, /verify passed/);
  assert.deepEqual(f.calls().trim().split('\n'), [f.root, 'run', 'verify']);
});

test('Codex configuration binds documented patch and shell hook names', () => {
  const config = JSON.parse(
    readFileSync(path.join(repository, '.codex/hooks.json'), 'utf8'),
  );
  assert.equal(config.hooks.PostToolUse[0].matcher, '^apply_patch$');
  assert.equal(config.hooks.PreToolUse[0].matcher, '^Bash$');
  assert.match(
    config.hooks.PreToolUse[0].hooks[0].command,
    /git rev-parse --show-toplevel/,
  );
});
