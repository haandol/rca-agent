"""Generate logical part content while keeping identities, source checks and approval on the server."""

from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from rca_agent.config.settings import LLM_DEFAULT_TIMEOUT_SECONDS
from rca_agent.ports.dto.models import ExecutionStep, Playbook, RcaReport, ScopingResult
from rca_agent.services.analysis_parts import validate_recovery_operations
from rca_agent.services.deployment_baseline import validate_observed_plan
from rca_agent.services.playbook_gen import PlaybookOutput
from rca_agent.services.runbook_contract import validate_runbook
from rca_agent.utils.agent_invocation import invoke_agent


class RecoveryOutput(BaseModel):
    """Model recommendations cannot set approval, provenance or stored artifact identity."""

    title: str
    summary: str
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    recommendation: Literal["ROLLBACK", "UNAVAILABLE"]
    playbook: PlaybookOutput | None = None


class CodeFile(BaseModel):
    """A proposed edit must identify exact observed bytes and an inclusive source range."""

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    original: str
    proposed: str
    evidence_refs: list[str] = Field(default_factory=list)


class CodePreview(BaseModel):
    """A code preview has no publication capability or model-owned test success."""

    status: Literal["PROPOSED", "UNAVAILABLE"]
    title: str
    repository: str | None = None
    base_revision: str | None = None
    files: list[CodeFile] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    tests_status: Literal["NOT_RUN"] = "NOT_RUN"
    limitations: list[str] = Field(default_factory=list)


class OperationFinding(BaseModel):
    """Separate measured observations from unverified upstream explanations."""

    statement: str
    status: Literal["OBSERVED", "UNVERIFIED"]
    evidence_refs: list[str] = Field(default_factory=list)


class OperationRecommendation(BaseModel):
    """Describe a proposed check without claiming an unexecuted CI change or test passed."""

    title: str
    description: str
    stage: str
    priority: str
    owner: str | None = None
    check: str
    failure_condition: str
    verification_plan: str
    validation_status: Literal["NOT_RUN"] = "NOT_RUN"
    evidence_refs: list[str] = Field(default_factory=list)


class OperationsOutput(BaseModel):
    """Operations proposals remain distinct from execution retrospective and VERIFIED promotion."""

    title: str
    summary: str
    findings: list[OperationFinding] = Field(default_factory=list)
    recommendations: list[OperationRecommendation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def unavailable_code(reason: str) -> dict:
    """Missing source yields an honest empty preview, never an inferred patch."""
    return {
        "status": "UNAVAILABLE",
        "title": "코드 변경 미리보기 제공 불가",
        "files": [],
        "test_plan": [],
        "tests_status": "NOT_RUN",
        "limitations": [reason],
    }


def verified_sources(artifacts: list[dict]) -> list[dict]:
    """Accept only actually supplied source bytes with content hash and explicit source identity."""
    verified = []
    for artifact in artifacts:
        text = artifact.get("text")
        if (
            isinstance(text, str)
            and artifact.get("path")
            and artifact.get("source_ref")
            and artifact.get("sha256") == hashlib.sha256(text.encode()).hexdigest()
        ):
            verified.append(artifact)
    return verified


def validate_code_preview(output: CodePreview, artifacts: list[dict]) -> dict:
    """Match every edit to one exact source range and render the diff from those verified bytes."""
    result = output.model_dump(mode="json")
    if output.status == "UNAVAILABLE":
        result["files"] = []
        return result
    sources = verified_sources(artifacts)
    if not output.files:
        raise ValueError("proposed code preview contains no edits")
    seen = set()
    used_sources = []
    for index, edit in enumerate(output.files):
        matches = [
            source for source in sources if source["path"] == edit.path and source["source_ref"] in edit.evidence_refs
        ]
        if len(matches) != 1 or edit.path in seen:
            raise ValueError("code preview must identify one actually read source per file")
        source = matches[0]
        if source.get("source_phase") != "current" and not any(
            item.get("phase") == "current" for item in source.get("observed_manifests", [])
        ):
            raise ValueError("code preview cannot target baseline or unverified source as incident code")
        targets = [
            item
            for item in sources
            if item["path"] == edit.path
            and item.get("is_current_target")
            and item.get("repository") == source.get("repository")
        ]
        if any(
            item.get("base_ref", item.get("base_revision")) != source.get("base_ref", source.get("base_revision"))
            for item in targets
        ):
            raise ValueError("code preview base differs from the actually read target; it may already be fixed")
        seen.add(edit.path)
        used_sources.append(source)
        if output.repository and source.get("repository") != output.repository:
            raise ValueError("code preview repository was not observed")
        if output.base_revision and source.get("base_revision") != output.base_revision:
            raise ValueError("code preview base revision was not observed")
        lines = source["text"].splitlines(keepends=True)
        if not edit.start_line <= edit.end_line <= len(lines):
            raise ValueError("code preview range is outside the observed file")
        original = "".join(lines[edit.start_line - 1 : edit.end_line])
        if edit.original != original:
            raise ValueError("code preview original does not match observed bytes")
        if edit.proposed == original:
            raise ValueError("code preview does not change the observed source")
        differences = difflib.unified_diff(
            lines,
            lines[: edit.start_line - 1] + edit.proposed.splitlines(keepends=True) + lines[edit.end_line :],
            fromfile=f"a/{edit.path}",
            tofile=f"b/{edit.path}",
        )
        result["files"][index]["unified_diff"] = "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in differences
        )
    bases = {source.get("base_ref", source.get("base_revision")) for source in used_sources}
    repositories = {source.get("repository") for source in used_sources}
    if len(bases) != 1 or not next(iter(bases)) or len(repositories) != 1:
        raise ValueError("code preview needs one actually read source base and repository context")
    result["base_revision"] = next(iter(bases))
    result["repository"] = next(iter(repositories))
    if not any(source.get("is_current_target") for source in used_sources):
        result["limitations"].append(
            "Preview is bound to the observed immutable source; current branch head is not established."
        )
    return result


def generate_code_preview(
    report: RcaReport, incident: dict, agent, *, timeout_seconds=LLM_DEFAULT_TIMEOUT_SECONDS
) -> dict:
    """Keep unsupported source edits unavailable while letting the SDK correct invalid observed edits."""
    sources = verified_sources(incident.get("source_artifacts", []))
    if not any(
        source.get("source_phase") == "current"
        or any(item.get("phase") == "current" for item in source.get("observed_manifests", []))
        for source in sources
    ):
        return unavailable_code(
            "장애 배포에 연결된 실제 소스가 없습니다. 정상 baseline을 수정 대상으로 추정하지 않습니다."
        )

    class ObservedCodePreview(CodePreview):
        """Reject invented source before the structured-output call is accepted."""

        @model_validator(mode="after")
        def check_source(self):
            """Reuse exact-source validation inside the SDK correction boundary."""
            validate_code_preview(self, sources)
            return self

    output = invoke_agent(
        agent,
        json.dumps(
            {"root_cause": report.root_cause, "confirmed": report.root_cause_confirmed, "sources": sources},
            ensure_ascii=False,
        ),
        ObservedCodePreview,
        timeout_seconds,
    )
    return validate_code_preview(output, sources)


def recovery_result(
    rca_id: str, scoping: ScopingResult, verification: dict, agent, *, timeout_seconds=LLM_DEFAULT_TIMEOUT_SECONDS
) -> dict:
    """Only the new early path uses verified rollback eligibility independently of root confirmation."""
    context = verification.get("rollback_context")
    if verification.get("valid") is not True or not context:
        return {
            "title": "신속 복구 검토",
            "summary": "검증된 롤백 입력이 부족합니다.",
            "reason": verification.get("reason", "복구 입력 호환성 검증을 완료하지 못했습니다."),
            "evidence_refs": [],
            "limitations": verification.get("limitations", []),
            "recommendation": "UNAVAILABLE",
            "playbook": None,
            "verification": verification,
        }

    class ObservedRecovery(RecoveryOutput):
        """Keep unsafe or target-drifting plans inside existing structured-output correction."""

        @model_validator(mode="after")
        def check_plan(self):
            """A proposed rollback must contain the complete validated causal recovery chain."""
            if self.recommendation == "ROLLBACK":
                if self.playbook is None or not self.playbook.execution_steps:
                    raise ValueError("rollback requires a complete playbook")
                steps = [step.model_dump() for step in self.playbook.execution_steps]
                validate_runbook(steps)
                validate_observed_plan(steps, context, scoping)
                validate_recovery_operations({**self.playbook.model_dump(mode="json"), "rollback_context": context})
                for index, step in enumerate(steps):
                    wait = step.get("metric_wait")
                    if wait and (
                        wait["failure_alarm_name"] not in step["success_criteria"]
                        or wait["metrics"]["failures"]["metric_name"] not in step["success_criteria"]
                    ):
                        raise ValueError(f"step {index}: success_criteria must name the failure metric and exact alarm")
            return self

    output = invoke_agent(
        agent,
        json.dumps({"scoping": scoping.model_dump(mode="json"), "verification": verification}, ensure_ascii=False),
        ObservedRecovery,
        timeout_seconds,
    )
    result = output.model_dump(mode="json")
    result["verification"] = verification
    if output.recommendation == "ROLLBACK":
        book = output.playbook
        result["playbook"] = Playbook(
            **book.model_dump(exclude={"execution_steps"}),
            playbook_id=str(uuid.uuid4()),
            rca_id=rca_id,
            rollback_context=context,
            execution_steps=[ExecutionStep(**step.model_dump()) for step in book.execution_steps],
        ).model_dump(mode="json")
    else:
        result["playbook"] = None
    return result


def generate_operations(
    incident: dict, root_result: dict, agent, *, timeout_seconds=LLM_DEFAULT_TIMEOUT_SECONDS
) -> dict:
    """Run operations from frozen inputs and the root outcome, without consulting approval or execution."""
    controls = [
        source
        for source in verified_sources(root_result.get("result", {}).get("control_artifacts", []))
        if source.get("source_kind") == "read_control_configuration"
    ]
    allowed_refs = {source["source_ref"] for source in controls}

    class QualifiedOperations(OperationsOutput):
        """An operational observation needs an actually read control, not an inferred missing safeguard."""

        @model_validator(mode="after")
        def check_control_evidence(self):
            """Keep unsupported findings UNVERIFIED while proposals remain NOT_RUN."""
            for finding in self.findings:
                if finding.status == "OBSERVED" and (
                    not finding.evidence_refs or not set(finding.evidence_refs) <= allowed_refs
                ):
                    raise ValueError(
                        "OBSERVED requires actually read CI/control source references; otherwise use UNVERIFIED"
                    )
            return self

    output = invoke_agent(
        agent,
        json.dumps(
            {
                "incident": incident,
                "root_result": root_result,
                "verified_control_sources": controls,
                "allowed_observed_control_refs": sorted(allowed_refs),
            },
            ensure_ascii=False,
        ),
        QualifiedOperations,
        timeout_seconds,
    )
    return output.model_dump(mode="json")


def source_artifacts_from_observations(observations: list) -> list[dict]:
    """Extract historical source bytes only when a supplied manifest binds the exact file hash."""
    records = []
    for observation in observations:
        try:
            body = json.loads(observation.get("summary", ""))
        except (ValueError, TypeError):
            continue
        for record in body.get("records", []):
            records.append((observation.get("id", ""), record))
    artifacts = []
    for observation_id, record in records:
        code = record.get("code")
        if not isinstance(code, dict) or not isinstance(code.get("text"), str) or not code.get("path"):
            continue
        digest = hashlib.sha256(code["text"].encode()).hexdigest()
        matches = [
            item["source"]
            for _, item in records
            if item.get("context") == record.get("context")
            and item.get("source", {}).get("files", {}).get(code["path"]) == digest
        ]
        if len(matches) != 1 or not matches[0].get("fingerprint"):
            continue
        artifacts.append(
            {
                "path": code["path"],
                "text": code["text"],
                "sha256": digest,
                "source_ref": f"observation:{observation_id}:{record.get('context', '')}",
                "base_revision": "snapshot:" + matches[0]["fingerprint"],
                "base_ref": "snapshot:" + matches[0]["fingerprint"],
                "source_phase": "current" if record.get("context") == "incident" else "baseline",
                "identity_kind": "source_snapshot",
            }
        )
    return artifacts
