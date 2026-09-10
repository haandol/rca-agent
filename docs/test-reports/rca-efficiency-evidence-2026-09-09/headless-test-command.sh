cd /Users/dongkyl/git/rca-agent/packages/headless-codex
.venv/bin/python -B - <<'PY'
import os, socket, sys
os.environ['PYTHONDONTWRITEBYTECODE']='1'
os.environ['DYNAMODB_TABLE_NAME']=''
os.environ['AWS_EC2_METADATA_DISABLED']='true'
os.environ['AWS_ACCESS_KEY_ID']='offline-test'
os.environ['AWS_SECRET_ACCESS_KEY']='offline-test'
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
def blocked(*args, **kwargs):
    raise AssertionError('Network prohibited during audit')
socket.socket.connect=blocked
socket.create_connection=blocked
import pytest
sys.exit(pytest.main([
    '-q', '-p', 'no:cacheprovider',
    'tests/test_codex_runner.py',
    'tests/test_artifact_validation.py',
    'tests/test_mcp_server.py',
    'tests/test_prompt_contracts.py',
    'tests/test_prompt_builder.py',
    'tests/test_gate_prompt_alignment.py',
]))
PY
