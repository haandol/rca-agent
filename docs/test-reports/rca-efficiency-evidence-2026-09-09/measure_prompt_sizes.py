"""Compare base prompts using one synthetic alarm, without model or AWS calls."""
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from headless_codex.ports.dto.models import AlarmContext
from headless_codex.services import prompt_builder

root = Path(__file__).resolve().parents[3]
folder = "packages/headless-codex/prompts"
alarm = AlarmContext(
    alarm_name="HighCPU",
    state_reason="CPU threshold crossed",
    region="us-east-1",
    metric_name="CPUUtilization",
    namespace="AWS/ECS",
)
with tempfile.TemporaryDirectory(prefix="rca-original-prompts-") as temporary:
    old_dir = Path(temporary)
    paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "bc5a7d5", folder], cwd=root, text=True
    ).splitlines()
    for name in paths:
        dest = old_dir / Path(name).relative_to(folder)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(subprocess.check_output(["git", "show", "bc5a7d5:" + name], cwd=root))
    with patch.object(prompt_builder, "_PROMPTS_DIR", old_dir):
        original = prompt_builder.build_prompt(alarm)
rca = prompt_builder.build_prompt(alarm, role="rca")
report = prompt_builder.build_prompt(alarm, role="report")
print(
    json.dumps(
        {
            "measurement": "same synthetic alarm; base prompts only, excludes appended RCA result and harness",
            "original_per_role_chars": len(original),
            "original_two_roles_chars": 2 * len(original),
            "new_rca_chars": len(rca),
            "new_report_chars": len(report),
            "new_two_roles_chars": len(rca) + len(report),
            "reduction_percent": round((1 - (len(rca) + len(report)) / (2 * len(original))) * 100, 1),
        },
        ensure_ascii=False,
        indent=2,
    )
)
