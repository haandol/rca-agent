from cc_headless.adapters.secondary.cc.cc_subprocess_runner import (  # noqa: F401
    _MCP_CONFIG_PATH,
    CcSubprocessRunner,
    _find_file,
    _watch_cancel,
)
from cc_headless.ports.dto.models import CcResult  # noqa: F401


def run_claude(prompt, *, mcp_config=None, cancel_checker=None):
    runner = CcSubprocessRunner()
    return runner.run(prompt, mcp_config=mcp_config, cancel_checker=cancel_checker)
