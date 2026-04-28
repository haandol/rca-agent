from rca_agent.prompts.common import LANGUAGE_DIRECTIVE

SCOPING_SYSTEM_PROMPT = f"""\
You are an SRE assistant performing **initial scoping** for a CloudWatch alarm.
Your goal is to gather just enough context to generate root-cause hypotheses — NOT to investigate deeply.

## Language
{LANGUAGE_DIRECTIVE}

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

{report_context}

Analyze the alarm and return the scoping result.
"""
