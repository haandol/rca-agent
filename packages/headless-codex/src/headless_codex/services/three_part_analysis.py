"""Run three logical analysis parts under one budget, with progressive immutable publication."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field

from headless_codex.ports.dto.models import CodexResult
from headless_codex.services.analysis_part_contract import (
    unavailable_proposal,
    validate_code_proposal,
    validate_operations,
    validate_recovery_narrative,
)
from headless_codex.services.analysis_part_workspace import (
    CONTEXT_NAME,
    RESULT_FILES,
    activate_role,
    fixed_path,
    read_object,
    write_once,
)
from headless_codex.services.analysis_parts import PARTS, TERMINAL, WORKFLOW, json_bytes
from headless_codex.services.execution_evidence import redact


class AnalysisPartInterruptedError(RuntimeError):
    """The common deadline or ownership was lost; do not create later model work."""


class LocalPartStore:
    """Use the same envelope in standalone evaluation without constructing a cloud client."""

    def __init__(self, *, engine: str = "headless-codex"):
        """Keep one evaluation's private source and records isolated in its execution context."""
        self.engine = engine
        self.incident = None
        self.parts = {}
        self.objects = {}

    def _put_object(self, key: str, raw: bytes) -> None:
        """Local-only immutable backing for internal root artifacts, with no cloud API."""
        if key in self.objects and self.objects[key] != raw:
            raise ValueError("local source differs")
        self.objects[key] = raw

    def _read_object(self, key: str) -> bytes:
        """Return original bytes for local redelivery validation without invoking S3."""
        return self.objects[key]

    def read_incident(self, rca_id: str):
        """Return the same pinned input on local retries, never another evaluation's context."""
        return deepcopy(self.incident)

    def freeze_incident(self, rca_id, claim_token, attempt, incident):
        """Model evaluation freezes supplied historical observations, not simulated live control proof."""
        payload = {**incident, "schema_version": 1, "rca_id": rca_id, "engine": self.engine}
        digest = hashlib.sha256(json_bytes(payload)).hexdigest()
        value = {
            "record": {
                "payload_sha256": digest,
                "payload_s3_key": f"analysis-parts/{self.engine}/{rca_id}/incident/{digest}.json",
            },
            "payload": payload,
        }
        if self.incident is not None and self.incident != value:
            raise ValueError("local incident already frozen")
        self.incident = value
        return deepcopy(value)

    def read_part(self, rca_id, part):
        """Keep terminal model-eval results immutable and reusable within this local run."""
        return deepcopy(self.parts.get(part))

    def start_part(self, rca_id, part, claim_token, attempt):
        """Only start a role after the prior stored outcome, without consulting execution state."""
        index = PARTS.index(part)
        if index and (self.parts.get(PARTS[index - 1], {}).get("record", {}).get("status") not in TERMINAL):
            raise ValueError("previous local part is not complete")
        if self.parts.get(part, {}).get("record", {}).get("status") in TERMINAL:
            return self.read_part(rca_id, part)
        self.parts[part] = {"record": {"status": "RUNNING", "part": part}, "payload": None}
        return self.read_part(rca_id, part)

    def publish_part(
        self,
        rca_id,
        part,
        claim_token,
        attempt,
        *,
        result,
        status="COMPLETED",
        limitations=None,
        error="",
        input_refs=None,
        approval_status="UNAVAILABLE",
        runbook_digest="",
    ):
        """Local evaluation can expose results but never enable live approval from supplied flags."""
        if self.incident is None:
            raise ValueError("local incident not frozen")
        if part not in self.parts:
            if status != "SKIPPED":
                raise ValueError("local part not started")
            self.parts[part] = {"record": {"part": part, "status": "WAITING"}, "payload": None}
        if status not in TERMINAL or (status != "COMPLETED" and not error):
            raise ValueError("local failure requires a reason")
        ref = self.incident["record"]
        payload = {
            "schema_version": 1,
            "workflow": WORKFLOW,
            "rca_id": rca_id,
            "engine": self.engine,
            "part": part,
            "status": status,
            "incident_ref": {"key": ref["payload_s3_key"], "sha256": ref["payload_sha256"]},
            "input_refs": input_refs or [],
            "result": result,
            "limitations": limitations or [],
        }
        if error:
            payload["error"] = error
        revision = hashlib.sha256(json_bytes(payload)).hexdigest()
        current = self.parts[part]
        if current.get("payload") is not None and current["payload"] != payload:
            raise ValueError("local part is immutable")
        record = {
            "rca_id": rca_id,
            "engine": self.engine,
            "part": part,
            "status": status,
            "revision": revision,
            "payload_sha256": revision,
            "payload_s3_key": f"analysis-parts/{self.engine}/{rca_id}/{part}/{revision}.json",
        }
        if part == "recovery":
            record["approval_status"] = "UNAVAILABLE"
        self.parts[part] = {"record": record, "payload": payload}
        return self.read_part(rca_id, part)


@dataclass
class AnalysisPartsRun:
    """Server context and callback shared by all specialists, separate from any execution approval."""

    store: object
    rca_id: str
    incident: dict
    claim_token: str = ""
    attempt: int = 1
    model_eval: bool = False
    recovery_eligibility: dict | None = None
    on_published: Callable | None = None
    outcomes: dict = field(default_factory=dict)

    def _check(self, deadline: float, cancel_checker: Callable | None) -> None:
        """Stop for shared budget/cancellation; ordinary role failure alone may reach the next role."""
        if time.monotonic() >= deadline or (cancel_checker is not None and cancel_checker()):
            raise AnalysisPartInterruptedError(
                "analysis deadline, cancellation or ownership interrupted remaining parts"
            )

    def _publish(
        self,
        token: str,
        part: str,
        result: dict,
        *,
        deadline: float,
        cancel_checker,
        status="COMPLETED",
        error="",
        approval_status="UNAVAILABLE",
        runbook_digest="",
        bookkeeping=False,
    ) -> dict:
        """Commit immutable storage before notifying callers or admitting the next specialist."""
        if bookkeeping:
            if status not in {"FAILED", "SKIPPED"} or not error or (cancel_checker and cancel_checker()):
                raise AnalysisPartInterruptedError("terminal bookkeeping lost ownership or invalid outcome")
        else:
            self._check(deadline, cancel_checker)
        prior = self.outcomes.get(PARTS[PARTS.index(part) - 1]) if PARTS.index(part) else None
        refs = (
            []
            if prior is None
            else [{"key": prior["record"]["payload_s3_key"], "sha256": prior["record"]["payload_sha256"]}]
        )
        if part == "root_cause":
            from headless_codex.services.analysis_root_archive import archive_root_files

            refs.append(archive_root_files(self.store, self.rca_id, token))
        if part == "operations":
            from headless_codex.services.analysis_root_archive import archive_operations_sources

            source_ref = archive_operations_sources(self.store, self.rca_id, token)
            if source_ref is not None:
                refs.append(source_ref)
        outcome = self.store.publish_part(
            self.rca_id,
            part,
            self.claim_token,
            self.attempt,
            result=result,
            status=status,
            limitations=result.get("limitations", []),
            error=error,
            input_refs=refs,
            approval_status=approval_status,
            runbook_digest=runbook_digest,
        )
        self.outcomes[part] = outcome
        write_once(token, f"analysis-part-{part}.json", outcome)
        if self.on_published is not None:
            self.on_published(part, deepcopy(outcome))
        return outcome

    def _budget_end(self, token: str, *, deadline: float, cancel_checker) -> None:
        """After the hard model deadline, record only server-derived failure/skips; never accept late model success."""
        for part in PARTS:
            if part in self.outcomes:
                continue
            current = self.store.read_part(self.rca_id, part)
            if current and current.get("payload") is not None:
                self.outcomes[part] = current
                continue
            status = "FAILED" if current and current["record"].get("status") == "RUNNING" else "SKIPPED"
            self._publish(
                token,
                part,
                {},
                deadline=deadline,
                cancel_checker=cancel_checker,
                status=status,
                error="shared analysis deadline exhausted",
                bookkeeping=True,
            )

    def run(self, runner, prompt: str, **kwargs) -> CodexResult:
        """Deactivate role tools on every exit, including a failed publication or lost ownership."""
        if kwargs.get("profile") == "model-eval":
            if not isinstance(self.store, LocalPartStore):
                raise ValueError("standalone model-eval requires local part storage")
            self.model_eval = True
        try:
            return self._run_ordered(runner, prompt, **kwargs)
        finally:
            with suppress(OSError, ValueError):
                activate_role(kwargs["execution_token"], "finished", time.monotonic())

    def _run_ordered(
        self,
        runner,
        prompt: str,
        *,
        execution_token: str,
        report_prompt: str | None,
        deadline: float,
        cancel_checker: Callable | None,
        profile: str,
        **kwargs,
    ) -> CodexResult:
        """Recovery precedes the unchanged RCA/Report pair; operations follows its saved result or failure."""
        from headless_codex.adapters.secondary.codex.codex_harness import (
            ANALYSIS_OPERATIONS_PROFILE,
            ANALYSIS_RECOVERY_PROFILE,
            MODEL_EVAL_OPERATIONS_PROFILE,
            MODEL_EVAL_RECOVERY_PROFILE,
        )
        from headless_codex.services.analysis_contract import validate_analysis_completion
        from headless_codex.services.artifact_validation import validate_completion_artifacts

        token = execution_token
        outputs = []
        self._check(deadline, cancel_checker)
        pinned = self.store.read_incident(self.rca_id)
        if pinned is None:
            pinned = self.store.freeze_incident(self.rca_id, self.claim_token, self.attempt, self.incident)
        self.incident = pinned["payload"]
        write_once(token, CONTEXT_NAME, {"incident": self.incident, "model_eval": self.model_eval})
        exhausted = False
        for part in PARTS:
            if time.monotonic() >= deadline:
                self._budget_end(token, deadline=deadline, cancel_checker=cancel_checker)
                exhausted = True
                break
            self._check(deadline, cancel_checker)
            saved = self.store.read_part(self.rca_id, part)
            if saved and saved["record"].get("status") in TERMINAL:
                self.outcomes[part] = saved
                if part == "root_cause" and saved["record"]["status"] == "COMPLETED":
                    from headless_codex.services.analysis_root_archive import restore_root_files

                    archives = [
                        ref
                        for ref in saved["payload"].get("input_refs", [])
                        if ref.get("kind") == "root_internal_artifacts"
                    ]
                    if len(archives) != 1:
                        raise ValueError("completed root internal archive unavailable")
                    restore_root_files(self.store, self.rca_id, token, archives[0])
                write_once(token, f"analysis-part-{part}.json", saved)
                continue
            self.store.start_part(self.rca_id, part, self.claim_token, self.attempt)
            activate_role(token, "code_proposal" if part == "root_cause" else part, deadline)
            if part == "root_cause":
                run_result = runner._run_pair(
                    prompt,
                    execution_token=token,
                    profile=profile,
                    report_prompt=report_prompt,
                    cancel_checker=cancel_checker,
                    deadline=deadline,
                    part_mode=True,
                    **kwargs,
                )
            else:
                stage_profile = (
                    (MODEL_EVAL_RECOVERY_PROFILE if self.model_eval else ANALYSIS_RECOVERY_PROFILE)
                    if part == "recovery"
                    else (MODEL_EVAL_OPERATIONS_PROFILE if self.model_eval else ANALYSIS_OPERATIONS_PROFILE)
                )
                run_result = runner._run_with_retry(
                    "Read this role's instructions and the fixed read_analysis_context input. "
                    "Persist your own result through the dedicated tool. Never wait for an execution or approval.",
                    execution_token=token,
                    profile=stage_profile,
                    cancel_checker=cancel_checker,
                    deadline=deadline,
                    **kwargs,
                )
            outputs.append(run_result.raw_output)
            if time.monotonic() >= deadline:
                self._budget_end(token, deadline=deadline, cancel_checker=cancel_checker)
                exhausted = True
                break
            self._check(deadline, cancel_checker)
            if not run_result.success or run_result.cancelled:
                self._publish(
                    token,
                    part,
                    {},
                    deadline=deadline,
                    cancel_checker=cancel_checker,
                    status="FAILED",
                    error=redact(run_result.result)[:500] or "specialist failed",
                )
                continue
            try:
                approval, digest = "UNAVAILABLE", ""
                if part == "recovery":
                    result = validate_recovery_narrative(read_object(token, RESULT_FILES[part]))
                    # Only server-frozen observations can authorize a plan. A model supplies no such fields.
                    trusted = self.recovery_eligibility or {}
                    if (
                        not self.model_eval
                        and trusted.get("approval_status") == "READY"
                        and result["recommendation"] == "ROLLBACK"
                    ):
                        result.update(
                            recommendation="ROLLBACK",
                            playbook=deepcopy(trusted["playbook"]),
                            verification=deepcopy(trusted["verification"]),
                        )
                        approval, digest = "READY", trusted["runbook_digest"]
                    else:
                        result.update(
                            recommendation="UNAVAILABLE",
                            playbook=None,
                            verification=deepcopy(
                                trusted.get(
                                    "verification", {"valid": False, "reason": "verified recovery context unavailable"}
                                )
                            )
                            if not self.model_eval
                            else {"valid": False, "reason": "model-eval has no live recovery eligibility"},
                        )
                elif part == "operations":
                    from headless_codex.services.operations_sources import qualify_operations

                    sources = [
                        read_object(token, path.name)
                        for path in fixed_path(token, CONTEXT_NAME).parent.glob("operations-source-*.json")
                        if not path.is_symlink()
                    ]
                    result = qualify_operations(validate_operations(read_object(token, RESULT_FILES[part])), sources)
                else:
                    base = fixed_path(token, CONTEXT_NAME).parent
                    artifacts = validate_completion_artifacts(base)
                    analysis = validate_analysis_completion(base)
                    proposal = (
                        read_object(token, RESULT_FILES["code_proposal"])
                        if fixed_path(token, RESULT_FILES["code_proposal"]).exists()
                        else unavailable_proposal("코드 미리보기 원문이 저장되지 않았습니다.")
                    )
                    sources = list(self.incident.get("source_artifacts", []))
                    if fixed_path(token, "root-source-artifacts.json").exists():
                        sources.extend(read_object(token, "root-source-artifacts.json")["sources"])
                    proposal = validate_code_proposal(proposal, sources)
                    result = {
                        "title": "근본원인과 코드 수정 미리보기",
                        "summary": artifacts.root_cause,
                        "root_cause": {
                            "description": artifacts.root_cause,
                            "confirmed": artifacts.confirmed,
                            "confidence": analysis.selected_confidence,
                            "selected_hypothesis_id": analysis.selected_hypothesis.hypothesis_id,
                        },
                        "report_markdown": artifacts.report_markdown,
                        "code_proposal": proposal,
                    }
            except (ValueError, KeyError, OSError, TypeError) as exc:
                self._publish(
                    token,
                    part,
                    {},
                    deadline=deadline,
                    cancel_checker=cancel_checker,
                    status="FAILED",
                    error=redact(str(exc))[:500] or "invalid role output",
                )
                continue
            self._publish(
                token,
                part,
                result,
                deadline=deadline,
                cancel_checker=cancel_checker,
                approval_status=approval,
                runbook_digest=digest,
            )
        activate_role(token, "finished", time.monotonic())
        failed = [part for part, output in self.outcomes.items() if output["record"]["status"] != "COMPLETED"]
        return CodexResult(
            success=not exhausted,
            result=(
                "Analysis parts completed"
                if not failed
                else "Analysis parts completed with explicit failures: " + ", ".join(failed)
            ),
            raw_output="\n".join(outputs),
        )


def _report_list(label: str, values: list) -> str:
    """Keep every reference or limitation visible without exposing a serialized object."""
    return f"**{label}**\n\n" + "\n".join(f"- {value}" for value in values) if values else ""


def _report_code(text: str, language: str = "") -> str:
    """Preserve command/diff bytes, including embedded Markdown fences."""
    import re

    fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", text)), default=3))
    return f"{fence}{language}\n{text}" + ("" if text.endswith("\n") else "\n") + fence


def _recovery_report(result: dict, record: dict) -> list[str]:
    """Show a proposed recovery and its explicit success criteria, never imply execution occurred."""
    lines = [result.get("title", ""), result.get("summary", "")]
    if result:
        lines += [
            f"권고: {result.get('recommendation', 'UNAVAILABLE')}",
            f"승인 가능 상태: {record.get('approval_status', 'UNAVAILABLE')}",
            f"판단 근거: {result.get('reason', '')}",
        ]
    verification = result.get("verification") or {}
    if verification.get("reason"):
        lines.append(f"서버 검증 설명: {verification['reason']}")
    book = result.get("playbook") or {}
    if book:
        lines += ["### 복구 런북", "승인이 필요한 실행 계획입니다. 이 보고서는 실행 완료를 뜻하지 않습니다."]
        for step in book.get("execution_steps", []):
            lines += [
                f"#### {step.get('step_id', '')} · {step.get('action', '')}",
                f"목적: {step.get('intent', '')}",
                f"성공 기준: {step.get('success_criteria', '')}",
            ]
            for command in step.get("commands", []):
                lines.append(_report_code(command, "sh"))
            for key, label in (("deployment_wait", "배포 수렴 대기"), ("metric_wait", "지표 확인 대기")):
                wait = step.get(key)
                if wait:
                    lines.append(f"{label}: 최대 {wait.get('max_wait_seconds', 900)}초")
                    for field_name, field_label in (
                        ("task_definition", "대상 태스크 정의"),
                        ("deployment_step_id", "기준 배포 단계"),
                        ("failure_alarm_name", "확인 알람"),
                    ):
                        if wait.get(field_name):
                            lines.append(f"{field_label}: {wait[field_name]}")
        lines.append(_report_list("전체 검증 기준", book.get("verification_steps", [])))
    lines += [
        _report_list("근거", result.get("evidence_refs", [])),
        _report_list("한계", result.get("limitations", [])),
    ]
    return lines


def _code_proposal_report(proposal: dict) -> list[str]:
    """Render source-bound preview locations and diff; retain NOT_RUN and unavailable reasons."""
    if not proposal:
        return []
    lines = [
        "### 코드 PR 미리보기",
        proposal.get("title", ""),
        f"제안 상태: {proposal.get('status', 'UNAVAILABLE')}",
        f"테스트 상태: {proposal.get('tests_status', 'NOT_RUN')}",
        "미리보기이며 브랜치나 PR을 게시하지 않았습니다.",
    ]
    for key, label in (("repository", "저장소"), ("base_revision", "기준 리비전")):
        if proposal.get(key):
            lines.append(f"{label}: {proposal[key]}")
    for change in proposal.get("files", []):
        lines += [
            f"#### {change['path']} · {change['start_line']}–{change['end_line']}행",
            _report_code(change["unified_diff"], "diff"),
            _report_list("소스 근거", change.get("evidence_refs", [])),
        ]
    lines += [
        _report_list("검증 계획", proposal.get("test_plan", [])),
        _report_list("한계", proposal.get("limitations", [])),
    ]
    return lines


def _operations_report(result: dict) -> list[str]:
    """Separate observed findings from unverified controls and unexecuted recommendations."""
    lines = [result.get("title", ""), result.get("summary", "")]
    for finding in result.get("findings", []):
        lines += [
            f"- [{finding['status']}] {finding['statement']}",
            _report_list("관측 근거", finding.get("evidence_refs", [])),
        ]
    for recommendation in result.get("recommendations", []):
        lines += [f"### {recommendation['title']}", recommendation["description"]]
        for key, label in (
            ("stage", "적용 단계"),
            ("priority", "우선순위"),
            ("owner", "담당"),
            ("check", "확인 항목"),
            ("failure_condition", "실패 조건"),
            ("verification_plan", "검증 계획"),
            ("validation_status", "검증 상태"),
        ):
            if recommendation.get(key):
                lines.append(f"{label}: {recommendation[key]}")
        lines.append(_report_list("근거", recommendation.get("evidence_refs", [])))
    lines.append(_report_list("한계", result.get("limitations", [])))
    return lines


def render_parts_report(outcomes: dict, *, root_report: str | None = None) -> str:
    """Render three readable parts; keep full root prose and collapse immutable source references."""
    labels = {"recovery": "정상화", "root_cause": "근본원인·코드 수정", "operations": "운영 개선"}
    references = [
        {
            "part": name,
            **{key: outcomes[name]["record"][key] for key in ("revision", "payload_s3_key", "payload_sha256")},
        }
        for name in PARTS
    ]
    sections = ["# 사고 분석"]
    for part in PARTS:
        payload = outcomes[part]["payload"]
        result = payload.get("result", {})
        sections += [f"## {labels[part]}", f"상태: {payload['status']}"]
        if payload.get("error"):
            sections.append(f"제한 사유: {payload['error']}")
        if part == "root_cause":
            report = root_report if root_report is not None else result.get("report_markdown", "")
            if report:
                sections.append(_nested_markdown(report))
            sections.extend(_code_proposal_report(result.get("code_proposal", {})))
        elif part == "recovery":
            sections.extend(_recovery_report(result, outcomes[part]["record"]))
        else:
            sections.extend(_operations_report(result))
        sections.append(_report_list("파트 한계", payload.get("limitations", [])))
    sections.append(
        "<details>\n<summary>Analysis parts manifest · 원본 참조</summary>\n\n"
        + _report_code(json.dumps(references, ensure_ascii=False, indent=2), "json")
        + "\n\n</details>"
    )
    return "\n\n".join(section for section in sections if section)


def _nested_markdown(markdown: str) -> str:
    """Keep all root detail and code fences while nesting its headings under one logical part."""
    import re

    fence = None
    lines = []
    for line in markdown.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)[0]
            elif marker.group(1)[0] == fence:
                fence = None
        if fence is None and not marker:
            line = re.sub(r"^(#{1,6})(?= )", lambda m: "#" * min(6, len(m.group(1)) + 2), line)
        lines.append(line)
    return "".join(lines)
