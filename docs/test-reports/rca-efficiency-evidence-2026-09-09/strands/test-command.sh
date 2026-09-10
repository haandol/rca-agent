cd /Users/dongkyl/git/rca-agent/packages/agent
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 AWS_EC2_METADATA_DISABLED=true OTEL_SDK_DISABLED=true .venv/bin/python -B - <<'PY'
import sys

def offline(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}:
        raise RuntimeError('Audit forbids network: ' + event)
sys.addaudithook(offline)
import pytest
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/test_evidence.py', 'tests/test_validation.py', 'tests/test_branching.py', 'tests/test_hypothesis.py', 'tests/test_prioritization.py', 'tests/test_scoping.py', 'tests/test_harness_timeout_and_isolation.py', 'tests/test_main.py']))
PY
