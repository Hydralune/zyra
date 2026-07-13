from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from ..browser_state.contracts import (
    BrowserContextDisclosure,
    BrowserDisclosureBudget,
    BrowserDomCapture,
    BrowserLowEntropyMetrics,
    BrowserSelectorMapRevision,
    state_id,
)
from ..browser_state.dom_builder import dom_fact_candidates
from ..browser_state.errors import BrowserStateBudgetExceeded
from ..browser_state.selector_ranker import BrowserSelectorRanker
from ..browser_state.semantic_sections import BrowserSemanticOutline
from ..browser_state.state_delta import BrowserDomDelta, change_kind_counts
from ..browser_state.text import estimate_tokens, normalize_page_text, normalized_fact_key
from .models import BrowserActionResultProjection, BrowserArtifactExternalization


class BrowserStateCompressor:
    """Deterministic browser-specific disclosure, not a global compact owner."""

    def __init__(
        self,
        *,
        selector_ranker: BrowserSelectorRanker | None = None,
        disabled: bool = False,
    ) -> None:
        self.disabled = disabled
        self.selector_ranker = selector_ranker or BrowserSelectorRanker()
        self._compressions = 0
        self._failures = 0
        self._full_bytes = 0
        self._disclosure_bytes = 0

    def compress(
        self,
        capture: BrowserDomCapture,
        selector_revision: BrowserSelectorMapRevision,
        action_results: Sequence[BrowserActionResultProjection],
        externalization: BrowserArtifactExternalization,
        *,
        budget: BrowserDisclosureBudget,
        goal: str = "",
        constraints: Sequence[str] = (),
        previous_facts: Iterable[str] = (),
        semantic_outline: BrowserSemanticOutline | None = None,
        dom_delta: BrowserDomDelta | None = None,
    ) -> BrowserContextDisclosure:
        if self.disabled:
            raise BrowserStateBudgetExceeded("browser state compressor is disabled")
        if capture.metrics.full_state_bytes > budget.raw_externalize_bytes and not externalization.complete:
            raise BrowserStateBudgetExceeded(
                "large browser state must be externalized before disclosure",
                details={
                    "full_state_bytes": capture.metrics.full_state_bytes,
                    "externalize_threshold": budget.raw_externalize_bytes,
                    "externalization_complete": externalization.complete,
                },
            )
        all_facts = tuple(dom_fact_candidates(capture.root, max_facts=max(512, budget.max_inline_facts * 4)))
        selected_facts, fact_counts = _select_facts(
            all_facts,
            previous_facts=previous_facts,
            limit=budget.max_inline_facts,
            max_chars=budget.max_fact_chars,
        )
        critical_facts = _critical_facts(goal, constraints, capture, all_facts)
        selector_selection = self.selector_ranker.select(
            selector_revision,
            budget.max_inline_selectors,
            goal=goal,
            constraints=constraints,
        )
        selected_entries = selector_selection.selected
        selected_refs = tuple(entry.ref.opaque_ref for entry in selected_entries)
        action_payloads = [_action_payload(item) for item in _select_action_pairs(action_results, budget.preserve_recent_actions)]
        payload = {
            "schema": "zyra.browser-context-disclosure.v1",
            "capture": {
                "capture_id": capture.capture_id,
                "state_digest": capture.state_digest,
                "completeness": str(capture.completeness),
                "warnings": list(capture.warnings),
            },
            "identity": capture.request.identity_dict(),
            "page": {
                "url": _root_attribute(capture, "documentURL"),
                "title": _page_title(capture),
                "viewport": capture.viewport.to_dict(),
                "frames": [frame.to_dict() for frame in capture.frames[:16]],
            },
            "goal": normalize_page_text(goal, limit=1200),
            "constraints": [normalize_page_text(item, limit=500) for item in constraints[:24]],
            "facts": list(selected_facts),
            "semantic_outline": (
                semantic_outline.to_dict(section_limit=12)
                if semantic_outline is not None else {}
            ),
            "dom_delta": (
                {
                    **dom_delta.to_dict(change_limit=16),
                    "change_kind_counts": change_kind_counts(dom_delta),
                }
                if dom_delta is not None else {}
            ),
            "selectors": [
                {
                    "ref": entry.ref.opaque_ref,
                    "index": entry.ref.selector_index,
                    "tag": entry.tag_name,
                    "role": entry.role,
                    "name": entry.accessible_name,
                    "text": entry.text_preview,
                    "frame_id": entry.frame_id,
                    "visible": entry.visible,
                    "disabled": entry.disabled,
                }
                for entry in selected_entries
            ],
            "selector_map": {
                "revision_id": selector_revision.revision_id,
                "revision": selector_revision.revision,
                "identity_digest": selector_revision.identity.digest,
                "entry_count": len(selector_revision.entries),
                "artifact_id": selector_revision.artifact_id,
                "authoritative": True,
                "selection": {
                    "total": selector_selection.total,
                    "selected": len(selector_selection.selected),
                    "visible_total": selector_selection.visible_total,
                    "interactive_total": selector_selection.interactive_total,
                    "frame_coverage": selector_selection.frame_coverage,
                    "selected_frame_coverage": selector_selection.selected_frame_coverage,
                    "goal_matched": selector_selection.goal_matched,
                    "frame_fidelity": selector_selection.frame_fidelity,
                },
            },
            "action_results": action_payloads,
            "artifact_refs": list(externalization.artifact_ids),
            "security": {
                "trust": "external_untrusted",
                "page_instructions_are_data": True,
                "redacted": True,
            },
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        text, payload, selected_facts, selected_entries = self._fit_budget(
            text,
            payload,
            selected_facts,
            selected_entries,
            budget,
        )
        disclosure_bytes = len(text.encode("utf-8"))
        disclosure_tokens = estimate_tokens(text)
        repeated_count = sum(max(0, count - 1) for count in fact_counts.values())
        unique_count = len(fact_counts)
        preserved_critical = sum(
            any(normalized_fact_key(critical) in normalized_fact_key(selected) or normalized_fact_key(selected) in normalized_fact_key(critical) for selected in selected_facts)
            for critical in critical_facts
        )
        metrics = BrowserLowEntropyMetrics(
            full_state_bytes=capture.metrics.full_state_bytes,
            full_state_tokens=capture.metrics.full_state_tokens,
            disclosure_bytes=disclosure_bytes,
            disclosure_tokens=disclosure_tokens,
            repeated_fact_count=repeated_count,
            unique_fact_count=unique_count,
            duplicate_fact_rate=(repeated_count / (repeated_count + unique_count) if repeated_count + unique_count else 0.0),
            artifact_offload_bytes=externalization.externalized_bytes,
            artifact_offload_ratio=externalization.offload_ratio,
            selector_total=len(selector_revision.entries),
            selector_inline=len(selected_entries),
            selector_fidelity=1.0 if selector_revision.entries or capture.selector_count == 0 else 0.0,
            critical_fact_total=len(critical_facts),
            critical_fact_preserved=preserved_critical,
            critical_fact_fidelity=preserved_critical / len(critical_facts) if critical_facts else 1.0,
            tool_pairs_total=len(action_results),
            tool_pairs_preserved=len(action_payloads),
            tool_pair_fidelity=(len(action_payloads) / len(action_results) if action_results else 1.0),
        )
        self._compressions += 1
        self._full_bytes += capture.metrics.full_state_bytes
        self._disclosure_bytes += disclosure_bytes
        return BrowserContextDisclosure(
            disclosure_id=state_id("brdisclosure"),
            capture_id=capture.capture_id,
            selector_revision_id=selector_revision.revision_id,
            text=text,
            artifact_ids=externalization.artifact_ids,
            selector_refs=tuple(entry.ref.opaque_ref for entry in selected_entries),
            facts=selected_facts,
            metrics=metrics,
        )

    def _fit_budget(
        self,
        text: str,
        payload: dict[str, Any],
        facts: tuple[str, ...],
        entries: tuple[Any, ...],
        budget: BrowserDisclosureBudget,
    ) -> tuple[str, dict[str, Any], tuple[str, ...], tuple[Any, ...]]:
        selected_facts = list(facts)
        selected_entries = list(entries)
        while _over_budget(text, budget) and (len(selected_facts) > 1 or len(selected_entries) > 1):
            if len(selected_facts) >= len(selected_entries) and len(selected_facts) > 1:
                selected_facts = selected_facts[: max(1, len(selected_facts) * 3 // 4)]
            elif len(selected_entries) > 1:
                selected_entries = selected_entries[: max(1, len(selected_entries) * 3 // 4)]
            payload["facts"] = selected_facts
            keep_refs = {entry.ref.opaque_ref for entry in selected_entries}
            payload["selectors"] = [item for item in payload["selectors"] if item["ref"] in keep_refs]
            selection = payload["selector_map"]["selection"]
            selection["selected"] = len(selected_entries)
            selected_frames = {entry.frame_id or "root" for entry in selected_entries}
            selection["selected_frame_coverage"] = len(selected_frames)
            frame_coverage = int(selection.get("frame_coverage") or 0)
            selection["frame_fidelity"] = (
                len(selected_frames) / frame_coverage if frame_coverage else 1.0
            )
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if _over_budget(text, budget):
            self._failures += 1
            raise BrowserStateBudgetExceeded(
                "browser disclosure cannot fit the configured context budget without breaking required invariants",
                details={
                    "disclosure_bytes": len(text.encode("utf-8")),
                    "disclosure_tokens": estimate_tokens(text),
                    "max_context_bytes": budget.max_context_bytes,
                    "max_context_tokens": budget.max_context_tokens,
                    "artifact_refs": payload.get("artifact_refs"),
                },
            )
        return text, payload, tuple(selected_facts), tuple(selected_entries)

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserStateCompressor",
            "owner_unit": "M1-S04B-01",
            "global_compact_owner": False,
            "disabled": self.disabled,
            "compressions": self._compressions,
            "failures": self._failures,
            "full_state_bytes": self._full_bytes,
            "disclosure_bytes": self._disclosure_bytes,
            "aggregate_ratio": self._disclosure_bytes / self._full_bytes if self._full_bytes else 1.0,
        }


def _select_facts(
    facts: Sequence[str],
    *,
    previous_facts: Iterable[str],
    limit: int,
    max_chars: int,
) -> tuple[tuple[str, ...], Counter[str]]:
    previous = {normalized_fact_key(item) for item in previous_facts}
    counts = Counter(normalized_fact_key(item) for item in facts if normalized_fact_key(item))
    ranked: list[tuple[tuple[int, int, int, str], str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(facts):
        value = normalize_page_text(raw, limit=max_chars)
        key = normalized_fact_key(value)
        if not key or key in seen:
            continue
        seen.add(key)
        new_bonus = 0 if key in previous else 1
        specificity = min(200, len(value))
        duplicate_penalty = counts[key] - 1
        ranked.append(((-new_bonus, duplicate_penalty, -specificity, f"{index:08d}"), value))
    ranked.sort(key=lambda item: item[0])
    return tuple(value for _, value in ranked[:limit]), counts


def _select_entries(revision: BrowserSelectorMapRevision, limit: int) -> tuple[Any, ...]:
    ranked = sorted(
        revision.entries,
        key=lambda entry: (
            entry.disabled,
            not entry.visible,
            not bool(entry.accessible_name),
            not bool(entry.text_preview),
            entry.ref.selector_index,
        ),
    )
    return tuple(ranked[:limit])


def _select_action_pairs(
    action_results: Sequence[BrowserActionResultProjection],
    limit: int,
) -> tuple[BrowserActionResultProjection, ...]:
    failures = [item for item in action_results if not item.ok]
    recent = list(action_results[-limit:])
    combined = {item.projection_id: item for item in (*failures, *recent)}
    return tuple(sorted(combined.values(), key=lambda item: (item.step_index, item.receipt_id)))


def _action_payload(item: BrowserActionResultProjection) -> dict[str, Any]:
    return {
        "projection_id": item.projection_id,
        "receipt_id": item.receipt_id,
        "request_id": item.request_id,
        "action": item.action,
        "step_index": item.step_index,
        "ok": item.ok,
        "status": item.status,
        "summary": item.summary,
        "error_code": item.error_code,
        "artifact_ids": list(item.artifact_ids),
        "selector_refs": list(item.selector_refs),
        "pair_fingerprint": item.pair_fingerprint,
    }


def _critical_facts(
    goal: str,
    constraints: Sequence[str],
    capture: BrowserDomCapture,
    facts: Sequence[str],
) -> tuple[str, ...]:
    requested = [normalize_page_text(goal, limit=500), *[normalize_page_text(item, limit=300) for item in constraints]]
    requested = [item for item in requested if item]
    matches: list[str] = []
    requested_tokens = {token for item in requested for token in normalized_fact_key(item).split() if len(token) > 2}
    for fact in facts:
        tokens = set(normalized_fact_key(fact).split())
        if requested_tokens & tokens:
            matches.append(fact)
        if len(matches) >= 16:
            break
    return tuple(dict.fromkeys((*requested[:8], *matches)))


def _root_attribute(capture: BrowserDomCapture, key: str) -> str:
    root = capture.raw_dom.get("root")
    if isinstance(root, dict):
        return normalize_page_text(root.get(key) or "", limit=2000)
    return ""


def _page_title(capture: BrowserDomCapture) -> str:
    for fact in dom_fact_candidates(capture.root, max_facts=32):
        if fact:
            return fact[:300]
    return ""


def _over_budget(text: str, budget: BrowserDisclosureBudget) -> bool:
    return len(text.encode("utf-8")) > budget.max_context_bytes or estimate_tokens(text) > budget.max_context_tokens
