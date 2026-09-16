"""Exercise the actual installed AWS CLI offline with the locked ECS service model."""

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import botocore
import pytest

from headless_codex.cli_models import materialize_ecs_model

PACKAGE = Path(__file__).resolve().parents[1]


def test_only_plain_ecs_model_is_materialized_from_installed_package(tmp_path, monkeypatch):
    """An inherited external model path cannot shadow the locked package used at image build time."""
    monkeypatch.setenv("AWS_DATA_PATH", str(tmp_path / "untrusted-models"))
    target = materialize_ecs_model(tmp_path / "models")
    assert target.relative_to(tmp_path / "models").as_posix() == "ecs/2014-11-13/service-2.json"
    assert list((tmp_path / "models").rglob("*.*")) == [target]
    assert target.read_bytes().startswith(b"{")
    model = json.loads(target.read_text())
    breaker = model["shapes"]["DeploymentCircuitBreaker"]["members"]
    assert {"resetOnHealthyTask", "thresholdConfiguration"} <= breaker.keys()
    assert botocore.__version__ == "1.43.58"


def cli_environment(directory):
    """Do not inherit credentials or provider tokens into an offline subprocess test."""
    return {
        "PATH": os.environ["PATH"],
        "AWS_DATA_PATH": str(directory),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_PAGER": "",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    }


def test_actual_cli_skeleton_keeps_mutable_circuit_breaker_fields(tmp_path):
    """Use the real CLI's input skeleton because its output generator violates a new unrelated minimum."""
    executable = shutil.which("aws")
    if executable is None:
        pytest.skip("AWS CLI binary required for offline wire-model regression")
    materialize_ecs_model(tmp_path)
    result = subprocess.run(
        [
            executable,
            "ecs",
            "update-service",
            "--region",
            "us-east-1",
            "--no-sign-request",
            "--generate-cli-skeleton",
            "input",
        ],
        env=cli_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    breaker = json.loads(result.stdout)["deploymentConfiguration"]["deploymentCircuitBreaker"]
    assert "resetOnHealthyTask" in breaker
    assert {"type", "value"} <= breaker["thresholdConfiguration"].keys()


def test_actual_cli_response_parser_preserves_fields_over_loopback_http(tmp_path):
    """The genuine CLI parses a local synthetic DescribeServices reply; no SDK impersonation or AWS request."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    executable = shutil.which("aws")
    if executable is None:
        pytest.skip("AWS CLI binary required for offline wire-model regression")
    materialize_ecs_model(tmp_path)
    breaker = {
        "enable": True,
        "rollback": True,
        "resetOnHealthyTask": True,
        "thresholdConfiguration": {"type": "BOUNDED_PERCENT", "value": 50},
    }
    service = {
        "serviceName": "offline-service",
        "capacityProviderStrategy": None,
        "deploymentConfiguration": {"deploymentCircuitBreaker": breaker},
    }
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            """Capture only the loopback test request and return the synthetic ECS wire payload."""
            received.append(
                {
                    "target": self.headers.get("X-Amz-Target"),
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            body = json.dumps({"services": [service], "failures": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-amz-json-1.1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            """Keep local access logs out of the test's evidence output."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = subprocess.run(
            [
                executable,
                "ecs",
                "describe-services",
                "--cluster",
                "offline-cluster",
                "--services",
                "offline-service",
                "--region",
                "us-east-1",
                "--no-sign-request",
                "--endpoint-url",
                f"http://127.0.0.1:{server.server_port}",
                "--output",
                "json",
            ],
            env=cli_environment(tmp_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)["services"][0]
        assert observed["deploymentConfiguration"]["deploymentCircuitBreaker"] == breaker
        assert len(received) == 1 and received[0]["target"].endswith("DescribeServices")
        assert received[0]["body"]["services"] == ["offline-service"]
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
    assert not worker.is_alive()


def test_docker_and_execution_mcp_forward_only_the_model_directory():
    """The execution MCP subprocess receives the image's model path instead of losing it at its env allowlist."""
    docker = (PACKAGE / "Dockerfile").read_text()
    assert "python -m headless_codex.cli_models /app/aws-models" in docker
    assert "COPY --from=builder /app/aws-models /app/aws-models" in docker
    assert 'ENV AWS_DATA_PATH="/app/aws-models"' in docker
    for rel in ["harness/execution/config.toml", "harness/execution/agents/execution-operator.toml"]:
        config = tomllib.loads((PACKAGE / rel).read_text())
        assert "AWS_DATA_PATH" in config["mcp_servers"]["playbook-execution"]["env_vars"]
