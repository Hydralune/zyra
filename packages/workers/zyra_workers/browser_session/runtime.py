from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from zyra_integrations.browser_use import ChromeProcessController

from .artifact_event_bridge import BrowserArtifactPort, BrowserCanonicalEventPort
from .errors import BrowserRuntimeDisabled, BrowserSessionNotFound
from .models import (
    BrowserRuntimeConfig,
    BrowserSessionCommand,
    BrowserSessionDiagnostic,
    BrowserSessionRef,
    BrowserSessionStartResult,
    BrowserSessionStopResult,
)
from .permission_bridge import BrowserPermissionPort
from .session_runtime import BrowserSessionRuntime
from .store import BrowserStatePort, JsonBrowserStateStore


class BrowserRuntime:
    def __init__(
        self,
        config: BrowserRuntimeConfig,
        *,
        state_store: BrowserStatePort | None = None,
        permission_port: BrowserPermissionPort | None = None,
        artifact_port: BrowserArtifactPort | None = None,
        event_port: BrowserCanonicalEventPort | None = None,
        process_controller: ChromeProcessController | None = None,
    ) -> None:
        self.config = config
        self.config.prepare_roots()
        self.state_store = state_store or JsonBrowserStateStore(config.state_root)
        self.permission_port = permission_port
        self.artifact_port = artifact_port
        self.event_port = event_port
        self.process_controller = process_controller or self._default_process_controller()
        self._runtime = BrowserSessionRuntime(
            config,
            state_store=self.state_store,
            permission_port=permission_port,
            artifact_port=artifact_port,
            event_port=event_port,
            process_controller=self.process_controller,
        )
        self._operation_lock = threading.RLock()
        self._operations = 0
        self._failures = 0
        self._last_operation = ""
        self._last_error = ""

    @staticmethod
    def _default_process_controller() -> ChromeProcessController | None:
        try:
            return ChromeProcessController()
        except TypeError:
            return None

    def _ensure_available(self) -> None:
        if self.config.disabled:
            raise BrowserRuntimeDisabled("browser runtime is disabled")

    def _record_success(self, operation: str) -> None:
        with self._operation_lock:
            self._operations += 1
            self._last_operation = operation
            self._last_error = ""

    def _record_failure(self, operation: str, error: BaseException) -> None:
        with self._operation_lock:
            self._operations += 1
            self._failures += 1
            self._last_operation = operation
            self._last_error = f"{type(error).__name__}: {error}"

    def start(self, command: BrowserSessionCommand) -> BrowserSessionStartResult:
        self._ensure_available()
        try:
            result = self._runtime.start(command)
        except Exception as error:
            self._record_failure("start", error)
            raise
        self._record_success("start")
        return result

    def ensure_started(self, command: BrowserSessionCommand) -> BrowserSessionStartResult:
        self._ensure_available()
        try:
            result = self._runtime.ensure_started(command)
        except Exception as error:
            self._record_failure("ensure_started", error)
            raise
        self._record_success("ensure_started")
        return result

    def stop(
        self,
        session_id: str,
        *,
        force: bool = False,
        reason: str = "requested",
    ) -> BrowserSessionStopResult:
        self._ensure_available()
        try:
            result = self._runtime.stop(session_id, force=force, reason=reason)
        except Exception as error:
            self._record_failure("stop", error)
            raise
        self._record_success("stop")
        return result

    def reconnect(self, session_id: str) -> BrowserSessionStartResult:
        self._ensure_available()
        try:
            result = self._runtime.reconnect(session_id)
        except Exception as error:
            self._record_failure("reconnect", error)
            raise
        self._record_success("reconnect")
        return result

    def diagnose(self, session_id: str) -> BrowserSessionDiagnostic:
        self._ensure_available()
        try:
            result = self._runtime.diagnose(session_id)
        except Exception as error:
            self._record_failure("diagnose", error)
            raise
        self._record_success("diagnose")
        return result

    def list_sessions(self, *, task_id: str = "") -> tuple[BrowserSessionRef, ...]:
        self._ensure_available()
        try:
            result = self._runtime.list_sessions(task_id=task_id)
        except Exception as error:
            self._record_failure("list_sessions", error)
            raise
        self._record_success("list_sessions")
        return result

    def get_session(self, session_id: str) -> BrowserSessionRef:
        self._ensure_available()
        session = self.state_store.get_session(session_id)
        if session is None:
            raise BrowserSessionNotFound(f"browser session {session_id} does not exist", session_id=session_id)
        return session

    def session_runtime_component(self) -> BrowserSessionRuntime:
        """Return the 04A state owner for tightly scoped downstream ports."""
        self._ensure_available()
        return self._runtime

    def target_runtime(self, session_id: str) -> Any:
        """Return the live target runtime; callers must not create a second owner."""
        self._ensure_available()
        return self._runtime.target_runtime(session_id)

    def cdp_runtime(self, session_id: str) -> Any:
        """Return the live CDP runtime bound to the session target generation."""
        self._ensure_available()
        return self._runtime.cdp_runtime(session_id)

    def stop_all(self, *, task_id: str = "", force: bool = False, reason: str = "runtime_shutdown") -> tuple[BrowserSessionStopResult, ...]:
        results: list[BrowserSessionStopResult] = []
        for session in self.list_sessions(task_id=task_id):
            if session.status == "stopped":
                continue
            results.append(self.stop(session.session_id, force=force, reason=reason))
        return tuple(results)

    def metadata(self) -> dict[str, str]:
        with self._operation_lock:
            return {
                "browser_runtime_id": "zyra-browser-productized-runtime",
                "browser_runtime_owner_unit": "M1-S04A-01",
                "browser_runtime_disabled": str(self.config.disabled).lower(),
                "browser_runtime_operations": str(self._operations),
                "browser_runtime_failures": str(self._failures),
                "browser_runtime_last_operation": self._last_operation,
                "browser_runtime_last_error": self._last_error,
                "browser_runtime_state_owner": type(self.state_store).__name__,
                "browser_runtime_vendor_required": "false",
                "browser_runtime_process_controller": type(self.process_controller).__name__ if self.process_controller else "unavailable",
                "browser_runtime_supervised_tasks": str(self._runtime.task_supervisor.snapshot().tasks),
                "browser_runtime_recovery_receipts": str(len(self._runtime.recovery.receipts())),
            }

    def snapshot(self) -> dict[str, Any]:
        state_snapshot = self.state_store.snapshot().to_dict() if hasattr(self.state_store, "snapshot") else {}
        return {
            "config": self.config.to_dict(),
            "metadata": self.metadata(),
            "state": state_snapshot,
            "sessions": [session.to_dict() for session in self.list_sessions()],
            "task_supervisor": self._runtime.task_supervisor.snapshot().to_dict(),
            "recovery": self._runtime.recovery.snapshot(),
            "connection_policies": {
                session_id: policy.public_dict()
                for session_id, policy in self._runtime._connection_policies.items()
            },
        }
