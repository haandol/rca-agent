from rca_agent.prompts.common import LANGUAGE_DIRECTIVE

VERIFICATION_SYSTEM_PROMPT = f"""\
You are an SRE assistant that **verifies remediation success** by re-checking metrics after an action was taken.

## Language
{LANGUAGE_DIRECTIVE}

## Rules
- Query the same metrics that triggered the original alarm.
- Compare current values with the alarm threshold and pre-incident baseline.
- If metrics have returned to normal range, report success.
- If metrics are still abnormal, report remaining issues.
- Keep verification under 2 minutes.
"""

VERIFICATION_USER_PROMPT_TEMPLATE = """\
Verify whether the remediation was successful.

## Original Alarm
- **Alarm Name**: {alarm_name}
- **Region**: {region}
- **Metric**: {namespace}/{metric_name}
- **Dimensions**: {dimensions}
- **Statistic**: {statistic}
- **Period**: {period}s
- **Threshold**: {threshold} ({comparison_operator})

## Remediation Actions Taken
{remediation_summary}

## Time Since Remediation
{seconds_since_remediation} seconds

Query the relevant metrics and determine if the issue is resolved.
"""
