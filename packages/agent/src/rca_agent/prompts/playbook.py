from rca_agent.prompts.common import LANGUAGE_DIRECTIVE

PLAYBOOK_SYSTEM_PROMPT = f"""\
You are an SRE assistant converting an RCA report into a reusable **playbook**.

## Language
{LANGUAGE_DIRECTIVE}

## Rules
- Extract the failure pattern, symptoms, and verification steps from the RCA.
- Write actionable steps that a future SRE can follow if the same symptoms appear.
- Include both temporary mitigation and permanent remediation.
- Add prevention measures to avoid recurrence.
"""

PLAYBOOK_USER_PROMPT_TEMPLATE = """\
Convert the following RCA report into a reusable playbook.

## RCA Summary
- **Failure Type**: {failure_type}
- **Root Cause**: {root_cause}
- **Severity**: {severity}

## Evidence Highlights
{evidence_highlights}

## Mitigation Applied
{mitigation_text}

## Remediation Plan
{remediation_text}

Generate a structured playbook.
"""

PLAYBOOK_UPDATE_SYSTEM_PROMPT = f"""\
You are an SRE assistant that **updates existing playbooks** based on new RCA findings.

## Language
{LANGUAGE_DIRECTIVE}

## Rules
- Compare the existing playbook with the new RCA report.
- If the new RCA provides additional verification steps, mitigations, or remediations \
that are NOT already in the existing playbook, merge them.
- If the existing playbook is already comprehensive and the new RCA adds nothing new, \
set needs_update to false.
- Do NOT remove existing content — only add or refine.
- Preserve the existing playbook's structure and language style.
"""

PLAYBOOK_UPDATE_USER_PROMPT_TEMPLATE = """\
Compare the existing playbook with the new RCA findings and decide whether to update.

## Existing Playbook
- **Failure Type**: {existing_failure_type}
- **Symptom Pattern**: {existing_symptom_pattern}
- **Verification Steps**: {existing_verification_steps}
- **Temporary Mitigation**: {existing_temporary_mitigation}
- **Permanent Remediation**: {existing_permanent_remediation}
- **Prevention Measures**: {existing_prevention_measures}

## New RCA Findings
- **Root Cause**: {root_cause}
- **Severity**: {severity}
- **Evidence Highlights**:
{evidence_highlights}
- **Mitigation Applied**: {mitigation_text}
- **Remediation Plan**: {remediation_text}

If the new RCA adds value, produce the updated playbook fields. \
If not, set needs_update to false.
"""
