"""Materialize the locked SDK's ECS wire model for the existing AWS CLI binary."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

import botocore


def materialize_ecs_model(destination: Path) -> Path:
    """Write only ECS's uncompressed model; older CLI loaders do not use the bundled gzip override.

    Read the installed package directly so an inherited AWS_DATA_PATH cannot
    replace the locked source. The CLI still executes and serializes actual AWS
    requests; this supplies its ECS field definitions, not a substitute transport.
    """
    relative = Path("ecs/2014-11-13/service-2.json")
    installed = Path(botocore.__file__).resolve().parent / "data" / relative
    compressed = installed.with_suffix(".json.gz")
    raw = gzip.decompress(compressed.read_bytes()) if compressed.exists() else installed.read_bytes()
    model = json.loads(raw)
    if model["metadata"]["apiVersion"] != "2014-11-13":
        raise ValueError("Installed ECS model API version does not match the CLI override")
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    return target


def main() -> None:
    """Prepare the image's explicit model directory without downloading or modifying AWS CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    materialize_ecs_model(parser.parse_args().destination)


if __name__ == "__main__":
    main()
