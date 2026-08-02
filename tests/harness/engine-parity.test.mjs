import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';

import { REPOSITORY_ROOT } from './evaluator.mjs';

const AGENT_DESTRUCTIVE =
  'packages/agent/src/rca_agent/services/destructive_actions.py';
const CC_DESTRUCTIVE =
  'packages/cc-headless/src/cc_headless/services/destructive_actions.py';
const AGENT_EMBED_KEY = 'packages/agent/src/rca_agent/utils/embed_key.py';
const CC_EMBED_KEY = 'packages/cc-headless/src/cc_headless/utils/embed_key.py';

async function readRepositoryFile(relativePath) {
  return readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8');
}

/**
 * Pull a frozenset/tuple literal's string members out of a Python source file.
 * Comparing the parsed members rather than the raw text lets the two modules
 * differ in ordering and comments while still being held to the same vocabulary.
 */
function pythonStringLiterals(source, name) {
  const start = source.indexOf(name);
  assert.notEqual(start, -1, `${name} is missing`);
  const open = source.indexOf('(', start);
  assert.notEqual(open, -1, `${name} has no opening delimiter`);

  let depth = 0;
  let end = -1;
  for (let index = open; index < source.length; index += 1) {
    const char = source[index];
    if (char === '(' || char === '{') depth += 1;
    if (char === ')' || char === '}') {
      depth -= 1;
      if (depth === 0) {
        end = index;
        break;
      }
    }
  }
  assert.notEqual(end, -1, `${name} is not closed`);

  const body = source.slice(open, end);
  return new Set([...body.matchAll(/"([^"]+)"/g)].map((match) => match[1]));
}

// The analysis side scores whether a proposed procedure is safe and the
// execution side decides whether a command runs. If those two vocabularies
// drift, analysis marks a procedure safe that execution then refuses — or worse,
// the reverse. The packages cannot import from each other, so this test is what
// keeps them identical.
test('both engines judge destructive actions with the same verb vocabulary', async () => {
  const [agent, ccHeadless] = await Promise.all([
    readRepositoryFile(AGENT_DESTRUCTIVE),
    readRepositoryFile(CC_DESTRUCTIVE),
  ]);

  assert.deepEqual(
    pythonStringLiterals(agent, 'DESTRUCTIVE_OPERATION_VERBS'),
    pythonStringLiterals(ccHeadless, 'DESTRUCTIVE_OPERATION_VERBS'),
  );
});

test('both engines recognise the same Korean destructive phrasing', async () => {
  const [agent, ccHeadless] = await Promise.all([
    readRepositoryFile(AGENT_DESTRUCTIVE),
    readRepositoryFile(CC_DESTRUCTIVE),
  ]);

  assert.deepEqual(
    pythonStringLiterals(agent, '_DESTRUCTIVE_KOREAN'),
    pythonStringLiterals(ccHeadless, '_DESTRUCTIVE_KOREAN'),
  );
});

/**
 * Strip a Python module down to what it executes: no comments, no docstrings, no
 * blank lines. Two renderers may explain themselves differently and still be the
 * same renderer; what they must not differ in is the text they produce.
 */
function pythonCodeLines(source) {
  const withoutDocstrings = source.replace(/"""[\s\S]*?"""/g, '');
  return withoutDocstrings
    .split('\n')
    .map((line) => line.replace(/\s+#.*$/, '').trimEnd())
    .filter((line) => line.trim() && !line.trim().startsWith('#'));
}

// Both engines write to and search the same vector indexes, so the embedding text
// for one incident has to come out byte-identical on either side. This renderer is
// what guarantees that, and it is duplicated because the packages cannot import
// from each other — the same reason the destructive vocabulary is duplicated.
//
// Drift here fails silently, which is why it is worth a test: a different label, a
// different separator or a different truncation length splits the embedding space,
// and the only symptom is that a playbook stops finding its own prior incidents.
// A search that matches nothing returns an empty result set, not an error.
test('both engines render embedding text with the same renderer', async () => {
  const [agent, ccHeadless] = await Promise.all([
    readRepositoryFile(AGENT_EMBED_KEY),
    readRepositoryFile(CC_EMBED_KEY),
  ]);

  assert.deepEqual(
    pythonCodeLines(agent),
    pythonCodeLines(ccHeadless),
    'the shared embedding renderer has diverged between the engines',
  );

  // The field cap is part of the rendered text, so a value that differs truncates
  // the same incident at two different points.
  const cap = (source) => source.match(/^EMBED_FIELD_MAX = (\d+)$/m)?.[1];
  assert.equal(cap(agent), cap(ccHeadless));
  assert.ok(cap(agent), 'EMBED_FIELD_MAX is missing');
});

// The execution agent reads execution steps out of the recorded playbook, and it
// reads them the same way regardless of which engine produced the analysis. A
// field one engine omits is a field the execution agent cannot act on.
test('both engines record the same execution step fields', async () => {
  const [agentValidation, ccValidation] = await Promise.all([
    readRepositoryFile('packages/agent/src/rca_agent/ports/dto/models.py'),
    readRepositoryFile(
      'packages/cc-headless/src/cc_headless/services/artifact_validation.py',
    ),
  ]);

  const ccFields = pythonStringLiterals(ccValidation, '_EXECUTION_STEP_FIELDS');
  const agentStepModel = agentValidation.slice(
    agentValidation.indexOf('class ExecutionStep(BaseModel):'),
  );
  for (const field of ccFields) {
    assert.match(
      agentStepModel,
      new RegExp(`^    ${field}:`, 'm'),
      `ExecutionStep is missing the field ${field}`,
    );
  }
});

test('neither engine can mark a playbook verified during analysis', async () => {
  const [agentModels, ccValidation] = await Promise.all([
    readRepositoryFile('packages/agent/src/rca_agent/ports/dto/models.py'),
    readRepositoryFile(
      'packages/cc-headless/src/cc_headless/services/artifact_validation.py',
    ),
  ]);

  // A playbook is a draft until an execution and its retrospective have
  // exercised the procedure, so DRAFT is the only status analysis may write.
  assert.match(ccValidation, /_PLAYBOOK_DRAFT_STATUS = "DRAFT"/);
  assert.match(
    ccValidation,
    /verification_status must be/,
    'analysis artifacts must be rejected when they claim any other status',
  );

  // Both engines share one two-value vocabulary. A third value on either side
  // would mean a status the other cannot interpret.
  const statusEnum = agentModels.slice(
    agentModels.indexOf('class PlaybookVerificationStatus(StrEnum):'),
    agentModels.indexOf('class AlarmTrigger(BaseModel):'),
  );
  const members = [...statusEnum.matchAll(/^    (\w+) = "([^"]+)"$/gm)];
  assert.deepEqual(
    members.map(([, name, value]) => [name, value]),
    [
      ['DRAFT', 'DRAFT'],
      ['VERIFIED', 'VERIFIED'],
    ],
  );
});

// The retrospective is the only actor that may promote a procedure, and the
// promotion only holds if nothing downstream can quietly undo it. Each engine
// enforces that in its own package, so this test is what keeps the one-way rule
// from being a rule in only one of them.
test('neither engine lets a model or a merge undo a promotion', async () => {
  const [agentGeneration, agentStore, ccMerge] = await Promise.all([
    readRepositoryFile('packages/agent/src/rca_agent/services/playbook_gen.py'),
    readRepositoryFile(
      'packages/agent/src/rca_agent/adapters/secondary/playbook/s3_vectors_playbook_store.py',
    ),
    readRepositoryFile(
      'packages/cc-headless/src/cc_headless/services/playbook_merge.py',
    ),
  ]);

  // A model-supplied status would make LLM output the authority on whether a
  // procedure has been proven, so the merge must drop the field outright.
  assert.ok(
    pythonStringLiterals(ccMerge, '_SERVER_OWNED_FIELDS').has(
      'verification_status',
    ),
    'the retrospective merge must ignore a model-supplied verification status',
  );

  // Rebuilding a merged playbook field by field is where a promotion goes
  // missing: an omitted field falls back to DRAFT instead of raising.
  assert.match(
    agentGeneration,
    /verification_status=existing\.verification_status/,
    'the Strands merge must carry the recorded status into the merged playbook',
  );
  // Reloading a recorded playbook is the other place a promotion disappears:
  // a status the loader does not reconstruct comes back as the default draft.
  assert.match(
    agentStore,
    /verification_status=_as_verification_status\(/,
    'the Strands loader must reconstruct the recorded verification status',
  );

  // The promotion itself must be one named operation with no inverse — a
  // demotion helper anywhere would make the one-way rule a convention.
  assert.match(ccMerge, /def promote_to_verified\(/);
  assert.doesNotMatch(ccMerge, /def demote|def to_draft|def unverify/);
});
