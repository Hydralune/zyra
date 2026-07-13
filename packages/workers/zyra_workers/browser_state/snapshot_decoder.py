from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import DOMRect, EnhancedSnapshotNode
from .text import finite_number


REQUIRED_COMPUTED_STYLES = (
    "display",
    "visibility",
    "opacity",
    "overflow",
    "overflow-x",
    "overflow-y",
    "cursor",
    "pointer-events",
    "position",
    "background-color",
)


@dataclass(frozen=True, slots=True)
class DecodedSnapshotDocument:
    document_index: int
    frame_id: str
    content_width: float
    content_height: float
    scroll_x: float
    scroll_y: float
    backend_node_ids: tuple[int, ...]
    nodes: Mapping[int, EnhancedSnapshotNode]
    node_index_to_backend_id: Mapping[int, int]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_index": self.document_index,
            "frame_id": self.frame_id,
            "content_width": self.content_width,
            "content_height": self.content_height,
            "scroll_x": self.scroll_x,
            "scroll_y": self.scroll_y,
            "backend_node_ids": list(self.backend_node_ids),
            "node_count": len(self.nodes),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class DecodedSnapshot:
    documents: tuple[DecodedSnapshotDocument, ...]
    nodes_by_backend_id: Mapping[int, EnhancedSnapshotNode]
    backend_document_index: Mapping[int, int]
    strings: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def snapshot_for(self, backend_node_id: int) -> EnhancedSnapshotNode | None:
        return self.nodes_by_backend_id.get(int(backend_node_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "documents": [document.to_dict() for document in self.documents],
            "node_count": len(self.nodes_by_backend_id),
            "string_count": len(self.strings),
            "warnings": list(self.warnings),
        }


def decode_snapshot(
    payload: Mapping[str, Any],
    *,
    device_pixel_ratio: float = 1.0,
) -> DecodedSnapshot:
    strings = tuple(str(item) for item in _sequence(payload.get("strings")))
    documents_payload = _sequence(payload.get("documents"))
    dpr = finite_number(device_pixel_ratio, default=1.0)
    if dpr <= 0:
        dpr = 1.0
    documents: list[DecodedSnapshotDocument] = []
    nodes_by_backend: dict[int, EnhancedSnapshotNode] = {}
    backend_document: dict[int, int] = {}
    warnings: list[str] = []
    for document_index, raw_document in enumerate(documents_payload):
        if not isinstance(raw_document, Mapping):
            warnings.append(f"document[{document_index}] is not an object")
            continue
        decoded = _decode_document(document_index, raw_document, strings, dpr)
        documents.append(decoded)
        warnings.extend(decoded.warnings)
        for backend_id, snapshot_node in decoded.nodes.items():
            if backend_id in nodes_by_backend:
                warnings.append(f"backend node {backend_id} appears in multiple snapshot documents")
            nodes_by_backend[backend_id] = snapshot_node
            backend_document[backend_id] = document_index
    return DecodedSnapshot(
        documents=tuple(documents),
        nodes_by_backend_id=nodes_by_backend,
        backend_document_index=backend_document,
        strings=strings,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _decode_document(
    document_index: int,
    document: Mapping[str, Any],
    strings: Sequence[str],
    dpr: float,
) -> DecodedSnapshotDocument:
    nodes = _mapping(document.get("nodes"))
    layout = _mapping(document.get("layout"))
    backend_ids = tuple(_as_int(item) for item in _sequence(nodes.get("backendNodeId")))
    node_count = len(backend_ids)
    warnings: list[str] = []
    if not backend_ids:
        warnings.append(f"snapshot document {document_index} has no backendNodeId array")

    layout_indexes = tuple(_as_int(item) for item in _sequence(layout.get("nodeIndex")))
    layout_lookup = {node_index: layout_index for layout_index, node_index in enumerate(layout_indexes)}
    clickable_indexes = set(_rare_indexes(nodes.get("isClickable")))
    stacking_indexes = set(_rare_indexes(layout.get("stackingContexts")))
    computed_style_names = tuple(str(item) for item in _sequence(document.get("computedStyleNames")))
    if not computed_style_names:
        computed_style_names = REQUIRED_COMPUTED_STYLES

    result: dict[int, EnhancedSnapshotNode] = {}
    for node_index, backend_id in enumerate(backend_ids):
        if backend_id <= 0:
            continue
        layout_index = layout_lookup.get(node_index)
        bounds = _rect_at(layout.get("bounds"), layout_index, dpr=dpr)
        client_rect = _rect_at(layout.get("clientRects"), layout_index, dpr=dpr)
        scroll_rect = _rect_at(layout.get("scrollRects"), layout_index, dpr=dpr)
        paint_order = _value_at(layout.get("paintOrders"), layout_index)
        if paint_order is None:
            paint_order = _value_at(layout.get("paintOrder"), layout_index)
        styles = _computed_styles_at(
            layout.get("styles"),
            layout_index,
            strings=strings,
            names=computed_style_names,
        )
        cursor = styles.get("cursor") if styles else None
        result[backend_id] = EnhancedSnapshotNode(
            is_clickable=node_index in clickable_indexes,
            cursor_style=cursor,
            bounds=bounds,
            clientRects=client_rect,
            scrollRects=scroll_rect,
            computed_styles=styles or None,
            paint_order=_optional_int(paint_order),
            stacking_contexts=1 if layout_index in stacking_indexes else None,
        )

    frame_id = _string_at(strings, document.get("frameId"))
    content_size = _mapping(document.get("contentSize"))
    return DecodedSnapshotDocument(
        document_index=document_index,
        frame_id=frame_id,
        content_width=finite_number(content_size.get("width")),
        content_height=finite_number(content_size.get("height")),
        scroll_x=finite_number(document.get("scrollOffsetX")),
        scroll_y=finite_number(document.get("scrollOffsetY")),
        backend_node_ids=backend_ids,
        nodes=result,
        node_index_to_backend_id={index: backend for index, backend in enumerate(backend_ids) if backend > 0},
        warnings=tuple(warnings),
    )


def _computed_styles_at(
    raw_styles: Any,
    index: int | None,
    *,
    strings: Sequence[str],
    names: Sequence[str],
) -> dict[str, str]:
    if index is None:
        return {}
    styles = _sequence(raw_styles)
    if index >= len(styles):
        return {}
    raw = styles[index]
    if isinstance(raw, Mapping):
        return {str(key): _string_at(strings, value) for key, value in raw.items()}
    values = _sequence(raw)
    result: dict[str, str] = {}
    for style_index, raw_value in enumerate(values):
        if style_index >= len(names):
            break
        result[str(names[style_index])] = _string_at(strings, raw_value)
    return result


def _rect_at(raw_rects: Any, index: int | None, *, dpr: float) -> DOMRect | None:
    if index is None:
        return None
    rects = _sequence(raw_rects)
    if index >= len(rects):
        return None
    raw = rects[index]
    if isinstance(raw, Mapping):
        x = finite_number(raw.get("x")) / dpr
        y = finite_number(raw.get("y")) / dpr
        width = finite_number(raw.get("width")) / dpr
        height = finite_number(raw.get("height")) / dpr
    else:
        values = _sequence(raw)
        if len(values) < 4:
            return None
        x, y, width, height = (finite_number(value) / dpr for value in values[:4])
    if not all(math.isfinite(value) for value in (x, y, width, height)):
        return None
    if width < 0 or height < 0:
        return None
    return DOMRect(x=x, y=y, width=width, height=height)


def _rare_indexes(value: Any) -> tuple[int, ...]:
    if isinstance(value, Mapping):
        return tuple(_as_int(item) for item in _sequence(value.get("index")))
    return tuple(_as_int(item) for item in _sequence(value))


def _value_at(value: Any, index: int | None) -> Any:
    if index is None:
        return None
    if isinstance(value, Mapping):
        indexes = tuple(_as_int(item) for item in _sequence(value.get("index")))
        values = _sequence(value.get("value"))
        try:
            rare_index = indexes.index(index)
        except ValueError:
            return None
        return values[rare_index] if rare_index < len(values) else None
    values = _sequence(value)
    return values[index] if index < len(values) else None


def _string_at(strings: Sequence[str], value: Any) -> str:
    if isinstance(value, str):
        return value
    index = _optional_int(value)
    if index is None or index < 0 or index >= len(strings):
        return ""
    return strings[index]


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def snapshot_node_count(payload: Mapping[str, Any]) -> int:
    total = 0
    for document in _sequence(payload.get("documents")):
        if isinstance(document, Mapping):
            total += len(_sequence(_mapping(document.get("nodes")).get("backendNodeId")))
    return total


def validate_snapshot_payload(payload: Mapping[str, Any]) -> tuple[str, ...]:
    issues: list[str] = []
    documents = _sequence(payload.get("documents"))
    if not documents:
        issues.append("snapshot_documents_missing")
        return tuple(issues)
    for index, document in enumerate(documents):
        if not isinstance(document, Mapping):
            issues.append(f"snapshot_document_{index}_invalid")
            continue
        nodes = _mapping(document.get("nodes"))
        backend_ids = _sequence(nodes.get("backendNodeId"))
        if not backend_ids:
            issues.append(f"snapshot_document_{index}_backend_nodes_missing")
        layout = _mapping(document.get("layout"))
        node_indexes = _sequence(layout.get("nodeIndex"))
        bounds = _sequence(layout.get("bounds"))
        if bounds and len(bounds) != len(node_indexes):
            issues.append(f"snapshot_document_{index}_layout_bounds_misaligned")
    return tuple(issues)
