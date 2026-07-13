from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .models import SelectorBinding, digest_value, stable_id
from .selector_guard import ElementSemantics, SelectorReceipt


class GeometryGuardError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class GeometryPhase(StrEnum):
    INITIAL_PROBE = "initial_probe"
    SCROLL = "scroll"
    POST_SCROLL_PROBE = "post_scroll_probe"
    HIT_TEST = "hit_test"
    READY = "ready"


class ScrollDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise ValueError("browser geometry point must be finite")

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(item) for item in (self.x, self.y, self.width, self.height)):
            raise ValueError("browser geometry rectangle must be finite")
        if self.width < 0 or self.height < 0:
            raise ValueError("browser geometry rectangle dimensions cannot be negative")

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Point:
        return Point(self.x + self.width / 2, self.y + self.height / 2)

    def contains(self, point: Point) -> bool:
        return self.x <= point.x <= self.right and self.y <= point.y <= self.bottom

    def intersect(self, other: "Rect") -> "Rect":
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        return Rect(left, top, max(0.0, right - left), max(0.0, bottom - top))

    def translate(self, dx: float, dy: float) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.width, self.height)

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True, slots=True)
class Viewport:
    width: float
    height: float
    scroll_x: float = 0.0
    scroll_y: float = 0.0
    device_scale_factor: float = 1.0
    page_scale_factor: float = 1.0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("browser viewport dimensions must be positive")
        if self.device_scale_factor <= 0 or self.page_scale_factor <= 0:
            raise ValueError("browser viewport scale factors must be positive")
        if not all(
            math.isfinite(item)
            for item in (
                self.width,
                self.height,
                self.scroll_x,
                self.scroll_y,
                self.device_scale_factor,
                self.page_scale_factor,
            )
        ):
            raise ValueError("browser viewport metrics must be finite")

    @property
    def client_rect(self) -> Rect:
        return Rect(0.0, 0.0, self.width, self.height)

    def to_dict(self) -> dict[str, float]:
        return {
            "width": self.width,
            "height": self.height,
            "scroll_x": self.scroll_x,
            "scroll_y": self.scroll_y,
            "device_scale_factor": self.device_scale_factor,
            "page_scale_factor": self.page_scale_factor,
        }


@dataclass(frozen=True, slots=True)
class LiveElementProbe:
    backend_node_id: int
    frame_id: str
    cdp_session_id: str
    rects: tuple[Rect, ...]
    viewport: Viewport
    connected: bool
    visible: bool
    disabled: bool
    inert: bool = False
    pointer_events: bool = True
    opacity: float = 1.0
    top_backend_node_id: int = 0
    top_ancestor_backend_ids: tuple[int, ...] = ()
    document_loader_id: str = ""
    node_name: str = ""
    input_type: str = ""
    scrollable_ancestor_backend_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rects", tuple(self.rects))
        object.__setattr__(self, "top_ancestor_backend_ids", tuple(self.top_ancestor_backend_ids))
        object.__setattr__(self, "scrollable_ancestor_backend_ids", tuple(self.scrollable_ancestor_backend_ids))
        if self.backend_node_id <= 0:
            raise ValueError("live element probe requires positive backend_node_id")
        if not 0 <= self.opacity <= 1:
            raise ValueError("live element opacity must be between zero and one")

    @property
    def bounding_rect(self) -> Rect:
        if not self.rects:
            return Rect(0, 0, 0, 0)
        left = min(rect.x for rect in self.rects)
        top = min(rect.y for rect in self.rects)
        right = max(rect.right for rect in self.rects)
        bottom = max(rect.bottom for rect in self.rects)
        return Rect(left, top, right - left, bottom - top)

    @property
    def geometry_digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_node_id": self.backend_node_id,
            "frame_id": self.frame_id,
            "cdp_session_id": self.cdp_session_id,
            "rects": [rect.to_dict() for rect in self.rects],
            "viewport": self.viewport.to_dict(),
            "connected": self.connected,
            "visible": self.visible,
            "disabled": self.disabled,
            "inert": self.inert,
            "pointer_events": self.pointer_events,
            "opacity": self.opacity,
            "top_backend_node_id": self.top_backend_node_id,
            "top_ancestor_backend_ids": list(self.top_ancestor_backend_ids),
            "document_loader_id": self.document_loader_id,
            "node_name": self.node_name,
            "input_type": self.input_type,
            "scrollable_ancestor_backend_ids": list(self.scrollable_ancestor_backend_ids),
        }


@dataclass(frozen=True, slots=True)
class ScrollPlan:
    delta_x: float
    delta_y: float
    direction: ScrollDirection | None
    required: bool
    maximum_attempts: int
    reason: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.delta_x) or not math.isfinite(self.delta_y):
            raise ValueError("browser scroll delta must be finite")
        if self.maximum_attempts < 0 or self.maximum_attempts > 8:
            raise ValueError("browser scroll attempts must be between zero and eight")

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta_x": self.delta_x,
            "delta_y": self.delta_y,
            "direction": str(self.direction) if self.direction else "",
            "required": self.required,
            "maximum_attempts": self.maximum_attempts,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class GeometryReceipt:
    receipt_id: str
    action_id: str
    selector_binding_digest: str
    initial_probe_digest: str
    final_probe_digest: str
    dispatch_point: Point
    scroll_plan: ScrollPlan
    scroll_attempts: int
    phases: tuple[GeometryPhase, ...]
    target_cdp_session_id: str
    frame_id: str

    @property
    def binding_digest(self) -> str:
        return digest_value(self.public_dict())

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "selector_binding_digest": self.selector_binding_digest,
            "initial_probe_digest": self.initial_probe_digest,
            "final_probe_digest": self.final_probe_digest,
            "dispatch_point": self.dispatch_point.to_dict(),
            "scroll_plan": self.scroll_plan.to_dict(),
            "scroll_attempts": self.scroll_attempts,
            "phases": [str(item) for item in self.phases],
            "target_cdp_session_id": self.target_cdp_session_id,
            "frame_id": self.frame_id,
        }


class ElementProbePort(Protocol):
    def probe(self, binding: SelectorBinding) -> LiveElementProbe: ...

    def scroll(self, binding: SelectorBinding, *, delta_x: float, delta_y: float) -> None: ...


@dataclass(slots=True)
class RecordingElementProbe:
    """Deterministic probe port useful for memory-CDP and policy integration."""

    probes: list[LiveElementProbe]
    operations: list[dict[str, Any]] = field(default_factory=list)
    _index: int = 0

    def probe(self, binding: SelectorBinding) -> LiveElementProbe:
        if not self.probes:
            raise GeometryGuardError("probe_unavailable", "no live element probe is configured")
        selected = self.probes[min(self._index, len(self.probes) - 1)]
        self._index += 1
        self.operations.append({"operation": "probe", "binding": binding.identity_digest, "probe": selected.geometry_digest})
        return selected

    def scroll(self, binding: SelectorBinding, *, delta_x: float, delta_y: float) -> None:
        self.operations.append(
            {
                "operation": "scroll",
                "binding": binding.identity_digest,
                "delta_x": delta_x,
                "delta_y": delta_y,
            }
        )


class BrowserGeometryGuard:
    """Scroll, fresh geometry and topmost hit-test gate for element actions."""

    def __init__(
        self,
        probe_port: ElementProbePort,
        *,
        maximum_scroll_attempts: int = 2,
        viewport_margin: float = 8.0,
        minimum_click_area: float = 1.0,
        disabled: bool = False,
    ) -> None:
        if maximum_scroll_attempts < 0 or maximum_scroll_attempts > 8:
            raise ValueError("maximum_scroll_attempts must be between zero and eight")
        if viewport_margin < 0 or minimum_click_area <= 0:
            raise ValueError("browser geometry limits are invalid")
        self.probe_port = probe_port
        self.maximum_scroll_attempts = maximum_scroll_attempts
        self.viewport_margin = viewport_margin
        self.minimum_click_area = minimum_click_area
        self.disabled = disabled

    def preflight(self, *, selector: SelectorReceipt, action: str) -> GeometryReceipt:
        if self.disabled:
            raise GeometryGuardError("geometry_guard_disabled", "browser geometry guard is disabled")
        phases: list[GeometryPhase] = [GeometryPhase.INITIAL_PROBE]
        initial = self.probe_port.probe(selector.binding)
        self._validate_probe(selector, initial, action=action)
        final = initial
        scroll_plan = plan_scroll(initial, margin=self.viewport_margin, maximum_attempts=self.maximum_scroll_attempts)
        attempts = 0
        while scroll_plan.required and attempts < scroll_plan.maximum_attempts:
            phases.append(GeometryPhase.SCROLL)
            self.probe_port.scroll(
                selector.binding,
                delta_x=scroll_plan.delta_x,
                delta_y=scroll_plan.delta_y,
            )
            attempts += 1
            phases.append(GeometryPhase.POST_SCROLL_PROBE)
            final = self.probe_port.probe(selector.binding)
            self._validate_probe(selector, final, action=action)
            scroll_plan = plan_scroll(final, margin=self.viewport_margin, maximum_attempts=self.maximum_scroll_attempts)
        if scroll_plan.required:
            raise GeometryGuardError(
                "element_offscreen",
                "browser element remains outside the viewport after bounded scroll",
                details={"attempts": attempts, "rect": final.bounding_rect.to_dict()},
            )
        point = choose_dispatch_point(final, margin=self.viewport_margin)
        phases.append(GeometryPhase.HIT_TEST)
        self._validate_hit_test(selector, final, point, action=action)
        phases.append(GeometryPhase.READY)
        receipt_id = stable_id(
            "brgeometry",
            selector.action_id,
            selector.binding.identity_digest,
            initial.geometry_digest,
            final.geometry_digest,
            point.to_dict(),
            attempts,
        )
        return GeometryReceipt(
            receipt_id=receipt_id,
            action_id=selector.action_id,
            selector_binding_digest=selector.binding.identity_digest,
            initial_probe_digest=initial.geometry_digest,
            final_probe_digest=final.geometry_digest,
            dispatch_point=point,
            scroll_plan=scroll_plan,
            scroll_attempts=attempts,
            phases=tuple(phases),
            target_cdp_session_id=final.cdp_session_id,
            frame_id=final.frame_id,
        )

    def revalidate(self, *, selector: SelectorReceipt, receipt: GeometryReceipt, action: str) -> GeometryReceipt:
        if receipt.selector_binding_digest != selector.binding.identity_digest:
            raise GeometryGuardError("geometry_selector_changed", "geometry receipt belongs to another selector binding")
        current = self.preflight(selector=selector, action=action)
        if current.target_cdp_session_id != receipt.target_cdp_session_id or current.frame_id != receipt.frame_id:
            raise GeometryGuardError("geometry_frame_changed", "element frame or CDP session changed after approval")
        return current

    def _validate_probe(self, selector: SelectorReceipt, probe: LiveElementProbe, *, action: str) -> None:
        binding = selector.binding
        mismatches: dict[str, Any] = {}
        if probe.backend_node_id != binding.backend_node_id:
            mismatches["backend_node_id"] = (binding.backend_node_id, probe.backend_node_id)
        if probe.cdp_session_id != binding.cdp_session_id:
            mismatches["cdp_session_id"] = (binding.cdp_session_id, probe.cdp_session_id)
        if binding.frame_id and probe.frame_id != binding.frame_id:
            mismatches["frame_id"] = (binding.frame_id, probe.frame_id)
        if binding.document_loader_id and probe.document_loader_id != binding.document_loader_id:
            mismatches["document_loader_id"] = (binding.document_loader_id, probe.document_loader_id)
        if mismatches:
            raise GeometryGuardError(
                "live_element_identity_mismatch",
                "live element no longer matches its selector receipt",
                details={"mismatches": mismatches},
            )
        if not probe.connected:
            raise GeometryGuardError("element_detached", "browser element is detached")
        if not probe.visible or probe.opacity <= 0 or probe.bounding_rect.area < self.minimum_click_area:
            raise GeometryGuardError("element_hidden", "browser element has no visible interaction geometry")
        if probe.disabled or probe.inert:
            raise GeometryGuardError("element_disabled", "browser element is disabled or inert")
        if action in mutating_pointer_actions() and not probe.pointer_events:
            raise GeometryGuardError("pointer_events_disabled", "browser element does not accept pointer events")
        validate_special_control(selector.semantics, probe, action=action)

    @staticmethod
    def _validate_hit_test(selector: SelectorReceipt, probe: LiveElementProbe, point: Point, *, action: str) -> None:
        if action not in mutating_pointer_actions():
            return
        if not probe.bounding_rect.contains(point):
            raise GeometryGuardError("dispatch_point_outside_element", "dispatch point is outside element geometry")
        accepted = {probe.backend_node_id, *probe.top_ancestor_backend_ids}
        if probe.top_backend_node_id not in accepted:
            raise GeometryGuardError(
                "element_occluded",
                "another element is topmost at the browser dispatch point",
                details={
                    "expected_backend_node_id": probe.backend_node_id,
                    "top_backend_node_id": probe.top_backend_node_id,
                    "dispatch_point": point.to_dict(),
                },
            )


def mutating_pointer_actions() -> frozenset[str]:
    return frozenset(
        {
            "click_element",
            "input_text",
            "submit_form",
            "check_element",
            "drag_element",
            "hover_element",
            "upload_file",
            "select_dropdown",
        }
    )


def plan_scroll(probe: LiveElementProbe, *, margin: float, maximum_attempts: int) -> ScrollPlan:
    rect = probe.bounding_rect
    viewport = probe.viewport.client_rect
    visible = rect.intersect(viewport)
    if visible.area >= min(rect.area, 4.0) and rect.center.x >= margin and rect.center.y >= margin and rect.center.x <= viewport.right - margin and rect.center.y <= viewport.bottom - margin:
        return ScrollPlan(0.0, 0.0, None, False, maximum_attempts, "element_interaction_point_visible")
    target_x = viewport.width / 2
    target_y = viewport.height / 2
    delta_x = rect.center.x - target_x
    delta_y = rect.center.y - target_y
    max_x = max(64.0, viewport.width * 0.9)
    max_y = max(64.0, viewport.height * 0.9)
    delta_x = max(-max_x, min(max_x, delta_x))
    delta_y = max(-max_y, min(max_y, delta_y))
    if abs(delta_y) >= abs(delta_x):
        direction = ScrollDirection.DOWN if delta_y > 0 else ScrollDirection.UP
    else:
        direction = ScrollDirection.RIGHT if delta_x > 0 else ScrollDirection.LEFT
    return ScrollPlan(delta_x, delta_y, direction, True, maximum_attempts, "element_interaction_point_offscreen")


def choose_dispatch_point(probe: LiveElementProbe, *, margin: float) -> Point:
    viewport = probe.viewport.client_rect
    candidates: list[Point] = []
    for rect in sorted(probe.rects, key=lambda item: item.area, reverse=True):
        visible = rect.intersect(viewport)
        if visible.area <= 0:
            continue
        inset_x = min(max(margin, 1.0), visible.width / 3)
        inset_y = min(max(margin, 1.0), visible.height / 3)
        candidates.extend(
            (
                visible.center,
                Point(visible.x + inset_x, visible.y + inset_y),
                Point(visible.right - inset_x, visible.y + inset_y),
                Point(visible.x + inset_x, visible.bottom - inset_y),
            )
        )
    for point in candidates:
        if viewport.contains(point) and probe.bounding_rect.contains(point):
            return point
    raise GeometryGuardError("no_dispatch_point", "browser element has no safe in-viewport dispatch point")


def validate_special_control(semantics: ElementSemantics, probe: LiveElementProbe, *, action: str) -> None:
    node_name = (probe.node_name or semantics.tag_name).casefold()
    input_type = (probe.input_type or semantics.input_type).casefold()
    if action == "upload_file" and not (node_name == "input" and input_type == "file"):
        raise GeometryGuardError("file_input_changed", "approved element is no longer a file input")
    if action in {"select_dropdown", "get_dropdown_options"} and node_name != "select" and semantics.role not in {"combobox", "listbox"}:
        raise GeometryGuardError("select_control_changed", "approved element is no longer a select control")
    if action == "check_element" and input_type not in {"checkbox", "radio"} and semantics.role not in {"checkbox", "radio", "switch"}:
        raise GeometryGuardError("check_control_changed", "approved element is no longer checkable")
    if action == "input_text" and input_type in {"file", "submit", "button", "reset", "checkbox", "radio"}:
        raise GeometryGuardError("text_control_changed", "approved element is no longer a text control")


def rects_from_cdp_quads(quads: Sequence[Sequence[float]]) -> tuple[Rect, ...]:
    rects: list[Rect] = []
    for quad in quads:
        if len(quad) != 8 or not all(isinstance(value, int | float) and math.isfinite(float(value)) for value in quad):
            continue
        xs = [float(quad[index]) for index in (0, 2, 4, 6)]
        ys = [float(quad[index]) for index in (1, 3, 5, 7)]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)
        if width > 0 and height > 0:
            rects.append(Rect(min(xs), min(ys), width, height))
    return tuple(rects)


def css_to_device_point(point: Point, viewport: Viewport) -> Point:
    scale = viewport.device_scale_factor * viewport.page_scale_factor
    return Point(point.x * scale, point.y * scale)
