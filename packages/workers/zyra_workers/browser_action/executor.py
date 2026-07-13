from __future__ import annotations

import base64
import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from zyra_core import ArtifactKind

from ..browser_session.screenshot_capture import HighlightFreeScreenshotCapture
from .file_policy import BrowserFilePolicy, FileReceipt
from .event_port import ArtifactPort, causal_metadata
from .form_policy import FormReceipt
from .clipboard_guard import BrowserClipboardGuard, ClipboardReceipt
from .geometry_guard import GeometryReceipt, Point
from .keyboard_codec import parse_key_chord
from .models import (
    ActionExecutionResult,
    ActionFailureKind,
    ActionPreflightReceipt,
    ActionRequest,
    digest_value,
)
from .permission_bridge import PermissionBridgeConsumption
from .secret_policy import BrowserSecretPolicy, SecretLease, SecretReceipt, SecretRedactor
from .selector_guard import SelectorReceipt


class BrowserExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class CdpTransport(Protocol):
    def send(self, method: str, params: Mapping[str, Any], *, cdp_session_id: str = "") -> Mapping[str, Any]: ...


class NativeDownloadRuntime(Protocol):
    def download(
        self,
        context: "ExecutionContext",
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, ...]]: ...

    def collect(
        self,
        context: "ExecutionContext",
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, ...]]: ...


class NetworkDispatchScope(Protocol):
    @property
    def effect_count(self) -> int: ...

    def __enter__(self) -> "NetworkDispatchScope": ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool: ...


class NetworkDispatchScopeFactory(Protocol):
    def __call__(self, context: "ExecutionContext") -> NetworkDispatchScope: ...


@dataclass(frozen=True, slots=True)
class CdpCommand:
    method: str
    params: Mapping[str, Any]
    cdp_session_id: str
    mutating: bool
    sequence: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", dict(self.params))

    def public_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "params": dict(self.params),
            "cdp_session_id": self.cdp_session_id,
            "mutating": self.mutating,
            "sequence": self.sequence,
        }


@dataclass(slots=True)
class RecordingCdpTransport:
    responses: Mapping[str, Sequence[Mapping[str, Any]] | Mapping[str, Any]] = field(default_factory=dict)
    commands: list[CdpCommand] = field(default_factory=list)
    _response_indices: dict[str, int] = field(default_factory=dict)

    def send(self, method: str, params: Mapping[str, Any], *, cdp_session_id: str = "") -> Mapping[str, Any]:
        self.commands.append(
            CdpCommand(
                method=method,
                params=dict(params),
                cdp_session_id=cdp_session_id,
                mutating=is_mutating_cdp(method, params),
                sequence=len(self.commands) + 1,
            )
        )
        configured = self.responses.get(method, {})
        if isinstance(configured, Sequence) and not isinstance(configured, Mapping | str | bytes | bytearray):
            index = self._response_indices.get(method, 0)
            self._response_indices[method] = index + 1
            if not configured:
                return {}
            return dict(configured[min(index, len(configured) - 1)])
        return dict(configured)  # type: ignore[arg-type]

    @property
    def mutating_commands(self) -> tuple[CdpCommand, ...]:
        return tuple(command for command in self.commands if command.mutating)

    def count(self, method: str) -> int:
        return sum(command.method == method for command in self.commands)


MUTATING_CDP_PREFIXES = (
    "Input.dispatch",
    "Input.insertText",
    "Input.synthesize",
    "DOM.setFileInputFiles",
    "DOM.scrollIntoViewIfNeeded",
    "Page.navigate",
    "Page.reload",
    "Page.printToPDF",
    "Page.setDownloadBehavior",
    "Browser.setDownloadBehavior",
    "Browser.grantPermissions",
    "Browser.resetPermissions",
    "Browser.setPermission",
    "Target.createTarget",
    "Target.closeTarget",
    "Target.activateTarget",
    "Runtime.callFunctionOn",
    "Runtime.evaluate",
    "Emulation.set",
)


def is_mutating_cdp(method: str, params: Mapping[str, Any] | None = None) -> bool:
    if method == "Runtime.callFunctionOn" and bool((params or {}).get("throwOnSideEffect")):
        return False
    return method.startswith(MUTATING_CDP_PREFIXES)


@dataclass(frozen=True, slots=True)
class ExecutionBindings:
    selector: SelectorReceipt | None = None
    geometry: GeometryReceipt | None = None
    network_receipt_id: str = ""
    network_receipt: Any = None
    file_receipt: FileReceipt | None = None
    secret_receipt: SecretReceipt | None = None
    clipboard_receipt: ClipboardReceipt | None = None
    form_receipt: FormReceipt | None = None


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    request: ActionRequest
    permission: PermissionBridgeConsumption
    bindings: ExecutionBindings

    @property
    def receipt(self) -> ActionPreflightReceipt:
        return self.permission.preflight


class BrowserSideEffectFence:
    """Only public dispatch entry for 04C browser side effects.

    The permission grant is already atomically consumed by the bridge.  This
    fence verifies every receipt binding again and then delegates to private
    typed primitives.  No Page/Element/Mouse object is exposed to callers.
    """

    def __init__(
        self,
        transport: CdpTransport,
        *,
        file_policy: BrowserFilePolicy | None = None,
        secret_policy: BrowserSecretPolicy | None = None,
        clipboard_guard: BrowserClipboardGuard | None = None,
        artifact_port: ArtifactPort | None = None,
        download_runtime: NativeDownloadRuntime | None = None,
        network_scope_factory: NetworkDispatchScopeFactory | None = None,
        require_network_scope: bool = False,
        disabled: bool = False,
    ) -> None:
        self.transport = transport
        self.file_policy = file_policy
        self.secret_policy = secret_policy
        self.clipboard_guard = clipboard_guard
        self.artifact_port = artifact_port
        self.download_runtime = download_runtime
        self.network_scope_factory = network_scope_factory
        self.require_network_scope = require_network_scope
        self.disabled = disabled
        self.execution_count = 0

    def dispatch(self, context: ExecutionContext) -> ActionExecutionResult:
        if self.disabled:
            raise BrowserExecutionError("executor_disabled", "browser side-effect fence is disabled")
        receipt = context.receipt
        if not context.permission.accepted or not receipt.authorizes_execution:
            raise BrowserExecutionError("grant_not_consumed", "browser action has no consumed exact grant")
        if receipt.action_id != context.request.identity.action_id:
            raise BrowserExecutionError("action_receipt_mismatch", "browser action receipt belongs to another action")
        if receipt.definition.name != context.request.action:
            raise BrowserExecutionError("action_definition_mismatch", "browser action definition changed before dispatch")
        self._validate_bindings(context)
        lease: SecretLease | None = None
        arguments: Mapping[str, Any] = receipt.execution_arguments
        try:
            if context.bindings.secret_receipt is not None:
                if self.secret_policy is None:
                    raise BrowserExecutionError("secret_policy_unavailable", "secret policy is required by the receipt")
                target_url = context.request.target_url or context.request.current_url
                lease = self.secret_policy.materialize(context.bindings.secret_receipt, target_url=target_url)
                arguments = lease.resolved_arguments
            before = command_effect_count(self.transport)
            self.execution_count += 1
            scope = (
                self.network_scope_factory(context)
                if context.bindings.network_receipt is not None and self.network_scope_factory is not None
                else None
            )
            if context.bindings.network_receipt is not None and scope is None and self.require_network_scope:
                raise BrowserExecutionError(
                    "network_interception_unavailable",
                    "network browser action requires live redirect interception",
                )
            if scope is None:
                output, summary, artifacts, network_effects, file_effects = self._dispatch_action(context, arguments)
            else:
                with scope:
                    output, summary, artifacts, network_effects, file_effects = self._dispatch_action(context, arguments)
                network_effects += max(0, int(scope.effect_count))
            after = command_effect_count(self.transport)
            cdp_effects = max(0, after - before)
            redactor = SecretRedactor(tuple(lease.values.values()) if lease else ())
            safe_output = redactor.redact(output)
            redactor.assert_clean(safe_output)
            return ActionExecutionResult(
                action_id=receipt.action_id,
                ok=True,
                summary=redactor.redact(summary),
                output=safe_output,
                artifact_ids=tuple(artifacts),
                side_effect_count=cdp_effects + network_effects + file_effects,
                cdp_effect_count=cdp_effects,
                network_effect_count=network_effects,
                file_effect_count=file_effects,
            )
        except BrowserExecutionError:
            raise
        except Exception as exc:
            redactor = SecretRedactor(tuple(lease.values.values()) if lease else ())
            raise BrowserExecutionError(
                "browser_action_execution_failed",
                redactor.redact(f"browser action execution failed: {type(exc).__name__}: {exc}"),
            ) from exc

    def _validate_bindings(self, context: ExecutionContext) -> None:
        receipt = context.receipt
        selector = context.bindings.selector
        geometry = context.bindings.geometry
        if receipt.definition.selector_required:
            if selector is None or receipt.selector_binding is None:
                raise BrowserExecutionError("selector_receipt_missing", "browser action requires a selector receipt")
            if selector.binding.identity_digest != receipt.selector_binding.identity_digest:
                raise BrowserExecutionError("selector_receipt_mismatch", "selector binding does not match permission receipt")
        if selector and geometry:
            if geometry.selector_binding_digest != selector.binding.identity_digest:
                raise BrowserExecutionError("geometry_receipt_mismatch", "geometry receipt belongs to another selector")
            if geometry.target_cdp_session_id != selector.binding.cdp_session_id:
                raise BrowserExecutionError("geometry_session_mismatch", "geometry receipt targets another CDP session")
        if receipt.file_receipt_id:
            if context.bindings.file_receipt is None or context.bindings.file_receipt.receipt_id != receipt.file_receipt_id:
                raise BrowserExecutionError("file_receipt_mismatch", "file receipt does not match permission receipt")
        if receipt.secret_receipt_id:
            if context.bindings.secret_receipt is None or context.bindings.secret_receipt.receipt_id != receipt.secret_receipt_id:
                raise BrowserExecutionError("secret_receipt_mismatch", "secret receipt does not match permission receipt")
        if receipt.clipboard_receipt_id:
            if context.bindings.clipboard_receipt is None or context.bindings.clipboard_receipt.receipt_id != receipt.clipboard_receipt_id:
                raise BrowserExecutionError("clipboard_receipt_mismatch", "clipboard receipt does not match permission receipt")
        if receipt.form_receipt_id:
            if context.bindings.form_receipt is None or context.bindings.form_receipt.receipt_id != receipt.form_receipt_id:
                raise BrowserExecutionError("form_receipt_mismatch", "form receipt does not match permission receipt")
        if receipt.network_receipt_id != context.bindings.network_receipt_id:
            raise BrowserExecutionError("network_receipt_mismatch", "network receipt does not match permission receipt")

    def _dispatch_action(
        self,
        context: ExecutionContext,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        action = context.receipt.definition.name
        handlers = {
            "open_url": self._navigate,
            "go_back": self._history_back,
            "reload_page": self._reload,
            "click_element": self._click,
            "input_text": self._input_text,
            "submit_form": self._submit,
            "upload_file": self._upload,
            "download_file": self._download,
            "collect_downloads": self._collect_downloads,
            "select_dropdown": self._select,
            "get_dropdown_options": self._dropdown_options,
            "check_element": self._check,
            "drag_element": self._drag,
            "hover_element": self._hover,
            "scroll_page": self._scroll_page,
            "scroll_to_text": self._scroll_to_text,
            "send_keys": self._send_keys,
            "take_screenshot": self._screenshot,
            "save_as_pdf": self._print_pdf,
            "evaluate_js": self._evaluate,
            "focus_target": self._focus_target,
            "close_target": self._close_target,
            "clipboard_read": self._clipboard_read,
            "clipboard_write": self._clipboard_write,
            "list_targets": self._list_targets,
            "wait": self._wait,
            "extract_text": self._extract_text,
            "snapshot_state": self._snapshot,
            "search_page": self._search,
            "hover": self._hover,
        }
        handler = handlers.get(action)
        if handler is None:
            raise BrowserExecutionError("action_not_implemented", f"browser action {action!r} has no typed executor")
        return handler(context, arguments)

    def _send(self, method: str, params: Mapping[str, Any], context: ExecutionContext, *, selector: bool = False) -> Mapping[str, Any]:
        session_id = ""
        if selector and context.bindings.selector:
            session_id = context.bindings.selector.binding.cdp_session_id
        elif context.receipt.selector_binding:
            session_id = context.receipt.selector_binding.cdp_session_id
        return self.transport.send(method, params, cdp_session_id=session_id)

    def _navigate(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        response = self._send("Page.navigate", {"url": str(arguments["url"])}, context)
        if response.get("errorText"):
            raise BrowserExecutionError("navigation_failed", str(response["errorText"]))
        return {"frame_id": response.get("frameId", ""), "loader_id": response.get("loaderId", "")}, "Navigation dispatched", (), 1, 0

    def _history_back(self, context: ExecutionContext, _arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        history = self._send("Page.getNavigationHistory", {}, context)
        current = int(history.get("currentIndex", 0))
        entries = list(history.get("entries", []))
        if current <= 0 or current > len(entries) - 1:
            raise BrowserExecutionError("history_empty", "browser target has no previous navigation entry")
        entry_id = entries[current - 1].get("id")
        self._send("Page.navigateToHistoryEntry", {"entryId": entry_id}, context)
        return {"entry_id": entry_id}, "Navigated to previous history entry", (), 1, 0

    def _reload(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        self._send("Page.reload", {"ignoreCache": bool(arguments.get("ignore_cache", False))}, context)
        return {}, "Page reload dispatched", (), 1, 0

    def _click(self, context: ExecutionContext, _arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        point = require_point(context)
        self._mouse_click(context, point)
        return {"x": point.x, "y": point.y}, "Element clicked through CDP input", (), 0, 0

    def _input_text(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        point = require_point(context)
        self._mouse_click(context, point)
        if bool(arguments.get("clear", True)):
            self._key_chord(context, "CTRL+A")
            self._key_chord(context, "BACKSPACE")
        text = str(arguments.get("text", ""))
        self._send("Input.insertText", {"text": text}, context, selector=True)
        return {"inserted_characters": len(text)}, "Text inserted into exact element", (), 0, 0

    def _submit(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        point = require_point(context)
        self._mouse_click(context, point)
        if bool(arguments.get("press_enter", True)):
            self._key_chord(context, "ENTER")
        return {}, "Form submit interaction dispatched", (), 1, 0

    def _upload(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        if self.file_policy is None or context.bindings.file_receipt is None or context.bindings.selector is None:
            raise BrowserExecutionError("upload_policy_missing", "upload requires file policy and selector receipts")
        receipt = context.bindings.file_receipt
        with self.file_policy.open_uploads(receipt) as opened:
            maximum = int(arguments.get("max_bytes") or 0)
            if maximum and sum(item.size for item in opened) > maximum:
                raise BrowserExecutionError(
                    "upload_requested_quota_exceeded",
                    "opened upload bytes exceed the action-specific limit",
                )
            expected_sha256 = str(arguments.get("expected_sha256") or "").casefold().removeprefix("sha256:")
            if expected_sha256:
                if len(opened) != 1 or opened[0].sha256.casefold() != expected_sha256:
                    raise BrowserExecutionError(
                        "upload_digest_mismatch",
                        "opened upload content differs from the approved expected digest",
                    )
            paths = [item.candidate.identity.real_path for item in opened]
            self._send(
                "DOM.setFileInputFiles",
                {"backendNodeId": context.bindings.selector.binding.backend_node_id, "files": paths},
                context,
                selector=True,
            )
            public_files = [
                {"name": item.candidate.basename, "size": item.size, "sha256": item.sha256}
                for item in opened
            ]
        return {"files": public_files}, "Files attached to exact approved input", (), 1, len(public_files)

    def _download(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        if self.download_runtime is None:
            raise BrowserExecutionError(
                "download_runtime_missing",
                "native download requires the grant-scoped download runtime",
            )
        output, artifact_ids = self.download_runtime.download(context, arguments)
        return output, "Browser download completed through quarantine", artifact_ids, 1, len(artifact_ids)

    def _collect_downloads(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        if self.download_runtime is None:
            raise BrowserExecutionError(
                "download_runtime_missing",
                "collect_downloads requires the session-owned download runtime",
            )
        output, artifact_ids = self.download_runtime.collect(context, arguments)
        return output, f"Projected {len(artifact_ids)} completed browser download(s)", artifact_ids, 0, len(artifact_ids)

    def _select(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        object_id = self._resolve_object(context)
        values = arguments.get("values") or [arguments.get("value")]
        response = self._send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": "function(values){const wanted=new Set(values.map(String));for(const o of this.options)o.selected=wanted.has(o.value)||wanted.has(o.text);this.dispatchEvent(new Event('input',{bubbles:true}));this.dispatchEvent(new Event('change',{bubbles:true}));return Array.from(this.selectedOptions).map(o=>({value:o.value,text:o.text}));}",
                "arguments": [{"value": list(values)}],
                "returnByValue": True,
            },
            context,
            selector=True,
        )
        selected = response.get("result", {}).get("value", [])
        return {"selected": selected}, "Dropdown selection changed", (), 1, 0

    def _dropdown_options(self, context: ExecutionContext, _arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        object_id = self._resolve_object(context)
        response = self._send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": "function(){return Array.from(this.options||[]).map((o,i)=>({index:i,value:o.value,text:o.text,selected:o.selected,disabled:o.disabled}));}",
                "returnByValue": True,
                "throwOnSideEffect": True,
            },
            context,
            selector=True,
        )
        options = response.get("result", {}).get("value", [])
        return {"options": options}, f"Read {len(options)} dropdown options", (), 0, 0

    def _check(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        object_id = self._resolve_object(context)
        desired = bool(arguments.get("checked", True))
        response = self._send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": "function(desired){if(Boolean(this.checked)!==desired)this.click();return Boolean(this.checked);}",
                "arguments": [{"value": desired}],
                "returnByValue": True,
            },
            context,
            selector=True,
        )
        actual = bool(response.get("result", {}).get("value"))
        if actual != desired:
            raise BrowserExecutionError("check_postcondition_failed", "check control did not reach requested state")
        return {"checked": actual}, "Check control updated", (), 0, 0

    def _drag(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        start = require_point(context)
        end = Point(float(arguments["target_x"]), float(arguments["target_y"]))
        self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": start.x, "y": start.y}, context, selector=True)
        self._send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": start.x, "y": start.y, "button": "left", "clickCount": 1}, context, selector=True)
        steps = max(2, min(20, int(arguments.get("steps", 8))))
        for index in range(1, steps + 1):
            fraction = index / steps
            self._send(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": start.x + (end.x - start.x) * fraction, "y": start.y + (end.y - start.y) * fraction, "button": "left"},
                context,
                selector=True,
            )
        self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": end.x, "y": end.y, "button": "left", "clickCount": 1}, context, selector=True)
        return {"start": start.to_dict(), "end": end.to_dict()}, "Element drag dispatched", (), 0, 0

    def _hover(self, context: ExecutionContext, _arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        point = require_point(context)
        self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point.x, "y": point.y}, context, selector=True)
        return point.to_dict(), "Pointer moved over element", (), 0, 0

    def _scroll_page(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        delta_x = float(arguments.get("delta_x", 0))
        delta_y = float(arguments.get("delta_y", 0))
        self._send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 1, "y": 1, "deltaX": delta_x, "deltaY": delta_y}, context)
        return {"delta_x": delta_x, "delta_y": delta_y}, "Page scrolled", (), 0, 0

    def _scroll_to_text(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        text = str(arguments["text"])
        occurrence = max(1, int(arguments.get("occurrence", 1)))
        query = text if bool(arguments.get("case_sensitive", False)) else text.casefold()
        response = self._send(
            "DOM.performSearch",
            {"query": query, "includeUserAgentShadowDOM": True},
            context,
        )
        search_id = str(response.get("searchId", ""))
        result_count = int(response.get("resultCount", 0))
        if not search_id or result_count < occurrence:
            if search_id:
                self._send("DOM.discardSearchResults", {"searchId": search_id}, context)
            raise BrowserExecutionError(
                "scroll_text_not_found",
                "requested text occurrence was not found in the current DOM",
                details={"occurrence": occurrence, "result_count": result_count},
            )
        try:
            selected = self._send(
                "DOM.getSearchResults",
                {"searchId": search_id, "fromIndex": occurrence - 1, "toIndex": occurrence},
                context,
            )
            node_ids = list(selected.get("nodeIds", []))
            if len(node_ids) != 1 or int(node_ids[0]) <= 0:
                raise BrowserExecutionError(
                    "scroll_text_node_missing",
                    "DOM search did not return one current node",
                )
            node_id = int(node_ids[0])
            self._send("DOM.scrollIntoViewIfNeeded", {"nodeId": node_id}, context)
        finally:
            self._send("DOM.discardSearchResults", {"searchId": search_id}, context)
        return {
            "query_digest": digest_value(text),
            "occurrence": occurrence,
            "result_count": result_count,
            "node_id": node_id,
        }, "Scrolled requested text occurrence into view", (), 0, 0

    def _send_keys(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        keys = arguments.get("keys")
        selected = keys if isinstance(keys, list) else [keys]
        for key in selected:
            self._key_chord(context, str(key))
        return {"key_count": len(selected)}, "Keyboard sequence dispatched", (), 0, 0

    def _screenshot(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        image_format = str(arguments.get("format", "png"))
        capture = HighlightFreeScreenshotCapture().capture(
            lambda method, params: self._send(method, params, context),
            image_format=image_format,
            capture_beyond_viewport=bool(
                arguments.get("capture_beyond_viewport", arguments.get("full_page", False))
            ),
            from_surface=bool(arguments.get("from_surface", True)),
            quality=(int(arguments.get("quality", 90)) if image_format in {"jpeg", "webp"} else None),
            cdp_session_id=context.bindings.selector.binding.cdp_session_id if context.bindings.selector else "",
        )
        data = capture.content
        if self.artifact_port is None:
            raise BrowserExecutionError(
                "screenshot_artifact_port_missing",
                "screenshot bytes require the canonical artifact port",
            )
        title = str(arguments.get("file_name") or f"browser-action-{context.request.identity.action_id}.{image_format}")
        artifact = self.artifact_port.write(
            kind=ArtifactKind.SCREENSHOT,
            title=title,
            content=data,
            metadata={
                **causal_metadata(context.request.identity, context.receipt),
                "highlight_removed": capture.highlight_removed,
                "highlight_restored": capture.highlight_restored,
                "screenshot_capture_id": capture.capture_id,
                "screenshot_sha256": capture.sha256,
            },
        )
        return {
            "artifact_id": artifact.artifact_id,
            "bytes": len(data),
            "sha256": capture.sha256.removeprefix("sha256:"),
            "format": image_format,
            "highlight_removed": capture.highlight_removed,
            "highlight_restored": capture.highlight_restored,
            "capture_id": capture.capture_id,
        }, "Screenshot captured to owned artifact", (artifact.artifact_id,), 0, 1

    def _print_pdf(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        response = self._send(
            "Page.printToPDF",
            {
                "printBackground": bool(arguments.get("print_background", True)),
                "landscape": bool(arguments.get("landscape", False)),
                "scale": float(arguments.get("scale", 1.0)),
                "preferCSSPageSize": True,
                "displayHeaderFooter": bool(arguments.get("display_header_footer", False)),
            },
            context,
        )
        data = base64.b64decode(str(response.get("data", "")), validate=True)
        if self.artifact_port is not None:
            title = str(arguments.get("file_name") or f"browser-action-{context.request.identity.action_id}.pdf")
            artifact = self.artifact_port.write(
                kind=ArtifactKind.FILE,
                title=title,
                content=data,
                metadata=causal_metadata(context.request.identity, context.receipt),
            )
            return {
                "artifact_id": artifact.artifact_id,
                "size": len(data),
                "sha256": digest_value(data.hex()).removeprefix("sha256:"),
            }, "PDF rendered to owned artifact", (artifact.artifact_id,), 0, 1
        if self.file_policy is None or context.bindings.file_receipt is None:
            raise BrowserExecutionError("pdf_artifact_port_missing", "PDF bytes require the canonical artifact port")
        with self.file_policy.open_download(context.bindings.file_receipt) as writer:
            writer.write(data)
            completed = writer.complete()
        return {"artifact": completed.to_dict()}, "PDF rendered to owned artifact", (completed.receipt_id,), 0, 1

    def _evaluate(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        response = self._send(
            "Runtime.evaluate",
            {"expression": str(arguments["code"]), "awaitPromise": bool(arguments.get("await_promise", True)), "returnByValue": bool(arguments.get("return_by_value", True)), "userGesture": False},
            context,
        )
        if response.get("exceptionDetails"):
            raise BrowserExecutionError("javascript_exception", "approved JavaScript evaluation raised an exception")
        return {"value": response.get("result", {}).get("value")}, "Approved JavaScript evaluated", (), 0, 0

    def _focus_target(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        target_id = str(arguments["target_id"])
        self._send("Target.activateTarget", {"targetId": target_id}, context)
        return {"target_id": target_id}, "Browser target focused", (), 0, 0

    def _close_target(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        target_id = str(arguments["target_id"])
        response = self._send("Target.closeTarget", {"targetId": target_id}, context)
        return {"target_id": target_id, "success": bool(response.get("success", True))}, "Browser target closed", (), 0, 0

    def _clipboard_read(self, context: ExecutionContext, _arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        if self.clipboard_guard is None or context.bindings.clipboard_receipt is None:
            raise BrowserExecutionError("clipboard_guard_missing", "clipboard read requires an origin-scoped guard")
        value = self.clipboard_guard.read(context.bindings.clipboard_receipt, target_url=context.request.target_url or context.request.current_url)
        return {"text": value, "characters": len(value)}, "Clipboard text read through one-use origin lease", (), 0, 0

    def _clipboard_write(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        if self.clipboard_guard is None or context.bindings.clipboard_receipt is None:
            raise BrowserExecutionError("clipboard_guard_missing", "clipboard write requires an origin-scoped guard")
        value = str(arguments.get("text", ""))
        self.clipboard_guard.write(context.bindings.clipboard_receipt, value, target_url=context.request.target_url or context.request.current_url)
        return {"characters": len(value)}, "Clipboard text written through one-use origin lease", (), 0, 0

    def _list_targets(self, context: ExecutionContext, _arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        response = self._send("Target.getTargets", {}, context)
        targets = [sanitize_target(item) for item in response.get("targetInfos", [])]
        return {"targets": targets}, f"Read {len(targets)} browser targets", (), 0, 0

    def _wait(self, _context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        seconds = max(0.0, min(10.0, float(arguments.get("seconds", 0))))
        time.sleep(seconds)
        return {"seconds": seconds}, "Browser wait completed", (), 0, 0

    def _extract_text(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        response = self._send("DOMSnapshot.captureSnapshot", {"computedStyles": [], "includeDOMRects": False, "includePaintOrder": False}, context)
        strings = response.get("strings", [])
        text = "\n".join(str(item) for item in strings if isinstance(item, str))
        limit = int(arguments.get("max_chars", 20_000))
        return {"text": text[:limit], "truncated": len(text) > limit}, "Page text extracted", (), 0, 0

    def _snapshot(self, context: ExecutionContext, _arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        response = self._send("Page.getFrameTree", {}, context)
        return {"frame_tree": response.get("frameTree", {})}, "Browser frame state captured", (), 0, 0

    def _search(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str, tuple[str, ...], int, int]:
        query = str(arguments["query"])
        response = self._send("DOM.performSearch", {"query": query, "includeUserAgentShadowDOM": True}, context)
        search_id = str(response.get("searchId", ""))
        count = int(response.get("resultCount", 0))
        if search_id:
            self._send("DOM.discardSearchResults", {"searchId": search_id}, context)
        return {"query": query, "result_count": count}, f"Found {count} matching DOM nodes", (), 0, 0

    def _mouse_click(self, context: ExecutionContext, point: Point) -> None:
        self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point.x, "y": point.y}, context, selector=True)
        self._send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point.x, "y": point.y, "button": "left", "clickCount": 1}, context, selector=True)
        self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point.x, "y": point.y, "button": "left", "clickCount": 1}, context, selector=True)

    def _key_chord(self, context: ExecutionContext, chord: str) -> None:
        for params in parse_key_chord(chord).event_sequence():
            self._send(
                "Input.dispatchKeyEvent",
                params,
                context,
                selector=bool(context.bindings.selector),
            )

    def _resolve_object(self, context: ExecutionContext) -> str:
        selector = context.bindings.selector
        if selector is None:
            raise BrowserExecutionError("selector_receipt_missing", "element object resolution requires selector")
        response = self._send("DOM.resolveNode", {"backendNodeId": selector.binding.backend_node_id}, context, selector=True)
        object_id = str(response.get("object", {}).get("objectId", ""))
        if not object_id:
            raise BrowserExecutionError("element_resolution_failed", "browser backend node could not be resolved")
        return object_id


def require_point(context: ExecutionContext) -> Point:
    geometry = context.bindings.geometry
    if geometry is None:
        raise BrowserExecutionError("geometry_receipt_missing", "browser action requires fresh geometry receipt")
    return geometry.dispatch_point


def command_effect_count(transport: CdpTransport) -> int:
    commands = getattr(transport, "commands", None)
    if isinstance(commands, Sequence):
        return sum(bool(getattr(command, "mutating", False)) for command in commands)
    return 0


def decode_key_chord(value: str) -> tuple[str, int]:
    parts = [part.strip() for part in value.replace("-", "+").split("+") if part.strip()]
    if not parts:
        raise BrowserExecutionError("invalid_key_chord", "keyboard chord is empty")
    modifiers = 0
    key = parts[-1]
    aliases = {"CONTROL": "CTRL", "CMD": "META", "COMMAND": "META", "OPTION": "ALT", "RETURN": "ENTER", "ESC": "ESCAPE"}
    for modifier in parts[:-1]:
        selected = aliases.get(modifier.upper(), modifier.upper())
        value_by_name = {"ALT": 1, "CTRL": 2, "META": 4, "SHIFT": 8}
        if selected not in value_by_name:
            raise BrowserExecutionError("invalid_key_modifier", f"unsupported keyboard modifier {modifier!r}")
        modifiers |= value_by_name[selected]
    key = aliases.get(key.upper(), key)
    if len(key) > 1:
        key = key.title() if key.upper() not in {"ENTER", "BACKSPACE", "TAB", "ESCAPE", "DELETE", "ARROWDOWN", "ARROWUP", "ARROWLEFT", "ARROWRIGHT"} else key
    return key, modifiers


def key_code(key: str) -> str:
    normalized = key.upper()
    mapping = {
        "ENTER": "Enter",
        "BACKSPACE": "Backspace",
        "TAB": "Tab",
        "ESCAPE": "Escape",
        "DELETE": "Delete",
        "ARROWDOWN": "ArrowDown",
        "ARROWUP": "ArrowUp",
        "ARROWLEFT": "ArrowLeft",
        "ARROWRIGHT": "ArrowRight",
        "SPACE": "Space",
    }
    if normalized in mapping:
        return mapping[normalized]
    if len(key) == 1 and key.isalpha():
        return "Key" + key.upper()
    if len(key) == 1 and key.isdigit():
        return "Digit" + key
    return key


def sanitize_target(value: Any) -> dict[str, Any]:
    item = dict(value) if isinstance(value, Mapping) else {}
    return {
        "target_id": str(item.get("targetId", "")),
        "type": str(item.get("type", "")),
        "title": str(item.get("title", ""))[:500],
        "url": str(item.get("url", ""))[:4096],
        "attached": bool(item.get("attached", False)),
        "opener_id": str(item.get("openerId", "")),
    }
