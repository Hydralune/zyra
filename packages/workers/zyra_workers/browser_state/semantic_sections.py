from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .models import EnhancedDOMTreeNode, NodeType
from .text import normalize_page_text, normalized_fact_key


class BrowserSectionKind(StrEnum):
    DOCUMENT = "document"
    LANDMARK = "landmark"
    HEADING = "heading"
    FORM = "form"
    TABLE = "table"
    LIST = "list"
    DIALOG = "dialog"
    ARTICLE = "article"
    NAVIGATION = "navigation"
    CONTENT = "content"


@dataclass(frozen=True, slots=True)
class BrowserSemanticElement:
    backend_node_id: int
    frame_id: str
    tag_name: str
    role: str
    name: str
    text: str
    xpath: str
    visible: bool
    interactive: bool
    disabled: bool

    @property
    def key(self) -> str:
        raw = "|".join((self.frame_id, str(self.backend_node_id), self.tag_name, self.role, self.name, self.xpath))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "backend_node_id": self.backend_node_id,
            "frame_id": self.frame_id,
            "tag_name": self.tag_name,
            "role": self.role,
            "name": self.name,
            "text": self.text,
            "xpath": self.xpath,
            "visible": self.visible,
            "interactive": self.interactive,
            "disabled": self.disabled,
        }


@dataclass(frozen=True, slots=True)
class BrowserSemanticSection:
    section_id: str
    kind: BrowserSectionKind
    level: int
    heading: str
    summary: str
    facts: tuple[str, ...]
    elements: tuple[BrowserSemanticElement, ...]
    parent_section_id: str = ""
    frame_id: str = ""
    repeated: bool = False
    priority: float = 0.0
    source_backend_node_ids: tuple[int, ...] = ()

    @property
    def interactive_count(self) -> int:
        return sum(item.interactive for item in self.elements)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "kind": str(self.kind),
            "level": self.level,
            "heading": self.heading,
            "summary": self.summary,
            "facts": list(self.facts),
            "elements": [item.to_dict() for item in self.elements],
            "parent_section_id": self.parent_section_id,
            "frame_id": self.frame_id,
            "repeated": self.repeated,
            "priority": self.priority,
            "source_backend_node_ids": list(self.source_backend_node_ids),
            "interactive_count": self.interactive_count,
        }


@dataclass(frozen=True, slots=True)
class BrowserSemanticOutline:
    sections: tuple[BrowserSemanticSection, ...]
    facts: tuple[str, ...]
    visible_elements: int
    interactive_elements: int
    frames: int
    repeated_sections: int
    omitted_sections: int
    source_nodes: int

    def ranked_sections(self, limit: int = 16) -> tuple[BrowserSemanticSection, ...]:
        return tuple(sorted(self.sections, key=lambda item: (-item.priority, item.level, item.section_id))[:limit])

    def to_dict(self, *, section_limit: int = 24) -> dict[str, Any]:
        return {
            "sections": [item.to_dict() for item in self.ranked_sections(section_limit)],
            "facts": list(self.facts),
            "visible_elements": self.visible_elements,
            "interactive_elements": self.interactive_elements,
            "frames": self.frames,
            "repeated_sections": self.repeated_sections,
            "omitted_sections": self.omitted_sections,
            "source_nodes": self.source_nodes,
        }


@dataclass(slots=True)
class _MutableSection:
    section_id: str
    kind: BrowserSectionKind
    level: int
    heading: str
    parent_section_id: str
    frame_id: str
    facts: list[str] = field(default_factory=list)
    elements: list[BrowserSemanticElement] = field(default_factory=list)
    backend_node_ids: list[int] = field(default_factory=list)

    def add(self, node: EnhancedDOMTreeNode, *, max_facts: int, max_elements: int) -> None:
        if node.backend_node_id:
            self.backend_node_ids.append(node.backend_node_id)
        element = _semantic_element(node)
        if element is not None and len(self.elements) < max_elements:
            self.elements.append(element)
        existing = {normalized_fact_key(item) for item in self.facts}
        for fact in _node_facts(node):
            key = normalized_fact_key(fact)
            if key and key not in existing and len(self.facts) < max_facts:
                self.facts.append(fact)
                existing.add(key)

    def freeze(self, goal_tokens: frozenset[str]) -> BrowserSemanticSection:
        summary = normalize_page_text(" | ".join((self.heading, *self.facts[:6])), limit=1200)
        content_tokens = _query_tokens((self.heading, summary, *self.facts))
        overlap = len(content_tokens & goal_tokens) / max(1, len(goal_tokens)) if goal_tokens else 0.0
        priority = _kind_priority(self.kind) + overlap * 8.0
        priority += min(3.0, len(self.elements) * 0.2)
        priority += min(2.0, len(self.facts) * 0.1)
        priority += sum(item.interactive and item.visible for item in self.elements) * 0.15
        return BrowserSemanticSection(
            section_id=self.section_id,
            kind=self.kind,
            level=self.level,
            heading=self.heading,
            summary=summary,
            facts=tuple(self.facts),
            elements=tuple(self.elements),
            parent_section_id=self.parent_section_id,
            frame_id=self.frame_id,
            priority=priority,
            source_backend_node_ids=tuple(dict.fromkeys(self.backend_node_ids)),
        )


class BrowserSemanticOutlineBuilder:
    """Extract a bounded long-page outline from 04B authoritative DOM state."""

    def __init__(
        self,
        *,
        max_sections: int = 128,
        max_section_facts: int = 24,
        max_section_elements: int = 32,
    ) -> None:
        if min(max_sections, max_section_facts, max_section_elements) <= 0:
            raise ValueError("semantic outline limits must be positive")
        self.max_sections = max_sections
        self.max_section_facts = max_section_facts
        self.max_section_elements = max_section_elements

    def build(
        self,
        root: EnhancedDOMTreeNode,
        *,
        goal: str = "",
        constraints: Sequence[str] = (),
    ) -> BrowserSemanticOutline:
        goal_tokens = _query_tokens((goal, *constraints))
        sections: list[BrowserSemanticSection] = []
        stack: list[tuple[int, str]] = []
        current: _MutableSection | None = None
        visible = 0
        interactive = 0
        frames: set[str] = set()
        source_nodes = 0
        for node, depth in _walk(root):
            source_nodes += 1
            frames.add(str(node.frame_id or ""))
            visible += bool(node.is_visible)
            element = _semantic_element(node)
            interactive += bool(element and element.interactive)
            boundary = _section_boundary(node, depth)
            if boundary:
                if current is not None:
                    sections.append(current.freeze(goal_tokens))
                kind, level, heading = boundary
                while stack and stack[-1][0] >= level:
                    stack.pop()
                parent_id = stack[-1][1] if stack else ""
                section_id = _section_id(node, kind, heading)
                current = _MutableSection(section_id, kind, level, heading, parent_id, str(node.frame_id or ""))
                stack.append((level, section_id))
            if current is None:
                current = _MutableSection(
                    _section_id(root, BrowserSectionKind.DOCUMENT, "document"),
                    BrowserSectionKind.DOCUMENT, 0, "document", "", str(root.frame_id or ""),
                )
            current.add(node, max_facts=self.max_section_facts, max_elements=self.max_section_elements)
        if current is not None:
            sections.append(current.freeze(goal_tokens))

        fingerprints = Counter(_section_fingerprint(item) for item in sections)
        marked = tuple(_mark_repetition(item, fingerprints[_section_fingerprint(item)] > 1) for item in sections)
        selected = tuple(sorted(marked, key=lambda item: (-item.priority, item.level, item.section_id))[: self.max_sections])
        facts = _deduplicate(
            fact for section in selected
            for fact in ((section.heading,) if section.heading != "document" else ()) + section.facts
        )
        return BrowserSemanticOutline(
            sections=selected,
            facts=facts,
            visible_elements=visible,
            interactive_elements=interactive,
            frames=len(frames - {""}) or 1,
            repeated_sections=sum(item.repeated for item in marked),
            omitted_sections=max(0, len(marked) - len(selected)),
            source_nodes=source_nodes,
        )


def _walk(root: EnhancedDOMTreeNode) -> Iterable[tuple[EnhancedDOMTreeNode, int]]:
    pending: list[tuple[EnhancedDOMTreeNode, int]] = [(root, 0)]
    visited: set[int] = set()
    while pending:
        node, depth = pending.pop()
        if id(node) in visited:
            continue
        visited.add(id(node))
        yield node, depth
        children = list(node.children_and_shadow_roots)
        if node.content_document is not None:
            children.append(node.content_document)
        pending.extend((child, depth + 1) for child in reversed(children))


def _section_boundary(node: EnhancedDOMTreeNode, depth: int) -> tuple[BrowserSectionKind, int, str] | None:
    if node.node_type != NodeType.ELEMENT_NODE:
        return None
    tag = node.tag_name
    role = str(node.ax_node.role or "").lower() if node.ax_node else ""
    name = normalize_page_text(node.ax_node.name or "", limit=300) if node.ax_node else ""
    text = normalize_page_text(node.get_all_children_text(max_depth=2), limit=300)
    heading = name or text or normalize_page_text(node.attributes.get("aria-label", ""), limit=300)
    if re.fullmatch(r"h[1-6]", tag):
        return BrowserSectionKind.HEADING, int(tag[1]), heading or tag
    if role == "heading":
        try:
            level = int(node.attributes.get("aria-level", "2"))
        except ValueError:
            level = 2
        return BrowserSectionKind.HEADING, max(1, min(6, level)), heading or "heading"
    kinds = {
        "main": BrowserSectionKind.LANDMARK,
        "article": BrowserSectionKind.ARTICLE,
        "form": BrowserSectionKind.FORM,
        "table": BrowserSectionKind.TABLE,
        "nav": BrowserSectionKind.NAVIGATION,
        "dialog": BrowserSectionKind.DIALOG,
        "ul": BrowserSectionKind.LIST,
        "ol": BrowserSectionKind.LIST,
    }
    roles = {
        "main": BrowserSectionKind.LANDMARK,
        "article": BrowserSectionKind.ARTICLE,
        "form": BrowserSectionKind.FORM,
        "table": BrowserSectionKind.TABLE,
        "navigation": BrowserSectionKind.NAVIGATION,
        "dialog": BrowserSectionKind.DIALOG,
        "alertdialog": BrowserSectionKind.DIALOG,
        "list": BrowserSectionKind.LIST,
        "region": BrowserSectionKind.LANDMARK,
    }
    kind = roles.get(role) or kinds.get(tag)
    if kind:
        return kind, min(8, max(2, depth)), heading or tag
    if tag in {"section", "aside"} and heading:
        return BrowserSectionKind.CONTENT, min(8, max(2, depth)), heading
    return None


def _semantic_element(node: EnhancedDOMTreeNode) -> BrowserSemanticElement | None:
    if node.node_type != NodeType.ELEMENT_NODE:
        return None
    role = str(node.ax_node.role or "") if node.ax_node else ""
    name = normalize_page_text(node.ax_node.name or "", limit=300) if node.ax_node else ""
    interactive = bool(
        node.has_js_click_listener
        or (node.snapshot_node and node.snapshot_node.is_clickable)
        or node.tag_name in {"a", "button", "input", "select", "textarea", "summary"}
        or role.lower() in {"button", "link", "checkbox", "radio", "textbox", "combobox", "menuitem", "tab", "switch"}
    )
    if not interactive and not name and node.tag_name not in {"img", "video", "audio"}:
        return None
    return BrowserSemanticElement(
        backend_node_id=node.backend_node_id,
        frame_id=str(node.frame_id or ""),
        tag_name=node.tag_name,
        role=role,
        name=name,
        text=normalize_page_text(node.get_meaningful_text_for_llm(), limit=500),
        xpath=node.xpath,
        visible=bool(node.is_visible),
        interactive=interactive,
        disabled=("disabled" in node.attributes or str(node.attributes.get("aria-disabled", "")).lower() == "true"),
    )


def _node_facts(node: EnhancedDOMTreeNode) -> tuple[str, ...]:
    if node.node_type == NodeType.TEXT_NODE:
        value = normalize_page_text(node.node_value, limit=500)
        return (value,) if value else ()
    if node.node_type != NodeType.ELEMENT_NODE or not node.is_visible:
        return ()
    role = str(node.ax_node.role or "") if node.ax_node else ""
    name = normalize_page_text(node.ax_node.name or "", limit=300) if node.ax_node else ""
    text = normalize_page_text(node.get_meaningful_text_for_llm(), limit=500)
    values = [f"{role}: {name}" if role and name else name or text]
    for key in ("placeholder", "title", "alt", "aria-label"):
        value = normalize_page_text(node.attributes.get(key, ""), limit=300)
        if value:
            values.append(f"{key}: {value}")
    if node.is_actually_scrollable and node.get_scroll_info_text():
        values.append(f"{node.tag_name} {node.get_scroll_info_text()}")
    return _deduplicate(values)


def _mark_repetition(section: BrowserSemanticSection, repeated: bool) -> BrowserSemanticSection:
    penalty = 1.5 if repeated and section.kind in {BrowserSectionKind.NAVIGATION, BrowserSectionKind.CONTENT} else 0.0
    return BrowserSemanticSection(
        section.section_id, section.kind, section.level, section.heading, section.summary,
        section.facts, section.elements, section.parent_section_id, section.frame_id,
        repeated, section.priority - penalty, section.source_backend_node_ids,
    )


def _section_id(node: EnhancedDOMTreeNode, kind: BrowserSectionKind, heading: str) -> str:
    raw = "|".join((str(node.target_id or ""), str(node.frame_id or ""), str(node.backend_node_id), str(kind), normalized_fact_key(heading), node.xpath))
    return "browser-section-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _section_fingerprint(section: BrowserSemanticSection) -> str:
    raw = "|".join((str(section.kind), normalized_fact_key(section.heading), *(normalized_fact_key(item) for item in section.facts[:8])))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _query_tokens(values: Iterable[str]) -> frozenset[str]:
    return frozenset(token for value in values for token in normalized_fact_key(value).split() if len(token) > 2)


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_page_text(value, limit=600)
        key = normalized_fact_key(normalized)
        if key and key not in seen:
            result.append(normalized)
            seen.add(key)
    return tuple(result)


def _kind_priority(kind: BrowserSectionKind) -> float:
    return {
        BrowserSectionKind.DIALOG: 8.0,
        BrowserSectionKind.FORM: 7.0,
        BrowserSectionKind.HEADING: 6.0,
        BrowserSectionKind.TABLE: 5.5,
        BrowserSectionKind.ARTICLE: 5.0,
        BrowserSectionKind.LANDMARK: 4.5,
        BrowserSectionKind.LIST: 3.5,
        BrowserSectionKind.CONTENT: 3.0,
        BrowserSectionKind.NAVIGATION: 2.0,
        BrowserSectionKind.DOCUMENT: 1.0,
    }[kind]
