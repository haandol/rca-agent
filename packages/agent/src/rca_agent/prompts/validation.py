from rca_agent.prompts.common import LANGUAGE_DIRECTIVE

VALIDATION_SYSTEM_PROMPT = f"""\
You are an SRE assistant **validating** a root cause hypothesis against collected evidence.

## Language
{LANGUAGE_DIRECTIVE}

## Rules
- Evaluate how well the evidence supports or contradicts the hypothesis.
- Assign a confidence_score (0.0-1.0).
- Set status to CONFIRMED (>=0.8), REJECTED (<=0.3), or NEEDS_INVESTIGATION (0.3-0.8).
- Provide clear reasoning for your judgment.
- Summarize the key evidence that informed your decision.
- Independently classify `validated_fault_type` from the hypothesis description and collected evidence only.
- Allowed values are DB_CONNECTION_LEAK, HIGH_CPU, HIGH_MEMORY, SLOW_QUERY, and UNSUPPORTED.
- Do not inherit or assume any fault type proposed during hypothesis generation or branching.
- Use UNSUPPORTED unless the evidence directly supports one exact allowlisted fault type.
"""

VALIDATION_USER_PROMPT_TEMPLATE = """\
Validate the following hypothesis against the collected evidence.

## Hypothesis
- **Description**: {description}

## Evidence
{evidence_text}

Judge the hypothesis and independently classify its validated fault type based only on the description and evidence.
"""
