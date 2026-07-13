from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from ..browser_state.text import estimate_tokens, normalize_page_text, normalized_fact_key
from .models import BrowserMessageKind, BrowserMessagePart, BrowserMessageRole, message_id


class BrowserHistoryDecisionKind(StrEnum):
    KEEP = "keep"
    DROP_DUPLICATE = "drop_duplicate"
    DROP_READ_ONCE = "drop_read_once"
    DROP_OLD_SUCCESS = "drop_old_success"
    TRUNCATE = "truncate"
    SYNTHESIZE = "synthesize"
    BLOCK_ORPHAN = "block_orphan"


@dataclass(frozen=True, slots=True)
class BrowserHistoryPolicy:
    max_tokens: int = 6000
    max_messages: int = 64
    preserve_recent_pairs: int = 8
    preserve_failures: int = 16
    preserve_state_messages: int = 1
    max_message_tokens: int = 1600
    summary_tokens: int = 900
    allow_read_once_replay: bool = False

    def __post_init__(self) -> None:
        values = (
            self.max_tokens, self.max_messages, self.preserve_recent_pairs,
            self.preserve_failures, self.preserve_state_messages,
            self.max_message_tokens, self.summary_tokens,
        )
        if min(values) <= 0:
            raise ValueError("browser history policy limits must be positive")


@dataclass(frozen=True, slots=True)
class BrowserHistoryDecision:
    message_id: str
    kind: BrowserHistoryDecisionKind
    reason: str
    original_tokens: int
    projected_tokens: int
    paired_message_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "kind": str(self.kind),
            "reason": self.reason,
            "original_tokens": self.original_tokens,
            "projected_tokens": self.projected_tokens,
            "paired_message_id": self.paired_message_id,
        }


@dataclass(frozen=True, slots=True)
class BrowserHistoryProjection:
    messages: tuple[BrowserMessagePart, ...]
    decisions: tuple[BrowserHistoryDecision, ...]
    input_messages: int
    output_messages: int
    input_tokens: int
    output_tokens: int
    tool_pairs_input: int
    tool_pairs_output: int
    orphan_calls: int
    orphan_results: int
    read_once_dropped: int
    duplicate_dropped: int
    summary_message_id: str = ""
    findings: tuple[str, ...] = ()

    @property
    def compression_ratio(self) -> float:
        return self.output_tokens / self.input_tokens if self.input_tokens else 1.0

    @property
    def tool_pair_fidelity(self) -> float:
        return self.tool_pairs_output / self.tool_pairs_input if self.tool_pairs_input else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [item.to_dict() for item in self.messages],
            "decisions": [item.to_dict() for item in self.decisions],
            "input_messages": self.input_messages,
            "output_messages": self.output_messages,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "compression_ratio": self.compression_ratio,
            "tool_pairs_input": self.tool_pairs_input,
            "tool_pairs_output": self.tool_pairs_output,
            "tool_pair_fidelity": self.tool_pair_fidelity,
            "orphan_calls": self.orphan_calls,
            "orphan_results": self.orphan_results,
            "read_once_dropped": self.read_once_dropped,
            "duplicate_dropped": self.duplicate_dropped,
            "summary_message_id": self.summary_message_id,
            "findings": list(self.findings),
        }


class BrowserHistoryNormalizer:
    """OMP-inspired deterministic history projection with atomic tool pairs.

    It does not own canonical session history.  Input comes from the current
    event/context owner and the returned projection is a bounded next-context
    view.  A call and its result are retained or removed together.
    """

    def __init__(self, policy: BrowserHistoryPolicy | None = None, *, disabled: bool = False) -> None:
        self.policy = policy or BrowserHistoryPolicy()
        self.disabled = disabled
        self._projections = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._blocked_orphans = 0

    def project(
        self,
        messages: Sequence[BrowserMessagePart],
        *,
        seen_source_ids: Iterable[str] = (),
        policy: BrowserHistoryPolicy | None = None,
    ) -> BrowserHistoryProjection:
        if self.disabled:
            raise RuntimeError("browser history normalizer is disabled")
        active_policy = policy or self.policy
        normalized = tuple(_normalize_message(item) for item in messages)
        input_tokens = sum(_tokens(item) for item in normalized)
        seen_sources = {str(item) for item in seen_source_ids if str(item)}
        pair_map, orphan_calls, orphan_results = _pair_messages(normalized)
        if orphan_calls or orphan_results:
            self._blocked_orphans += orphan_calls + orphan_results
            raise ValueError(
                "browser message history contains an orphan tool call/result; "
                f"calls={orphan_calls}, results={orphan_results}"
            )

        decisions: list[BrowserHistoryDecision] = []
        keep: set[int] = set(range(len(normalized)))
        fingerprints: dict[str, int] = {}
        state_indices = [index for index, item in enumerate(normalized) if item.kind == BrowserMessageKind.STATE]
        allowed_state = set(state_indices[-active_policy.preserve_state_messages :])

        for index, item in enumerate(normalized):
            tokens = _tokens(item)
            if item.read_once and item.source_id in seen_sources and not active_policy.allow_read_once_replay:
                _drop_atomic(index, keep, pair_map)
                decisions.append(BrowserHistoryDecision(
                    item.message_id, BrowserHistoryDecisionKind.DROP_READ_ONCE,
                    "read-once source was already consumed", tokens, 0,
                    _paired_id(index, normalized, pair_map),
                ))
                continue
            if item.kind == BrowserMessageKind.STATE and index not in allowed_state:
                _drop_atomic(index, keep, pair_map)
                decisions.append(BrowserHistoryDecision(
                    item.message_id, BrowserHistoryDecisionKind.DROP_OLD_SUCCESS,
                    "only the most recent authoritative browser state is disclosed", tokens, 0,
                ))
                continue
            previous_index = fingerprints.get(item.fingerprint)
            if previous_index is not None and not _is_failure(item):
                _drop_atomic(previous_index, keep, pair_map)
                previous = normalized[previous_index]
                decisions.append(BrowserHistoryDecision(
                    previous.message_id, BrowserHistoryDecisionKind.DROP_DUPLICATE,
                    f"superseded by duplicate message {item.message_id}", _tokens(previous), 0,
                    _paired_id(previous_index, normalized, pair_map),
                ))
            fingerprints[item.fingerprint] = index

        truncated: dict[int, BrowserMessagePart] = {}
        for index in sorted(keep):
            item = normalized[index]
            tokens = _tokens(item)
            if tokens <= active_policy.max_message_tokens:
                continue
            if item.kind in {BrowserMessageKind.ACTION_CALL, BrowserMessageKind.ACTION_RESULT}:
                replacement = _truncate_message(item, active_policy.max_message_tokens)
                truncated[index] = replacement
                decisions.append(BrowserHistoryDecision(
                    item.message_id, BrowserHistoryDecisionKind.TRUNCATE,
                    "tool message exceeded its per-result budget", tokens, _tokens(replacement),
                    _paired_id(index, normalized, pair_map),
                ))
            elif item.kind != BrowserMessageKind.STATE:
                replacement = _truncate_message(item, active_policy.max_message_tokens)
                truncated[index] = replacement
                decisions.append(BrowserHistoryDecision(
                    item.message_id, BrowserHistoryDecisionKind.TRUNCATE,
                    "message exceeded its per-message budget", tokens, _tokens(replacement),
                ))

        failures = [index for index in keep if _is_failure(normalized[index])]
        protected_failures = set(failures[-active_policy.preserve_failures :])
        pair_starts = [
            min(left, right) for left, right in _unique_pairs(pair_map)
            if left in keep and right in keep
        ]
        protected_pair_starts = set(pair_starts[-active_policy.preserve_recent_pairs :])
        protected: set[int] = set(protected_failures)
        for pair_start in protected_pair_starts:
            protected.add(pair_start)
            protected.add(pair_map[pair_start])
        protected.update(allowed_state)

        def projected_tokens() -> int:
            return sum(_tokens(truncated.get(index, normalized[index])) for index in keep)

        removable = sorted(
            (index for index in keep if index not in protected),
            key=lambda index: (_retention_score(normalized[index]), index),
        )
        for index in removable:
            if len(keep) <= active_policy.max_messages and projected_tokens() <= active_policy.max_tokens:
                break
            if index not in keep:
                continue
            item = normalized[index]
            paired = pair_map.get(index)
            _drop_atomic(index, keep, pair_map)
            decisions.append(BrowserHistoryDecision(
                item.message_id, BrowserHistoryDecisionKind.DROP_OLD_SUCCESS,
                "removed oldest low-priority history to fit the next-context budget",
                _tokens(item), 0,
                normalized[paired].message_id if paired is not None else "",
            ))

        summary = _summarize_dropped(normalized, keep, decisions, active_policy.summary_tokens)
        projected = [truncated.get(index, normalized[index]) for index in sorted(keep)]
        if summary is not None:
            projected.insert(0, summary)
            decisions.append(BrowserHistoryDecision(
                summary.message_id, BrowserHistoryDecisionKind.SYNTHESIZE,
                "deterministic summary of removed browser history", 0, _tokens(summary),
            ))

        while projected and (
            len(projected) > active_policy.max_messages
            or sum(_tokens(item) for item in projected) > active_policy.max_tokens
        ):
            candidate = next((item for item in projected if item.kind == BrowserMessageKind.COMPACTED_HISTORY), None)
            if candidate is not None:
                projected.remove(candidate)
                continue
            raise ValueError(
                "browser history cannot fit configured budget without breaking state, failure, or tool-pair invariants"
            )

        output_tokens = sum(_tokens(item) for item in projected)
        output_pair_map, output_orphan_calls, output_orphan_results = _pair_messages(projected)
        if output_orphan_calls or output_orphan_results:
            raise AssertionError("history projection broke atomic tool-pair preservation")
        tool_pairs_input = len(_unique_pairs(pair_map))
        tool_pairs_output = len(_unique_pairs(output_pair_map))
        findings: list[str] = []
        if output_tokens >= active_policy.max_tokens * 0.9:
            findings.append("context_budget_near_limit")
        if tool_pairs_output < tool_pairs_input:
            findings.append("old_tool_pairs_compacted")
        read_once_dropped = sum(item.kind == BrowserHistoryDecisionKind.DROP_READ_ONCE for item in decisions)
        duplicate_dropped = sum(item.kind == BrowserHistoryDecisionKind.DROP_DUPLICATE for item in decisions)
        projection = BrowserHistoryProjection(
            messages=tuple(projected),
            decisions=tuple(decisions),
            input_messages=len(normalized),
            output_messages=len(projected),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_pairs_input=tool_pairs_input,
            tool_pairs_output=tool_pairs_output,
            orphan_calls=0,
            orphan_results=0,
            read_once_dropped=read_once_dropped,
            duplicate_dropped=duplicate_dropped,
            summary_message_id=summary.message_id if summary else "",
            findings=tuple(findings),
        )
        self._projections += 1
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens
        return projection

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserHistoryNormalizer",
            "owner_unit": "M1-S04B-01",
            "canonical_history_owner": False,
            "disabled": self.disabled,
            "projections": self._projections,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "aggregate_ratio": self._output_tokens / self._input_tokens if self._input_tokens else 1.0,
            "blocked_orphans": self._blocked_orphans,
        }


def _normalize_message(item: BrowserMessagePart) -> BrowserMessagePart:
    content = normalize_page_text(item.content, limit=max(1000, len(item.content)))
    tokens = item.token_estimate or estimate_tokens(content)
    metadata = dict(item.metadata)
    metadata.setdefault("normalized", True)
    return replace(item, content=content, token_estimate=tokens, metadata=metadata)


def _tokens(item: BrowserMessagePart) -> int:
    return item.token_estimate or estimate_tokens(item.content)


def _pair_messages(messages: Sequence[BrowserMessagePart]) -> tuple[dict[int, int], int, int]:
    calls: dict[str, list[int]] = defaultdict(list)
    results: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(messages):
        pair_id = _pair_identity(item)
        if item.kind == BrowserMessageKind.ACTION_CALL:
            calls[pair_id].append(index)
        elif item.kind == BrowserMessageKind.ACTION_RESULT:
            results[pair_id].append(index)
    pair_map: dict[int, int] = {}
    orphan_calls = 0
    orphan_results = 0
    for pair_id in calls.keys() | results.keys():
        left = calls.get(pair_id, [])
        right = results.get(pair_id, [])
        count = min(len(left), len(right))
        for call_index, result_index in zip(left[:count], right[:count], strict=True):
            pair_map[call_index] = result_index
            pair_map[result_index] = call_index
        orphan_calls += len(left) - count
        orphan_results += len(right) - count
    return pair_map, orphan_calls, orphan_results


def _pair_identity(item: BrowserMessagePart) -> str:
    metadata = item.metadata
    for key in ("projection_id", "receipt_id", "tool_call_id", "request_id"):
        value = metadata.get(key) if isinstance(metadata, Mapping) else None
        if value:
            return str(value)
    if item.causation_id:
        return item.causation_id
    return item.source_id


def _unique_pairs(pair_map: Mapping[int, int]) -> set[tuple[int, int]]:
    return {(min(left, right), max(left, right)) for left, right in pair_map.items()}


def _drop_atomic(index: int, keep: set[int], pair_map: Mapping[int, int]) -> None:
    keep.discard(index)
    paired = pair_map.get(index)
    if paired is not None:
        keep.discard(paired)


def _paired_id(index: int, messages: Sequence[BrowserMessagePart], pair_map: Mapping[int, int]) -> str:
    paired = pair_map.get(index)
    return messages[paired].message_id if paired is not None else ""


def _truncate_message(item: BrowserMessagePart, max_tokens: int) -> BrowserMessagePart:
    max_chars = max(128, max_tokens * 4)
    content = item.content
    if len(content) <= max_chars:
        return item
    head_chars = max_chars * 2 // 3
    tail_chars = max_chars - head_chars
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    marker = f"\n[… externalized/trimmed; sha256={digest}; original_chars={len(content)} …]\n"
    value = content[: max(0, head_chars - len(marker))] + marker + content[-tail_chars:]
    metadata = {**dict(item.metadata), "truncated": True, "original_chars": len(content), "content_sha256": digest}
    return replace(item, content=value, token_estimate=estimate_tokens(value), metadata=metadata)


def _is_failure(item: BrowserMessagePart) -> bool:
    if item.kind != BrowserMessageKind.ACTION_RESULT:
        return False
    metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
    if metadata.get("ok") is False:
        return True
    status = str(metadata.get("status", "")).lower()
    return status in {"failed", "error", "denied", "blocked", "timeout"} or bool(metadata.get("error_code"))


def _retention_score(item: BrowserMessagePart) -> tuple[int, int, int, str]:
    failure = 1 if _is_failure(item) else 0
    state = 1 if item.kind == BrowserMessageKind.STATE else 0
    paired = 1 if item.kind in {BrowserMessageKind.ACTION_CALL, BrowserMessageKind.ACTION_RESULT} else 0
    return (failure + state, paired, item.priority, item.created_at)


def _summarize_dropped(
    messages: Sequence[BrowserMessagePart],
    keep: set[int],
    decisions: Sequence[BrowserHistoryDecision],
    max_tokens: int,
) -> BrowserMessagePart | None:
    dropped = [item for index, item in enumerate(messages) if index not in keep]
    if not dropped:
        return None
    kinds = Counter(str(item.kind) for item in dropped)
    successful_actions: list[str] = []
    failures: list[str] = []
    artifact_ids: list[str] = []
    selector_refs: list[str] = []
    source_ids: list[str] = []
    for item in dropped:
        artifact_ids.extend(item.artifact_ids)
        selector_refs.extend(item.selector_refs)
        source_ids.append(item.source_id)
        if item.kind == BrowserMessageKind.ACTION_RESULT:
            summary = normalize_page_text(item.content, limit=240)
            if _is_failure(item):
                failures.append(summary)
            else:
                successful_actions.append(summary)
    parts = [
        "Compacted browser history (deterministic, non-authoritative):",
        "removed message kinds: " + ", ".join(f"{key}={value}" for key, value in sorted(kinds.items())),
    ]
    if successful_actions:
        parts.append("completed actions: " + " | ".join(dict.fromkeys(successful_actions[-8:])))
    if failures:
        parts.append("preserved failure summaries: " + " | ".join(dict.fromkeys(failures[-8:])))
    if artifact_ids:
        parts.append("full details remain in artifacts: " + ", ".join(dict.fromkeys(artifact_ids)))
    text = "\n".join(parts)
    if estimate_tokens(text) > max_tokens:
        text = text[: max(128, max_tokens * 4)]
    source_digest = hashlib.sha256("|".join(source_ids).encode("utf-8")).hexdigest()[:24]
    return BrowserMessagePart(
        message_id=message_id("history", source_digest),
        role=BrowserMessageRole.META,
        kind=BrowserMessageKind.COMPACTED_HISTORY,
        content=text,
        source_id=f"browser-history:{source_digest}",
        artifact_ids=tuple(dict.fromkeys(artifact_ids)),
        selector_refs=tuple(dict.fromkeys(selector_refs)),
        trust="zyra_derived",
        read_once=False,
        priority=420,
        token_estimate=estimate_tokens(text),
        metadata={
            "source_message_count": len(dropped),
            "source_fingerprint": source_digest,
            "decision_count": len(decisions),
            "canonical_history_owner": False,
        },
    )
