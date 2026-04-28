from rca_agent.prompts.common import LANGUAGE_DIRECTIVE

REPORT_SYSTEM_PROMPT = f"""\
You are an SRE assistant generating a structured **RCA report** for an incident.

## Language
{LANGUAGE_DIRECTIVE}

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
