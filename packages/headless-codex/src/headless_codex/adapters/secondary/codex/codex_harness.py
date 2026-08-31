from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from headless_codex.config.settings import (
    CODEX_BEDROCK_BASE_URL,
    CODEX_MODEL,
    CODEX_MODEL_PROVIDER,
    CODEX_REASONING_EFFORT,
    validate_codex_model_contract,
)

ANALYSIS_PROFILE = "analysis"
ANALYSIS_RCA_PROFILE = "analysis-rca"
ANALYSIS_REPORT_PROFILE = "analysis-report"
MODEL_EVAL_PROFILE = "model-eval"
MODEL_EVAL_RCA_PROFILE = "model-eval-rca"
MODEL_EVAL_REPORT_PROFILE = "model-eval-report"
EXECUTION_PROFILE = "execution"
RETROSPECTIVE_PROFILE = "retrospective"

_PROFILE_CONFIG = {
    ANALYSIS_PROFILE: Path("analysis/config.toml"),
    ANALYSIS_RCA_PROFILE: Path("analysis/rca.config.toml"),
    ANALYSIS_REPORT_PROFILE: Path("analysis/report.config.toml"),
    MODEL_EVAL_PROFILE: Path("analysis/model-eval.config.toml"),
    MODEL_EVAL_RCA_PROFILE: Path("analysis/model-eval-rca.config.toml"),
    MODEL_EVAL_REPORT_PROFILE: Path("analysis/model-eval-report.config.toml"),
    EXECUTION_PROFILE: Path("execution/config.toml"),
    RETROSPECTIVE_PROFILE: Path("retrospective/config.toml"),
}
_PROFILE_AGENTS = {
    ANALYSIS_PROFILE: ("rca-specialist.toml", "report-specialist.toml"),
    MODEL_EVAL_PROFILE: ("rca-specialist-model-eval.toml", "report-specialist-model-eval.toml"),
}


def find_harness_root() -> Path:
    current = Path(__file__).resolve().parent
    for parent in [current, *current.parents]:
        candidate = parent / "harness"
        if candidate.is_dir():
            return candidate
    return Path("/app/harness")


HARNESS_ROOT = find_harness_root()
PACKAGE_ROOT = HARNESS_ROOT.parent


def runtime_home_root() -> Path:
    configured = os.environ.get("CODEX_RUNTIME_HOME_ROOT")
    root = Path(configured) if configured else Path.home() / ".codex-runs"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def prepare_codex_home(home: Path, profile: str, extra_env: dict[str, str] | None = None) -> Path:
    validate_codex_model_contract()
    relative_config = _PROFILE_CONFIG.get(profile)
    if relative_config is None:
        raise ValueError(f"Unknown Codex harness profile: {profile}")

    replacements = {
        "{{PACKAGE_ROOT}}": str(PACKAGE_ROOT),
        "{{CODEX_HOME}}": str(home),
        "{{BEDROCK_BASE_URL}}": CODEX_BEDROCK_BASE_URL,
        "{{CODEX_MODEL}}": CODEX_MODEL,
        "{{CODEX_REASONING_EFFORT}}": CODEX_REASONING_EFFORT,
        "{{CODEX_MODEL_PROVIDER}}": CODEX_MODEL_PROVIDER,
    }
    context_names = (
        "RCA_EXECUTION_TOKEN",
        "RCA_SESSION_ID",
        "RCA_CLAIM_TOKEN",
        "RCA_ATTEMPT",
        "PLAYBOOK_EXECUTION_TOKEN",
        "PLAYBOOK_EXECUTION_ID",
        "PLAYBOOK_APPROVED_STEP_IDS",
        "PLAYBOOK_APPROVED_SUCCESS_CRITERIA",
    )
    for name in context_names:
        value = (extra_env or {}).get(name, "")
        replacements[f"{{{{{name}}}}}"] = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def render(source: Path) -> str:
        content = source.read_text()
        for marker, value in replacements.items():
            content = content.replace(marker, value)
        return content

    source = HARNESS_ROOT / relative_config
    rendered = render(source)

    config_path = home / "config.toml"
    config_path.write_text(rendered)
    agent_source = source.parent / "agents"
    agent_names = _PROFILE_AGENTS.get(profile, ())
    if agent_source.is_dir() and agent_names:
        agent_destination = home / "agents"
        agent_destination.mkdir()
        for name in agent_names:
            agent_config = agent_source / name
            instructions_name = name.replace("-model-eval", "").replace(".toml", ".md")
            instructions = (agent_source / instructions_name).read_text()
            content = f"developer_instructions = {json.dumps(instructions, ensure_ascii=False)}\n"
            content += render(agent_config)
            (agent_destination / agent_config.name).write_text(content)
    shutil.copytree(HARNESS_ROOT / "skills", home / "skills")
    return config_path


def prepare_workspace(workspace: Path, profile: str) -> None:
    if profile in {ANALYSIS_PROFILE, MODEL_EVAL_PROFILE}:
        guidance = HARNESS_ROOT / "analysis" / "AGENTS.md"
    elif profile in {ANALYSIS_RCA_PROFILE, MODEL_EVAL_RCA_PROFILE}:
        guidance = HARNESS_ROOT / "analysis" / "agents" / "rca-specialist.md"
    elif profile in {ANALYSIS_REPORT_PROFILE, MODEL_EVAL_REPORT_PROFILE}:
        guidance = HARNESS_ROOT / "analysis" / "agents" / "report-specialist.md"
    elif profile == EXECUTION_PROFILE:
        guidance = HARNESS_ROOT / "execution" / "AGENTS.md"
    elif profile == RETROSPECTIVE_PROFILE:
        guidance = HARNESS_ROOT / "retrospective" / "agents" / "retrospective-analyst.md"
    else:
        raise ValueError(f"Unknown Codex harness profile: {profile}")
    shutil.copy2(guidance, workspace / "AGENTS.md")


def codex_environment(home: Path, extra: dict[str, str]) -> dict[str, str]:
    environment = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home),
        **extra,
    }
    original_home = Path(os.environ.get("HOME", ""))
    aws_config = original_home / ".aws" / "config"
    aws_credentials = original_home / ".aws" / "credentials"
    if "AWS_CONFIG_FILE" not in environment and aws_config.is_file():
        environment["AWS_CONFIG_FILE"] = str(aws_config)
    if "AWS_SHARED_CREDENTIALS_FILE" not in environment and aws_credentials.is_file():
        environment["AWS_SHARED_CREDENTIALS_FILE"] = str(aws_credentials)
    return environment


def codex_exec_args(workspace: Path, last_message: Path) -> list[str]:
    return [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--strict-config",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-C",
        str(workspace),
        "-o",
        str(last_message),
        "-",
    ]
