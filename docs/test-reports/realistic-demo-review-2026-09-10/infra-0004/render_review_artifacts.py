"""Run official report scripts, then repair only their code-anchor letter casing."""

import argparse
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

BASE = Path(__file__).resolve().parent.parent
ROOT = BASE.parents[2]
TOOLS = Path("/private/tmp/rca-adr-tools-0.8.19/scripts")
parser = argparse.ArgumentParser()
parser.add_argument("--draft", action="store_true")
args = parser.parse_args()


def render(target):
    dest = BASE / target
    data = json.loads((dest / "findings.json").read_text())
    if not args.draft:
        assert data["artifactState"] == "FINAL_SYNTHESIS_RECEIVED", "main final data not received"
        assert data.get("mainFinalDataSource"), "missing final-data authority"
    log = []
    for kind in ("materialize", "validate", "report"):
        command = ["node", str(TOOLS / f"adr-impl-review-{kind}.mjs")]
        command += (
            [str(dest / "findings.json"), "--out", str(dest / "adr-impl-review-report.html")]
            if kind == "report" else [str(dest)]
        )
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        log.append(dict(command=command, exitCode=result.returncode, output=result.stdout + result.stderr))
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
    html_path = dest / "adr-impl-review-report.html"
    raw = html_path.read_text()
    before_hash = hashlib.sha256(html_path.read_bytes()).hexdigest()
    corrections = []

    def normalize(match):
        original = match.group(1)
        desired = original.lower()
        assert f'href="#{desired}"' in raw
        corrections.append(dict(original=original, replacement=desired))
        return f'id="{desired}"'

    # Official 0.8.19 output uses C1 in code ids but c1 in its own ToC hrefs.
    # Only exact code-evidence ids are normalized; no content or layout changes.
    raw = re.sub(r'id="(code-h\d+-C\d+-\d+)"', normalize, raw)
    html_path.write_text(raw)
    record = dict(
        checkedAt=datetime.now(timezone.utc).isoformat(),
        draft=args.draft,
        officialCommands=log,
        officialRendererOutputSha256=before_hash,
        ownedHtmlAnchorNormalization=corrections,
        deliveredHtmlSha256=hashlib.sha256(html_path.read_bytes()).hexdigest(),
        browser="NOT_OPENED",
        openHelperExecuted=False,
        reason="Explicit user policy block: no local file://, HTTP, shell, or other-browser workaround.",
    )
    (dest / ("draft-official-validation.json" if args.draft else "official-validation.json")).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    return f"{target}: official materialize/validate/report PASS; {len(corrections)} code anchors normalized; NOT_OPENED"


with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    for result in pool.map(render, ("infra-0004", "infra-0007", "agent-0016")):
        print(result)
