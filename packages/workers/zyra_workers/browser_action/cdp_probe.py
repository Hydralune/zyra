from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .executor import CdpTransport
from .geometry_guard import (
    ElementProbePort,
    GeometryGuardError,
    LiveElementProbe,
    Rect,
    Viewport,
    rects_from_cdp_quads,
)
from .models import SelectorBinding


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    include_user_agent_shadow_dom: bool = True
    ignore_pointer_events_none: bool = False
    minimum_opacity: float = 0.01
    maximum_ancestor_depth: int = 32

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_opacity <= 1:
            raise ValueError("probe minimum opacity must be between zero and one")
        if not 1 <= self.maximum_ancestor_depth <= 128:
            raise ValueError("probe ancestor depth must be between one and 128")


@dataclass(slots=True)
class CdpElementProbePort(ElementProbePort):
    """Frame-correct CDP geometry port adapted from browser-use watchdogs.

    It uses the 04B binding's exact CDP session/backend node.  It never looks
    up a nearby element and never falls back to document-wide JavaScript click.
    Read probes use narrow functions with ``throwOnSideEffect``.  Scrolling is
    explicit and is invoked only after permission consumption by the gateway.
    """

    transport: CdpTransport
    config: ProbeConfig = field(default_factory=ProbeConfig)
    operations: list[dict[str, Any]] = field(default_factory=list)

    def probe(self, binding: SelectorBinding) -> LiveElementProbe:
        self._validate_binding(binding)
        session = binding.cdp_session_id
        self.operations.append({"operation": "probe_start", "binding": binding.identity_digest})
        node = self._describe(binding)
        if int(node.get("backendNodeId", 0)) != binding.backend_node_id:
            raise GeometryGuardError(
                "probe_backend_mismatch",
                "CDP described a different backend node than the selector binding",
            )
        frame_id = str(node.get("frameId") or binding.frame_id)
        if binding.frame_id and frame_id and frame_id != binding.frame_id:
            raise GeometryGuardError("probe_frame_mismatch", "CDP element moved to another frame")
        quads = self._content_quads(binding)
        rects = rects_from_cdp_quads(quads)
        if not rects:
            rects = self._box_model_rects(binding)
        viewport = self._viewport(binding)
        runtime_state = self._runtime_state(binding)
        dispatch_point = largest_visible_center(rects, viewport)
        hit = self._hit_test(binding, dispatch_point) if dispatch_point is not None else {}
        top_backend_node_id = int(hit.get("backendNodeId", 0))
        top_ancestors = self._ancestor_backend_ids(
            binding,
            node_id=int(hit.get("nodeId", 0)),
            backend_node_id=top_backend_node_id,
        )
        scrollable = tuple(
            int(item)
            for item in runtime_state.get("scrollableAncestorBackendNodeIds", [])
            if isinstance(item, int) and item > 0
        )
        probe = LiveElementProbe(
            backend_node_id=binding.backend_node_id,
            frame_id=frame_id,
            cdp_session_id=session,
            rects=rects,
            viewport=viewport,
            connected=bool(runtime_state.get("connected", False)),
            visible=bool(runtime_state.get("visible", False)),
            disabled=bool(runtime_state.get("disabled", False)),
            inert=bool(runtime_state.get("inert", False)),
            pointer_events=str(runtime_state.get("pointerEvents", "auto")) != "none",
            opacity=bounded_float(runtime_state.get("opacity"), default=1.0, minimum=0.0, maximum=1.0),
            top_backend_node_id=top_backend_node_id,
            top_ancestor_backend_ids=top_ancestors,
            document_loader_id=binding.document_loader_id,
            node_name=str(node.get("nodeName", "")),
            input_type=str(runtime_state.get("inputType", "")),
            scrollable_ancestor_backend_ids=scrollable,
        )
        self.operations.append({"operation": "probe_complete", "probe": probe.geometry_digest})
        return probe

    def scroll(self, binding: SelectorBinding, *, delta_x: float, delta_y: float) -> None:
        self._validate_binding(binding)
        if not math.isfinite(delta_x) or not math.isfinite(delta_y):
            raise GeometryGuardError("invalid_scroll_delta", "browser scroll delta must be finite")
        # DOM.scrollIntoViewIfNeeded keeps the mutation tied to the exact
        # backend node and avoids page-global evaluate/nearest-element logic.
        self.transport.send(
            "DOM.scrollIntoViewIfNeeded",
            {"backendNodeId": binding.backend_node_id},
            cdp_session_id=binding.cdp_session_id,
        )
        self.operations.append(
            {
                "operation": "scroll_into_view",
                "binding": binding.identity_digest,
                "requested_delta_x": delta_x,
                "requested_delta_y": delta_y,
            }
        )

    def _describe(self, binding: SelectorBinding) -> Mapping[str, Any]:
        response = self.transport.send(
            "DOM.describeNode",
            {
                "backendNodeId": binding.backend_node_id,
                "depth": 0,
                "pierce": self.config.include_user_agent_shadow_dom,
            },
            cdp_session_id=binding.cdp_session_id,
        )
        node = response.get("node", {})
        if not isinstance(node, Mapping):
            raise GeometryGuardError("probe_node_missing", "CDP did not describe the approved element")
        return node

    def _content_quads(self, binding: SelectorBinding) -> Sequence[Sequence[float]]:
        try:
            response = self.transport.send(
                "DOM.getContentQuads",
                {"backendNodeId": binding.backend_node_id},
                cdp_session_id=binding.cdp_session_id,
            )
        except Exception:
            return ()
        quads = response.get("quads", ())
        return quads if isinstance(quads, Sequence) else ()

    def _box_model_rects(self, binding: SelectorBinding) -> tuple[Rect, ...]:
        try:
            response = self.transport.send(
                "DOM.getBoxModel",
                {"backendNodeId": binding.backend_node_id},
                cdp_session_id=binding.cdp_session_id,
            )
        except Exception:
            return ()
        model = response.get("model", {})
        if not isinstance(model, Mapping):
            return ()
        content = model.get("content", ())
        if not isinstance(content, Sequence):
            return ()
        return rects_from_cdp_quads((content,))

    def _viewport(self, binding: SelectorBinding) -> Viewport:
        metrics = self.transport.send(
            "Page.getLayoutMetrics",
            {},
            cdp_session_id=binding.cdp_session_id,
        )
        visual = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
        layout = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
        if not isinstance(visual, Mapping):
            visual = {}
        if not isinstance(layout, Mapping):
            layout = {}
        width = bounded_float(visual.get("clientWidth", layout.get("clientWidth")), default=0.0, minimum=0.0, maximum=100_000.0)
        height = bounded_float(visual.get("clientHeight", layout.get("clientHeight")), default=0.0, minimum=0.0, maximum=100_000.0)
        if width <= 0 or height <= 0:
            raise GeometryGuardError("viewport_unavailable", "CDP did not return a positive visual viewport")
        return Viewport(
            width=width,
            height=height,
            scroll_x=bounded_float(visual.get("pageX"), default=0.0, minimum=-10_000_000.0, maximum=10_000_000.0),
            scroll_y=bounded_float(visual.get("pageY"), default=0.0, minimum=-10_000_000.0, maximum=10_000_000.0),
            device_scale_factor=bounded_float(visual.get("scale"), default=1.0, minimum=0.01, maximum=100.0),
            page_scale_factor=bounded_float(visual.get("zoom"), default=1.0, minimum=0.01, maximum=100.0),
        )

    def _runtime_state(self, binding: SelectorBinding) -> Mapping[str, Any]:
        resolved = self.transport.send(
            "DOM.resolveNode",
            {"backendNodeId": binding.backend_node_id},
            cdp_session_id=binding.cdp_session_id,
        )
        object_id = str(resolved.get("object", {}).get("objectId", ""))
        if not object_id:
            raise GeometryGuardError("probe_object_missing", "CDP could not resolve approved element object")
        response = self.transport.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": READ_ONLY_ELEMENT_PROBE,
                "returnByValue": True,
                "throwOnSideEffect": True,
                "silent": True,
            },
            cdp_session_id=binding.cdp_session_id,
        )
        if response.get("exceptionDetails"):
            raise GeometryGuardError("probe_side_effect_rejected", "CDP rejected read-only element probe")
        value = response.get("result", {}).get("value", {})
        return value if isinstance(value, Mapping) else {}

    def _hit_test(self, binding: SelectorBinding, point: tuple[float, float]) -> Mapping[str, Any]:
        x, y = point
        response = self.transport.send(
            "DOM.getNodeForLocation",
            {
                "x": int(round(x)),
                "y": int(round(y)),
                "includeUserAgentShadowDOM": self.config.include_user_agent_shadow_dom,
                "ignorePointerEventsNone": self.config.ignore_pointer_events_none,
            },
            cdp_session_id=binding.cdp_session_id,
        )
        return response

    def _ancestor_backend_ids(
        self,
        binding: SelectorBinding,
        *,
        node_id: int,
        backend_node_id: int,
    ) -> tuple[int, ...]:
        if backend_node_id <= 0:
            return ()
        if backend_node_id == binding.backend_node_id:
            return (backend_node_id,)
        if node_id <= 0:
            return (backend_node_id,)
        response = self.transport.send(
            "DOM.getNodeStackTraces",
            {"nodeId": node_id},
            cdp_session_id=binding.cdp_session_id,
        )
        # Node stack traces are not an ancestry API; retain the top node only.
        # Exact ancestry is checked by the runtime read probe/hit result when
        # available.  Never infer that an unrelated node is acceptable.
        return (backend_node_id,) if response is not None else ()

    @staticmethod
    def _validate_binding(binding: SelectorBinding) -> None:
        if binding.backend_node_id <= 0 or binding.target_generation <= 0 or binding.cdp_generation <= 0:
            raise GeometryGuardError("probe_binding_invalid", "browser probe requires a complete positive selector binding")
        if not binding.cdp_session_id or not binding.target_id:
            raise GeometryGuardError("probe_binding_invalid", "browser probe requires target and CDP session identity")


READ_ONLY_ELEMENT_PROBE = """
function () {
  const style = this.ownerDocument.defaultView.getComputedStyle(this);
  const rect = this.getBoundingClientRect();
  const visible = Boolean(
    this.isConnected &&
    rect.width > 0 && rect.height > 0 &&
    style.display !== 'none' && style.visibility !== 'hidden' &&
    Number(style.opacity || 1) > 0
  );
  const ancestors = [];
  for (let node = this.parentElement, depth = 0; node && depth < 32; node = node.parentElement, depth++) {
    const s = this.ownerDocument.defaultView.getComputedStyle(node);
    if (/(auto|scroll)/.test(`${s.overflowX} ${s.overflowY}`)) {
      const id = Number(node.getAttribute('data-zyra-backend-node-id') || 0);
      if (id > 0) ancestors.push(id);
    }
  }
  return {
    connected: Boolean(this.isConnected),
    visible,
    disabled: Boolean(this.disabled || this.getAttribute('aria-disabled') === 'true'),
    inert: Boolean(this.inert || this.closest('[inert]')),
    pointerEvents: style.pointerEvents,
    opacity: Number(style.opacity || 1),
    inputType: String(this.type || ''),
    scrollableAncestorBackendNodeIds: ancestors,
  };
}
""".strip()


def largest_visible_center(rects: Sequence[Rect], viewport: Viewport) -> tuple[float, float] | None:
    visible = [rect.intersect(viewport.client_rect) for rect in rects]
    visible = [rect for rect in visible if rect.area > 0]
    if not visible:
        return None
    selected = max(visible, key=lambda item: item.area)
    center = selected.center
    return center.x, center.y


def bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(minimum, min(maximum, parsed))
