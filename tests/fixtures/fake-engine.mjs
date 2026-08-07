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
const firstTerms = (groups) => groups.map(([term]) => term).join(' ');

process.stdout.write(
  JSON.stringify({
    schemaVersion: 1,
    scenarioId: scenario.id,
    engine,
    rootCause: `${firstTerms(scenario.expectation.rootCauseTermGroups)} ${firstTerms(
      scenario.expectation.semanticTermGroups,
    )}`,
    rootCauseConfirmed: true,
    evidenceIds: scenario.expectation.requiredEvidenceIds,
    artifacts: scenario.expectation.requiredArtifacts,
    remediation: {
      summary: `${firstTerms(
        scenario.expectation.remediationTermGroups,
      )} ${firstTerms(scenario.expectation.semanticTermGroups)}`,
      safe: true,
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
