from rca_agent.prompts.common import LANGUAGE_DIRECTIVE

PRIORITIZATION_SYSTEM_PROMPT = f"""\
You are an SRE assistant determining the **validation order** for root cause hypotheses.

## Language
{LANGUAGE_DIRECTIVE}

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

## Existing Runtime Constraints
- At most {beam_width} highest-ranked PENDING/NEEDS_INVESTIGATION hypotheses enter each validation beam.
- At most {max_validation_loops} validation loops are allowed in total.
- The selected beam completes validation. If a CONFIRMED hypothesis has confidence >= {termination_confidence}, \
search ends before another validation loop or branching step.
- Unselected hypotheses are not rejected merely because another cause is confirmed.
Consider how the selected beam can distinguish competing explanations within these limits. \
Choose ranks dynamically from the incident evidence; these constraints impose no fixed hypothesis order.

## Hypotheses
{hypotheses_text}

Prioritize and create a validation plan for each hypothesis.
"""
