# Reproducibility artifacts

Repository: /Users/dongkyl/git/rca-agent
Audited HEAD: bc5a7d5

These files were saved after the audit from the successful stdin scripts and
captured tool output. The output files are transcript copies, not new runs.
No tests or probes were rerun while packaging these artifacts.

The audit used the existing packages/agent/.venv (Python 3.14 and the installed
Strands SDK). Dependencies were not installed or updated. The environment was not
snapshotted; SDK changes can affect message framing and character counts.

test-command.sh contains the exact 159-test invocation, with its original working
directory made explicit. test-output.txt contains the successful result.

The two probe scripts contain the Python bodies of the successful stdin runs.
Run from packages/agent with the same environment:

```sh
cd /Users/dongkyl/git/rca-agent/packages/agent
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 AWS_EC2_METADATA_DISABLED=true OTEL_SDK_DISABLED=true .venv/bin/python -B ../../docs/test-reports/rca-efficiency-evidence-2026-09-09/strands/probe-validation-history.py
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 AWS_EC2_METADATA_DISABLED=true OTEL_SDK_DISABLED=true .venv/bin/python -B ../../docs/test-reports/rca-efficiency-evidence-2026-09-09/strands/probe-pipeline.py
```

Both probes install a Python audit hook rejecting socket.connect,
socket.getaddrinfo, and socket.sendto. Model and data responses are synthetic.

probe-pipeline.py contains the regeneration and duplicate-branching reproductions,
plus the scoping and serialization measurements executed in that same original
probe. Synthetic elapsed times vary between runs. Its output preserves the
captured combined tool output, including the scoping timeout warning.
