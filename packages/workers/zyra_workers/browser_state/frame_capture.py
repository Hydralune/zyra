from __future__ import annotations

"""Same-origin frame AX and flattened OOPIF capture for 04B browser state.

Chrome exposes out-of-process iframes as independent flattened CDP sessions.
04A already owns target discovery, attachment, and session generations.  This
module only reads that live topology and composes child state into the current
browser disclosure; it does not attach, detach, focus, or persist targets.
"""

import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .contracts import BrowserDomCaptureRequest, BrowserFrameState, BrowserViewportState
from .dom_builder import DomBuildReport, build_enhanced_dom
from .errors import BrowserDomCaptureFailed, BrowserDomCaptureStale, BrowserDomRootMissing
from .models import EnhancedDOMTreeNode, SerializedDOMState
from .serializer import DOMTreeSerializer
from .snapshot_decoder import decode_snapshot, snapshot_node_count, validate_snapshot_payload
from .text import finite_number


class BrowserFrameCdpPort(Protocol):
    def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        cdp_session_id: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


class BrowserFrameTargetPort(Protocol):
    @property
    def active_target_id(self) -> str: ...
    def snapshot(self) -> Any: ...
    def target(self, target_id: str) -> Any: ...
    def cdp_session(self, target_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class BrowserFrameCaptureCandidate:
    frame_id: str
    target_id: str
    cdp_session_id: str
    url: str
    parent_frame_id: str = ""
    security_origin: str = ""
    target_type: str = "iframe"
    oopif: bool = False
    same_origin: bool = False

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.target_id, self.cdp_session_id, self.frame_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "target_id": self.target_id,
            "cdp_session_id": self.cdp_session_id,
            "url": self.url,
            "parent_frame_id": self.parent_frame_id,
            "security_origin": self.security_origin,
            "target_type": self.target_type,
            "oopif": self.oopif,
            "same_origin": self.same_origin,
        }


@dataclass(slots=True)
class BrowserOopifCapture:
    candidate: BrowserFrameCaptureCandidate
    root: EnhancedDOMTreeNode
    serialized_state: SerializedDOMState
    raw_dom: Mapping[str, Any]
    raw_ax: Mapping[str, Any]
    raw_snapshot: Mapping[str, Any]
    frame_tree: Mapping[str, Any]
    viewport: BrowserViewportState
    dom_nodes: int
    ax_nodes: int
    snapshot_nodes: int
    selector_candidates: int
    duration_ms: float
    warnings: tuple[str, ...] = ()

    def frame_states(self) -> tuple[BrowserFrameState, ...]:
        frames = _frame_states(
            self.frame_tree,
            target_id=self.candidate.target_id,
            cdp_session_id=self.candidate.cdp_session_id,
            oopif=True,
            default_frame_id=self.candidate.frame_id,
            default_url=self.candidate.url,
            default_parent_frame_id=self.candidate.parent_frame_id,
        )
        return frames

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "frames": [item.to_dict() for item in self.frame_states()],
            "viewport": self.viewport.to_dict(),
            "dom_nodes": self.dom_nodes,
            "ax_nodes": self.ax_nodes,
            "snapshot_nodes": self.snapshot_nodes,
            "selector_candidates": self.selector_candidates,
            "duration_ms": self.duration_ms,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class BrowserFrameComposition:
    same_origin_ax: Mapping[str, Any]
    oopif_captures: tuple[BrowserOopifCapture, ...]
    frames: tuple[BrowserFrameState, ...]
    warnings: tuple[str, ...]
    planned_count: int
    captured_count: int
    omitted_count: int
    same_origin_ax_count: int
    oopif_count: int

    @property
    def degraded(self) -> bool:
        return self.omitted_count > 0 or bool(self.warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "frames": [item.to_dict() for item in self.frames],
            "warnings": list(self.warnings),
            "planned_count": self.planned_count,
            "captured_count": self.captured_count,
            "omitted_count": self.omitted_count,
            "same_origin_ax_count": self.same_origin_ax_count,
            "oopif_count": self.oopif_count,
            "degraded": self.degraded,
            "oopif_captures": [item.to_dict() for item in self.oopif_captures],
        }


class BrowserFrameCapturePlanner:
    """Map Page frame tree and 04A target snapshot without changing owners."""

    def __init__(self, *, max_iframes: int = 32) -> None:
        self.max_iframes = max(0, int(max_iframes))

    def plan(
        self,
        request: BrowserDomCaptureRequest,
        frame_tree: Mapping[str, Any],
        target_runtime: BrowserFrameTargetPort,
    ) -> tuple[BrowserFrameCaptureCandidate, ...]:
        root_frames = _flatten_frame_tree(frame_tree)
        root_origin = ""
        if root_frames:
            root_origin = str(root_frames[0].get("securityOrigin") or "")
        candidates: list[BrowserFrameCaptureCandidate] = []
        root_frame_id = str(root_frames[0].get("id") or "") if root_frames else ""
        for frame in root_frames[1:]:
            frame_id = str(frame.get("id") or "")
            if not frame_id:
                continue
            origin = str(frame.get("securityOrigin") or "")
            candidates.append(BrowserFrameCaptureCandidate(
                frame_id=frame_id,
                target_id=request.target_id,
                cdp_session_id=request.cdp_session_id,
                url=str(frame.get("url") or ""),
                parent_frame_id=str(frame.get("parentId") or root_frame_id),
                security_origin=origin,
                target_type="frame",
                oopif=False,
                same_origin=not origin or not root_origin or origin == root_origin,
            ))

        snapshot = target_runtime.snapshot()
        targets = tuple(getattr(snapshot, "targets", ()) or ())
        sessions = tuple(getattr(snapshot, "cdp_sessions", ()) or ())
        session_by_target: dict[str, str] = {}
        for session in sessions:
            target_id = str(getattr(session, "target_id", "") or "")
            session_id = str(getattr(session, "cdp_session_id", "") or "")
            generation = int(getattr(session, "generation", -1))
            if target_id and session_id and generation == request.target_generation:
                session_by_target.setdefault(target_id, session_id)
        known_frame_ids = {item.frame_id for item in candidates}
        for target in targets:
            target_id = str(getattr(target, "target_id", "") or "")
            target_type = str(getattr(target, "target_type", "") or "")
            if target_id == request.target_id or target_type not in {"iframe", "frame"}:
                continue
            cdp_session_id = session_by_target.get(target_id, "")
            if not cdp_session_id:
                candidates.append(BrowserFrameCaptureCandidate(
                    frame_id=target_id,
                    target_id=target_id,
                    cdp_session_id="",
                    url=str(getattr(target, "url", "") or ""),
                    target_type=target_type,
                    oopif=True,
                    same_origin=False,
                ))
                continue
            frame_id = target_id
            if target_id in known_frame_ids:
                frame_id = target_id
            candidates.append(BrowserFrameCaptureCandidate(
                frame_id=frame_id,
                target_id=target_id,
                cdp_session_id=cdp_session_id,
                url=str(getattr(target, "url", "") or ""),
                target_type=target_type,
                oopif=True,
                same_origin=False,
            ))
        deduped: dict[tuple[str, str, bool], BrowserFrameCaptureCandidate] = {}
        for item in candidates:
            deduped.setdefault((item.frame_id, item.target_id, item.oopif), item)
        oopif_frame_ids = {item.frame_id for item in deduped.values() if item.oopif}
        values = sorted(
            (
                item for item in deduped.values()
                if item.oopif or item.frame_id not in oopif_frame_ids
            ),
            key=lambda item: (not item.oopif, item.parent_frame_id, item.frame_id, item.target_id),
        )
        return tuple(values[: self.max_iframes])


class BrowserFrameCaptureRuntime:
    """Capture child frame semantics through existing flattened CDP sessions."""

    def __init__(
        self,
        *,
        request_timeout_seconds: float = 10.0,
        max_iframes: int = 32,
        computed_styles: Sequence[str] = (),
        disabled: bool = False,
    ) -> None:
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.max_iframes = max(0, int(max_iframes))
        self.computed_styles = tuple(computed_styles)
        self.disabled = disabled
        self.planner = BrowserFrameCapturePlanner(max_iframes=self.max_iframes)
        self._plans = 0
        self._same_origin_ax = 0
        self._oopif_captures = 0
        self._child_failures = 0

    def capture(
        self,
        request: BrowserDomCaptureRequest,
        *,
        root_frame_tree: Mapping[str, Any],
        cdp_runtime: BrowserFrameCdpPort,
        target_runtime: BrowserFrameTargetPort,
    ) -> BrowserFrameComposition:
        if self.disabled or self.max_iframes <= 0:
            return BrowserFrameComposition({}, (), (), (), 0, 0, 0, 0, 0)
        candidates = self.planner.plan(request, root_frame_tree, target_runtime)
        self._plans += 1
        warnings: list[str] = []
        ax_nodes: list[Mapping[str, Any]] = []
        captures: list[BrowserOopifCapture] = []
        frames: list[BrowserFrameState] = []
        omitted = 0
        same_origin_count = 0
        oopif_count = 0
        for candidate in candidates:
            if candidate.oopif:
                if not candidate.cdp_session_id:
                    omitted += 1
                    warnings.append(f"oopif_session_missing:{candidate.target_id}")
                    frames.append(BrowserFrameState(
                        frame_id=candidate.frame_id,
                        parent_frame_id=candidate.parent_frame_id,
                        target_id=candidate.target_id,
                        cdp_session_id="",
                        url=candidate.url,
                        security_origin=candidate.security_origin,
                        cross_origin=True,
                        oopif=True,
                        complete=False,
                        error="flattened_cdp_session_missing",
                    ))
                    continue
                try:
                    capture = self._capture_oopif(candidate, request, cdp_runtime, target_runtime)
                except Exception as error:
                    omitted += 1
                    self._child_failures += 1
                    warnings.append(f"oopif_capture_failed:{candidate.target_id}:{type(error).__name__}")
                    frames.append(BrowserFrameState(
                        frame_id=candidate.frame_id,
                        parent_frame_id=candidate.parent_frame_id,
                        target_id=candidate.target_id,
                        cdp_session_id=candidate.cdp_session_id,
                        url=candidate.url,
                        security_origin=candidate.security_origin,
                        cross_origin=True,
                        oopif=True,
                        complete=False,
                        error=f"{type(error).__name__}: {error}",
                    ))
                    continue
                captures.append(capture)
                frames.extend(capture.frame_states())
                oopif_count += 1
                continue
            if not candidate.same_origin:
                # Cross-origin frames without a flattened session are explicit
                # omissions rather than false same-origin successes.
                omitted += 1
                warnings.append(f"cross_origin_frame_session_missing:{candidate.frame_id}")
                frames.append(BrowserFrameState(
                    frame_id=candidate.frame_id,
                    parent_frame_id=candidate.parent_frame_id,
                    target_id=candidate.target_id,
                    cdp_session_id=candidate.cdp_session_id,
                    url=candidate.url,
                    security_origin=candidate.security_origin,
                    cross_origin=True,
                    oopif=False,
                    complete=False,
                    error="cross_origin_frame_not_attached",
                ))
                continue
            try:
                result = cdp_runtime.send(
                    "Accessibility.getFullAXTree",
                    {"frameId": candidate.frame_id},
                    cdp_session_id=candidate.cdp_session_id,
                    timeout_seconds=self.request_timeout_seconds,
                )
                nodes = _sequence(result.get("nodes")) if isinstance(result, Mapping) else ()
                if not nodes:
                    raise BrowserDomRootMissing("same-origin frame AX tree returned no nodes")
                for node in nodes:
                    if isinstance(node, Mapping):
                        ax_nodes.append({**dict(node), "_zyra_frame_id": candidate.frame_id})
                same_origin_count += 1
                frames.append(BrowserFrameState(
                    frame_id=candidate.frame_id,
                    parent_frame_id=candidate.parent_frame_id,
                    target_id=candidate.target_id,
                    cdp_session_id=candidate.cdp_session_id,
                    url=candidate.url,
                    security_origin=candidate.security_origin,
                    cross_origin=False,
                    oopif=False,
                ))
            except Exception as error:
                omitted += 1
                self._child_failures += 1
                warnings.append(f"same_origin_frame_ax_failed:{candidate.frame_id}:{type(error).__name__}")
                frames.append(BrowserFrameState(
                    frame_id=candidate.frame_id,
                    parent_frame_id=candidate.parent_frame_id,
                    target_id=candidate.target_id,
                    cdp_session_id=candidate.cdp_session_id,
                    url=candidate.url,
                    security_origin=candidate.security_origin,
                    cross_origin=False,
                    oopif=False,
                    complete=False,
                    error=f"{type(error).__name__}: {error}",
                ))
        self._same_origin_ax += same_origin_count
        self._oopif_captures += oopif_count
        return BrowserFrameComposition(
            same_origin_ax={"nodes": ax_nodes},
            oopif_captures=tuple(captures),
            frames=tuple(frames),
            warnings=tuple(dict.fromkeys(warnings)),
            planned_count=len(candidates),
            captured_count=same_origin_count + oopif_count,
            omitted_count=omitted,
            same_origin_ax_count=same_origin_count,
            oopif_count=oopif_count,
        )

    def _capture_oopif(
        self,
        candidate: BrowserFrameCaptureCandidate,
        request: BrowserDomCaptureRequest,
        cdp: BrowserFrameCdpPort,
        target_runtime: BrowserFrameTargetPort,
    ) -> BrowserOopifCapture:
        started = time.perf_counter()
        self._assert_child_current(candidate, request, target_runtime, phase="before")
        dom = self._send_required(cdp, "DOM.getDocument", {"depth": -1, "pierce": True}, candidate)
        snapshot = self._send_required(cdp, "DOMSnapshot.captureSnapshot", {
            "computedStyles": list(self.computed_styles),
            "includePaintOrder": True,
            "includeDOMRects": True,
            "includeBlendedBackgroundColors": False,
            "includeTextColorOpacities": False,
        }, candidate)
        ax = self._send_required(cdp, "Accessibility.getFullAXTree", {}, candidate)
        frame_tree = self._send_optional(cdp, "Page.getFrameTree", {}, candidate)
        viewport_payload = self._send_optional(cdp, "Runtime.evaluate", {
            "expression": "JSON.stringify({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,scrollX,scrollY,documentWidth:document.documentElement.scrollWidth,documentHeight:document.documentElement.scrollHeight})",
            "returnByValue": True,
        }, candidate)
        self._assert_child_current(candidate, request, target_runtime, phase="after")
        issues = validate_snapshot_payload(snapshot)
        if issues:
            raise BrowserDomCaptureFailed(
                "OOPIF DOMSnapshot response failed validation",
                details={"target_id": candidate.target_id, "issues": list(issues)},
            )
        if not isinstance(dom.get("root"), Mapping):
            raise BrowserDomRootMissing("OOPIF DOM.getDocument returned no root")
        if not _sequence(ax.get("nodes")):
            raise BrowserDomRootMissing("OOPIF Accessibility tree returned no nodes")
        viewport = _viewport(viewport_payload, snapshot)
        decoded = decode_snapshot(snapshot, device_pixel_ratio=viewport.device_pixel_ratio)
        report: DomBuildReport = build_enhanced_dom(
            dom,
            ax,
            decoded,
            target_id=candidate.target_id,
            cdp_session_id=candidate.cdp_session_id,
            default_frame_id=_root_frame_id(frame_tree) or candidate.frame_id,
            sensitive_values=(),
        )
        serializer = DOMTreeSerializer(
            report.root,
            previous_cached_state=None,
            enable_bbox_filtering=True,
            paint_order_filtering=True,
            session_id=request.browser_session_id,
        )
        serialized, timing = serializer.serialize_accessible_elements()
        if serialized._root is None:
            raise BrowserDomRootMissing("OOPIF serializer returned no root state")
        warnings = list(report.warnings)
        warnings.extend(str(item) for item in timing.get("warnings", ()) if item)
        return BrowserOopifCapture(
            candidate=candidate,
            root=report.root,
            serialized_state=serialized,
            raw_dom=dom,
            raw_ax=ax,
            raw_snapshot=snapshot,
            frame_tree=frame_tree,
            viewport=viewport,
            dom_nodes=report.node_count,
            ax_nodes=len(_sequence(ax.get("nodes"))),
            snapshot_nodes=snapshot_node_count(snapshot),
            selector_candidates=len(serialized.selector_map),
            duration_ms=(time.perf_counter() - started) * 1000,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _assert_child_current(
        self,
        candidate: BrowserFrameCaptureCandidate,
        request: BrowserDomCaptureRequest,
        target_runtime: BrowserFrameTargetPort,
        *,
        phase: str,
    ) -> None:
        snapshot = target_runtime.snapshot()
        if int(getattr(snapshot, "generation", -1)) != request.target_generation:
            raise BrowserDomCaptureStale(
                f"target generation changed during OOPIF capture ({phase})",
                details={"target_id": candidate.target_id},
            )
        try:
            current = target_runtime.cdp_session(candidate.target_id)
        except Exception as error:
            raise BrowserDomCaptureStale(
                f"OOPIF target detached during capture ({phase})",
                details={"target_id": candidate.target_id},
            ) from error
        if str(getattr(current, "cdp_session_id", "") or "") != candidate.cdp_session_id:
            raise BrowserDomCaptureStale(
                f"OOPIF CDP session changed during capture ({phase})",
                details={
                    "target_id": candidate.target_id,
                    "expected_session_id": candidate.cdp_session_id,
                    "actual_session_id": str(getattr(current, "cdp_session_id", "") or ""),
                },
            )

    def _send_required(
        self,
        cdp: BrowserFrameCdpPort,
        method: str,
        params: Mapping[str, Any],
        candidate: BrowserFrameCaptureCandidate,
    ) -> dict[str, Any]:
        try:
            value = cdp.send(
                method,
                params,
                cdp_session_id=candidate.cdp_session_id,
                timeout_seconds=self.request_timeout_seconds,
            )
        except Exception as error:
            raise BrowserDomCaptureFailed(
                f"OOPIF required CDP method {method} failed",
                details={"target_id": candidate.target_id, "method": method, "error": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise BrowserDomCaptureFailed(f"OOPIF required CDP method {method} returned non-object")
        return dict(value)

    def _send_optional(
        self,
        cdp: BrowserFrameCdpPort,
        method: str,
        params: Mapping[str, Any],
        candidate: BrowserFrameCaptureCandidate,
    ) -> dict[str, Any]:
        try:
            return dict(cdp.send(
                method,
                params,
                cdp_session_id=candidate.cdp_session_id,
                timeout_seconds=self.request_timeout_seconds,
            ))
        except Exception as error:
            return {"_zyra_optional_error": f"{type(error).__name__}: {error}"}

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserFrameCaptureRuntime",
            "owner_unit": "M1-S04B-02",
            "target_session_owner": "BrowserTargetRuntime/M1-04A",
            "disabled": self.disabled,
            "max_iframes": self.max_iframes,
            "plans": self._plans,
            "same_origin_ax_captures": self._same_origin_ax,
            "oopif_captures": self._oopif_captures,
            "child_failures": self._child_failures,
        }


def merge_frame_roots(root: EnhancedDOMTreeNode, composition: BrowserFrameComposition) -> EnhancedDOMTreeNode:
    """Attach OOPIF roots to the page tree while retaining per-node identity."""

    for capture in composition.oopif_captures:
        child = capture.root
        child.parent_node = root
        if root.children_nodes is None:
            root.children_nodes = []
        root.children_nodes.append(child)
    return root


def merge_oopif_serialized_states(serialized: Any, composition: BrowserFrameComposition) -> Any:
    """Merge independently serialized OOPIF states into the canonical page view.

    Chromium exposes an out-of-process iframe through a distinct CDP target.  Its
    document root can be attached to the enhanced DOM tree, but browser-use's
    serializer intentionally treats iframe document boundaries as terminal when
    it walks the root target.  Reusing each child target's already filtered
    ``SerializedDOMState`` keeps those interactive elements visible without
    pretending that they belong to the root CDP session.

    ``DOMSelectorMap`` historically keys by an integer local index/backend id.
    Backend ids are normally process-wide, but a defensive synthetic key is
    allocated on collision; selector authority is carried by the node's own
    target/session/backend tuple, not by this compatibility key.
    """

    selector_map = serialized.selector_map
    used = {int(key) for key in selector_map}
    next_key = max(used, default=0) + 1
    root = getattr(serialized, "_root", None)
    for capture in composition.oopif_captures:
        child_state = capture.serialized_state
        child_root = getattr(child_state, "_root", None)
        if root is not None and child_root is not None:
            root.children.append(child_root)
        for source_key, node in sorted(
            child_state.selector_map.items(),
            key=lambda item: (int(item[0]), int(item[1].backend_node_id)),
        ):
            proposed = int(source_key)
            if proposed in used:
                while next_key in used:
                    next_key += 1
                proposed = next_key
                next_key += 1
            selector_map[proposed] = node
            used.add(proposed)
    return serialized


def merged_raw_payloads(
    *,
    root_dom: Mapping[str, Any],
    root_ax: Mapping[str, Any],
    root_snapshot: Mapping[str, Any],
    composition: BrowserFrameComposition,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    same_origin_nodes = list(_sequence(composition.same_origin_ax.get("nodes")))
    root_nodes = list(_sequence(root_ax.get("nodes")))
    dom = {
        "root_target": dict(root_dom),
        "oopif_targets": {
            capture.candidate.target_id: dict(capture.raw_dom)
            for capture in composition.oopif_captures
        },
    }
    ax = {
        "nodes": [*root_nodes, *same_origin_nodes],
        "root_target": dict(root_ax),
        "oopif_targets": {
            capture.candidate.target_id: dict(capture.raw_ax)
            for capture in composition.oopif_captures
        },
    }
    snapshot = {
        "root_target": dict(root_snapshot),
        "oopif_targets": {
            capture.candidate.target_id: dict(capture.raw_snapshot)
            for capture in composition.oopif_captures
        },
    }
    return dom, ax, snapshot


def _flatten_frame_tree(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = []

    def visit(tree: Any) -> None:
        if not isinstance(tree, Mapping):
            return
        frame = tree.get("frame")
        if isinstance(frame, Mapping):
            result.append(dict(frame))
        for child in _sequence(tree.get("childFrames")):
            visit(child)

    visit(payload.get("frameTree"))
    return tuple(result)


def _frame_states(
    payload: Mapping[str, Any],
    *,
    target_id: str,
    cdp_session_id: str,
    oopif: bool,
    default_frame_id: str,
    default_url: str,
    default_parent_frame_id: str,
) -> tuple[BrowserFrameState, ...]:
    frames = _flatten_frame_tree(payload)
    if not frames:
        return (BrowserFrameState(
            frame_id=default_frame_id,
            parent_frame_id=default_parent_frame_id,
            target_id=target_id,
            cdp_session_id=cdp_session_id,
            url=default_url,
            cross_origin=oopif,
            oopif=oopif,
            complete=not bool(payload.get("_zyra_optional_error")),
            error=str(payload.get("_zyra_optional_error") or ""),
        ),)
    root_origin = str(frames[0].get("securityOrigin") or "")
    return tuple(BrowserFrameState(
        frame_id=str(frame.get("id") or default_frame_id),
        parent_frame_id=str(frame.get("parentId") or default_parent_frame_id),
        target_id=target_id,
        cdp_session_id=cdp_session_id,
        url=str(frame.get("url") or default_url),
        name=str(frame.get("name") or ""),
        loader_id=str(frame.get("loaderId") or ""),
        security_origin=str(frame.get("securityOrigin") or ""),
        cross_origin=oopif or bool(
            root_origin
            and frame.get("securityOrigin")
            and str(frame.get("securityOrigin")) != root_origin
        ),
        oopif=oopif,
    ) for frame in frames)


def _viewport(runtime_payload: Mapping[str, Any], snapshot: Mapping[str, Any]) -> BrowserViewportState:
    value = runtime_payload.get("result")
    if isinstance(value, Mapping):
        value = value.get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    data = value if isinstance(value, Mapping) else {}
    first_document = next((item for item in _sequence(snapshot.get("documents")) if isinstance(item, Mapping)), {})
    content = first_document.get("contentSize") if isinstance(first_document, Mapping) else {}
    content = content if isinstance(content, Mapping) else {}
    return BrowserViewportState(
        width=max(0.0, finite_number(data.get("width"), default=1280.0)),
        height=max(0.0, finite_number(data.get("height"), default=720.0)),
        device_pixel_ratio=max(0.1, finite_number(data.get("dpr"), default=1.0)),
        scroll_x=finite_number(data.get("scrollX")),
        scroll_y=finite_number(data.get("scrollY")),
        document_width=max(0.0, finite_number(data.get("documentWidth") or content.get("width"))),
        document_height=max(0.0, finite_number(data.get("documentHeight") or content.get("height"))),
    )


def _root_frame_id(payload: Mapping[str, Any]) -> str:
    tree = payload.get("frameTree")
    if isinstance(tree, Mapping) and isinstance(tree.get("frame"), Mapping):
        return str(tree["frame"].get("id") or "")
    return ""


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()
