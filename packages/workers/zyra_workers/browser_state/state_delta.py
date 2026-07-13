from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import BrowserDomCapture, BrowserSelectorEntry, BrowserSelectorMapRevision
from .models import EnhancedDOMTreeNode, NodeType
from .text import normalize_page_text, normalized_fact_key


class BrowserDomChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MOVED = "moved"
    TEXT_CHANGED = "text_changed"
    ATTRIBUTE_CHANGED = "attribute_changed"
    VISIBILITY_CHANGED = "visibility_changed"
    GEOMETRY_CHANGED = "geometry_changed"
    INTERACTIVITY_CHANGED = "interactivity_changed"
    FRAME_CHANGED = "frame_changed"


@dataclass(frozen=True, slots=True)
class BrowserNodeSignature:
    stable_key: str
    backend_node_id: int
    frame_id: str
    target_id: str
    tag_name: str
    role: str
    accessible_name: str
    text: str
    attributes: tuple[tuple[str, str], ...]
    xpath: str
    parent_key: str
    depth: int
    visible: bool
    interactive: bool
    disabled: bool
    bounds: tuple[float, float, float, float] | None
    paint_order: int | None
    subtree_digest: str

    @property
    def identity_tokens(self) -> frozenset[str]:
        values = (
            self.tag_name,
            self.role,
            self.accessible_name,
            self.text,
            *(value for _, value in self.attributes),
        )
        tokens: set[str] = set()
        for value in values:
            tokens.update(token for token in normalized_fact_key(value).split() if len(token) > 1)
        return frozenset(tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_key": self.stable_key,
            "backend_node_id": self.backend_node_id,
            "frame_id": self.frame_id,
            "target_id": self.target_id,
            "tag_name": self.tag_name,
            "role": self.role,
            "accessible_name": self.accessible_name,
            "text": self.text,
            "attributes": dict(self.attributes),
            "xpath": self.xpath,
            "parent_key": self.parent_key,
            "depth": self.depth,
            "visible": self.visible,
            "interactive": self.interactive,
            "disabled": self.disabled,
            "bounds": list(self.bounds) if self.bounds is not None else None,
            "paint_order": self.paint_order,
            "subtree_digest": self.subtree_digest,
        }


@dataclass(frozen=True, slots=True)
class BrowserDomChange:
    kind: BrowserDomChangeKind
    stable_key: str
    before: BrowserNodeSignature | None
    after: BrowserNodeSignature | None
    confidence: float
    importance: float
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "stable_key": self.stable_key,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "confidence": self.confidence,
            "importance": self.importance,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class BrowserDomDelta:
    previous_capture_id: str
    capture_id: str
    previous_revision_id: str
    revision_id: str
    changes: tuple[BrowserDomChange, ...]
    before_nodes: int
    after_nodes: int
    matched_nodes: int
    unchanged_nodes: int
    added_nodes: int
    removed_nodes: int
    changed_nodes: int
    selector_continuity: float
    structural_similarity: float
    meaningful_change_ratio: float
    full_navigation: bool
    warnings: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.changes) or self.full_navigation

    @property
    def summary(self) -> str:
        if self.full_navigation:
            return (
                f"document changed: {self.before_nodes} -> {self.after_nodes} nodes, "
                f"{self.added_nodes} added and {self.removed_nodes} removed"
            )
        return (
            f"DOM delta: {self.added_nodes} added, {self.removed_nodes} removed, "
            f"{self.changed_nodes} changed, selector continuity {self.selector_continuity:.2f}"
        )

    def important_changes(self, limit: int = 24) -> tuple[BrowserDomChange, ...]:
        return tuple(sorted(self.changes, key=lambda item: (-item.importance, str(item.kind), item.stable_key))[:limit])

    def to_dict(self, *, change_limit: int = 100) -> dict[str, Any]:
        return {
            "previous_capture_id": self.previous_capture_id,
            "capture_id": self.capture_id,
            "previous_revision_id": self.previous_revision_id,
            "revision_id": self.revision_id,
            "summary": self.summary,
            "changed": self.changed,
            "before_nodes": self.before_nodes,
            "after_nodes": self.after_nodes,
            "matched_nodes": self.matched_nodes,
            "unchanged_nodes": self.unchanged_nodes,
            "added_nodes": self.added_nodes,
            "removed_nodes": self.removed_nodes,
            "changed_nodes": self.changed_nodes,
            "selector_continuity": self.selector_continuity,
            "structural_similarity": self.structural_similarity,
            "meaningful_change_ratio": self.meaningful_change_ratio,
            "full_navigation": self.full_navigation,
            "warnings": list(self.warnings),
            "changes": [item.to_dict() for item in self.important_changes(change_limit)],
        }


def capture_signatures(capture: BrowserDomCapture) -> tuple[BrowserNodeSignature, ...]:
    return tuple(_walk_signatures(capture.root))


def selector_signatures(revision: BrowserSelectorMapRevision) -> tuple[BrowserNodeSignature, ...]:
    signatures: list[BrowserNodeSignature] = []
    for entry in revision.entries:
        bounds = _entry_bounds(entry)
        key = _selector_key(entry)
        signatures.append(BrowserNodeSignature(
            stable_key=key,
            backend_node_id=entry.backend_node_id,
            frame_id=entry.frame_id,
            target_id=entry.target_id,
            tag_name=entry.tag_name,
            role=entry.role,
            accessible_name=entry.accessible_name,
            text=entry.text_preview,
            attributes=(),
            xpath=entry.xpath,
            parent_key="",
            depth=max(0, entry.xpath.count("/")),
            visible=entry.visible,
            interactive=entry.interactive,
            disabled=entry.disabled,
            bounds=bounds,
            paint_order=entry.paint_order,
            subtree_digest=entry.stable_hash,
        ))
    return tuple(signatures)


def compare_selector_revisions(
    before: BrowserSelectorMapRevision | None,
    after: BrowserSelectorMapRevision,
) -> BrowserDomDelta:
    if before is None:
        changes = tuple(
            BrowserDomChange(
                kind=BrowserDomChangeKind.ADDED,
                stable_key=signature.stable_key,
                before=None,
                after=signature,
                confidence=1.0,
                importance=_node_importance(signature),
            )
            for signature in selector_signatures(after)
        )
        return BrowserDomDelta(
            previous_capture_id="",
            capture_id=after.capture_id,
            previous_revision_id="",
            revision_id=after.revision_id,
            changes=changes,
            before_nodes=0,
            after_nodes=len(after.entries),
            matched_nodes=0,
            unchanged_nodes=0,
            added_nodes=len(after.entries),
            removed_nodes=0,
            changed_nodes=0,
            selector_continuity=1.0,
            structural_similarity=0.0,
            meaningful_change_ratio=1.0 if after.entries else 0.0,
            full_navigation=True,
            warnings=("initial_selector_generation",),
        )
    before_signatures = selector_signatures(before)
    after_signatures = selector_signatures(after)
    full_navigation = before.identity.document_loader_id != after.identity.document_loader_id
    full_navigation = full_navigation or before.identity.target_id != after.identity.target_id
    return compare_signatures(
        before_signatures,
        after_signatures,
        previous_capture_id=before.capture_id,
        capture_id=after.capture_id,
        previous_revision_id=before.revision_id,
        revision_id=after.revision_id,
        full_navigation=full_navigation,
    )


def compare_signatures(
    before: Sequence[BrowserNodeSignature],
    after: Sequence[BrowserNodeSignature],
    *,
    previous_capture_id: str = "",
    capture_id: str = "",
    previous_revision_id: str = "",
    revision_id: str = "",
    full_navigation: bool = False,
) -> BrowserDomDelta:
    before_by_backend = _group_by_backend(before)
    after_by_backend = _group_by_backend(after)
    matches: dict[int, int] = {}
    confidence: dict[tuple[int, int], float] = {}
    used_after: set[int] = set()

    for before_index, item in enumerate(before):
        candidates = after_by_backend.get((item.target_id, item.frame_id, item.backend_node_id), ())
        if len(candidates) == 1 and candidates[0] not in used_after:
            after_index = candidates[0]
            matches[before_index] = after_index
            confidence[(before_index, after_index)] = 1.0
            used_after.add(after_index)

    after_by_key: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(after):
        if index not in used_after:
            after_by_key[item.stable_key].append(index)
    for before_index, item in enumerate(before):
        if before_index in matches:
            continue
        candidates = [index for index in after_by_key.get(item.stable_key, ()) if index not in used_after]
        if len(candidates) == 1:
            after_index = candidates[0]
            matches[before_index] = after_index
            confidence[(before_index, after_index)] = 0.95
            used_after.add(after_index)

    unmatched_before = [index for index in range(len(before)) if index not in matches]
    unmatched_after = [index for index in range(len(after)) if index not in used_after]
    candidates: list[tuple[float, int, int]] = []
    for before_index in unmatched_before:
        for after_index in unmatched_after:
            score = signature_similarity(before[before_index], after[after_index])
            if score >= 0.72:
                candidates.append((score, before_index, after_index))
    for score, before_index, after_index in sorted(candidates, reverse=True):
        if before_index in matches or after_index in used_after:
            continue
        matches[before_index] = after_index
        confidence[(before_index, after_index)] = score
        used_after.add(after_index)

    changes: list[BrowserDomChange] = []
    unchanged = 0
    changed_keys: set[str] = set()
    for before_index, after_index in matches.items():
        left = before[before_index]
        right = after[after_index]
        detected = _changes_for_pair(left, right, confidence[(before_index, after_index)])
        if detected:
            changes.extend(detected)
            changed_keys.add(right.stable_key)
        else:
            unchanged += 1
    for before_index, item in enumerate(before):
        if before_index not in matches:
            changes.append(BrowserDomChange(
                kind=BrowserDomChangeKind.REMOVED,
                stable_key=item.stable_key,
                before=item,
                after=None,
                confidence=1.0,
                importance=_node_importance(item),
            ))
    for after_index, item in enumerate(after):
        if after_index not in used_after:
            changes.append(BrowserDomChange(
                kind=BrowserDomChangeKind.ADDED,
                stable_key=item.stable_key,
                before=None,
                after=item,
                confidence=1.0,
                importance=_node_importance(item),
            ))

    matched = len(matches)
    added = len(after) - matched
    removed = len(before) - matched
    union = len(before) + len(after) - matched
    selector_continuity = matched / max(1, min(len(before), len(after)))
    structural_similarity = matched / max(1, union)
    meaningful = sum(item.importance for item in changes)
    total_importance = sum(_node_importance(item) for item in (*before, *after)) / 2
    warnings: list[str] = []
    if selector_continuity < 0.5 and before and after:
        warnings.append("low_selector_continuity")
    if full_navigation:
        warnings.append("document_loader_changed")
    return BrowserDomDelta(
        previous_capture_id=previous_capture_id,
        capture_id=capture_id,
        previous_revision_id=previous_revision_id,
        revision_id=revision_id,
        changes=tuple(changes),
        before_nodes=len(before),
        after_nodes=len(after),
        matched_nodes=matched,
        unchanged_nodes=unchanged,
        added_nodes=added,
        removed_nodes=removed,
        changed_nodes=len(changed_keys),
        selector_continuity=min(1.0, selector_continuity),
        structural_similarity=min(1.0, structural_similarity),
        meaningful_change_ratio=min(1.0, meaningful / max(1.0, total_importance)),
        full_navigation=full_navigation,
        warnings=tuple(warnings),
    )


def signature_similarity(left: BrowserNodeSignature, right: BrowserNodeSignature) -> float:
    if left.tag_name != right.tag_name:
        return 0.0
    score = 0.20
    if left.role and left.role == right.role:
        score += 0.12
    if left.frame_id == right.frame_id:
        score += 0.08
    if left.parent_key and left.parent_key == right.parent_key:
        score += 0.10
    if left.xpath and left.xpath == right.xpath:
        score += 0.15
    token_union = left.identity_tokens | right.identity_tokens
    if token_union:
        score += 0.25 * (len(left.identity_tokens & right.identity_tokens) / len(token_union))
    if left.attributes or right.attributes:
        left_attrs = set(left.attributes)
        right_attrs = set(right.attributes)
        union = left_attrs | right_attrs
        score += 0.10 * (len(left_attrs & right_attrs) / max(1, len(union)))
    if left.bounds and right.bounds:
        score += 0.10 * _geometry_similarity(left.bounds, right.bounds)
    return min(1.0, score)


def _walk_signatures(root: EnhancedDOMTreeNode) -> Iterable[BrowserNodeSignature]:
    stack: list[tuple[EnhancedDOMTreeNode, str, int]] = [(root, "", 0)]
    while stack:
        node, parent_key, depth = stack.pop()
        if node.node_type in {NodeType.ELEMENT_NODE, NodeType.TEXT_NODE}:
            signature = _node_signature(node, parent_key=parent_key, depth=depth)
            yield signature
            next_parent = signature.stable_key
        else:
            next_parent = parent_key
        children = list(node.children_and_shadow_roots)
        if node.content_document is not None:
            children.append(node.content_document)
        for child in reversed(children):
            stack.append((child, next_parent, depth + 1))


def _node_signature(node: EnhancedDOMTreeNode, *, parent_key: str, depth: int) -> BrowserNodeSignature:
    role = node.ax_node.role if node.ax_node and node.ax_node.role else ""
    name = node.ax_node.name if node.ax_node and node.ax_node.name else ""
    text = normalize_page_text(node.node_value or node.get_meaningful_text_for_llm(), limit=600)
    attributes = tuple(sorted(
        (str(key).lower(), normalize_page_text(value, limit=300))
        for key, value in node.attributes.items()
        if key.lower() not in {"style", "srcset", "integrity", "nonce"}
    ))
    bounds = _node_bounds(node)
    interactive = bool(
        node.has_js_click_listener
        or (node.snapshot_node and node.snapshot_node.is_clickable)
        or node.tag_name in {"a", "button", "input", "select", "textarea", "summary"}
        or role in {"button", "link", "checkbox", "radio", "textbox", "combobox", "menuitem", "tab"}
    )
    disabled = str(node.attributes.get("disabled", "")).lower() not in {"", "false", "0"}
    key_payload = "|".join((
        str(node.target_id or ""), str(node.frame_id or ""), node.tag_name, node.xpath,
        str(node.backend_node_id), name, text[:120],
    ))
    stable_key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()[:24]
    subtree_payload = "|".join((
        node.tag_name, name, text, repr(attributes), str(len(node.children)),
        str(bool(node.shadow_roots)), str(bool(node.content_document)),
    ))
    return BrowserNodeSignature(
        stable_key=stable_key,
        backend_node_id=node.backend_node_id,
        frame_id=str(node.frame_id or ""),
        target_id=str(node.target_id or ""),
        tag_name=node.tag_name,
        role=str(role),
        accessible_name=normalize_page_text(name, limit=300),
        text=text,
        attributes=attributes,
        xpath=node.xpath,
        parent_key=parent_key,
        depth=depth,
        visible=bool(node.is_visible),
        interactive=interactive,
        disabled=disabled,
        bounds=bounds,
        paint_order=node.snapshot_node.paint_order if node.snapshot_node else None,
        subtree_digest=hashlib.sha256(subtree_payload.encode("utf-8")).hexdigest(),
    )


def _changes_for_pair(
    left: BrowserNodeSignature,
    right: BrowserNodeSignature,
    confidence: float,
) -> tuple[BrowserDomChange, ...]:
    changes: list[BrowserDomChange] = []
    importance = max(_node_importance(left), _node_importance(right))
    common = {"match_confidence": confidence}
    if left.parent_key != right.parent_key or left.xpath != right.xpath:
        changes.append(BrowserDomChange(
            BrowserDomChangeKind.MOVED, right.stable_key, left, right, confidence, importance,
            {**common, "before_xpath": left.xpath, "after_xpath": right.xpath},
        ))
    if normalized_fact_key(left.text) != normalized_fact_key(right.text):
        changes.append(BrowserDomChange(
            BrowserDomChangeKind.TEXT_CHANGED, right.stable_key, left, right, confidence, importance,
            {**common, "before": left.text, "after": right.text},
        ))
    if left.attributes != right.attributes:
        before_attrs = dict(left.attributes)
        after_attrs = dict(right.attributes)
        changed = sorted(key for key in before_attrs.keys() | after_attrs.keys() if before_attrs.get(key) != after_attrs.get(key))
        changes.append(BrowserDomChange(
            BrowserDomChangeKind.ATTRIBUTE_CHANGED, right.stable_key, left, right, confidence, importance,
            {**common, "attributes": changed[:32]},
        ))
    if left.visible != right.visible:
        changes.append(BrowserDomChange(
            BrowserDomChangeKind.VISIBILITY_CHANGED, right.stable_key, left, right, confidence, importance,
            {**common, "before": left.visible, "after": right.visible},
        ))
    if left.interactive != right.interactive or left.disabled != right.disabled:
        changes.append(BrowserDomChange(
            BrowserDomChangeKind.INTERACTIVITY_CHANGED, right.stable_key, left, right, confidence, importance + 0.5,
            {**common, "interactive": [left.interactive, right.interactive], "disabled": [left.disabled, right.disabled]},
        ))
    if _bounds_changed(left.bounds, right.bounds):
        changes.append(BrowserDomChange(
            BrowserDomChangeKind.GEOMETRY_CHANGED, right.stable_key, left, right, confidence, importance * 0.5,
            {**common, "before": left.bounds, "after": right.bounds},
        ))
    if left.frame_id != right.frame_id or left.target_id != right.target_id:
        changes.append(BrowserDomChange(
            BrowserDomChangeKind.FRAME_CHANGED, right.stable_key, left, right, confidence, importance + 1.0,
            {**common, "before_frame": left.frame_id, "after_frame": right.frame_id},
        ))
    return tuple(changes)


def _node_importance(item: BrowserNodeSignature) -> float:
    score = 1.0
    if item.interactive:
        score += 2.0
    if item.visible:
        score += 0.5
    if item.role:
        score += 0.5
    if item.accessible_name:
        score += 0.5
    if item.text:
        score += min(1.0, len(item.text) / 200)
    if item.disabled:
        score += 0.25
    return score


def _selector_key(entry: BrowserSelectorEntry) -> str:
    payload = "|".join((
        entry.target_id, entry.frame_id, entry.tag_name, entry.xpath,
        entry.accessible_name, entry.text_preview[:120], entry.stable_hash,
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _entry_bounds(entry: BrowserSelectorEntry) -> tuple[float, float, float, float] | None:
    try:
        return tuple(float(entry.bounds[key]) for key in ("x", "y", "width", "height"))  # type: ignore[return-value]
    except (KeyError, TypeError, ValueError):
        return None


def _node_bounds(node: EnhancedDOMTreeNode) -> tuple[float, float, float, float] | None:
    rect = node.absolute_position or (node.snapshot_node.bounds if node.snapshot_node else None)
    if rect is None:
        return None
    values = (float(rect.x), float(rect.y), float(rect.width), float(rect.height))
    return values if all(math.isfinite(value) for value in values) else None


def _bounds_changed(
    left: tuple[float, float, float, float] | None,
    right: tuple[float, float, float, float] | None,
) -> bool:
    if left is None or right is None:
        return left != right
    return any(abs(a - b) > max(2.0, abs(a) * 0.02) for a, b in zip(left, right, strict=True))


def _geometry_similarity(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    left_area = max(0.0, lw) * max(0.0, lh)
    right_area = max(0.0, rw) * max(0.0, rh)
    ix = max(lx, rx)
    iy = max(ly, ry)
    ax = min(lx + lw, rx + rw)
    ay = min(ly + lh, ry + rh)
    intersection = max(0.0, ax - ix) * max(0.0, ay - iy)
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _group_by_backend(items: Sequence[BrowserNodeSignature]) -> dict[tuple[str, str, int], tuple[int, ...]]:
    grouped: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    for index, item in enumerate(items):
        if item.backend_node_id:
            grouped[(item.target_id, item.frame_id, item.backend_node_id)].append(index)
    return {key: tuple(values) for key, values in grouped.items()}


def change_kind_counts(delta: BrowserDomDelta) -> Mapping[str, int]:
    return dict(Counter(str(item.kind) for item in delta.changes))
