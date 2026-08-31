import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';

import { REPOSITORY_ROOT } from './evaluator.mjs';

const AGENT_DESTRUCTIVE =
  'packages/agent/src/rca_agent/services/destructive_actions.py';
const CC_DESTRUCTIVE =
  'packages/headless-codex/src/headless_codex/services/destructive_actions.py';
const AGENT_EMBED_KEY = 'packages/agent/src/rca_agent/utils/embed_key.py';
const CC_EMBED_KEY =
  'packages/headless-codex/src/headless_codex/utils/embed_key.py';
const AGENT_SESSION_STORE =
  'packages/agent/src/rca_agent/adapters/secondary/session/dynamodb_session_store.py';
const CC_SESSION_STORE =
  'packages/headless-codex/src/headless_codex/adapters/secondary/session/dynamodb_session_store.py';
const AGENT_SETTINGS = 'packages/agent/src/rca_agent/config/settings.py';
const CC_SETTINGS =
  'packages/headless-codex/src/headless_codex/config/settings.py';

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
  const delimiters = [
    source.indexOf('(', start),
    source.indexOf('{', start),
  ].filter((index) => index !== -1);
  assert.ok(delimiters.length > 0, `${name} has no opening delimiter`);
  const open = Math.min(...delimiters);

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

// Execution command classification is deliberately stricter than natural
// language scoring. Reversible procedures may still use operations that the
// command boundary conservatively refuses to execute automatically.
test('the execution command gate stays stricter than natural-language scoring', async () => {
  const ccHeadless = await readRepositoryFile(CC_DESTRUCTIVE);
  const commandVerbs = pythonStringLiterals(
    ccHeadless,
    'DESTRUCTIVE_OPERATION_VERBS',
  );
  const naturalLanguageTerms = pythonStringLiterals(
    ccHeadless,
    'IRREVERSIBLE_ACTION_ENGLISH',
  );

  for (const reversibleVerb of ['close', 'release', 'disable']) {
    assert.equal(commandVerbs.has(reversibleVerb), true);
    assert.equal(naturalLanguageTerms.has(reversibleVerb), false);
  }
});

test('both engines recognise the same irreversible natural-language terms', async () => {
  const [agent, ccHeadless] = await Promise.all([
    readRepositoryFile(AGENT_DESTRUCTIVE),
    readRepositoryFile(CC_DESTRUCTIVE),
  ]);

  assert.deepEqual(
    pythonStringLiterals(agent, 'IRREVERSIBLE_ACTION_ENGLISH'),
    pythonStringLiterals(ccHeadless, 'IRREVERSIBLE_ACTION_ENGLISH'),
  );
  assert.deepEqual(
    pythonStringLiterals(agent, 'IRREVERSIBLE_ACTION_KOREAN'),
    pythonStringLiterals(ccHeadless, 'IRREVERSIBLE_ACTION_KOREAN'),
  );
});

test('both engines share the same active incident identity and lifecycle vocabulary', async () => {
  const [agentStore, ccStore, agentSettings, ccSettings] = await Promise.all([
    readRepositoryFile(AGENT_SESSION_STORE),
    readRepositoryFile(CC_SESSION_STORE),
    readRepositoryFile(AGENT_SETTINGS),
    readRepositoryFile(CC_SETTINGS),
  ]);

  for (const source of [agentStore, ccStore]) {
    assert.match(source, /_ACTIVE_INCIDENT_SK = "ACTIVE_INCIDENT"/);
    assert.match(
      source,
      /return f"cloudwatch:\{alarm\.region\}:alarm:\{alarm\.alarm_name\}"/,
    );
    assert.match(
      source,
      /hashlib\.sha256\(build_alarm_identity\(alarm\)\.encode\(\)\)\.hexdigest\(\)/,
    );
    assert.match(source, /_ACTIVE_EXECUTION_SK = "EXEC_ACTIVE"/);
  }

  assert.deepEqual(
    pythonStringLiterals(agentStore, '_ANALYSIS_SESSION_SKS'),
    pythonStringLiterals(ccStore, '_ANALYSIS_SESSION_SKS'),
  );
  assert.deepEqual(
    pythonStringLiterals(agentStore, '_INCIDENT_TERMINAL_STATES'),
    pythonStringLiterals(ccStore, '_INCIDENT_TERMINAL_STATES'),
  );

  const cooldown = (source) =>
    source.match(
      /ACTIVE_INCIDENT_OK_COOLDOWN_SECONDS = int\(os\.environ\.get\("ACTIVE_INCIDENT_OK_COOLDOWN_SECONDS", "(\d+)"\)\)/,
    )?.[1];
  assert.equal(cooldown(agentSettings), cooldown(ccSettings));
  assert.equal(cooldown(agentSettings), '300');
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
      'packages/headless-codex/src/headless_codex/services/artifact_validation.py',
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
      'packages/headless-codex/src/headless_codex/services/artifact_validation.py',
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

// The retrospective is the only actor that may promote a procedure. Later
// analysis may enrich its description without losing that proof, but changing
// the executable procedure creates an unproven revision that must return to
// DRAFT. Each engine enforces this independently, so keep both branches aligned.
test('both engines preserve verification only while procedures are unchanged', async () => {
  const [agentGeneration, agentStore, ccMerge, ccPipeline] = await Promise.all([
    readRepositoryFile('packages/agent/src/rca_agent/services/playbook_gen.py'),
    readRepositoryFile(
      'packages/agent/src/rca_agent/adapters/secondary/playbook/s3_vectors_playbook_store.py',
    ),
    readRepositoryFile(
      'packages/headless-codex/src/headless_codex/services/playbook_merge.py',
    ),
    readRepositoryFile(
      'packages/headless-codex/src/headless_codex/services/pipeline.py',
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

  // Strands compares the final executable steps with the recorded procedure:
  // equal steps retain VERIFIED, while any change invalidates that proof.
  assert.match(
    agentGeneration,
    /verification_status = \(\s*existing\.verification_status\s*if execution_steps == existing\.execution_steps\s*else PlaybookVerificationStatus\.DRAFT\s*\)/,
    'the Strands merge must preserve unchanged procedures and draft changed ones',
  );

  // Headless Codex applies the same comparison after its additive merge.
  assert.match(
    ccPipeline,
    /procedures_unchanged = merged\.get\("execution_steps"\) == existing\.get\("execution_steps"\)/,
    'the CC merge must compare the resulting procedure with the recorded one',
  );
  assert.match(
    ccPipeline,
    /normalize_verification_status\(existing\.get\(VERIFICATION_STATUS_FIELD\)\)\s*if procedures_unchanged\s*else PLAYBOOK_DRAFT/,
    'the CC merge must preserve unchanged procedures and draft changed ones',
  );

  // Reloading a recorded playbook is the other place a promotion disappears:
  // a status the loader does not reconstruct comes back as the default draft.
  assert.match(
    agentStore,
    /verification_status=_as_verification_status\(/,
    'the Strands loader must reconstruct the recorded verification status',
  );

  // Promotion remains a named retrospective operation. Analysis can only retain
  // its result or invalidate it by changing the procedure.
  assert.match(ccMerge, /def promote_to_verified\(/);
});
