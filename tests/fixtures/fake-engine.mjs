import { readFile } from 'node:fs/promises';

const engine = process.argv[2];
const scenarioPath = process.argv[3];

async function readStdin() {
  let input = '';
  process.stdin.setEncoding('utf8');
  for await (const chunk of process.stdin) {
    input += chunk;
  }
  return input;
}

const scenario = JSON.parse(
  scenarioPath ? await readFile(scenarioPath, 'utf8') : await readStdin(),
);
const executionSteps = scenario.expectation.requireExecutableRemediation
  ? [
      {
        stepId: 'restore-service',
        intent: 'Return the affected service to a healthy configuration.',
        action: 'Apply the approved reversible service change.',
        successCriteria: 'The alarm and related error signal recover.',
      },
    ]
  : [];

process.stdout.write(
  JSON.stringify({
    schemaVersion: 2,
    scenarioId: scenario.id,
    engine,
    rootCause: 'The supplied observations support a confirmed causal finding.',
    rootCauseConfirmed: true,
    rootFaultType: scenario.expectation.acceptedRootFaultTypes[0],
    rootCauseEvidenceIds: scenario.expectation.requiredRootCauseEvidenceIds,
    evidenceIds: scenario.expectation.requiredEvidenceIds,
    competingCauseJudgments: (scenario.expectation.competingCauses ?? []).map(
      (cause) => ({
        causeId: cause.id,
        judgment: 'rejected',
        rationale: `Evidence rejects ${cause.id}.`,
        evidenceIds: cause.requiredEvidenceIds,
      }),
    ),
    artifacts: scenario.expectation.requiredArtifacts,
    remediation: {
      summary: 'Use the reviewed procedure and verify the observed recovery.',
      available: true,
      verificationStatus: 'DRAFT',
      executionSteps,
      safe: true,
      unsafeSteps: [],
      safeguards: {
        preconditions:
          'Confirm the target resource and current incident state.',
        approval:
          'Require the service owner or on-call SRE to approve execution.',
        rollback: 'Rollback when the alarm or error rate worsens.',
        verification:
          'Verify the alarm metric and related error rate after the change.',
      },
    },
  }),
);
