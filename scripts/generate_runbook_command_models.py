"""Generate dashboard CLI requirements from the engines' locked botocore models.

Run with packages/headless-codex/.venv/bin/python. No AWS calls or new packages.
Use --check in verification; update both engine locks before regenerating.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import botocore
import tomllib
from botocore import xform_name
from botocore.session import Session


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for engine in ("agent", "headless-codex"):
        lock = tomllib.loads((root / "packages" / engine / "uv.lock").read_text())
        version = next(p["version"] for p in lock["package"] if p["name"] == "botocore")
        if version != botocore.__version__:
            raise SystemExit(f"{engine} locks botocore {version}, installed {botocore.__version__}")
    session = Session()
    requirements: dict[str, list[str]] = {}
    for service in sorted(session.get_available_services()):
        model = session.get_service_model(service)
        for operation in sorted(model.operation_names):
            shape = model.operation_model(operation).input_shape
            required = sorted("--" + xform_name(name, "-") for name in shape.required_members) if shape else []
            requirements[f"{service} {xform_name(operation, '-')}"] = required
    content = json.dumps(
        {"schema_version": 1, "botocore_version": botocore.__version__, "required_options": requirements},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ) + "\n"
    target = root / "packages/dashboard/server/utils/runbook-command-models.json"
    if args.check:
        if not target.exists() or target.read_text() != content:
            raise SystemExit("runbook command models differ from the locked botocore models; regenerate")
    else:
        target.write_text(content)
    print(f"{'Verified' if args.check else 'Generated'} {len(requirements)} operations from botocore {botocore.__version__}")


if __name__ == "__main__":
    main()
