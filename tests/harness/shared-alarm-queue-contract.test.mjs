import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';

import { REPOSITORY_ROOT } from './evaluator.mjs';

test('headless deployment refuses to delete a non-empty legacy alarm queue', async () => {
  const source = await readFile(
    path.join(REPOSITORY_ROOT, 'packages/infra/scripts/deploy-service.sh'),
    'utf8',
  );

  assert.match(source, /assert_legacy_headless_queue_empty/);
  assert.match(source, /ApproximateNumberOfMessages/);
  assert.match(source, /ApproximateNumberOfMessagesNotVisible/);
  assert.match(source, /ApproximateNumberOfMessagesDelayed/);
  assert.match(source, /if \(\( total > 0 \)\)/);
  assert.match(
    source,
    /if \[\[ "\$svc" == "headless-codex" \]\]; then\s+assert_legacy_headless_queue_empty/,
  );
});
