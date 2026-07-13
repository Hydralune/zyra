from __future__ import annotations

import base64
import copy
import hashlib
import json
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zyra_core import ArtifactKind, ArtifactRef, now_iso
from zyra_runtime import LocalArtifactStore

from .clipboard_guard import ClipboardPort
from .deadline_runtime import ActionDeadline
from .download_guard import DownloadControlPort
from .event_port import ArtifactPort
from .executor import CdpCommand, is_mutating_cdp
from .integration_models import BrowserActionIntegrationError, DispatchBoundary, PlanPhase
from .models import digest_value, stable_id
from .network_policy import BrowserNetworkPolicy, NetworkReceipt
from .redirect_guard import (
    BrowserRedirectGuard,
    FetchInterceptionPort,
    InterceptionDecision,
    InterceptionDisposition,
    RedirectGuardError,
    intercepted_request_from_cdp,
)
from .secret_policy import SecretRedactor


_ALLOWED_CDP_PREFIXES = (
    "Browser.",
    "DOM.",
    "DOMDebugger.",
    "DOMSnapshot.",
    "Emulation.",
    "Fetch.",
    "Input.",
    "Network.",
    "Page.",
    "Runtime.",
    "Target.",
)

_FORBIDDEN_RAW_METHODS = frozenset(
    {
        "Browser.close",
        "Browser.crash",
        "Page.crash",
        "SystemInfo.getProcessInfo",
        "Tracing.start",
        "Tracing.end",
    }
)

_SENSITIVE_PARAM_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "cookies",
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "headers",
        "postdata",
    }
)


@dataclass(frozen=True, slots=True)
class SessionTransportConfig:
    request_timeout_seconds: float = 30.0
    maximum_request_bytes: int = 2_000_000
    maximum_response_bytes: int = 16_000_000
    maximum_inline_binary_bytes: int = 8_000_000
    redirect_settle_seconds: float = 0.5
    require_known_cdp_session: bool = True

    def __post_init__(self) -> None:
        if self.request_timeout_seconds <= 0 or self.redirect_settle_seconds < 0:
            raise ValueError("browser session transport timeouts are invalid")
        if min(self.maximum_request_bytes, self.maximum_response_bytes, self.maximum_inline_binary_bytes) < 1024:
            raise ValueError("browser session transport byte limits are too small")


@dataclass(frozen=True, slots=True)
class CdpCommandOutcome:
    command: CdpCommand
    started_at: str
    completed_at: str
    request_bytes: int
    response_bytes: int
    ok: bool
    error_code: str = ""
    response_digest: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "command": self.command.public_dict(),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "ok": self.ok,
            "error_code": self.error_code,
            "response_digest": self.response_digest,
        }


class SessionBoundCdpTransport:
    """04C adapter over the single live 04A CDP and target owners."""

    def __init__(
        self,
        cdp_runtime: Any,
        target_runtime: Any,
        *,
        browser_session_id: str,
        config: SessionTransportConfig | None = None,
        disabled: bool = False,
    ) -> None:
        if cdp_runtime is None or target_runtime is None or not browser_session_id:
            raise ValueError("session-bound CDP transport requires 04A owners and browser session id")
        self.cdp_runtime = cdp_runtime
        self.target_runtime = target_runtime
        self.browser_session_id = browser_session_id
        self.config = config or SessionTransportConfig()
        self.disabled = disabled
        self.commands: list[CdpCommand] = []
        self.outcomes: list[CdpCommandOutcome] = []
        self._deadline: ActionDeadline | None = None
        self._action_id = ""
        self._lock = threading.RLock()
        self._sent = 0
        self._failed = 0
        self._oversize = 0

    def bind_action(self, action_id: str, deadline: ActionDeadline) -> None:
        self._ensure_available()
        if not action_id or deadline.action_id != action_id:
            raise BrowserActionIntegrationError(
                "browser_transport_action_mismatch",
                "CDP transport deadline belongs to another action",
                phase=PlanPhase.DISPATCH,
                action_id=action_id,
            )
        with self._lock:
            if self._action_id and self._action_id != action_id:
                raise BrowserActionIntegrationError(
                    "browser_transport_busy",
                    "CDP transport is already bound to another action",
                    phase=PlanPhase.DISPATCH,
                    action_id=action_id,
                )
            self._action_id = action_id
            self._deadline = deadline

    def release_action(self, action_id: str) -> None:
        with self._lock:
            if self._action_id and self._action_id != action_id:
                raise BrowserActionIntegrationError(
                    "browser_transport_action_mismatch",
                    "cannot release another browser action transport binding",
                    phase=PlanPhase.DISPATCH,
                    action_id=action_id,
                )
            self._action_id = ""
            self._deadline = None

    def send(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        cdp_session_id: str = "",
    ) -> Mapping[str, Any]:
        self._ensure_available()
        self._validate_method(method)
        if not isinstance(params, Mapping):
            raise BrowserActionIntegrationError(
                "browser_cdp_params_not_object",
                "CDP parameters must be an object",
                phase=PlanPhase.DISPATCH,
                action_id=self._action_id,
            )
        with self._lock:
            deadline = self._deadline
            action_id = self._action_id
        if deadline is None or not action_id:
            raise BrowserActionIntegrationError(
                "browser_transport_unbound",
                "CDP transport cannot dispatch outside an active 04C action",
                phase=PlanPhase.DISPATCH,
            )
        remaining = deadline.checkpoint(
            PlanPhase.DISPATCH,
            boundary=DispatchBoundary.AFTER_GRANT_BEFORE_EFFECT,
            side_effect_count=self.mutating_count(action_id=action_id),
            details={"method": method},
        )
        request_bytes = len(json.dumps(params, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
        if request_bytes > self.config.maximum_request_bytes:
            self._oversize += 1
            raise BrowserActionIntegrationError(
                "browser_cdp_request_too_large",
                "CDP request exceeds the action boundary byte limit",
                phase=PlanPhase.DISPATCH,
                action_id=action_id,
                details={"method": method, "bytes": request_bytes, "maximum": self.config.maximum_request_bytes},
            )
        selected_session = self._select_session(cdp_session_id)
        command = CdpCommand(
            method=method,
            params=redact_cdp_params(params),
            cdp_session_id=selected_session,
            mutating=is_mutating_cdp(method, params),
            sequence=len(self.commands) + 1,
        )
        started_at = now_iso()
        self.commands.append(command)
        timeout = self.config.request_timeout_seconds
        if remaining is not None:
            timeout = max(0.01, min(timeout, remaining))
        try:
            response = self.cdp_runtime.send(
                method,
                dict(params),
                cdp_session_id=selected_session,
                timeout_seconds=timeout,
            )
            if not isinstance(response, Mapping):
                raise BrowserActionIntegrationError(
                    "browser_cdp_response_not_object",
                    "CDP response is not an object",
                    phase=PlanPhase.DISPATCH,
                    action_id=action_id,
                )
            response_bytes = len(json.dumps(response, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
            if response_bytes > self.config.maximum_response_bytes:
                self._oversize += 1
                raise BrowserActionIntegrationError(
                    "browser_cdp_response_too_large",
                    "CDP response exceeds the pre-projection byte limit",
                    phase=PlanPhase.RESULT_PROJECTION,
                    action_id=action_id,
                    details={"method": method, "bytes": response_bytes, "maximum": self.config.maximum_response_bytes},
                    side_effect_count=self.mutating_count(action_id=action_id),
                )
        except Exception as exc:
            self._failed += 1
            self.outcomes.append(
                CdpCommandOutcome(
                    command,
                    started_at,
                    now_iso(),
                    request_bytes,
                    0,
                    False,
                    error_code=getattr(exc, "code", type(exc).__name__),
                )
            )
            raise
        self._sent += 1
        self.outcomes.append(
            CdpCommandOutcome(
                command,
                started_at,
                now_iso(),
                request_bytes,
                response_bytes,
                True,
                response_digest=digest_value(response),
            )
        )
        return dict(response)

    def _select_session(self, requested: str) -> str:
        if requested:
            if self.config.require_known_cdp_session:
                snapshot = self.target_runtime.snapshot()
                known = {
                    str(getattr(item, "cdp_session_id", "") or "")
                    for item in getattr(snapshot, "cdp_sessions", ())
                }
                if requested not in known:
                    raise BrowserActionIntegrationError(
                        "browser_cdp_session_stale",
                        "requested CDP session is not current in the 04A target owner",
                        phase=PlanPhase.RESUME_VALIDATION,
                        action_id=self._action_id,
                        details={"requested": requested, "known_count": len(known)},
                    )
            return requested
        session = self.target_runtime.active_cdp_session(timeout=self.config.request_timeout_seconds)
        selected = str(getattr(session, "cdp_session_id", "") or "")
        if not selected:
            raise BrowserActionIntegrationError(
                "browser_active_cdp_session_missing",
                "04A active target has no current CDP session",
                phase=PlanPhase.DISPATCH,
                action_id=self._action_id,
            )
        return selected

    @staticmethod
    def _validate_method(method: str) -> None:
        if not method or method in _FORBIDDEN_RAW_METHODS or not method.startswith(_ALLOWED_CDP_PREFIXES):
            raise BrowserActionIntegrationError(
                "browser_cdp_method_denied",
                f"CDP method {method!r} is outside the productized browser boundary",
                phase=PlanPhase.DISPATCH,
            )

    @property
    def mutating_commands(self) -> tuple[CdpCommand, ...]:
        return tuple(command for command in self.commands if command.mutating)

    def count(self, method: str) -> int:
        return sum(command.method == method for command in self.commands)

    def mutating_count(self, *, action_id: str = "") -> int:
        if action_id and self._action_id and action_id != self._action_id:
            return 0
        return len(self.mutating_commands)

    def command_slice(self, start: int) -> tuple[CdpCommand, ...]:
        if start < 0:
            raise ValueError("CDP command slice start cannot be negative")
        return tuple(self.commands[start:])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            action_id = self._action_id
        return {
            "runtime_id": "zyra-session-bound-cdp-transport",
            "owner_unit": "M1-S04C-02",
            "canonical_cdp_owner": type(self.cdp_runtime).__name__,
            "canonical_target_owner": type(self.target_runtime).__name__,
            "browser_session_id": self.browser_session_id,
            "disabled": self.disabled,
            "active_action_id": action_id,
            "commands": len(self.commands),
            "mutating_commands": len(self.mutating_commands),
            "sent": self._sent,
            "failed": self._failed,
            "oversize": self._oversize,
        }

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserActionIntegrationError(
                "browser_cdp_transport_disabled",
                "session-bound CDP transport is disabled",
                phase=PlanPhase.DISPATCH,
                action_id=self._action_id,
            )


class BrowserActionArtifactPort(ArtifactPort):
    def __init__(
        self,
        store: LocalArtifactStore,
        *,
        run_id: str,
        task_id: str,
        node_id: str = "",
        browser_session_id: str,
        maximum_bytes: int = 128 * 1024 * 1024,
        disabled: bool = False,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.task_id = task_id
        self.node_id = node_id
        self.browser_session_id = browser_session_id
        self.maximum_bytes = maximum_bytes
        self.disabled = disabled
        self.artifacts: list[ArtifactRef] = []
        self._lock = threading.RLock()

    def write(
        self,
        *,
        kind: ArtifactKind,
        title: str,
        content: bytes,
        metadata: Mapping[str, Any],
    ) -> ArtifactRef:
        if self.disabled:
            raise BrowserActionIntegrationError(
                "browser_artifact_port_disabled",
                "browser action artifact port is disabled",
                phase=PlanPhase.RESULT_PROJECTION,
            )
        if len(content) > self.maximum_bytes:
            raise BrowserActionIntegrationError(
                "browser_artifact_too_large",
                "browser action artifact exceeds its owned storage limit",
                phase=PlanPhase.RESULT_PROJECTION,
                details={"bytes": len(content), "maximum": self.maximum_bytes},
            )
        required = ("run_id", "task_id", "browser_session_id", "action_id", "preflight_receipt_id")
        missing = [key for key in required if not str(metadata.get(key) or "")]
        if missing:
            raise BrowserActionIntegrationError(
                "browser_artifact_causality_missing",
                "browser action artifact is missing causal identity",
                phase=PlanPhase.RESULT_PROJECTION,
                details={"missing": missing},
            )
        if str(metadata.get("run_id")) != self.run_id or str(metadata.get("task_id")) != self.task_id:
            raise BrowserActionIntegrationError(
                "browser_artifact_task_mismatch",
                "browser action artifact belongs to another run/task",
                phase=PlanPhase.RESULT_PROJECTION,
            )
        if str(metadata.get("browser_session_id")) != self.browser_session_id:
            raise BrowserActionIntegrationError(
                "browser_artifact_session_mismatch",
                "browser action artifact belongs to another browser session",
                phase=PlanPhase.RESULT_PROJECTION,
            )
        extension = artifact_extension(kind, title, content)
        safe_metadata = SecretRedactor().redact(dict(metadata))
        artifact = self.store.write_bytes(
            run_id=self.run_id,
            task_id=self.task_id,
            content=content,
            title=str(title)[:500],
            kind=kind,
            extension=extension,
            producer_node_id=self.node_id or None,
            metadata={
                **safe_metadata,
                "browser_session_id": self.browser_session_id,
                "sha256": hashlib.sha256(content).hexdigest(),
                "storage_owner": "LocalArtifactStore",
                "owner_unit": "M1-S04C-02",
            },
        )
        with self._lock:
            self.artifacts.append(artifact)
        return artifact

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            values = tuple(self.artifacts)
        return {
            "runtime_id": "zyra-browser-action-artifact-port",
            "owner_unit": "M1-S04C-02",
            "canonical_owner": "LocalArtifactStore",
            "disabled": self.disabled,
            "browser_session_id": self.browser_session_id,
            "artifacts": len(values),
            "artifact_ids": [artifact.artifact_id for artifact in values],
        }


class CdpClipboardPort(ClipboardPort):
    def __init__(self, transport: SessionBoundCdpTransport, *, disabled: bool = False) -> None:
        self.transport = transport
        self.disabled = disabled
        self.operations: list[dict[str, Any]] = []
        self._grants: set[tuple[str, str]] = set()
        self._lock = threading.RLock()

    def grant(self, *, origin: str, browser_context_id: str, read: bool, write: bool) -> None:
        self._ensure_available()
        permissions: list[str] = []
        if read:
            permissions.append("clipboardReadWrite")
        if write and "clipboardReadWrite" not in permissions:
            permissions.append("clipboardReadWrite")
        self.transport.send(
            "Browser.grantPermissions",
            {"origin": origin, "permissions": permissions},
        )
        with self._lock:
            self._grants.add((origin, browser_context_id))
            self.operations.append({"operation": "grant", "origin": origin, "read": read, "write": write})

    def read_text(self, *, origin: str, browser_context_id: str) -> str:
        self._require_grant(origin, browser_context_id)
        response = self.transport.send(
            "Runtime.evaluate",
            {
                "expression": "navigator.clipboard.readText()",
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        value = str(response.get("result", {}).get("value", ""))
        self.operations.append({"operation": "read", "origin": origin, "characters": len(value)})
        return value

    def write_text(self, value: str, *, origin: str, browser_context_id: str) -> None:
        self._require_grant(origin, browser_context_id)
        encoded = json.dumps(str(value), ensure_ascii=False)
        response = self.transport.send(
            "Runtime.evaluate",
            {
                "expression": f"navigator.clipboard.writeText({encoded})",
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        if response.get("exceptionDetails"):
            raise BrowserActionIntegrationError(
                "browser_clipboard_write_failed",
                "browser clipboard write raised an exception",
                phase=PlanPhase.DISPATCH,
            )
        self.operations.append({"operation": "write", "origin": origin, "value_digest": digest_value(value)})

    def revoke(self, *, origin: str, browser_context_id: str) -> None:
        try:
            self.transport.send("Browser.resetPermissions", {})
        finally:
            with self._lock:
                self._grants.discard((origin, browser_context_id))
                self.operations.append({"operation": "revoke", "origin": origin})

    def _require_grant(self, origin: str, browser_context_id: str) -> None:
        self._ensure_available()
        with self._lock:
            if (origin, browser_context_id) not in self._grants:
                raise BrowserActionIntegrationError(
                    "browser_clipboard_grant_missing",
                    "browser clipboard operation has no active origin grant",
                    phase=PlanPhase.DISPATCH,
                )

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserActionIntegrationError(
                "browser_clipboard_port_disabled",
                "browser clipboard CDP port is disabled",
                phase=PlanPhase.DISPATCH,
            )


class CdpDownloadControlPort(DownloadControlPort):
    def __init__(self, transport: SessionBoundCdpTransport, *, disabled: bool = False) -> None:
        self.transport = transport
        self.disabled = disabled
        self.operations: list[dict[str, Any]] = []

    def arm(self, *, browser_context_id: str, download_path: str, events_enabled: bool) -> None:
        self._ensure_available()
        path = str(Path(download_path).resolve(strict=False))
        self.transport.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": path,
                "eventsEnabled": bool(events_enabled),
            },
        )
        self.operations.append({"operation": "arm", "browser_context_id": browser_context_id, "download_path": path})

    def cancel(self, guid: str) -> None:
        self._ensure_available()
        self.transport.send("Browser.cancelDownload", {"guid": guid})
        self.operations.append({"operation": "cancel", "guid": guid})

    def disarm(self, *, browser_context_id: str) -> None:
        self._ensure_available()
        try:
            self.transport.send("Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": False})
        finally:
            self.operations.append({"operation": "disarm", "browser_context_id": browser_context_id})

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserActionIntegrationError(
                "browser_download_control_disabled",
                "browser download control port is disabled",
                phase=PlanPhase.DISPATCH,
            )


class _CdpFetchPort(FetchInterceptionPort):
    def __init__(self, cdp_runtime: Any, *, cdp_session_id: str, timeout_seconds: float) -> None:
        self.cdp_runtime = cdp_runtime
        self.cdp_session_id = cdp_session_id
        self.timeout_seconds = timeout_seconds
        self.operations: list[dict[str, Any]] = []

    def continue_request(self, interception_id: str) -> None:
        self.cdp_runtime.send(
            "Fetch.continueRequest",
            {"requestId": interception_id},
            cdp_session_id=self.cdp_session_id,
            timeout_seconds=self.timeout_seconds,
        )
        self.operations.append({"operation": "continue", "interception_id": interception_id})

    def fail_request(self, interception_id: str, error_reason: str) -> None:
        self.cdp_runtime.send(
            "Fetch.failRequest",
            {"requestId": interception_id, "errorReason": error_reason},
            cdp_session_id=self.cdp_session_id,
            timeout_seconds=self.timeout_seconds,
        )
        self.operations.append({"operation": "fail", "interception_id": interception_id, "error_reason": error_reason})


class BrowserNetworkInterception:
    """Live Fetch.requestPaused adapter for every admitted network action."""

    def __init__(
        self,
        *,
        action_id: str,
        network_policy: BrowserNetworkPolicy,
        initial_receipt: NetworkReceipt,
        cdp_runtime: Any,
        target_runtime: Any,
        timeout_seconds: float,
        block_cross_origin_subresources: bool = False,
        disabled: bool = False,
    ) -> None:
        self.action_id = action_id
        self.network_policy = network_policy
        self.initial_receipt = initial_receipt
        self.cdp_runtime = cdp_runtime
        self.target_runtime = target_runtime
        self.timeout_seconds = timeout_seconds
        self.block_cross_origin_subresources = block_cross_origin_subresources
        self.disabled = disabled
        self.decisions: list[InterceptionDecision] = []
        self.errors: list[str] = []
        self._queue: queue.Queue[Mapping[str, Any] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cdp_session_id = ""
        self._target_id = ""
        self._port: _CdpFetchPort | None = None
        self._guard: BrowserRedirectGuard | None = None
        self._started = False
        self._lock = threading.RLock()

    def start(self) -> None:
        if self.disabled:
            raise BrowserActionIntegrationError(
                "browser_network_interception_disabled",
                "network action requires live redirect interception",
                phase=PlanPhase.SECURITY_PREFLIGHT,
                action_id=self.action_id,
            )
        with self._lock:
            if self._started:
                raise BrowserActionIntegrationError(
                    "browser_network_interception_replayed",
                    "network interception already started for this action",
                    phase=PlanPhase.DISPATCH,
                    action_id=self.action_id,
                )
            target = self.target_runtime.ensure_valid_focus(timeout=self.timeout_seconds)
            cdp_session = self.target_runtime.active_cdp_session(timeout=self.timeout_seconds)
            self._target_id = str(target.target_id)
            self._cdp_session_id = str(cdp_session.cdp_session_id)
            self._port = _CdpFetchPort(
                self.cdp_runtime,
                cdp_session_id=self._cdp_session_id,
                timeout_seconds=self.timeout_seconds,
            )
            self._guard = BrowserRedirectGuard(
                self.network_policy,
                self._port,
                action_id=self.action_id,
                initial_receipt=self.initial_receipt,
                block_cross_origin_subresources=self.block_cross_origin_subresources,
            )
            self.cdp_runtime.register("Fetch.requestPaused", self._on_paused)
            self.cdp_runtime.send(
                "Fetch.enable",
                {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                cdp_session_id=self._cdp_session_id,
                timeout_seconds=self.timeout_seconds,
            )
            self._worker = threading.Thread(target=self._run, name=f"zyra-fetch-{self.action_id[-8:]}", daemon=True)
            self._worker.start()
            self._started = True

    @property
    def effect_count(self) -> int:
        # Enabling/disabling interception changes the live browser boundary;
        # each handled request is another observable network effect.
        return (2 if self._cdp_session_id else 0) + len(self.decisions)

    def __enter__(self) -> "BrowserNetworkInterception":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            if exc is None:
                self.settle(timeout=self.timeout_seconds)
        finally:
            self.close()
        return False

    def _on_paused(self, payload: Mapping[str, Any]) -> None:
        if not self._started:
            return
        self._queue.put(copy.deepcopy(dict(payload)))

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            try:
                if payload is None:
                    return
                if self._guard is None:
                    self.errors.append("redirect_guard_missing")
                    continue
                intercepted = intercepted_request_from_cdp(payload, target_id=self._target_id)
                decision = self._guard.handle(intercepted)
                self.decisions.append(decision)
            except Exception as exc:
                self.errors.append(f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
                request_id = str(dict(payload or {}).get("requestId") or "")
                if request_id and self._port is not None:
                    try:
                        self._port.fail_request(request_id, "BlockedByClient")
                    except Exception as fail_exc:
                        self.errors.append(f"fail_request:{type(fail_exc).__name__}:{fail_exc}")
            finally:
                self._queue.task_done()

    def settle(self, *, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        if self._queue.unfinished_tasks:
            raise BrowserActionIntegrationError(
                "browser_redirect_settle_timeout",
                "browser redirect interception did not settle before action completion",
                phase=PlanPhase.DISPATCH,
                action_id=self.action_id,
                side_effect_count=1,
            )
        if self.errors:
            raise BrowserActionIntegrationError(
                "browser_redirect_interception_failed",
                "browser redirect interception failed closed",
                phase=PlanPhase.DISPATCH,
                action_id=self.action_id,
                details={"errors": list(self.errors)},
                side_effect_count=1,
            )
        denied = [decision for decision in self.decisions if decision.disposition == InterceptionDisposition.FAIL]
        if denied:
            raise BrowserActionIntegrationError(
                "browser_redirect_denied",
                "browser network policy denied a redirect or subrequest before dispatch",
                phase=PlanPhase.DISPATCH,
                action_id=self.action_id,
                details={"decisions": [decision.public_dict() for decision in denied]},
                side_effect_count=1,
            )

    def close(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=self.timeout_seconds)
        try:
            self.cdp_runtime.unregister("Fetch.requestPaused", self._on_paused)
        finally:
            try:
                self.cdp_runtime.send(
                    "Fetch.disable",
                    {},
                    cdp_session_id=self._cdp_session_id,
                    timeout_seconds=self.timeout_seconds,
                )
            except Exception as exc:
                self.errors.append(f"Fetch.disable:{type(exc).__name__}:{exc}")

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-browser-network-interception",
            "owner_unit": "M1-S04C-02",
            "action_id": self.action_id,
            "started": self._started,
            "target_id": self._target_id,
            "cdp_session_id": self._cdp_session_id,
            "decisions": [decision.public_dict() for decision in self.decisions],
            "errors": list(self.errors),
        }


def redact_cdp_params(params: Mapping[str, Any]) -> dict[str, Any]:
    redactor = SecretRedactor()

    def scrub(value: Any, key: str = "") -> Any:
        normalized = key.casefold().replace("-", "_")
        if any(token in normalized for token in _SENSITIVE_PARAM_KEYS):
            if normalized in {"headers"} and isinstance(value, Mapping):
                return {str(name): "[REDACTED]" for name in value}
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {str(name): scrub(item, str(name)) for name, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [scrub(item) for item in value]
        if isinstance(value, str) and len(value) > 4096:
            return {"digest": digest_value(value), "characters": len(value)}
        return redactor.redact(value)

    return scrub(dict(params))


def artifact_extension(kind: ArtifactKind, title: str, content: bytes) -> str:
    if kind == ArtifactKind.SCREENSHOT:
        if content.startswith(b"\x89PNG"):
            return ".png"
        if content.startswith(b"\xff\xd8"):
            return ".jpg"
        if content.startswith(b"RIFF") and b"WEBP" in content[:16]:
            return ".webp"
        return ".img"
    if content.startswith(b"%PDF"):
        return ".pdf"
    if kind in {ArtifactKind.TEXT, ArtifactKind.TRACE}:
        return ".txt"
    if kind == ArtifactKind.MARKDOWN:
        return ".md"
    if kind == ArtifactKind.STRUCTURED_DATA:
        return ".json"
    suffix = Path(title).suffix.lower()
    if suffix and len(suffix) <= 10 and all(character.isalnum() or character == "." for character in suffix):
        return suffix
    return ".bin"
