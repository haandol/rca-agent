"""Offline fixtures that use the real analysis reducer rather than invented final state."""

import json

import pytest

from headless_codex.services.analysis_contract import normalize_validation_artifact


@pytest.fixture
def analysis_artifacts(tmp_path):
    """Create one valid, independent three-hypothesis generation for reducer tests."""
    base = tmp_path / "analysis"
    base.mkdir()
    hypotheses = [
        {
            "hypothesis_id": identifier,
            "tree_id": "tree-1",
            "title": title,
            "description": title,
            "fault_type": "unsupported",
            "category": category,
            "confidence_score": 0.5,
            "required_evidence": ["measured evidence"],
            "status": "PENDING",
            "parent_id": None,
            "depth": 0,
        }
        for identifier, title, category in (
            ("root", "External transaction blocking", "DEPENDENCY"),
            ("other", "Changed query implementation", "DEPLOYMENT"),
            ("third", "Increased request concurrency", "TRAFFIC"),
        )
    ]
    (base / "hypotheses.json").write_text(
        json.dumps(
            {
                "stage": "HYPOTHESIS_GENERATION",
                "tree_id": "tree-1",
                "hypotheses": hypotheses,
                "summary": "three independent mechanisms",
                "output_summary": "generated",
            }
        )
    )
    return base


@pytest.fixture
def save_validation(analysis_artifacts):
    """Save a real normalized loop, preserving the reducer's selection and limits."""

    def save(loop, **buckets):
        """Accept explicit judgments and return the persisted server artifact."""
        artifact = {
            "stage": "VALIDATION",
            "loop_index": loop,
            "summary": "measured judgments",
            "output_summary": "validation",
            "confirmed": [],
            "rejected": [],
            "needs_investigation": [],
            "closed": [],
            "new_hypotheses": [],
            **buckets,
        }
        name = f"validation-{loop}.json"
        normalized, _ = normalize_validation_artifact(analysis_artifacts, name, json.dumps(artifact))
        (analysis_artifacts / name).write_text(normalized)
        return json.loads(normalized)

    return save


@pytest.fixture
def judgment():
    """Build an explicit test judgment without assigning its authoritative status."""

    def entry(identifier, confidence, *, fault_type="unsupported", reasoning="measured", evidence=None, failed=False):
        """Keep reason, citations and failed-collection flags supplied by each test."""
        return {
            "hypothesis_id": identifier,
            "confidence": confidence,
            "fault_type": fault_type,
            "reasoning": reasoning,
            "evidence_summary": evidence if evidence is not None else ["observed"],
            "evidence_collection_failed": failed,
        }

    return entry
