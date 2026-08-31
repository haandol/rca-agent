from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from headless_codex.services.fault_taxonomy import FaultType, parse_fault_type

CONFIRMATION_THRESHOLD = 0.8
REJECTION_THRESHOLD = 0.3
TERMINATION_THRESHOLD = 0.9
MAX_VALIDATION_LOOPS = 3
MAX_REGENERATION_ROUNDS = 2
MAX_BRANCHING_DEPTH = 3

_VALIDATION_NAME = re.compile(r"validation-([1-9][0-9]*)\.json")
_HYPOTHESIS_ROUND_NAME = re.compile(r"hypotheses-([2-3])\.json")
_TERMINAL_BUCKETS = {"confirmed", "rejected", "closed"}
_RESULT_BUCKETS = ("confirmed", "rejected", "needs_investigation", "closed")
_CATEGORIES = {"DEPLOYMENT", "INFRASTRUCTURE", "TRAFFIC", "DEPENDENCY", "CONFIGURATION"}
_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣_]+")
_STOPWORDS = {
    "및",
    "인한",
    "으로",
    "의해",
    "때문",
    "대한",
    "관련",
    "문제",
    "원인",
    "장애",
    "발생",
    "the",
    "and",
    "for",
    "with",
    "from",
    "due",
    "to",
    "is",
    "of",
    "a",
    "an",
}
_ACCEPTED_SIMILARITY_THRESHOLD = 0.6


class AnalysisContractError(ValueError):
    pass


@dataclass(frozen=True)
class HypothesisContext:
    hypothesis_id: str
    tree_id: str
    title: str
    description: str
    initial_fault_type: FaultType
    category: str
    required_evidence: tuple[str, ...]
    depth: int
    parent_id: str | None
    initial_confidence: float


@dataclass(frozen=True)
class AnalysisDecision:
    action: str
    root_cause_confirmed: bool
    selected_hypothesis_id: str
    reason: str
    expansion_blocked: bool
    blocked_streak: int
    generation_round: int

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "root_cause_confirmed": self.root_cause_confirmed,
            "selected_hypothesis_id": self.selected_hypothesis_id,
            "reason": self.reason,
            "expansion_blocked": self.expansion_blocked,
            "blocked_streak": self.blocked_streak,
            "generation_round": self.generation_round,
        }


@dataclass
class AnalysisSnapshot:
    hypotheses: dict[str, HypothesisContext] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    confidences: dict[str, float] = field(default_factory=dict)
    reasoning: dict[str, str] = field(default_factory=dict)
    evidence_summaries: dict[str, list[str]] = field(default_factory=dict)
    evidence_failures: dict[str, bool] = field(default_factory=dict)
    validated_fault_types: dict[str, FaultType] = field(default_factory=dict)
    blocked_streak: int = 0
    generation_round: int = 1
    latest_decision: AnalysisDecision | None = None


@dataclass(frozen=True)
class AnalysisResult:
    snapshot: AnalysisSnapshot
    latest_validation: dict
    selected_hypothesis: HypothesisContext
    selected_confidence: float
    selected_fault_type: FaultType

    @property
    def confirmed(self) -> bool:
        return bool(self.snapshot.latest_decision and self.snapshot.latest_decision.root_cause_confirmed)


def _load_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except OSError as exc:
        raise AnalysisContractError(f"{label} is missing") from exc
    except json.JSONDecodeError as exc:
        raise AnalysisContractError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AnalysisContractError(f"{label} must be a JSON object")
    return value


def _parse_object(content: str, label: str) -> dict:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AnalysisContractError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AnalysisContractError(f"{label} must be a JSON object")
    return value


def _required_string(value: dict, field_name: str, label: str) -> str:
    field = value.get(field_name)
    if not isinstance(field, str) or not field.strip():
        raise AnalysisContractError(f"{label} required string field is missing: {field_name}")
    return field


def _confidence(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise AnalysisContractError(f"{label} confidence must be between 0 and 1")
    return float(value)


def generation_round_for_filename(filename: str) -> int | None:
    if filename == "hypotheses.json":
        return 1
    match = _HYPOTHESIS_ROUND_NAME.fullmatch(filename)
    return int(match.group(1)) if match else None


def hypothesis_round_paths(base: Path) -> list[tuple[int, Path]]:
    paths: list[tuple[int, Path]] = []
    initial = base / "hypotheses.json"
    if initial.is_file():
        paths.append((1, initial))
    for path in base.glob("hypotheses-*.json"):
        round_index = generation_round_for_filename(path.name)
        if round_index is not None:
            paths.append((round_index, path))
    paths.sort()
    if paths and [round_index for round_index, _ in paths] != list(range(1, len(paths) + 1)):
        raise AnalysisContractError("hypothesis generation rounds must be contiguous from 1")
    if len(paths) > MAX_REGENERATION_ROUNDS + 1:
        raise AnalysisContractError("hypothesis regeneration exceeds the maximum of 2 rounds")
    return paths


def validation_paths(base: Path, *, through_loop_index: int | None = None) -> list[tuple[int, Path]]:
    paths: list[tuple[int, Path]] = []
    for path in base.glob("validation-*.json"):
        match = _VALIDATION_NAME.fullmatch(path.name)
        if match is None:
            continue
        loop_index = int(match.group(1))
        if through_loop_index is None or loop_index <= through_loop_index:
            paths.append((loop_index, path))
    paths.sort()
    if not paths:
        raise AnalysisContractError("validation-{N}.json is missing")
    indexes = [index for index, _ in paths]
    if indexes != list(range(1, len(indexes) + 1)):
        raise AnalysisContractError("validation loop indexes must be contiguous from 1")
    if indexes[-1] > MAX_VALIDATION_LOOPS:
        raise AnalysisContractError("validation loop count exceeds the maximum of 3")
    return paths


def _validate_generation_artifact(
    artifact: dict,
    *,
    round_index: int,
    after_loop_index: int,
    known_ids: set[str],
) -> list[HypothesisContext]:
    label = "hypotheses.json" if round_index == 1 else f"hypotheses-{round_index}.json"
    if artifact.get("stage") != "HYPOTHESIS_GENERATION":
        raise AnalysisContractError(f"{label} stage must be HYPOTHESIS_GENERATION")
    tree_id = _required_string(artifact, "tree_id", label)
    hypotheses = artifact.get("hypotheses")
    if not isinstance(hypotheses, list) or not 3 <= len(hypotheses) <= 5:
        raise AnalysisContractError(f"{label} must contain 3 to 5 root hypotheses")

    if round_index == 1:
        if after_loop_index != 0:
            raise AnalysisContractError("the initial hypothesis round cannot follow a validation loop")
    elif after_loop_index < 1:
        raise AnalysisContractError(f"{label} must follow the validation loop that requested regeneration")

    contexts: list[HypothesisContext] = []
    current_ids: set[str] = set()
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict):
            raise AnalysisContractError(f"{label} hypothesis entries must be objects")
        hypothesis_id = _required_string(hypothesis, "hypothesis_id", label)
        if hypothesis_id in known_ids or hypothesis_id in current_ids:
            raise AnalysisContractError("hypothesis IDs must be unique across all generation rounds")
        current_ids.add(hypothesis_id)
        if _required_string(hypothesis, "tree_id", label) != tree_id:
            raise AnalysisContractError(f"{label} hypothesis tree_id must match the round tree_id")
        title = _required_string(hypothesis, "title", label)
        description = _required_string(hypothesis, "description", label)
        category = _required_string(hypothesis, "category", label)
        if category not in _CATEGORIES:
            raise AnalysisContractError(f"{label} hypothesis category is invalid")
        if hypothesis.get("status") != "PENDING":
            raise AnalysisContractError(f"{label} root hypotheses must start as PENDING")
        if hypothesis.get("parent_id") is not None:
            raise AnalysisContractError(f"{label} root hypotheses must not have a parent")
        if hypothesis.get("depth") != 0:
            raise AnalysisContractError(f"{label} root hypotheses must have depth 0")
        fault_type = parse_fault_type(hypothesis.get("fault_type"))
        if fault_type is None:
            raise AnalysisContractError(f"{label} hypothesis fault_type is invalid")
        required_evidence = hypothesis.get("required_evidence")
        if not isinstance(required_evidence, list) or any(
            not isinstance(item, str) or not item.strip() for item in required_evidence
        ):
            raise AnalysisContractError(f"{label} required_evidence must contain non-empty strings")
        contexts.append(
            HypothesisContext(
                hypothesis_id=hypothesis_id,
                tree_id=tree_id,
                title=title,
                description=description,
                initial_fault_type=fault_type,
                category=category,
                required_evidence=tuple(required_evidence),
                depth=0,
                parent_id=None,
                initial_confidence=_confidence(hypothesis.get("confidence_score"), label),
            )
        )
    return contexts


def normalize_generation_artifact(base: Path, filename: str, content: str) -> str:
    round_index = generation_round_for_filename(filename)
    if round_index is None:
        raise AnalysisContractError(f"unsupported hypothesis artifact filename: {filename}")
    existing_rounds = hypothesis_round_paths(base)
    expected_round = len(existing_rounds) + 1
    if round_index != expected_round:
        raise AnalysisContractError(f"expected hypothesis generation round {expected_round}, got {round_index}")

    after_loop_index = 0
    if round_index > 1:
        try:
            prior = replay_analysis(base, allow_incomplete=True)
        except AnalysisContractError as exc:
            raise AnalysisContractError(f"cannot regenerate hypotheses: {exc}") from exc
        decision = prior.snapshot.latest_decision
        if decision is None or decision.action != "REGENERATE":
            raise AnalysisContractError("hypothesis regeneration was not requested by the server")
        after_loop_index = len(validation_paths(base))

    artifact = _parse_object(content, filename)
    known_ids: set[str] = set()
    if existing_rounds:
        prior = replay_analysis(base, allow_incomplete=True)
        known_ids.update(prior.snapshot.hypotheses)
    _validate_generation_artifact(
        artifact,
        round_index=round_index,
        after_loop_index=after_loop_index,
        known_ids=known_ids,
    )
    artifact["generation_round"] = round_index
    artifact["after_loop_index"] = after_loop_index
    return json.dumps(artifact, ensure_ascii=False)


def _load_generation_round(
    snapshot: AnalysisSnapshot,
    artifact: dict,
    *,
    round_index: int,
    expected_after_loop: int,
) -> None:
    actual_round = artifact.get("generation_round", round_index)
    actual_after = artifact.get("after_loop_index", expected_after_loop)
    if actual_round != round_index:
        raise AnalysisContractError(f"hypothesis generation round metadata must equal {round_index}")
    if actual_after != expected_after_loop:
        raise AnalysisContractError(
            f"hypothesis generation round {round_index} must follow validation loop {expected_after_loop}"
        )
    contexts = _validate_generation_artifact(
        artifact,
        round_index=round_index,
        after_loop_index=expected_after_loop,
        known_ids=set(snapshot.hypotheses),
    )
    for context in contexts:
        snapshot.hypotheses[context.hypothesis_id] = context
        snapshot.statuses[context.hypothesis_id] = "PENDING"
        snapshot.confidences[context.hypothesis_id] = context.initial_confidence
        snapshot.reasoning[context.hypothesis_id] = ""
        snapshot.evidence_summaries[context.hypothesis_id] = []
        snapshot.evidence_failures[context.hypothesis_id] = False
        snapshot.validated_fault_types[context.hypothesis_id] = FaultType.UNSUPPORTED
    snapshot.generation_round = round_index


def _entry_evidence(entry: dict, label: str) -> list[str]:
    evidence = entry.get("evidence_summary", [])
    if not isinstance(evidence, list) or any(not isinstance(item, str) or not item.strip() for item in evidence):
        raise AnalysisContractError(f"{label} evidence_summary must contain non-empty strings")
    return evidence


def _classify(
    score: float,
    *,
    has_required_evidence: bool,
    evidence: list[str],
    evidence_failed: bool,
) -> str:
    if score >= CONFIRMATION_THRESHOLD:
        return "confirmed" if not has_required_evidence or (evidence and not evidence_failed) else "needs_investigation"
    if score <= REJECTION_THRESHOLD:
        return "rejected"
    return "needs_investigation"


def _remove_result_entry(artifact: dict, hypothesis_id: str) -> None:
    for bucket in _RESULT_BUCKETS:
        artifact[bucket] = [entry for entry in artifact[bucket] if entry.get("hypothesis_id") != hypothesis_id]


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text)} - _STOPWORDS


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _auto_reject_similar(snapshot: AnalysisSnapshot, normalized: dict) -> None:
    accepted = [
        context
        for hypothesis_id, context in snapshot.hypotheses.items()
        if snapshot.statuses.get(hypothesis_id) == "confirmed"
    ]
    if not accepted:
        return

    accepted_tokens = [(context, _tokens(context.description)) for context in accepted]
    for hypothesis_id, status in list(snapshot.statuses.items()):
        if status not in {"PENDING", "needs_investigation"}:
            continue
        hypothesis = snapshot.hypotheses[hypothesis_id]
        hypothesis_tokens = _tokens(hypothesis.description)
        if not any(
            hypothesis.category == accepted_hypothesis.category
            and _jaccard(hypothesis_tokens, tokens) >= _ACCEPTED_SIMILARITY_THRESHOLD
            for accepted_hypothesis, tokens in accepted_tokens
        ):
            continue
        _remove_result_entry(normalized, hypothesis_id)
        normalized["rejected"].append(
            {
                "hypothesis_id": hypothesis_id,
                "confidence": snapshot.confidences.get(hypothesis_id, 0.0),
                "reasoning": "Review gate: an accepted hypothesis already covers the same cause area",
                "evidence_summary": snapshot.evidence_summaries.get(hypothesis_id, []),
                "evidence_collection_failed": snapshot.evidence_failures.get(hypothesis_id, False),
                "server_rejected": True,
            }
        )
        snapshot.statuses[hypothesis_id] = "rejected"
        snapshot.reasoning[hypothesis_id] = "Review gate: accepted-similar hypothesis"


def _best_hypothesis(snapshot: AnalysisSnapshot, candidates: list[str] | None = None) -> str:
    ids = candidates or list(snapshot.hypotheses)
    if not ids:
        return ""
    return max(ids, key=lambda hypothesis_id: snapshot.confidences.get(hypothesis_id, 0.0))


def _decision_for(
    snapshot: AnalysisSnapshot,
    loop_index: int,
    *,
    accepted_before_loop: bool,
) -> AnalysisDecision:
    confirmed_ids = [hypothesis_id for hypothesis_id, status in snapshot.statuses.items() if status == "confirmed"]
    strong = [
        hypothesis_id
        for hypothesis_id in confirmed_ids
        if snapshot.confidences.get(hypothesis_id, 0.0) >= TERMINATION_THRESHOLD
    ]
    if strong:
        selected = _best_hypothesis(snapshot, strong)
        return AnalysisDecision(
            "REPORT",
            True,
            selected,
            "CONFIRMED",
            False,
            0,
            snapshot.generation_round,
        )

    accepted = [
        hypothesis_id
        for hypothesis_id in confirmed_ids
        if snapshot.confidences.get(hypothesis_id, 0.0) >= CONFIRMATION_THRESHOLD
    ]
    new_streak = snapshot.blocked_streak + 1 if accepted and accepted_before_loop else 0
    if accepted and new_streak >= 2:
        selected = _best_hypothesis(snapshot, accepted)
        return AnalysisDecision(
            "REPORT",
            True,
            selected,
            "REVIEW_GATE_GRACE_EXHAUSTED",
            False,
            new_streak,
            snapshot.generation_round,
        )

    current_max_depth = max((hypothesis.depth for hypothesis in snapshot.hypotheses.values()), default=0)
    if current_max_depth >= MAX_BRANCHING_DEPTH:
        return AnalysisDecision(
            "REPORT",
            False,
            _best_hypothesis(snapshot),
            "MAX_DEPTH",
            False,
            new_streak,
            snapshot.generation_round,
        )

    if loop_index >= MAX_VALIDATION_LOOPS:
        return AnalysisDecision(
            "REPORT",
            False,
            _best_hypothesis(snapshot),
            "MAX_LOOPS",
            False,
            new_streak,
            snapshot.generation_round,
        )

    statuses = list(snapshot.statuses.values())
    if statuses and all(status in {"rejected", "closed"} for status in statuses):
        if snapshot.generation_round <= MAX_REGENERATION_ROUNDS:
            return AnalysisDecision(
                "REGENERATE",
                False,
                _best_hypothesis(snapshot),
                "ALL_REJECTED",
                False,
                0,
                snapshot.generation_round,
            )
        return AnalysisDecision(
            "REPORT",
            False,
            _best_hypothesis(snapshot),
            "ALL_REJECTED",
            False,
            0,
            snapshot.generation_round,
        )

    return AnalysisDecision(
        "CONTINUE",
        False,
        _best_hypothesis(snapshot, accepted) if accepted else _best_hypothesis(snapshot),
        "EXPANSION_BLOCKED" if accepted else "CONTINUE",
        bool(accepted),
        new_streak,
        snapshot.generation_round,
    )


def _validate_new_hypotheses(
    artifact: dict,
    snapshot: AnalysisSnapshot,
    *,
    classifications: dict[str, str],
    label: str,
) -> list[HypothesisContext]:
    new_hypotheses = artifact.get("new_hypotheses")
    if not isinstance(new_hypotheses, list):
        raise AnalysisContractError(f"{label} new_hypotheses must be a list")

    contexts: list[HypothesisContext] = []
    per_parent: dict[str, int] = {}
    new_ids: set[str] = set()
    for hypothesis in new_hypotheses:
        if not isinstance(hypothesis, dict):
            raise AnalysisContractError(f"{label} new_hypotheses entries must be objects")
        hypothesis_id = _required_string(hypothesis, "hypothesis_id", label)
        if hypothesis_id in snapshot.hypotheses or hypothesis_id in new_ids:
            raise AnalysisContractError(f"{label} new hypothesis IDs must be unique")
        new_ids.add(hypothesis_id)
        parent_id = _required_string(hypothesis, "parent_id", label)
        parent = snapshot.hypotheses.get(parent_id)
        if parent is None:
            raise AnalysisContractError(f"{label} new hypothesis parent_id is unknown")
        if classifications.get(parent_id, snapshot.statuses.get(parent_id)) != "needs_investigation":
            raise AnalysisContractError(f"{label} may branch only from NEEDS_INVESTIGATION")
        depth = hypothesis.get("depth")
        if isinstance(depth, bool) or not isinstance(depth, int) or depth != parent.depth + 1:
            raise AnalysisContractError(f"{label} new hypothesis depth must equal parent depth + 1")
        if depth > MAX_BRANCHING_DEPTH:
            raise AnalysisContractError(f"{label} new hypothesis depth exceeds 3")
        if hypothesis.get("status") != "PENDING":
            raise AnalysisContractError(f"{label} new hypotheses must start as PENDING")
        tree_id = _required_string(hypothesis, "tree_id", label)
        if tree_id != parent.tree_id:
            raise AnalysisContractError(f"{label} child tree_id must match its parent")
        category = _required_string(hypothesis, "category", label)
        if category not in _CATEGORIES:
            raise AnalysisContractError(f"{label} new hypothesis category is invalid")
        fault_type = parse_fault_type(hypothesis.get("fault_type"))
        if fault_type is None:
            raise AnalysisContractError(f"{label} new hypothesis fault_type is invalid")
        required_evidence = hypothesis.get("required_evidence")
        if not isinstance(required_evidence, list) or any(
            not isinstance(item, str) or not item.strip() for item in required_evidence
        ):
            raise AnalysisContractError(f"{label} new hypothesis required_evidence is invalid")
        per_parent[parent_id] = per_parent.get(parent_id, 0) + 1
        if per_parent[parent_id] > 3:
            raise AnalysisContractError(f"{label} creates more than 3 children for one parent")
        contexts.append(
            HypothesisContext(
                hypothesis_id=hypothesis_id,
                tree_id=tree_id,
                title=_required_string(hypothesis, "title", label),
                description=_required_string(hypothesis, "description", label),
                initial_fault_type=fault_type,
                category=category,
                required_evidence=tuple(required_evidence),
                depth=depth,
                parent_id=parent_id,
                initial_confidence=_confidence(hypothesis.get("confidence_score"), label),
            )
        )
    return contexts


def _apply_validation_loop(
    snapshot: AnalysisSnapshot,
    artifact: dict,
    *,
    loop_index: int,
    accept_server_metadata: bool = False,
) -> dict:
    label = f"validation-{loop_index}.json"
    if artifact.get("stage") != "VALIDATION" or artifact.get("loop_index") != loop_index:
        raise AnalysisContractError(f"{label} has an invalid stage or loop_index")
    for field_name in ("summary", "output_summary"):
        _required_string(artifact, field_name, label)
    for bucket in _RESULT_BUCKETS:
        if not isinstance(artifact.get(bucket), list):
            raise AnalysisContractError(f"{label} {bucket} must be a list")
    if artifact.get("server_decision") is not None and not accept_server_metadata:
        raise AnalysisContractError(f"{label} server_decision is server-owned")
    if artifact["closed"] and (
        not accept_server_metadata
        or any(not isinstance(entry, dict) or entry.get("server_closed") is not True for entry in artifact["closed"])
    ):
        raise AnalysisContractError(f"{label} closed classifications are server-owned")

    accepted_before_loop = any(status == "confirmed" for status in snapshot.statuses.values())
    entries: list[dict] = []
    seen_ids: set[str] = set()
    for bucket in ("confirmed", "rejected", "needs_investigation"):
        for raw_entry in artifact[bucket]:
            if not isinstance(raw_entry, dict):
                raise AnalysisContractError(f"{label} {bucket} entries must be objects")
            hypothesis_id = _required_string(raw_entry, "hypothesis_id", label)
            if hypothesis_id not in snapshot.hypotheses:
                raise AnalysisContractError(f"{label} references an unknown hypothesis")
            if hypothesis_id in seen_ids:
                raise AnalysisContractError(f"{label} references a hypothesis more than once")
            seen_ids.add(hypothesis_id)
            entry = dict(raw_entry)
            entry["confidence"] = _confidence(entry.get("confidence"), label)
            entry["reasoning"] = _required_string(entry, "reasoning", label)
            entry["evidence_summary"] = _entry_evidence(entry, label)
            if not isinstance(entry.get("evidence_collection_failed"), bool):
                raise AnalysisContractError(f"{label} evidence_collection_failed must be a boolean")
            if entry.get("server_rejected") is True and not accept_server_metadata:
                raise AnalysisContractError(f"{label} server_rejected is server-owned")
            entries.append(entry)

    classifications: dict[str, str] = {}
    normalized = dict(artifact)
    normalized.update({bucket: [] for bucket in _RESULT_BUCKETS})
    for entry in entries:
        hypothesis_id = entry["hypothesis_id"]
        context = snapshot.hypotheses[hypothesis_id]
        bucket = _classify(
            entry["confidence"],
            has_required_evidence=bool(context.required_evidence),
            evidence=entry["evidence_summary"],
            evidence_failed=entry["evidence_collection_failed"],
        )
        classifications[hypothesis_id] = bucket
        if bucket == "confirmed":
            validated_fault_type = parse_fault_type(entry.get("fault_type"))
            if validated_fault_type is None:
                raise AnalysisContractError(f"{label} confirmed fault_type is invalid")
            snapshot.validated_fault_types[hypothesis_id] = validated_fault_type
        normalized[bucket].append(entry)
        snapshot.statuses[hypothesis_id] = bucket
        snapshot.confidences[hypothesis_id] = entry["confidence"]
        snapshot.reasoning[hypothesis_id] = entry["reasoning"]
        snapshot.evidence_summaries[hypothesis_id] = entry["evidence_summary"]
        snapshot.evidence_failures[hypothesis_id] = entry["evidence_collection_failed"]

    contexts = _validate_new_hypotheses(
        artifact,
        snapshot,
        classifications=classifications,
        label=label,
    )
    normalized["new_hypotheses"] = artifact["new_hypotheses"]
    for context in contexts:
        snapshot.hypotheses[context.hypothesis_id] = context
        snapshot.statuses[context.hypothesis_id] = "PENDING"
        snapshot.confidences[context.hypothesis_id] = context.initial_confidence
        snapshot.reasoning[context.hypothesis_id] = ""
        snapshot.evidence_summaries[context.hypothesis_id] = []
        snapshot.evidence_failures[context.hypothesis_id] = False
        snapshot.validated_fault_types[context.hypothesis_id] = FaultType.UNSUPPORTED

    _auto_reject_similar(snapshot, normalized)
    decision = _decision_for(
        snapshot,
        loop_index,
        accepted_before_loop=accepted_before_loop,
    )
    if decision.action == "REPORT":
        for hypothesis_id, status in list(snapshot.statuses.items()):
            if status not in {"PENDING", "needs_investigation"}:
                continue
            _remove_result_entry(normalized, hypothesis_id)
            normalized["closed"].append(
                {
                    "hypothesis_id": hypothesis_id,
                    "confidence": snapshot.confidences.get(hypothesis_id, 0.0),
                    "reasoning": f"Server termination: {decision.reason}",
                    "evidence_summary": snapshot.evidence_summaries.get(hypothesis_id, []),
                    "evidence_collection_failed": snapshot.evidence_failures.get(hypothesis_id, False),
                    "server_closed": True,
                }
            )
            snapshot.statuses[hypothesis_id] = "closed"
            snapshot.reasoning[hypothesis_id] = f"Server termination: {decision.reason}"

    normalized["server_decision"] = decision.as_dict()
    snapshot.blocked_streak = decision.blocked_streak
    snapshot.latest_decision = decision
    return normalized


def normalize_validation_artifact(base: Path, filename: str, content: str) -> tuple[str, AnalysisDecision]:
    match = _VALIDATION_NAME.fullmatch(filename)
    if match is None:
        raise AnalysisContractError(f"unsupported validation artifact filename: {filename}")
    loop_index = int(match.group(1))
    if loop_index > MAX_VALIDATION_LOOPS:
        raise AnalysisContractError("validation loop count exceeds the maximum of 3")
    existing = []
    try:
        existing = validation_paths(base)
    except AnalysisContractError as exc:
        if "is missing" not in str(exc):
            raise
    expected_loop = len(existing) + 1
    if loop_index != expected_loop:
        raise AnalysisContractError(f"expected validation loop {expected_loop}, got {loop_index}")
    replay = replay_analysis(base, allow_incomplete=True)
    if replay.snapshot.latest_decision and replay.snapshot.latest_decision.action == "REPORT":
        raise AnalysisContractError("the server already terminated analysis")
    if replay.snapshot.latest_decision and replay.snapshot.latest_decision.action == "REGENERATE":
        raise AnalysisContractError("a new hypothesis generation round is required before validation continues")
    normalized = _apply_validation_loop(
        replay.snapshot,
        _parse_object(content, filename),
        loop_index=loop_index,
    )
    return json.dumps(normalized, ensure_ascii=False), replay.snapshot.latest_decision


def replay_analysis(
    base: Path,
    *,
    through_loop_index: int | None = None,
    allow_incomplete: bool = False,
) -> AnalysisResult:
    rounds = hypothesis_round_paths(base)
    if not rounds:
        raise AnalysisContractError("hypotheses.json is missing")
    if rounds[0][0] != 1:
        raise AnalysisContractError("the initial hypothesis generation round is missing")

    snapshot = AnalysisSnapshot()
    round_artifacts = {round_index: _load_object(path, path.name) for round_index, path in rounds}
    _load_generation_round(snapshot, round_artifacts[1], round_index=1, expected_after_loop=0)

    try:
        validations = validation_paths(base, through_loop_index=through_loop_index)
    except AnalysisContractError as exc:
        if allow_incomplete and "is missing" in str(exc):
            selected_id = _best_hypothesis(snapshot)
            return AnalysisResult(
                snapshot=snapshot,
                latest_validation={},
                selected_hypothesis=snapshot.hypotheses[selected_id],
                selected_confidence=snapshot.confidences[selected_id],
                selected_fault_type=FaultType.UNSUPPORTED,
            )
        raise

    latest_validation: dict = {}
    for loop_index, path in validations:
        if snapshot.latest_decision and snapshot.latest_decision.action == "REPORT":
            raise AnalysisContractError(f"{path.name} appears after the server terminated analysis")
        if snapshot.latest_decision and snapshot.latest_decision.action == "REGENERATE":
            next_round = snapshot.generation_round + 1
            artifact = round_artifacts.get(next_round)
            if artifact is None:
                raise AnalysisContractError(
                    f"hypotheses-{next_round}.json is required after validation-{loop_index - 1}.json"
                )
            _load_generation_round(
                snapshot,
                artifact,
                round_index=next_round,
                expected_after_loop=loop_index - 1,
            )
            snapshot.latest_decision = None
        artifact = _load_object(path, path.name)
        server_decision = artifact.get("server_decision")
        if server_decision is not None and not isinstance(server_decision, dict):
            raise AnalysisContractError(f"{path.name} server_decision must be an object")
        normalized = _apply_validation_loop(
            snapshot,
            artifact,
            loop_index=loop_index,
            accept_server_metadata=server_decision is not None,
        )
        if server_decision is not None and server_decision != normalized["server_decision"]:
            raise AnalysisContractError(f"{path.name} server_decision does not match the server reducer")
        latest_validation = normalized

    if snapshot.latest_decision and snapshot.latest_decision.action == "REGENERATE":
        next_round = snapshot.generation_round + 1
        if next_round in round_artifacts:
            _load_generation_round(
                snapshot,
                round_artifacts[next_round],
                round_index=next_round,
                expected_after_loop=len(validations),
            )
            snapshot.latest_decision = None
        elif not allow_incomplete:
            raise AnalysisContractError(f"hypotheses-{next_round}.json is required after all hypotheses were rejected")

    expected_rounds = snapshot.generation_round
    if len(round_artifacts) != expected_rounds:
        raise AnalysisContractError("a hypothesis generation round exists without a server regeneration decision")

    selected_id = (
        snapshot.latest_decision.selected_hypothesis_id
        if snapshot.latest_decision and snapshot.latest_decision.selected_hypothesis_id
        else _best_hypothesis(snapshot)
    )
    selected = snapshot.hypotheses[selected_id]
    return AnalysisResult(
        snapshot=snapshot,
        latest_validation=latest_validation,
        selected_hypothesis=selected,
        selected_confidence=snapshot.confidences.get(selected_id, selected.initial_confidence),
        selected_fault_type=snapshot.validated_fault_types.get(selected_id, FaultType.UNSUPPORTED),
    )


def validate_analysis_completion(base: Path) -> AnalysisResult:
    result = replay_analysis(base)
    decision = result.snapshot.latest_decision
    if decision is None or decision.action != "REPORT":
        raise AnalysisContractError("analysis did not reach a server-owned REPORT decision")
    nonterminal = [
        hypothesis_id for hypothesis_id, status in result.snapshot.statuses.items() if status not in _TERMINAL_BUCKETS
    ]
    if nonterminal:
        raise AnalysisContractError(f"analysis has non-terminal hypotheses: {', '.join(sorted(nonterminal))}")
    return result
