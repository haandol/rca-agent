"""Check report data and SVG markup without opening a browser or serving HTML."""

from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parent.parent
TARGETS = ("infra-0004", "infra-0007", "agent-0016")


class Page(HTMLParser):
    """Collect actual HTML anchors while excluding CSS and JavaScript strings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.links = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.tags.append((tag, values))
        if "id" in values:
            self.ids.append(values["id"])
        if tag == "a" and values.get("href", "").startswith("#"):
            self.links.append(values["href"][1:])


for target in TARGETS:
    dest = BASE / target
    data = json.loads((dest / "findings.json").read_text())
    file = dest / "adr-impl-review-report.html"
    raw = file.read_text()
    page = Page()
    page.feed(raw)
    assert file.stat().st_size > 0
    assert len(page.ids) == len(set(page.ids)), "duplicate HTML ids"
    assert set(page.links) <= set(page.ids), "broken internal anchors"
    assert any(tag == "html" and attrs.get("lang") == "ko" for tag, attrs in page.tags)
    assert not any(
        tag in ("script", "img", "iframe", "link") and (attrs.get("src") or attrs.get("href"))
        for tag, attrs in page.tags
    ), "report has an external rendering dependency"
    assert "comprehensionCheck" not in data, "unsolicited comprehension gate"
    assert not any("comprehension" in value for value in page.ids)
    diagrams = re.findall(r"<svg\b.*?</svg>", raw, re.S)
    assert len(diagrams) == len(data["diagramRequirements"])
    svg_checks = []
    for source in diagrams:
        svg = ET.fromstring(source)
        viewbox = [float(value) for value in svg.attrib["viewBox"].split()]
        assert len(viewbox) == 4 and viewbox[2] > 0 and viewbox[3] > 0
        texts = [node for node in svg.iter() if node.tag.endswith("}text")]
        edges = [node for node in svg.iter() if node.tag.endswith(("}line", "}path", "}polyline"))]
        assert texts and edges, "source-only diagram or missing relationship geometry"
        title_id = svg.attrib.get("aria-labelledby")
        assert title_id and any(node.attrib.get("id") == title_id for node in svg.iter())
        svg_checks.append(dict(viewBox=viewbox, textNodes=len(texts), relationshipPrimitives=len(edges)))
    ids = [row["contractId"] for row in data["contractCoverage"]]
    assigned = [cid for hill in data["reviewHike"]["hills"] for cid in hill["contractIds"]]
    assert sorted(ids) == sorted(assigned) and len(assigned) == len(set(assigned))
    assert any(
        item["kind"] == "diff"
        for hill in data["reviewHike"]["hills"]
        for component in hill["components"]
        for item in component["codeEvidence"]
    )
    assert data["status"] == "Proposed"
    result = dict(
        checkedAt=datetime.now(timezone.utc).isoformat(),
        artifactState=data["artifactState"],
        staticChecks="PASS",
        htmlBytes=file.stat().st_size,
        htmlSha256=hashlib.sha256(file.read_bytes()).hexdigest(),
        contractRows=len(ids),
        hills=len(data["reviewHike"]["hills"]),
        svgCount=len(diagrams),
        svgChecks=svg_checks,
        internalAnchors="PASS",
        language="ko",
        externalRenderingDependencies=[],
        quizIncluded=False,
        browser="NOT_OPENED",
        policyBlock="Prior automatic approval review rejected local file://; user forbids HTTP, shell, or other-browser workarounds.",
        openHelperExecuted=False,
        visualInspection="NOT_PERFORMED",
        limitation="XML, geometry presence and anchor checks only; no browser layout, pixel, screenshot, or label-overlap inspection.",
    )
    (dest / "static-validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (dest / "artifact-status.json").write_text(json.dumps(dict(
        artifactState=data["artifactState"],
        verdict=data["verdict"],
        codeReviewVerdict=data.get("codeReviewVerdict"),
        modelQualityState=data.get("modelQualityState", "PENDING"),
        officialValidation="PASS — official-validation.json",
        staticValidation="PASS — static-validation.json",
        browser="NOT_OPENED",
        visualInspection="NOT_PERFORMED",
        checkedAt=result["checkedAt"],
    ), ensure_ascii=False, indent=2) + "\n")
    (dest / "browser-status.txt").write_text(
        f"NOT_OPENED {file}\n"
        "정책 차단: 이전 자동 승인 검토가 local file:// 열기를 거부했고 사용자가 HTTP·shell·다른 브라우저 우회를 금지했다.\n"
        "open helper 미실행. 브라우저/스크린샷/시각 레이아웃 검사 미수행. 정적 SVG XML 및 내부 앵커만 검사했다.\n"
    )
    print(f"{target}: static PASS; {len(ids)} rows, {len(diagrams)} SVGs, {file.stat().st_size} bytes; NOT_OPENED")
