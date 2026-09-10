import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';

import {
  evaluateResults,
  evaluateScenario,
  loadResults,
  loadScenarios,
  REPOSITORY_ROOT,
  ROOT_FAULT_TYPES,
  validateScenario,
} from './evaluator.mjs';
import { projectIncidentCaptures } from './scenario-capture-boundary.mjs';

const archive = path.join(
  REPOSITORY_ROOT,
  'tests/fixtures/historical/original-four',
);
const expectedTypes = {
  'exception-session-cleanup': 'db-leak',
  'maintenance-transaction-lock': 'unsupported',
  'pool-config-regression': 'unsupported',
  'query-amplification': 'slow-query',
};

// In-memory evaluator probes ONLY. Never serialize these as model fixtures.
// This isolates each rejection rule with otherwise valid structure.
function structuralProbe(scenario) {
  return {
    schemaVersion: 2,
    scenarioId: scenario.id,
    engine: 'strands',
    rootCause: 'Unit-test structural probe, not a model conclusion.',
    rootCauseConfirmed: true,
    rootFaultType: scenario.expectation.acceptedRootFaultTypes[0],
    rootCauseEvidenceIds: scenario.expectation.requiredRootCauseEvidenceIds,
    evidenceIds: scenario.expectation.requiredEvidenceIds,
    artifacts: scenario.expectation.requiredArtifacts,
    competingCauseJudgments: scenario.expectation.competingCauses.map(
      (cause) => ({
        causeId: cause.id,
        judgment: 'rejected',
        evidenceIds: cause.requiredEvidenceIds,
        rationale: 'Unit-test judgment, not model evidence.',
      }),
    ),
    remediation: {
      summary: 'Unit-test procedure, not an executed remediation.',
      available: true,
      safe: true,
      unsafeSteps: [],
      verificationStatus: 'DRAFT',
      executionSteps: [
        {
          stepId: 'restore',
          intent: 'Restore the recorded original state.',
          action: 'Use the owner-approved recovery procedure.',
          successCriteria: 'Verify service recovery under the same load.',
        },
      ],
      safeguards: {
        preconditions: 'Confirm recorded ownership and original state.',
        approval: 'Require the service owner.',
        rollback: 'Stop and restore the recorded original state on failure.',
        verification: 'Compare service symptoms under identical load.',
      },
    },
  };
}

test('active catalog distinguishes measured local inputs from illustrative alarm envelopes', async () => {
  const scenarios = await loadScenarios();
  assert.deepEqual(
    scenarios.map(({ id }) => id),
    Object.keys(expectedTypes),
  );
  for (const scenario of scenarios) {
    validateScenario(scenario);
    assert.deepEqual(scenario.executionModes, ['model-eval']);
    assert.equal(scenario.provenance.kind, 'local-postgresql');
    assert.equal(scenario.provenance.awsMeasured, false);
    assert.equal(
      scenario.provenance.calibration,
      'local-mechanism-measured-alarm-pending',
    );
    assert.equal(
      scenario.provenance.alarmEnvelope,
      'synthetic-illustrative-not-an-observed-aws-alarm',
    );
    assert.equal(scenario.alarm.namespace, 'Healthcare/Sensor');
    assert.deepEqual(scenario.alarm.dimensions, {
      ServiceName: 'healthcare-sensor-app',
    });
    assert.equal(scenario.alarm.period, 60);
    assert.equal(scenario.alarm.evaluationPeriods, 2);
    assert.equal(Object.hasOwn(scenario.alarm, 'threshold'), false);
    assert.equal(Object.hasOwn(scenario.alarm, 'stateChangeTime'), false);
    assert.deepEqual(scenario.expectation.acceptedRootFaultTypes, [
      expectedTypes[scenario.id],
    ]);
    assert.equal(scenario.expectation.requireConfirmedRootCause, true);
    assert.equal(scenario.expectation.requireExecutableRemediation, true);
    assert.deepEqual(scenario.expectation.requiredArtifacts, [
      'scoping',
      'hypotheses',
      'validation',
      'report',
      'playbook',
    ]);
    assert.ok(scenario.expectation.competingCauses.length >= 2);
    assert.ok(scenario.expectation.requiredRootCauseEvidenceIds.length >= 4);
    for (const observation of scenario.observations) {
      assert.match(observation.id, /^obs-\d{2}$/);
      const raw = JSON.parse(observation.summary);
      assert.ok(
        ['local-postgresql-measurement', 'repository-source-snapshot'].includes(
          raw.provenance,
        ),
      );
      assert.ok(Date.parse(raw.window.start) < Date.parse(raw.window.end));
      assert.deepEqual(
        raw.records.map((record) => record.context),
        ['baseline', 'incident'],
      );
      assert.equal(raw.window.end, scenario.provenance.incidentCutoff);
      assert.ok(raw.resource.runId);
      assert.ok(raw.resource.schema);
      assert.deepEqual(Object.keys(raw.source), ['captureId']);
      assert.match(raw.source.captureId, /^capture-\d{4}$/);
      assert.ok(Object.keys(raw.units).length > 0);
      assert.ok(raw.records.length > 0);
      assert.doesNotMatch(
        observation.summary,
        /FAULT_|rootFaultType|acceptedRootFaultTypes|phases\[|case=|phase=|"phase"|"case"|restored_write|release_events|bounded_hold_events|rollback_complete|owned_sessions_after_dispose|checked_out_before_dispose|remaining_owned_schema_locks|proof-time-aligned|operator-capture-map/,
      );
    }
  }
});

async function measuredInputs(scenario) {
  const bytes = await readFile(
    path.join(REPOSITORY_ROOT, scenario.provenance.source),
  );
  assert.equal(
    createHash('sha256').update(bytes).digest('hex'),
    scenario.provenance.sourceSha256,
  );
  const directory = path.dirname(
    path.join(REPOSITORY_ROOT, scenario.provenance.source),
  );
  const snippets = JSON.parse(
    await readFile(path.join(directory, 'source-snippets.json'), 'utf8'),
  );
  return { proof: JSON.parse(bytes), snippets: snippets[scenario.id] };
}

test('catalog captures reproduce the reviewed UTC proof projection and neutral operator mapping', async () => {
  for (const scenario of await loadScenarios()) {
    const { proof, snippets } = await measuredInputs(scenario);
    assert.equal(proof.checks.utc_phase_windows, true);
    assert.equal(proof.checks.utc_operation_windows, true);
    assert.equal(proof.checks.identical_query_inputs, true);
    assert.equal(proof.checks.all_four_mechanisms, true);
    assert.equal(proof.schema_removed, true);
    assert.deepEqual(proof.cleanup_errors, []);
    const projected = projectIncidentCaptures(scenario.id, proof, snippets);
    assert.deepEqual(scenario.observations, projected.observations);
    const manifest = JSON.parse(
      await readFile(
        path.join(REPOSITORY_ROOT, scenario.provenance.operatorMapping),
        'utf8',
      ),
    );
    assert.deepEqual(
      manifest.scenarios[scenario.id].captures,
      projected.operatorMapping,
    );
    for (const pointers of Object.values(projected.operatorMapping)) {
      assert.ok(pointers.length > 0);
      for (const pointer of pointers) {
        assert.match(pointer, /^\/phases\/\d+\//);
        assert.notEqual(
          pointer
            .slice(1)
            .split('/')
            .reduce((value, key) => value?.[key], proof),
          undefined,
        );
      }
    }
  }
});

test('restoration, shutdown results, operator labels and later events cannot change model captures', async () => {
  for (const scenario of await loadScenarios()) {
    const { proof, snippets } = await measuredInputs(scenario);
    const expected = projectIncidentCaptures(
      scenario.id,
      proof,
      snippets,
    ).observations;
    const changed = structuredClone(proof);
    changed.checks = { fabricatedPass: 'OPERATOR_ONLY_SENTINEL' };
    changed.local_query_calibration = 'OPERATOR_ONLY_SENTINEL';
    changed.schema_removed = false;
    changed.cleanup_errors = ['OPERATOR_ONLY_SENTINEL'];
    for (const phase of changed.phases) {
      if (phase.phase === 'restore') {
        for (const key of Object.keys(phase).filter(
          (key) => !['case', 'phase'].includes(key),
        )) {
          phase[key] = 'OPERATOR_ONLY_SENTINEL';
        }
        continue;
      }
      phase.completed_at = 'OPERATOR_ONLY_SENTINEL';
      phase.checked_out_before_dispose = 'OPERATOR_ONLY_SENTINEL';
      phase.owned_sessions_after_dispose = 'OPERATOR_ONLY_SENTINEL';
      phase.remaining_owned_schema_locks = 'OPERATOR_ONLY_SENTINEL';
      phase.groundTruth = 'OPERATOR_ONLY_SENTINEL';
      if (phase.case === 'lock' && phase.phase === 'fault') {
        for (const key of [
          'restored_write',
          'release_events',
          'maintenance_exit_code',
          'bounded_hold_events',
          'checked_out_before_dispose',
        ]) {
          phase[key] = 'OPERATOR_ONLY_SENTINEL';
        }
        phase.events.push({
          event: 'db_operation',
          operation: 'restored_write',
          result: 'OPERATOR_ONLY_SENTINEL',
        });
      }
    }
    const actual = projectIncidentCaptures(
      scenario.id,
      changed,
      snippets,
    ).observations;
    assert.deepEqual(actual, expected);
    assert.doesNotMatch(JSON.stringify(actual), /OPERATOR_ONLY_SENTINEL/);
  }
});

test('lock incident cutoff precedes release and excludes that incident post-recovery request', async () => {
  const scenario = (await loadScenarios()).find(
    ({ id }) => id === 'maintenance-transaction-lock',
  );
  const { proof } = await measuredInputs(scenario);
  const incident = proof.phases.find(
    (phase) => phase.case === 'lock' && phase.phase === 'fault',
  );
  assert.equal(
    scenario.provenance.incidentCutoff,
    incident.blocked_write.completed_at,
  );
  assert.ok(
    Date.parse(scenario.provenance.incidentCutoff) <
      Date.parse(incident.release_events[0].released_at),
  );
  const text = JSON.stringify(scenario.observations);
  assert.ok(text.includes(incident.blocked_write.operation.request_id));
  assert.ok(!text.includes(incident.restored_write.operation.request_id));
  assert.ok(!text.includes(incident.release_events[0].released_at));
});

test('capture construction refuses missing UTC evidence rather than inventing dates', async () => {
  const scenario = (await loadScenarios()).find(
    ({ id }) => id === 'pool-config-regression',
  );
  const { proof, snippets } = await measuredInputs(scenario);
  const incident = proof.phases.find(
    (phase) => phase.case === 'pool' && phase.phase === 'fault',
  );
  delete incident.measurement_completed_at;
  assert.throws(
    () => projectIncidentCaptures(scenario.id, proof, snippets),
    /recorded UTC timestamp/,
  );
});

test('illustrative drafts remain labeled and the unsuccessful first DB run is preserved', async () => {
  const drafts = await loadScenarios(
    path.join(REPOSITORY_ROOT, 'tests/fixtures/historical/illustrative-drafts'),
  );
  assert.equal(drafts.length, 4);
  for (const draft of drafts) {
    assert.equal(draft.provenance.kind, 'synthetic-illustrative');
    assert.equal(draft.provenance.awsMeasured, false);
  }
  const incomplete = JSON.parse(
    await readFile(
      path.join(
        REPOSITORY_ROOT,
        'tests/fixtures/observations/realistic-local-20260910/proof-01.json.txt',
      ),
      'utf8',
    ),
  );
  assert.equal(Object.hasOwn(incomplete, 'checks'), false);
  assert.ok(
    incomplete.phases.some((phase) =>
      phase.queries?.some((query) => query.outcome === 'error'),
    ),
  );
});

test('every realistic case rejects wrong type, missing confirmation and root evidence', async () => {
  for (const scenario of await loadScenarios()) {
    const probe = structuralProbe(scenario);
    assert.equal(evaluateScenario(scenario, probe).passed, true);
    for (const rootFaultType of ROOT_FAULT_TYPES.filter(
      (type) => !scenario.expectation.acceptedRootFaultTypes.includes(type),
    )) {
      assert.equal(
        evaluateScenario(scenario, { ...probe, rootFaultType }).dimensions
          .rootCauseIdentified,
        false,
        `${scenario.id} must reject ${rootFaultType}`,
      );
    }
    assert.equal(
      evaluateScenario(scenario, { ...probe, rootCauseConfirmed: false })
        .passed,
      false,
    );
    for (const omitted of probe.rootCauseEvidenceIds) {
      const result = {
        ...probe,
        rootCauseEvidenceIds: probe.rootCauseEvidenceIds.filter(
          (id) => id !== omitted,
        ),
      };
      const evaluated = evaluateScenario(scenario, result);
      assert.equal(evaluated.dimensions.rootCauseIdentified, false);
      assert.equal(evaluated.dimensions.evidenceLinked, true);
      assert.equal(evaluated.passed, false);
    }
  }
});

test('realistic alternatives cannot borrow another rejection or global citations', async () => {
  for (const scenario of await loadScenarios()) {
    const probe = structuralProbe(scenario);
    const [first, second] = probe.competingCauseJudgments;
    for (const judgments of [
      [],
      [
        { ...first, evidenceIds: second.evidenceIds },
        { ...second, evidenceIds: first.evidenceIds },
      ],
      [
        {
          ...first,
          evidenceIds: [...first.evidenceIds, ...second.evidenceIds],
        },
      ],
      [{ ...first, judgment: 'inconclusive' }, second],
      [{ ...first, evidenceIds: [] }, second],
    ]) {
      const result = evaluateScenario(scenario, {
        ...probe,
        competingCauseJudgments: judgments,
      });
      assert.equal(result.dimensions.evidenceLinked, true);
      assert.equal(result.dimensions.competingCausesRejected, false);
      assert.equal(result.passed, false);
    }
    const overlap = structuredClone(scenario);
    overlap.expectation.competingCauses[1].requiredEvidenceIds.push(
      overlap.expectation.competingCauses[0].requiredEvidenceIds[0],
    );
    assert.throws(() => validateScenario(overlap), /pairwise disjoint/);
  }
});

test('realistic catalog still requires all artifacts and safe executable draft remediation', async () => {
  for (const scenario of await loadScenarios()) {
    const probe = structuralProbe(scenario);
    for (const artifact of probe.artifacts) {
      assert.equal(
        evaluateScenario(scenario, {
          ...probe,
          artifacts: probe.artifacts.filter((value) => value !== artifact),
        }).passed,
        false,
      );
    }
    for (const change of [
      { available: false },
      { safe: false },
      { unsafeSteps: ['restore'] },
      { executionSteps: [] },
      { verificationStatus: 'VERIFIED' },
    ]) {
      assert.equal(
        evaluateScenario(scenario, {
          ...probe,
          remediation: { ...probe.remediation, ...change },
        }).dimensions.remediationSafe,
        false,
      );
    }
  }
});

test('historical results cannot earn credit for any replacement scenario', async () => {
  const scenarios = await loadScenarios();
  const results = await loadResults(path.join(archive, 'results'));
  const report = await evaluateResults({
    scenarios,
    results,
    digest: { digest: 'unused', inputFiles: [] },
  });
  assert.equal(report.passed, false);
  assert.equal(report.evaluations.length, 0);
  assert.equal(
    report.failures.filter((failure) => failure.startsWith('missing result:'))
      .length,
    8,
  );
  assert.equal(
    report.failures.filter((failure) =>
      failure.startsWith('unexpected scenario result:'),
    ).length,
    8,
  );
  await assert.rejects(
    loadResults(path.join(REPOSITORY_ROOT, 'tests/fixtures/results')),
    /no result JSON files/,
  );
});

test('archived inputs and normalized snapshots match their recorded original bytes', async () => {
  const manifest = await readFile(path.join(archive, 'README.md'), 'utf8');
  const entries = [
    ...manifest.matchAll(/\| `[^`]+` \| `([^`]+)` \| `([a-f0-9]{64})` \|/g),
  ];
  assert.equal(entries.length, 12);
  for (const [, relative, expected] of entries) {
    const bytes = await readFile(path.join(archive, relative));
    assert.equal(
      createHash('sha256').update(bytes).digest('hex'),
      expected,
      relative,
    );
  }
});
