from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .contracts import (
    BrowserCaptureMetrics,
    BrowserDomCapture,
    BrowserDomCaptureRequest,
    BrowserFrameState,
    BrowserViewportState,
    CaptureCompleteness,
)
from .capture_policy import BrowserDomCapturePolicy
from .dom_builder import DomBuildReport, build_enhanced_dom
from .errors import (
    BrowserDomCaptureDisabled,
    BrowserDomCaptureFailed,
    BrowserDomCaptureStale,
    BrowserDomRootMissing,
)
from .serializer import DOMTreeSerializer
from .snapshot_decoder import decode_snapshot, snapshot_node_count, validate_snapshot_payload
from .text import estimate_tokens, finite_number


class BrowserCdpRequestPort(Protocol):
    def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        cdp_session_id: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...

    def snapshot(self) -> Any: ...


class BrowserTargetStatePort(Protocol):
    @property
    def active_target_id(self) -> str: ...
    def ensure_valid_focus(self, *, timeout: float = 3.0) -> Any: ...
    def active_cdp_session(self, *, timeout: float = 2.0) -> Any: ...
    def snapshot(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class BrowserDomRuntimeSnapshot:
    disabled: bool
    captures: int
    failures: int
    stale_rejections: int
    degraded_captures: int
    last_capture_id: str
    last_error: str
    last_duration_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": "BrowserDomStateRuntime",
            "owner_unit": "M1-S04B-01",
            "disabled": self.disabled,
            "captures": self.captures,
            "failures": self.failures,
            "stale_rejections": self.stale_rejections,
            "degraded_captures": self.degraded_captures,
            "last_capture_id": self.last_capture_id,
            "last_error": self.last_error,
            "last_duration_ms": self.last_duration_ms,
        }


class BrowserDomStateRuntime:
    """Capture and compose the authoritative browser-specific DOM state.

    Root DOMSnapshot, DOM and AX responses are mandatory. Child frame failure
    may degrade fidelity but is never rewritten as an empty successful state.
    """

    def __init__(
        self,
        *,
        request_timeout_seconds: float = 10.0,
        max_capture_retries: int = 1,
        disabled: bool = False,
        max_iframes: int = 32,
        computed_styles: Sequence[str] = (
            "display", "visibility", "opacity", "overflow", "overflow-x", "overflow-y",
            "cursor", "pointer-events", "position", "background-color",
        ),
        capture_policy: BrowserDomCapturePolicy | None = None,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("DOM capture timeout must be positive")
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.max_capture_retries = max(0, int(max_capture_retries))
        self.disabled = disabled
        self.max_iframes = max(0, int(max_iframes))
        self.computed_styles = tuple(dict.fromkeys(str(item) for item in computed_styles if str(item)))
        self.capture_policy = capture_policy or BrowserDomCapturePolicy(disabled=disabled)
        self._lock = threading.RLock()
        self._captures = 0
        self._failures = 0
        self._stale_rejections = 0
        self._degraded = 0
        self._last_capture_id = ""
        self._last_error = ""
        self._last_duration_ms = 0.0

    def capture_from_browser_runtime(
        self,
        browser_runtime: Any,
        *,
        run_id: str,
        task_id: str,
        canonical_session_id: str,
        browser_session_id: str,
        session_revision: int,
        worker_request_id: str,
        node_id: str = "",
        step_index: int = 0,
        constraints: Mapping[str, Any] | None = None,
    ) -> BrowserDomCapture:
        if self.disabled:
            raise BrowserDomCaptureDisabled("browser DOM state runtime is disabled")
        try:
            session_runtime = browser_runtime.session_runtime_component()
            target_runtime = browser_runtime.target_runtime(browser_session_id)
            cdp_runtime = browser_runtime.cdp_runtime(browser_session_id)
        except AttributeError as error:
            raise BrowserDomCaptureFailed(
                "04A browser runtime does not expose its live target/CDP components",
                details={"browser_session_id": browser_session_id},
            ) from error
        target = target_runtime.ensure_valid_focus(timeout=browser_runtime.config.connect_timeout_seconds)
        cdp_session = target_runtime.active_cdp_session(timeout=browser_runtime.config.connect_timeout_seconds)
        target_snapshot = target_runtime.snapshot()
        cdp_snapshot = cdp_runtime.snapshot()
        request = BrowserDomCaptureRequest(
            run_id=run_id,
            task_id=task_id,
            canonical_session_id=canonical_session_id,
            browser_session_id=browser_session_id,
            worker_request_id=worker_request_id,
            target_id=target.target_id,
            cdp_session_id=cdp_session.cdp_session_id,
            session_revision=session_revision,
            target_generation=int(target_snapshot.generation),
            cdp_generation=int(cdp_snapshot.generation),
            node_id=node_id,
            step_index=step_index,
            constraints=dict(constraints or {}),
        )
        return self.capture(
            request,
            cdp_runtime=cdp_runtime,
            target_runtime=target_runtime,
            session_runtime=session_runtime,
        )

    def capture(
        self,
        request: BrowserDomCaptureRequest,
        *,
        cdp_runtime: BrowserCdpRequestPort,
        target_runtime: BrowserTargetStatePort,
        session_runtime: Any | None = None,
    ) -> BrowserDomCapture:
        if self.disabled:
            raise BrowserDomCaptureDisabled("browser DOM state runtime is disabled")
        self.capture_policy.require_allowed(self.capture_policy.admit_request(request))
        started = time.perf_counter()
        last_error: BaseException | None = None
        for attempt in range(self.max_capture_retries + 1):
            try:
                capture = self._capture_once(
                    request,
                    cdp_runtime=cdp_runtime,
                    target_runtime=target_runtime,
                    session_runtime=session_runtime,
                )
                duration_ms = (time.perf_counter() - started) * 1000
                capture.metrics = BrowserCaptureMetrics(
                    **{**capture.metrics.to_dict(), "capture_duration_ms": duration_ms}
                )
                self._record_success(capture, duration_ms)
                return capture
            except BrowserDomCaptureStale as error:
                last_error = error
                self._stale_rejections += 1
                if attempt >= self.max_capture_retries:
                    break
                refreshed = self._refresh_request(request, target_runtime, cdp_runtime)
                request = refreshed
            except BaseException as error:  # noqa: BLE001 - typed boundary records source failure.
                last_error = error
                break
        duration_ms = (time.perf_counter() - started) * 1000
        self._record_failure(last_error, duration_ms)
        if isinstance(last_error, BrowserDomCaptureFailed):
            raise last_error
        raise BrowserDomCaptureFailed(
            "browser DOM capture failed",
            details={
                "capture_id": request.capture_id,
                "error": f"{type(last_error).__name__}: {last_error}" if last_error else "unknown",
            },
        ) from last_error

    def _capture_once(
        self,
        request: BrowserDomCaptureRequest,
        *,
        cdp_runtime: BrowserCdpRequestPort,
        target_runtime: BrowserTargetStatePort,
        session_runtime: Any | None,
    ) -> BrowserDomCapture:
        self._assert_generation(request, target_runtime, cdp_runtime, phase="before_capture")
        cdp_session_id = request.cdp_session_id

        dom_started = time.perf_counter()
        dom = self._send_required(
            cdp_runtime,
            "DOM.getDocument",
            {"depth": -1, "pierce": True},
            cdp_session_id=cdp_session_id,
        )
        dom_ms = _elapsed_ms(dom_started)

        snapshot_started = time.perf_counter()
        snapshot = self._send_required(
            cdp_runtime,
            "DOMSnapshot.captureSnapshot",
            {
                "computedStyles": list(self.computed_styles),
                "includePaintOrder": True,
                "includeDOMRects": True,
                "includeBlendedBackgroundColors": False,
                "includeTextColorOpacities": False,
            },
            cdp_session_id=cdp_session_id,
        )
        snapshot_ms = _elapsed_ms(snapshot_started)

        ax_started = time.perf_counter()
        ax = self._send_required(
            cdp_runtime,
            "Accessibility.getFullAXTree",
            {},
            cdp_session_id=cdp_session_id,
        )
        ax_ms = _elapsed_ms(ax_started)

        frame_tree = self._send_optional(
            cdp_runtime,
            "Page.getFrameTree",
            {},
            cdp_session_id=cdp_session_id,
        )
        viewport_payload = self._send_optional(
            cdp_runtime,
            "Runtime.evaluate",
            {
                "expression": "JSON.stringify({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,scrollX,scrollY,documentWidth:document.documentElement.scrollWidth,documentHeight:document.documentElement.scrollHeight})",
                "returnByValue": True,
            },
            cdp_session_id=cdp_session_id,
        )
        loader_id = _loader_id(frame_tree)
        if loader_id and not request.document_loader_id:
            request = replace(request, document_loader_id=loader_id)

        policy_decision = self.capture_policy.inspect_responses(
            request,
            dom=dom,
            ax=ax,
            snapshot=snapshot,
            frame_tree=frame_tree,
        )
        self.capture_policy.require_allowed(policy_decision)

        self._assert_generation(request, target_runtime, cdp_runtime, phase="after_capture")
        issues = list(validate_snapshot_payload(snapshot))
        if issues:
            raise BrowserDomCaptureFailed(
                "DOMSnapshot response failed validation",
                details={"issues": issues, "capture_id": request.capture_id},
            )
        if not isinstance(dom.get("root"), Mapping):
            raise BrowserDomRootMissing("DOM.getDocument returned no root")
        if not _sequence(ax.get("nodes")):
            raise BrowserDomRootMissing("Accessibility.getFullAXTree returned no root nodes")

        viewport = _viewport(viewport_payload, snapshot)
        decoded = decode_snapshot(snapshot, device_pixel_ratio=viewport.device_pixel_ratio)
        compose_started = time.perf_counter()
        build_report = build_enhanced_dom(
            dom,
            ax,
            decoded,
            target_id=request.target_id,
            cdp_session_id=request.cdp_session_id,
            default_frame_id=_root_frame_id(frame_tree),
            sensitive_values=_sensitive_values(request.constraints),
        )
        serializer = DOMTreeSerializer(
            build_report.root,
            previous_cached_state=None,
            enable_bbox_filtering=True,
            paint_order_filtering=True,
            session_id=request.browser_session_id,
        )
        serialized, serializer_timing = serializer.serialize_accessible_elements()
        compose_ms = _elapsed_ms(compose_started)
        if serialized._root is None:
            raise BrowserDomRootMissing("DOM serializer returned no root state")
        frames = _frames(frame_tree, request)
        warnings = list(build_report.warnings)
        warnings.extend(policy_decision.warnings)
        warnings.extend(str(item) for item in serializer_timing.get("warnings", ()) if item)
        completeness = CaptureCompleteness.DEGRADED if warnings else CaptureCompleteness.COMPLETE
        raw_payload = {"dom": dom, "ax": ax, "snapshot": snapshot, "frames": frame_tree}
        encoded = json.dumps(raw_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        metrics = BrowserCaptureMetrics(
            full_state_bytes=len(encoded),
            full_state_tokens=estimate_tokens(encoded),
            dom_nodes=build_report.node_count,
            ax_nodes=len(_sequence(ax.get("nodes"))),
            snapshot_nodes=snapshot_node_count(snapshot),
            selector_candidates=len(serialized.selector_map),
            frames=len(frames),
            omitted_frames=sum(not frame.complete for frame in frames),
            capture_duration_ms=0.0,
            dom_duration_ms=dom_ms,
            ax_duration_ms=ax_ms,
            snapshot_duration_ms=snapshot_ms,
            compose_duration_ms=compose_ms,
            listener_detection_skipped=build_report.node_count > 10000,
        )
        return BrowserDomCapture(
            request=request,
            root=build_report.root,
            serialized_state=serialized,
            raw_dom=dom,
            raw_ax=ax,
            raw_snapshot=snapshot,
            frames=frames,
            viewport=viewport,
            metrics=metrics,
            completeness=completeness,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _assert_generation(
        self,
        request: BrowserDomCaptureRequest,
        target_runtime: BrowserTargetStatePort,
        cdp_runtime: BrowserCdpRequestPort,
        *,
        phase: str,
    ) -> None:
        target = target_runtime.snapshot()
        cdp = cdp_runtime.snapshot()
        actual = {
            "target_id": str(target_runtime.active_target_id),
            "target_generation": int(getattr(target, "generation", -1)),
            "cdp_generation": int(getattr(cdp, "generation", -1)),
        }
        expected = {
            "target_id": request.target_id,
            "target_generation": request.target_generation,
            "cdp_generation": request.cdp_generation,
        }
        if actual != expected:
            raise BrowserDomCaptureStale(
                f"browser target/CDP generation changed {phase}",
                details={"phase": phase, "expected": expected, "actual": actual},
            )

    def _refresh_request(
        self,
        request: BrowserDomCaptureRequest,
        target_runtime: BrowserTargetStatePort,
        cdp_runtime: BrowserCdpRequestPort,
    ) -> BrowserDomCaptureRequest:
        target = target_runtime.ensure_valid_focus(timeout=self.request_timeout_seconds)
        cdp_session = target_runtime.active_cdp_session(timeout=self.request_timeout_seconds)
        target_snapshot = target_runtime.snapshot()
        cdp_snapshot = cdp_runtime.snapshot()
        return BrowserDomCaptureRequest(
            run_id=request.run_id,
            task_id=request.task_id,
            canonical_session_id=request.canonical_session_id,
            browser_session_id=request.browser_session_id,
            worker_request_id=request.worker_request_id,
            target_id=target.target_id,
            cdp_session_id=cdp_session.cdp_session_id,
            session_revision=request.session_revision,
            target_generation=int(target_snapshot.generation),
            cdp_generation=int(cdp_snapshot.generation),
            node_id=request.node_id,
            step_index=request.step_index,
            previous_capture_id=request.capture_id,
            constraints=request.constraints,
        )

    def _send_required(
        self,
        cdp: BrowserCdpRequestPort,
        method: str,
        params: Mapping[str, Any],
        *,
        cdp_session_id: str,
    ) -> dict[str, Any]:
        try:
            result = cdp.send(
                method,
                params,
                cdp_session_id=cdp_session_id,
                timeout_seconds=self.request_timeout_seconds,
            )
        except Exception as error:
            raise BrowserDomCaptureFailed(
                f"required CDP method {method} failed",
                details={"method": method, "error": f"{type(error).__name__}: {error}"},
            ) from error
        if not isinstance(result, Mapping):
            raise BrowserDomCaptureFailed(f"required CDP method {method} returned a non-object")
        return dict(result)

    def _send_optional(
        self,
        cdp: BrowserCdpRequestPort,
        method: str,
        params: Mapping[str, Any],
        *,
        cdp_session_id: str,
    ) -> dict[str, Any]:
        try:
            return dict(cdp.send(
                method,
                params,
                cdp_session_id=cdp_session_id,
                timeout_seconds=self.request_timeout_seconds,
            ))
        except Exception as error:
            return {"_zyra_optional_error": f"{type(error).__name__}: {error}"}

    def _record_success(self, capture: BrowserDomCapture, duration_ms: float) -> None:
        with self._lock:
            self._captures += 1
            self._degraded += capture.completeness == CaptureCompleteness.DEGRADED
            self._last_capture_id = capture.capture_id
            self._last_error = ""
            self._last_duration_ms = duration_ms

    def _record_failure(self, error: BaseException | None, duration_ms: float) -> None:
        with self._lock:
            self._failures += 1
            self._last_error = f"{type(error).__name__}: {error}" if error else "unknown"
            self._last_duration_ms = duration_ms

    def snapshot(self) -> BrowserDomRuntimeSnapshot:
        with self._lock:
            return BrowserDomRuntimeSnapshot(
                disabled=self.disabled,
                captures=self._captures,
                failures=self._failures,
                stale_rejections=self._stale_rejections,
                degraded_captures=self._degraded,
                last_capture_id=self._last_capture_id,
                last_error=self._last_error,
                last_duration_ms=self._last_duration_ms,
            )


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


def _frames(payload: Mapping[str, Any], request: BrowserDomCaptureRequest) -> tuple[BrowserFrameState, ...]:
    root = payload.get("frameTree")
    result: list[BrowserFrameState] = []

    def visit(tree: Any, parent_id: str = "") -> None:
        if not isinstance(tree, Mapping):
            return
        frame = tree.get("frame")
        if isinstance(frame, Mapping):
            frame_id = str(frame.get("id") or "")
            result.append(BrowserFrameState(
                frame_id=frame_id,
                parent_frame_id=str(frame.get("parentId") or parent_id),
                target_id=request.target_id,
                cdp_session_id=request.cdp_session_id,
                url=str(frame.get("url") or ""),
                name=str(frame.get("name") or ""),
                loader_id=str(frame.get("loaderId") or ""),
                security_origin=str(frame.get("securityOrigin") or ""),
                cross_origin=bool(parent_id and frame.get("securityOrigin")),
                oopif=False,
            ))
            parent_id = frame_id
        for child in _sequence(tree.get("childFrames")):
            visit(child, parent_id)

    visit(root)
    if not result:
        result.append(BrowserFrameState(
            frame_id="",
            target_id=request.target_id,
            cdp_session_id=request.cdp_session_id,
            complete=False,
            error=str(payload.get("_zyra_optional_error") or "frame_tree_unavailable"),
        ))
    return tuple(result)


def _loader_id(payload: Mapping[str, Any]) -> str:
    tree = payload.get("frameTree")
    if isinstance(tree, Mapping) and isinstance(tree.get("frame"), Mapping):
        return str(tree["frame"].get("loaderId") or "")
    return ""


def _root_frame_id(payload: Mapping[str, Any]) -> str:
    tree = payload.get("frameTree")
    if isinstance(tree, Mapping) and isinstance(tree.get("frame"), Mapping):
        return str(tree["frame"].get("id") or "")
    return ""


def _sensitive_values(constraints: Mapping[str, Any]) -> tuple[str, ...]:
    values = constraints.get("sensitive_values") or constraints.get("sensitive_data") or ()
    if isinstance(values, Mapping):
        values = values.values()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        values = (values,) if values else ()
    return tuple(str(item) for item in values if str(item))


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
