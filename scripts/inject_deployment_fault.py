#!/usr/bin/env python3
"""Compatibility entry point for the single journaled native write-column demo.

Legacy flag, red-herring and parameter-group drivers are retired. All controls
and recovery guarantees now come from run_realistic_demo; no execution approval
or fabricated alarm/event is available here.
"""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).with_name("run_realistic_demo.py")), run_name="__main__"
    )
