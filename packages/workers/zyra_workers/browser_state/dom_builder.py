from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import (
    DOMRect,
    EnhancedAXNode,
    EnhancedAXProperty,
    EnhancedDOMTreeNode,
    EnhancedSnapshotNode,
    NodeType,
)
from .snapshot_decoder import DecodedSnapshot
from .text import finite_number, normalize_page_text, redact_attributes


@dataclass(frozen=True, slots=True)
class DomBuildReport:
    root: EnhancedDOMTreeNode
    node_count: int
    element_count: int
    text_node_count: int
    ax_matched_count: int
    snapshot_matched_count: int
    shadow_root_count: int
    iframe_count: int
    content_document_count: int
    duplicate_backend_ids: tuple[int, ...]
    prompt_injection_nodes: tuple[int, ...]
    redacted_attribute_nodes: tuple[int, ...]
    warnings: tuple[str, ...]

    @property
    def ax_coverage(self) -> float:
        return self.ax_matched_count / self.element_count if self.element_count else 1.0

    @property
    def snapshot_coverage(self) -> float:
        return self.snapshot_matched_count / self.element_count if self.element_count else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_count": self.node_count,
            "element_count": self.element_count,
            "text_node_count": self.text_node_count,
            "ax_matched_count": self.ax_matched_count,
            "ax_coverage": self.ax_coverage,
            "snapshot_matched_count": self.snapshot_matched_count,
            "snapshot_coverage": self.snapshot_coverage,
            "shadow_root_count": self.shadow_root_count,
            "iframe_count": self.iframe_count,
            "content_document_count": self.content_document_count,
            "duplicate_backend_ids": list(self.duplicate_backend_ids),
            "prompt_injection_nodes": list(self.prompt_injection_nodes),
            "redacted_attribute_nodes": list(self.redacted_attribute_nodes),
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class _BuildStats:
    nodes: int = 0
    elements: int = 0
    text_nodes: int = 0
    ax_matches: int = 0
    snapshot_matches: int = 0
    shadow_roots: int = 0
    iframes: int = 0
    content_documents: int = 0
    backend_counts: Counter[int] = None  # type: ignore[assignment]
    injection_nodes: set[int] = None  # type: ignore[assignment]
    redacted_nodes: set[int] = None  # type: ignore[assignment]
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.backend_counts = Counter()
        self.injection_nodes = set()
        self.redacted_nodes = set()
        self.warnings = []


def build_enhanced_dom(
    dom_payload: Mapping[str, Any],
    ax_payload: Mapping[str, Any],
    decoded_snapshot: DecodedSnapshot,
    *,
    target_id: str,
    cdp_session_id: str,
    default_frame_id: str = "",
    js_click_backend_ids: Iterable[int] = (),
    sensitive_values: Iterable[str] = (),
) -> DomBuildReport:
    root_payload = dom_payload.get("root")
    if not isinstance(root_payload, Mapping):
        raise ValueError("DOM.getDocument result does not contain a root node")
    ax_lookup = build_ax_lookup(ax_payload)
    click_ids = {int(item) for item in js_click_backend_ids if int(item) > 0}
    stats = _BuildStats()
    root = _build_node(
        root_payload,
        parent=None,
        ax_lookup=ax_lookup,
        decoded_snapshot=decoded_snapshot,
        target_id=target_id,
        cdp_session_id=cdp_session_id,
        inherited_frame_id=default_frame_id,
        js_click_backend_ids=click_ids,
        sensitive_values=tuple(sensitive_values),
        stats=stats,
        path=(),
    )
    duplicates = tuple(sorted(backend_id for backend_id, count in stats.backend_counts.items() if count > 1))
    if duplicates:
        stats.warnings.append(f"duplicate backend node ids in DOM tree: {duplicates[:20]}")
    return DomBuildReport(
        root=root,
        node_count=stats.nodes,
        element_count=stats.elements,
        text_node_count=stats.text_nodes,
        ax_matched_count=stats.ax_matches,
        snapshot_matched_count=stats.snapshot_matches,
        shadow_root_count=stats.shadow_roots,
        iframe_count=stats.iframes,
        content_document_count=stats.content_documents,
        duplicate_backend_ids=duplicates,
        prompt_injection_nodes=tuple(sorted(stats.injection_nodes)),
        redacted_attribute_nodes=tuple(sorted(stats.redacted_nodes)),
        warnings=tuple(dict.fromkeys((*decoded_snapshot.warnings, *stats.warnings))),
    )


def build_ax_lookup(ax_payload: Mapping[str, Any]) -> dict[int, EnhancedAXNode]:
    result: dict[int, EnhancedAXNode] = {}
    for raw_node in _sequence(ax_payload.get("nodes")):
        if not isinstance(raw_node, Mapping):
            continue
        backend_id = _as_int(raw_node.get("backendDOMNodeId"))
        if backend_id <= 0:
            continue
        properties: list[EnhancedAXProperty] = []
        for raw_property in _sequence(raw_node.get("properties")):
            if not isinstance(raw_property, Mapping):
                continue
            name = str(raw_property.get("name") or "")
            value = _ax_value(raw_property.get("value"))
            properties.append(EnhancedAXProperty(name=name, value=value))
        result[backend_id] = EnhancedAXNode(
            ax_node_id=str(raw_node.get("nodeId") or ""),
            ignored=bool(raw_node.get("ignored")),
            role=_ax_text(raw_node.get("role")),
            name=_ax_text(raw_node.get("name")),
            description=_ax_text(raw_node.get("description")),
            properties=properties or None,
            child_ids=[str(item) for item in _sequence(raw_node.get("childIds"))] or None,
        )
    return result


def _build_node(
    raw: Mapping[str, Any],
    *,
    parent: EnhancedDOMTreeNode | None,
    ax_lookup: Mapping[int, EnhancedAXNode],
    decoded_snapshot: DecodedSnapshot,
    target_id: str,
    cdp_session_id: str,
    inherited_frame_id: str,
    js_click_backend_ids: set[int],
    sensitive_values: Sequence[str],
    stats: _BuildStats,
    path: tuple[int, ...],
) -> EnhancedDOMTreeNode:
    node_id = _as_int(raw.get("nodeId"))
    backend_id = _as_int(raw.get("backendNodeId"))
    node_type = _node_type(raw.get("nodeType"))
    node_name = str(raw.get("nodeName") or "")
    tag_name = node_name.casefold() if node_type == NodeType.ELEMENT_NODE else node_name
    frame_id = str(raw.get("frameId") or inherited_frame_id or "")
    attributes = _attributes(raw.get("attributes"))
    redacted, redaction_signals = redact_attributes(attributes, sensitive_values=sensitive_values)
    if redaction_signals and backend_id > 0:
        stats.redacted_nodes.add(backend_id)
    raw_value = str(raw.get("nodeValue") or "")
    node_value = normalize_page_text(raw_value, limit=20000) if node_type == NodeType.TEXT_NODE else raw_value
    if "[untrusted-page-instruction]" in node_value and backend_id > 0:
        stats.injection_nodes.add(backend_id)

    snapshot_node = decoded_snapshot.snapshot_for(backend_id)
    ax_node = ax_lookup.get(backend_id)
    is_visible = _visible(snapshot_node, redacted)
    is_scrollable = _scrollable(snapshot_node)
    absolute_position = _copy_rect(snapshot_node.bounds if snapshot_node else None)
    shadow_type = raw.get("shadowRootType")
    node = EnhancedDOMTreeNode(
        node_id=node_id,
        backend_node_id=backend_id,
        node_type=node_type,
        node_name=node_name,
        node_value=node_value,
        attributes=redacted,
        is_scrollable=is_scrollable,
        is_visible=is_visible,
        absolute_position=absolute_position,
        target_id=target_id,
        frame_id=frame_id or None,
        session_id=cdp_session_id or None,
        content_document=None,
        shadow_root_type=str(shadow_type) if shadow_type else None,
        shadow_roots=None,
        parent_node=parent,
        children_nodes=None,
        ax_node=ax_node,
        snapshot_node=snapshot_node,
        has_js_click_listener=backend_id in js_click_backend_ids,
    )
    stats.nodes += 1
    if backend_id > 0:
        stats.backend_counts[backend_id] += 1
    if node_type == NodeType.ELEMENT_NODE:
        stats.elements += 1
    elif node_type == NodeType.TEXT_NODE:
        stats.text_nodes += 1
    if ax_node is not None:
        stats.ax_matches += 1
    if snapshot_node is not None:
        stats.snapshot_matches += 1
    if tag_name in {"iframe", "frame"}:
        stats.iframes += 1

    children: list[EnhancedDOMTreeNode] = []
    for index, child in enumerate(_sequence(raw.get("children"))):
        if isinstance(child, Mapping):
            children.append(_build_node(
                child,
                parent=node,
                ax_lookup=ax_lookup,
                decoded_snapshot=decoded_snapshot,
                target_id=target_id,
                cdp_session_id=cdp_session_id,
                inherited_frame_id=frame_id,
                js_click_backend_ids=js_click_backend_ids,
                sensitive_values=sensitive_values,
                stats=stats,
                path=(*path, index),
            ))
    node.children_nodes = children or None

    shadow_roots: list[EnhancedDOMTreeNode] = []
    for index, shadow in enumerate(_sequence(raw.get("shadowRoots"))):
        if not isinstance(shadow, Mapping):
            continue
        stats.shadow_roots += 1
        shadow_roots.append(_build_node(
            shadow,
            parent=node,
            ax_lookup=ax_lookup,
            decoded_snapshot=decoded_snapshot,
            target_id=target_id,
            cdp_session_id=cdp_session_id,
            inherited_frame_id=frame_id,
            js_click_backend_ids=js_click_backend_ids,
            sensitive_values=sensitive_values,
            stats=stats,
            path=(*path, 100000 + index),
        ))
    node.shadow_roots = shadow_roots or None

    content_document = raw.get("contentDocument")
    if isinstance(content_document, Mapping):
        stats.content_documents += 1
        node.content_document = _build_node(
            content_document,
            parent=node,
            ax_lookup=ax_lookup,
            decoded_snapshot=decoded_snapshot,
            target_id=target_id,
            cdp_session_id=cdp_session_id,
            inherited_frame_id=str(content_document.get("frameId") or frame_id),
            js_click_backend_ids=js_click_backend_ids,
            sensitive_values=sensitive_values,
            stats=stats,
            path=(*path, 200000),
        )
    _apply_iframe_offset(node)
    return node


def _apply_iframe_offset(node: EnhancedDOMTreeNode) -> None:
    """Copy and offset child-frame rectangles; never mutate snapshot coordinates."""

    if node.content_document is None or node.absolute_position is None:
        return
    offset_x = node.absolute_position.x
    offset_y = node.absolute_position.y
    for descendant in walk_dom(node.content_document):
        rect = descendant.absolute_position
        if rect is not None:
            descendant.absolute_position = DOMRect(
                x=rect.x + offset_x,
                y=rect.y + offset_y,
                width=rect.width,
                height=rect.height,
            )


def walk_dom(root: EnhancedDOMTreeNode) -> Iterable[EnhancedDOMTreeNode]:
    stack = [root]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        marker = id(node)
        if marker in seen:
            continue
        seen.add(marker)
        yield node
        if node.content_document is not None:
            stack.append(node.content_document)
        stack.extend(reversed(node.children_and_shadow_roots))


def nodes_by_backend_id(root: EnhancedDOMTreeNode) -> dict[int, tuple[EnhancedDOMTreeNode, ...]]:
    grouped: dict[int, list[EnhancedDOMTreeNode]] = defaultdict(list)
    for node in walk_dom(root):
        if node.backend_node_id > 0:
            grouped[node.backend_node_id].append(node)
    return {key: tuple(value) for key, value in grouped.items()}


def dom_fact_candidates(root: EnhancedDOMTreeNode, *, max_facts: int = 512) -> tuple[str, ...]:
    facts: list[str] = []
    seen: set[str] = set()
    for node in walk_dom(root):
        values = []
        if node.ax_node:
            values.extend([node.ax_node.name or "", node.ax_node.description or ""])
        if node.node_type == NodeType.TEXT_NODE:
            values.append(node.node_value)
        values.extend([
            node.attributes.get("aria-label", ""),
            node.attributes.get("title", ""),
            node.attributes.get("alt", ""),
        ])
        for value in values:
            fact = normalize_page_text(value, limit=500)
            key = fact.casefold()
            if len(key) < 3 or key in seen or fact == "[REDACTED]":
                continue
            seen.add(key)
            facts.append(fact)
            if len(facts) >= max_facts:
                return tuple(facts)
    return tuple(facts)


def _visible(snapshot: EnhancedSnapshotNode | None, attributes: Mapping[str, str]) -> bool | None:
    if attributes.get("hidden") not in (None, "", "false", "False"):
        return False
    if snapshot is None:
        return None
    styles = snapshot.computed_styles or {}
    if styles.get("display", "").casefold() == "none":
        return False
    if styles.get("visibility", "").casefold() in {"hidden", "collapse"}:
        return False
    opacity = finite_number(styles.get("opacity", "1"), default=1.0)
    if opacity <= 0:
        return False
    bounds = snapshot.bounds
    if bounds is not None and (bounds.width <= 0 or bounds.height <= 0):
        return False
    return True


def _scrollable(snapshot: EnhancedSnapshotNode | None) -> bool | None:
    if snapshot is None:
        return None
    styles = snapshot.computed_styles or {}
    overflow = " ".join((styles.get("overflow", ""), styles.get("overflow-x", ""), styles.get("overflow-y", ""))).casefold()
    if any(value in overflow for value in ("auto", "scroll", "overlay")):
        return True
    scroll = snapshot.scrollRects
    bounds = snapshot.bounds
    if scroll is not None and bounds is not None:
        return scroll.width > bounds.width + 1 or scroll.height > bounds.height + 1
    return False


def _attributes(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items()}
    values = _sequence(value)
    result: dict[str, str] = {}
    for index in range(0, len(values) - 1, 2):
        result[str(values[index])] = str(values[index + 1])
    return result


def _node_type(value: Any) -> NodeType:
    try:
        return NodeType(int(value))
    except (TypeError, ValueError):
        return NodeType.ELEMENT_NODE


def _copy_rect(rect: DOMRect | None) -> DOMRect | None:
    if rect is None:
        return None
    return DOMRect(x=rect.x, y=rect.y, width=rect.width, height=rect.height)


def _ax_text(value: Any) -> str | None:
    result = _ax_value(value)
    return None if result is None else str(result)


def _ax_value(value: Any) -> str | bool | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    if isinstance(value, (str, bool, int, float)):
        return value if isinstance(value, bool) else str(value)
    return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()
