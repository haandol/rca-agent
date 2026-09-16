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
const awsCaptureDirectory = path.join(
  REPOSITORY_ROOT,
  'tests/fixtures/observations/aws-maintenance-20260910T063725Z',
);
const isAwsCapture = (scenario) => scenario.provenance.kind === 'aws-incident';
const localPredecessor = async () =>
  JSON.parse(
    await readFile(
      path.join(awsCaptureDirectory, 'localPredecessor.json.txt'),
      'utf8',
    ),
  );

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

test('historical catalog distinguishes measured AWS maintenance from local illustrative alarm inputs', async () => {
  const scenarios = await loadScenarios(
    path.join(REPOSITORY_ROOT, 'tests/scenarios/history'),
  );
  assert.deepEqual(
    scenarios.map(({ id }) => id),
    Object.keys(expectedTypes),
  );
  for (const scenario of scenarios) {
    validateScenario(scenario);
    assert.deepEqual(scenario.executionModes, ['model-eval']);
    const aws = scenario.id === 'maintenance-transaction-lock';
    assert.equal(
      scenario.provenance.kind,
      aws ? 'aws-incident' : 'local-postgresql',
    );
    assert.equal(scenario.provenance.awsMeasured, aws);
    assert.equal(
      scenario.provenance.calibration,
      aws
        ? 'observed-alarm-transition-not-a-recovery-verdict'
        : 'local-mechanism-measured-alarm-pending',
    );
    assert.equal(
      scenario.provenance.alarmEnvelope,
      aws
        ? 'observed-aws-alarm-transition'
        : 'synthetic-illustrative-not-an-observed-aws-alarm',
    );
    assert.equal(scenario.alarm.namespace, 'Healthcare/Sensor');
    assert.deepEqual(scenario.alarm.dimensions, {
      ServiceName: 'healthcare-sensor-app',
    });
    assert.equal(scenario.alarm.period, 60);
    assert.equal(scenario.alarm.evaluationPeriods, 2);
    assert.equal(Object.hasOwn(scenario.alarm, 'threshold'), aws);
    assert.equal(Object.hasOwn(scenario.alarm, 'stateChangeTime'), aws);
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
        (aws
          ? ['aws-incident-measurement']
          : ['local-postgresql-measurement', 'repository-source-snapshot']
        ).includes(raw.provenance),
      );
      assert.ok(Date.parse(raw.window.start) < Date.parse(raw.window.end));
      if (aws) {
        assert.ok(
          raw.records.every((record) =>
            ['baseline', 'incident'].includes(record.context),
          ),
        );
        assert.equal(raw.resource.region, 'us-east-1');
        assert.ok(raw.resource.cluster);
      } else {
        assert.deepEqual(
          raw.records.map((record) => record.context),
          ['baseline', 'incident'],
        );
      }
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
  if (isAwsCapture(scenario)) {
    const proof = JSON.parse(bytes);
    const snippets = Object.fromEntries(
      await Promise.all(
        Object.entries(proof.files).map(async ([key, descriptor]) => [
          key,
          await readFile(path.join(directory, descriptor.file)),
        ]),
      ),
    );
    return { proof, snippets };
  }
  const snippets = JSON.parse(
    await readFile(path.join(directory, 'source-snippets.json'), 'utf8'),
  );
  return { proof: JSON.parse(bytes), snippets: snippets[scenario.id] };
}

test('catalog captures reproduce the reviewed UTC proof projection and neutral operator mapping', async () => {
  for (const scenario of [
    ...(await loadScenarios(
      path.join(REPOSITORY_ROOT, 'tests/scenarios/history'),
    )),
    await localPredecessor(),
  ]) {
    const { proof, snippets } = await measuredInputs(scenario);
    if (!isAwsCapture(scenario)) {
      assert.equal(proof.checks.utc_phase_windows, true);
      assert.equal(proof.checks.utc_operation_windows, true);
      assert.equal(proof.checks.identical_query_inputs, true);
      assert.equal(proof.checks.all_four_mechanisms, true);
      assert.equal(proof.schema_removed, true);
      assert.deepEqual(proof.cleanup_errors, []);
    }
    const projected = projectIncidentCaptures(scenario.id, proof, snippets);
    assert.deepEqual(scenario.observations, projected.observations);
    if (isAwsCapture(scenario))
      assert.deepEqual(scenario.alarm, projected.alarm);
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
        if (isAwsCapture(scenario)) {
          const [key, fragment] = pointer.split('#');
          assert.ok(Object.hasOwn(proof.files, key));
          const source = key.endsWith('Source')
            ? snippets[key].toString()
            : JSON.parse(snippets[key]);
          assert.notEqual(
            fragment
              ? fragment
                  .slice(1)
                  .split('/')
                  .reduce((value, part) => value?.[part], source)
              : source,
            undefined,
          );
          continue;
        }
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
  const local = (
    await loadScenarios(path.join(REPOSITORY_ROOT, 'tests/scenarios/history'))
  ).filter((scenario) => !isAwsCapture(scenario));
  for (const scenario of [...local, await localPredecessor()]) {
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

test('archived local lock cutoff still excludes its post-recovery request', async () => {
  const scenario = await localPredecessor();
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

async function awsInputs() {
  const scenario = (
    await loadScenarios(path.join(REPOSITORY_ROOT, 'tests/scenarios/history'))
  ).find(({ id }) => id === 'maintenance-transaction-lock');
  return { scenario, ...(await measuredInputs(scenario)) };
}

// Rehash deliberately modified fixture copies so selector tests reach past integrity validation.
function replaceCapture(proof, snippets, key, change) {
  const value = JSON.parse(snippets[key].toString());
  change(value);
  snippets[key] = Buffer.from(JSON.stringify(value));
  proof.files[key].sha256 = createHash('sha256')
    .update(snippets[key])
    .digest('hex');
}

test('AWS maintenance binds real PID, task ownership and installed rollback source without changing gates', async () => {
  const { scenario, proof, snippets } = await awsInputs();
  assert.deepEqual(
    scenario.expectation,
    (await localPredecessor()).expectation,
  );
  assert.deepEqual(scenario.executionModes, ['model-eval']);
  const projected = projectIncidentCaptures(scenario.id, proof, snippets);
  const observations = projected.observations.map(({ summary }) =>
    JSON.parse(summary),
  );
  const incident = observations[1].records[0];
  assert.equal(incident.maintenance.backend_pid, 5489);
  assert.equal(incident.maintenance.run_id, proof.runId);
  assert.equal(incident.identity.TaskARN, incident.ownedTask.taskArn);
  assert.equal(incident.ownedTask.lastStatus, 'RUNNING');
  assert.match(
    incident.ownedTask.taskArn,
    /\/ca14a8288c8b46b8a7460c3800a7932b$/,
  );
  const owner = incident.snapshot.record.activity.find(
    ({ pid }) => pid === 5489,
  );
  assert.equal(owner.application_name, `healthcare-maint-${proof.runId}`);
  const blocked = incident.snapshot.record.activity.find(
    ({ pid }) => pid === 5212,
  );
  assert.deepEqual(blocked.blocking_pids, [5489]);
  assert.equal(blocked.wait_event, 'relation');
  const lifetime = observations[4].records[0].observations;
  assert.deepEqual(
    lifetime.map(({ record }) => record.pool_checked_out),
    [1, 0],
  );
  assert.ok(
    lifetime[1].record.locks.some(
      ({ pid, granted }) => pid === 5489 && granted,
    ),
  );
  const source = observations[2].records[1].sourceExcerpt;
  assert.equal(source.sha256, proof.files.maintenanceSource.sha256);
  const text = source.lines.map(({ text: line }) => line).join('\n');
  assert.match(
    text,
    /loop\.add_signal_handler\(signum, request_stop, signum\)/,
  );
  assert.match(text, /stop\.set\(\)/);
  assert.match(text, /await transaction\.rollback\(\)/);
  assert.match(text, /await conn\.close\(timeout=5\)/);
  assert.ok(!JSON.stringify(observations).includes('local_c1cf824ddbfd4bdb'));
  assert.ok(!JSON.stringify(observations).includes('rollback_complete'));
});

test('AWS captures retain event time even when fetched later and exclude incomplete metric periods', async () => {
  const { scenario, proof, snippets } = await awsInputs();
  const blockers = JSON.parse(snippets.blockers);
  assert.ok(
    Date.parse(blockers.ResponseMetadata.HTTPHeaders.date) >
      Date.parse(proof.incidentCutoff),
  );
  assert.ok(
    blockers.events.every(
      ({ timestamp }) => timestamp < Date.parse(proof.incidentCutoff),
    ),
  );
  const { observations, alarm } = projectIncidentCaptures(
    scenario.id,
    proof,
    snippets,
  );
  assert.equal(alarm.stateChangeTime, '2026-09-10T15:41:40.992000+09:00');
  assert.ok(
    Date.parse(alarm.stateChangeTime) < Date.parse(proof.incidentCutoff),
  );
  assert.equal(Object.hasOwn(alarm, 'datapointsToAlarm'), false);
  const metrics = JSON.parse(observations[0].summary).records;
  for (const record of metrics) {
    for (const series of Object.values(record.metrics)) {
      for (const point of series.Datapoints) {
        assert.ok(
          Date.parse(point.Timestamp) + series.periodSeconds * 1000 <=
            Date.parse(proof.incidentCutoff),
        );
      }
    }
  }
  const before = JSON.stringify(observations);
  replaceCapture(proof, snippets, 'statusIncident', (document) => {
    for (const series of Object.values(document.data.metrics.values)) {
      series.Datapoints.push({
        Timestamp: '2026-09-10T06:41:00Z',
        Sum: 'INCOMPLETE_PERIOD_SENTINEL',
        Unit: 'Count',
      });
    }
  });
  assert.equal(
    JSON.stringify(
      projectIncidentCaptures(scenario.id, proof, snippets).observations,
    ),
    before,
  );
});

test('AWS projection ignores operator cleanup, later task state, recovery metrics and retrospective labels', async () => {
  const { scenario, proof, snippets } = await awsInputs();
  const original = projectIncidentCaptures(scenario.id, proof, snippets);
  for (const key of ['baseline', 'statusBefore', 'statusIncident']) {
    replaceCapture(proof, snippets, key, (document) => {
      for (const field of [
        'checks',
        'recoveryVerified',
        'scenarioSuccess',
        'maintenanceRestoration',
        'revisionObservation',
      ]) {
        document.data[field] = 'OPERATOR_ONLY_SENTINEL';
      }
      document.data.groundTruth = 'OPERATOR_ONLY_SENTINEL';
    });
  }
  replaceCapture(proof, snippets, 'alarmStatus', (document) => {
    for (const field of Object.keys(document.data).filter(
      (key) => key !== 'alarms',
    )) {
      document.data[field] = 'OPERATOR_ONLY_SENTINEL';
    }
  });
  for (const key of ['maintenanceLogs', 'blockers']) {
    replaceCapture(proof, snippets, key, (document) => {
      document.events.push({
        timestamp: Date.parse(proof.incidentCutoff) + 60_000,
        message: JSON.stringify({
          event: 'maintenance_released',
          rollback_complete: true,
          verdict: 'OPERATOR_ONLY_SENTINEL',
        }),
      });
      document.retrospective = 'OPERATOR_ONLY_SENTINEL';
      document.cleanup = 'OPERATOR_ONLY_SENTINEL';
    });
  }
  const projected = projectIncidentCaptures(scenario.id, proof, snippets);
  assert.deepEqual(projected, original);
  assert.doesNotMatch(
    JSON.stringify(projected.observations),
    /OPERATOR_ONLY_SENTINEL/,
  );
});

test('AWS projection rejects missing or conflicting incident ownership, source and cutoff evidence', async () => {
  const cases = [
    [
      'statusBefore',
      (value) => {
        value.data.maintenanceTasks[0].tags = [];
      },
    ],
    [
      'statusIncident',
      (value) => {
        value.data.maintenanceTasks[0].startedBy = 'foreign';
      },
    ],
    [
      'statusIncident',
      (value) => {
        value.data.maintenanceTasks[0].taskDefinitionArn += '-foreign';
      },
    ],
    [
      'statusIncident',
      (value) => {
        value.data.maintenanceTasks[0].lastStatus = 'STOPPED';
      },
    ],
    [
      'statusBefore',
      (value) => {
        value.at = '2026-09-10T06:42:00Z';
      },
    ],
    [
      'definition',
      (value) => {
        value.data.response.taskDefinition.containerDefinitions[0].command = [
          'unobserved',
        ];
      },
    ],
    [
      'maintenanceLogs',
      (value) => {
        const event = value.events.find(
          ({ message }) => JSON.parse(message).event === 'ecs_runtime_identity',
        );
        const record = JSON.parse(event.message);
        record.TaskARN += '-foreign';
        event.message = JSON.stringify(record);
      },
    ],
    [
      'maintenanceLogs',
      (value) => {
        const event = value.events.find(
          ({ message }) => JSON.parse(message).event === 'source_manifest',
        );
        const record = JSON.parse(event.message);
        record.files['maintenance.py'] = '0'.repeat(64);
        event.message = JSON.stringify(record);
      },
    ],
    [
      'blockers',
      (value) => {
        const record = JSON.parse(value.events[0].message);
        record.activity.find(({ pid }) => pid === 5489).application_name =
          'foreign';
        value.events[0].message = JSON.stringify(record);
      },
    ],
    [
      'blockers',
      (value) => {
        value.events[0].timestamp = Date.parse('2026-09-10T06:42:00Z');
      },
    ],
    [
      'alarmStatus',
      (value) => {
        value.data.alarms.find(
          ({ MetricName }) => MetricName === 'VitalIngestFailures',
        ).StateTransitionedTimestamp = '2026-09-10T06:42:00Z';
      },
    ],
  ];
  for (const [key, change] of cases) {
    const { scenario, proof, snippets } = await awsInputs();
    replaceCapture(proof, snippets, key, change);
    assert.throws(
      () => projectIncidentCaptures(scenario.id, proof, snippets),
      undefined,
      key,
    );
  }
  const { scenario, proof, snippets } = await awsInputs();
  snippets.blockers = Buffer.concat([snippets.blockers, Buffer.from(' ')]);
  assert.throws(
    () => projectIncidentCaptures(scenario.id, proof, snippets),
    /capture hash mismatch: blockers/,
  );
});

test('capture construction refuses missing UTC evidence rather than inventing dates', async () => {
  const scenario = (
    await loadScenarios(path.join(REPOSITORY_ROOT, 'tests/scenarios/history'))
  ).find(({ id }) => id === 'pool-config-regression');
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
  for (const scenario of await loadScenarios(
    path.join(REPOSITORY_ROOT, 'tests/scenarios/history'),
  )) {
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
  for (const scenario of await loadScenarios(
    path.join(REPOSITORY_ROOT, 'tests/scenarios/history'),
  )) {
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
  for (const scenario of await loadScenarios(
    path.join(REPOSITORY_ROOT, 'tests/scenarios/history'),
  )) {
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
  const scenarios = await loadScenarios(
    path.join(REPOSITORY_ROOT, 'tests/scenarios/history'),
  );
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

test('single active native-column fixture uses fresh local proof and existing unsupported type', async () => {
  const scenarios = await loadScenarios();
  assert.deepEqual(
    scenarios.map((item) => item.id),
    ['write-column-regression'],
  );
  const [scenario] = scenarios;
  assert.equal(scenario.provenance.kind, 'local-postgresql');
  assert.equal(scenario.provenance.awsMeasured, false);
  assert.equal(
    scenario.provenance.status,
    'incident-input-ready-aws-capture-pending',
  );
  assert.deepEqual(scenario.expectation.acceptedRootFaultTypes, [
    'unsupported',
  ]);
  const probe = structuralProbe(scenario);
  assert.equal(evaluateScenario(scenario, probe).passed, true);
  for (const change of [
    { rootCauseConfirmed: false },
    { rootCauseEvidenceIds: [] },
    { rootFaultType: 'db-leak' },
    { artifacts: ['report'] },
    { remediation: { ...probe.remediation, safe: false } },
  ]) {
    assert.equal(
      evaluateScenario(scenario, { ...probe, ...change }).passed,
      false,
    );
  }
  const results = await loadResults(path.join(archive, 'results'));
  const report = await evaluateResults({
    scenarios,
    results,
    digest: { digest: 'probe', inputFiles: [] },
  });
  assert.equal(report.passed, false);
  assert.equal(report.evaluations.length, 0);
});

/** In-memory synthetic unit data exercises projection rules without claiming DB measurements. */
function nativeProofProbe() {
  const snippets = {};
  const phases = ['normal', 'fault'].map((phase, index) => {
    const text = `TIMESTAMP_COLUMN = "${index ? 'sampled_at' : 'timestamp'}"\n`;
    const files = {
      'revision/write.py': createHash('sha256').update(text).digest('hex'),
    };
    const fileMap =
      '{' +
      Object.entries(files)
        .map(
          ([key, value]) => `${JSON.stringify(key)}: ${JSON.stringify(value)}`,
        )
        .join(', ') +
      '}';
    const source = {
      verified: true,
      revision: index ? 'v2' : 'v1',
      base_fingerprint: 'a'.repeat(64),
      files,
      fingerprint: createHash('sha256').update(fileMap).digest('hex'),
    };
    snippets[index ? 'incident' : 'baseline'] = {
      path: 'revision/write.py',
      text,
    };
    const observed_at = `2026-09-15T12:0${index}:10+00:00`;
    return {
      phase,
      source,
      checked_out: 0,
      events: [
        {
          event: 'db_schema_snapshot',
          observed_at,
          table_name: 'sensor_readings',
          column_names: ['timestamp'],
        },
        {
          event: index ? 'db_write_error' : 'write_completed',
          observed_at,
          sqlstate: index ? '42703' : undefined,
          count: index ? undefined : 1,
        },
      ],
    };
  });
  return {
    snippets,
    proof: {
      run_id: 'synthetic-unit-only',
      schema: 'synthetic_unit',
      boundary: 'local_postgresql_service',
      phases,
      phase_windows: phases.map((phase, index) => ({
        phase: phase.phase,
        case: 'column',
        started_at: `2026-09-15T12:0${index}:00+00:00`,
        completed_at: `2026-09-15T12:0${index}:30+00:00`,
      })),
    },
  };
}

test('new native capture checks fresh time/schema/source and excludes recovery and private payloads', () => {
  const { proof, snippets } = nativeProofProbe();
  const original = projectIncidentCaptures(
    'write-column-regression',
    proof,
    snippets,
  );
  assert.equal(original.observations.length, 5);
  const later = structuredClone(proof);
  later.phases.push({
    phase: 'restore',
    rootCauseConfirmed: true,
    outcome: 'RESOLVED',
  });
  later.cleanup_errors = ['not an incident input'];
  later.checks = { allPassed: true };
  later.phases[1].events[1].parameters = 'PRIVATE_SENTINEL';
  assert.deepEqual(
    projectIncidentCaptures('write-column-regression', later, snippets),
    original,
  );
  assert.doesNotMatch(
    JSON.stringify(original),
    /PRIVATE_SENTINEL|RESOLVED|allPassed/,
  );
  for (const mutate of [
    (p) => {
      p.phases[1].events[1].observed_at = '2026-09-15T12:03:00+00:00';
    },
    (p) => {
      p.phases[0].events = p.phases[0].events.filter(
        (event) => event.event !== 'db_schema_snapshot',
      );
    },
    (p) => {
      p.phases[1].source.fingerprint = '0'.repeat(64);
    },
    (p) => {
      p.phases[1].source.base_fingerprint = 'b'.repeat(64);
    },
    (p) => {
      p.phase_windows[1].completed_at = null;
    },
  ]) {
    const changed = structuredClone(proof);
    mutate(changed);
    assert.throws(() =>
      projectIncidentCaptures('write-column-regression', changed, snippets),
    );
  }
  const changed = structuredClone(snippets);
  changed.incident.text += '# wrong bytes';
  assert.throws(() =>
    projectIncidentCaptures('write-column-regression', proof, changed),
  );
});

test('active native-column observations reproduce supplied actual PostgreSQL proof bytes', async () => {
  const [scenario] = await loadScenarios();
  const bytes = await readFile(
    path.join(REPOSITORY_ROOT, scenario.provenance.source),
  );
  assert.equal(
    createHash('sha256').update(bytes).digest('hex'),
    scenario.provenance.sourceSha256,
  );
  const proof = JSON.parse(bytes);
  const directory = path.dirname(
    path.join(REPOSITORY_ROOT, scenario.provenance.source),
  );
  const snippets = {
    baseline: {
      path: 'revision/write.py',
      text: await readFile(path.join(directory, 'normal-write.py'), 'utf8'),
    },
    incident: {
      path: 'revision/write.py',
      text: await readFile(path.join(directory, 'fault-write.py'), 'utf8'),
    },
  };
  const projection = projectIncidentCaptures(scenario.id, proof, snippets);
  assert.deepEqual(projection.observations, scenario.observations);
  assert.equal(projection.cutoff, scenario.provenance.incidentCutoff);
  const normal = proof.phases.find((phase) => phase.phase === 'normal');
  const fault = proof.phases.find((phase) => phase.phase === 'fault');
  assert.equal(normal.rows_after - normal.rows_before, 6);
  assert.equal(fault.rows_after - fault.rows_before, 0);
  assert.ok(
    fault.events.some(
      (event) => event.event === 'db_write_error' && event.sqlstate === '42703',
    ),
  );
  assert.deepEqual(
    normal.schema_snapshot.column_names,
    fault.schema_snapshot.column_names,
  );
  assert.ok(normal.schema_snapshot.column_names.includes('timestamp'));
  assert.ok(!normal.schema_snapshot.column_names.includes('sampled_at'));
  assert.ok(
    normal.events.some(
      (event) =>
        event.event === 'write_completed' &&
        event.schema_name === null &&
        event.completion_semantics === 'committed_rows',
    ),
  );
  assert.ok(fault.read_status === 200 && fault.health.db_connected === true);
  const changed = structuredClone(proof);
  changed.phases.find((phase) => phase.phase === 'restore').events = [
    { rootCauseConfirmed: true },
  ];
  changed.checks = {};
  changed.schema_removed = false;
  assert.deepEqual(
    projectIncidentCaptures(scenario.id, changed, snippets),
    projection,
  );
  const [synthetic] = await loadScenarios(
    path.join(REPOSITORY_ROOT, 'tests/scenarios/history/synthetic'),
  );
  assert.equal(synthetic.provenance.kind, 'synthetic-illustrative');
  assert.equal(synthetic.provenance.awsMeasured, false);
});
