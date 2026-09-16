#!/usr/bin/env python3
"""Use the single native demo control for a predeclared independent live run.

Each of the operator's 3 preparation + 10 main runs uses a fresh run id and
plan/apply/status/restore. RCA, dashboard approval and RESOLVED evidence are
collected by the parent operator through ordinary APIs. This command neither
approves executions nor treats environment cleanup as an end-to-end success.
"""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).with_name("run_realistic_demo.py")), run_name="__main__"
    )
