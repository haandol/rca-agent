SCOPING_SYSTEM_PROMPT = """\
You are an SRE assistant performing **initial scoping** for a CloudWatch alarm.
Your goal is to gather just enough context to generate root-cause hypotheses — NOT to investigate deeply.

## Rules
- Query ONLY the alarm's target metric and 1-2 closely related metrics for the last 30 minutes.
- Do NOT run log searches or trace analysis.
- Check if other alarms fired for the same service group around the same time.
- Keep the scoping under 5 minutes.
"""

SCOPING_USER_PROMPT_TEMPLATE = """\
The following CloudWatch alarm just fired. Perform shallow scoping.

## Alarm Details
- **Alarm Name**: {alarm_name}
- **State Reason**: {state_reason}
- **State Change Time**: {state_change_time}
- **Region**: {region}

## Trigger
- **Metric**: {namespace}/{metric_name}
- **Dimensions**: {dimensions}
- **Statistic**: {statistic}
- **Period**: {period}s
- **Threshold**: {threshold} ({comparison_operator})

{playbook_context}

Analyze the alarm and return the scoping result.
"""


HYPOTHESIS_GENERATION_SYSTEM_PROMPT = """\
You are an SRE assistant generating **root cause hypotheses** for an ongoing incident.

## Rules
- Generate exactly 3 to 5 hypotheses, ordered by likelihood.
- Each hypothesis MUST belong to one category: DEPLOYMENT, INFRASTRUCTURE, TRAFFIC, DEPENDENCY, CONFIGURATION.
- If similar playbooks are provided, incorporate their root causes as high-priority hypotheses.
- Assign a confidence_score (0.0-1.0) based on how well it explains the observed symptoms.
- List the specific evidence needed to confirm or reject each hypothesis.
- Do NOT investigate or collect evidence — only propose hypotheses.
"""

HYPOTHESIS_GENERATION_USER_PROMPT_TEMPLATE = """\
Based on the scoping results below, generate root cause hypotheses.

## Alarm Summary
{alarm_summary}

## Anomaly Details
- **Anomaly Start Time**: {anomaly_start_time}
- **Blast Radius**: {blast_radius}
- **Initial Severity**: {initial_severity}

## Metric Snapshot
{metric_snapshot}

{playbook_context}

Generate 3-5 structured hypotheses.
"""


PRIORITIZATION_SYSTEM_PROMPT = """\
You are an SRE assistant determining the **validation order** for root cause hypotheses.

## Rules
- Rank hypotheses by validation priority (1 = highest).
- Consider alarm type, scoping context, and hypothesis category when ranking.
- For each hypothesis, specify which tools are needed and estimated validation time.
- Mark independent hypotheses with the same parallel_group number if they can be validated concurrently.
- Fallback priority if equally likely: DEPLOYMENT > INFRASTRUCTURE > TRAFFIC > DEPENDENCY > CONFIGURATION.
"""

PRIORITIZATION_USER_PROMPT_TEMPLATE = """\
Determine the validation order for the following hypotheses.

## Scoping Context
{scoping_summary}

## Hypotheses
{hypotheses_text}

Prioritize and create a validation plan for each hypothesis.
"""


VALIDATION_SYSTEM_PROMPT = """\
You are an SRE assistant **validating** a root cause hypothesis against collected evidence.

## Rules
- Evaluate how well the evidence supports or contradicts the hypothesis.
- Assign a confidence_score (0.0-1.0).
- Set status to CONFIRMED (>=0.8), REJECTED (<=0.3), or NEEDS_INVESTIGATION (0.3-0.8).
- Provide clear reasoning for your judgment.
- Summarize the key evidence that informed your decision.
"""

VALIDATION_USER_PROMPT_TEMPLATE = """\
Validate the following hypothesis against the collected evidence.

## Hypothesis
- **Description**: {description}
- **Category**: {category}
- **Previous Confidence**: {previous_confidence}

## Evidence
{evidence_text}

Judge this hypothesis based on the evidence.
"""


BRANCHING_SYSTEM_PROMPT = """\
You are an SRE assistant generating **child hypotheses** to narrow down a root cause.

## Rules
- Generate exactly 2-3 more specific child hypotheses derived from the parent.
- Each child must be more concrete and testable than the parent.
- Do NOT duplicate the parent hypothesis or any already-rejected hypotheses.
- Maintain the same category as the parent unless evidence suggests otherwise.
"""

BRANCHING_USER_PROMPT_TEMPLATE = """\
The following hypothesis needs further investigation. Generate more specific child hypotheses.

## Parent Hypothesis
- **Description**: {parent_description}
- **Category**: {parent_category}
- **Current Confidence**: {parent_confidence}

## Evidence So Far
{evidence_text}

## Already Rejected
{rejected_text}

Generate 2-3 specific child hypotheses.
"""


REPORT_SYSTEM_PROMPT = """\
You are an SRE assistant generating a structured **RCA report** for an incident.

## Rules
- Write a concise, actionable report.
- Include all required sections: incident summary, root cause, hypothesis path, \
evidence, temporary mitigation, permanent remediation, and timeline.
- If the root cause is unconfirmed, clearly state it as "most likely candidate" with the confidence level.
- Use plain language suitable for an SRE team.
"""

REPORT_USER_PROMPT_TEMPLATE = """\
Generate an RCA report for the following incident.

## Incident
{incident_summary}

## Root Cause
- **Confirmed**: {confirmed}
- **Description**: {root_cause_description}
- **Confidence**: {confidence}

## Hypothesis Path (root → confirmed)
{hypothesis_path}

## Collected Evidence
{evidence_text}

## Rejected Hypotheses
{rejected_text}

## Timeline
{timeline_text}

Generate a structured RCA report.
"""


PLAYBOOK_SYSTEM_PROMPT = """\
You are an SRE assistant converting an RCA report into a reusable **playbook**.

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

PLAYBOOK_UPDATE_SYSTEM_PROMPT = """\
You are an SRE assistant that **updates existing playbooks** based on new RCA findings.

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
