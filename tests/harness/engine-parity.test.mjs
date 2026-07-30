import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';

import { REPOSITORY_ROOT } from './evaluator.mjs';

const AGENT_DESTRUCTIVE =
  'packages/agent/src/rca_agent/services/destructive_actions.py';
const CC_DESTRUCTIVE =
  'packages/cc-headless/src/cc_headless/services/destructive_actions.py';

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
  // exercised the procedure, so DRAFT must be the only status analysis can write.
  assert.match(ccValidation, /_PLAYBOOK_DRAFT_STATUS = "DRAFT"/);

  const statusEnum = agentModels.slice(
    agentModels.indexOf('class PlaybookVerificationStatus(StrEnum):'),
    agentModels.indexOf('class AlarmTrigger(BaseModel):'),
  );
  const members = [...statusEnum.matchAll(/^    (\w+) = "([^"]+)"$/gm)];
  assert.deepEqual(
    members.map(([, name, value]) => [name, value]),
    [['DRAFT', 'DRAFT']],
  );
});
