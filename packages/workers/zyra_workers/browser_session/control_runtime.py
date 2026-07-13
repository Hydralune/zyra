from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from zyra_core import ControlCommand, EventRecord, EventType, to_jsonable

from .canonical_ports import BrowserCanonicalPorts, CanonicalProjectionResult
from .lifecycle_transactions import (
    BrowserLifecycleAction,
    BrowserLifecycleTransactionRequest,
    BrowserLifecycleTransactionRuntime,
)
from .models import BrowserSessionCommand
from .session_lease import BrowserSessionLeaseStore


class BrowserControlAction(StrEnum):
    START = "start"
    ENSURE_STARTED = "ensure-started"
    RECONNECT = "reconnect"
    STOP = "stop"
    CANCEL = "cancel"
    DIAGNOSE = "diagnose"
    LIST = "list"
    STOP_ALL = "stop-all"


@dataclass(frozen=True, slots=True)
class BrowserControlResult:
    ok: bool
    action: BrowserControlAction
    result: Any = None
    event_record: EventRecord | None = None
    projection: CanonicalProjectionResult | None = None
    error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": str(self.action),
            "result": to_jsonable(self.result),
            "event_record": to_jsonable(self.event_record) if self.event_record else None,
            "projection": self.projection.to_dict() if self.projection else None,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


class BrowserSessionControlRuntime:
    """Runs lifecycle control against an injected BrowserRuntime only."""

    def __init__(
        self,
        browser_runtime: Any,
        *,
        canonical_ports: BrowserCanonicalPorts | None = None,
        lease_store: BrowserSessionLeaseStore | None = None,
        transaction_runtime: BrowserLifecycleTransactionRuntime | None = None,
        disabled: bool = False,
    ) -> None:
        if browser_runtime is None:
            raise ValueError("BrowserSessionControlRuntime requires an existing BrowserRuntime")
        self.browser_runtime = browser_runtime
        self.canonical_ports = canonical_ports
        self.disabled = disabled
        self.transactions = transaction_runtime or BrowserLifecycleTransactionRuntime(
            browser_runtime,
            lease_store,
            canonical_ports=canonical_ports,
            disabled=disabled,
        )

    def execute(
        self,
        command: ControlCommand | str,
        *,
        session_command: BrowserSessionCommand | None = None,
        session_id: str = "",
        task_id: str = "",
        force: bool = False,
        reason: str = "requested",
    ) -> BrowserControlResult:
        action, run_id, resolved_task_id, node_id, arguments = self._normalize_command(command, task_id=task_id)
        if self.disabled:
            return self._failure(action, run_id, resolved_task_id, node_id, "browser_control_runtime_disabled")
        session_id = str(session_id or arguments.get("browser_session_id") or arguments.get("session_id") or "")
        force = bool(arguments.get("force", force))
        reason = str(arguments.get("reason") or reason)
        lifecycle_action = BrowserLifecycleAction(str(action))
        transaction_request = BrowserLifecycleTransactionRequest(
            action=lifecycle_action,
            run_id=run_id,
            task_id=resolved_task_id,
            worker_request_id=str(arguments.get("worker_request_id") or arguments.get("request_id") or "browser-control"),
            request_id=str(arguments.get("control_request_id") or arguments.get("request_id") or "") or f"browser-control:{run_id}:{resolved_task_id}:{action}:{session_id}",
            browser_session_id=session_id,
            node_id=node_id or "",
            expected_generation=(int(arguments["expected_generation"]) if arguments.get("expected_generation") not in (None, "") else None),
            force=force,
            reason=(reason if action != BrowserControlAction.CANCEL or reason != "requested" else "control_cancelled"),
            task_filter=resolved_task_id,
            ttl_seconds=float(arguments.get("transaction_ttl_seconds") or 60.0),
            arguments=arguments,
        )
        receipt = self.transactions.execute(transaction_request, session_command=session_command)
        event = receipt.event_record
        projection = None
        return BrowserControlResult(
            ok=receipt.ok,
            action=action,
            result=receipt.result,
            event_record=event,
            projection=projection,
            error=receipt.error_code,
            metadata={
                "runtime_id": "zyra-browser-session-control-runtime",
                "browser_session_id": receipt.browser_session_id,
                "browser_lifecycle_receipt_id": receipt.receipt_id,
                "browser_lifecycle_generation_before": receipt.generation_before,
                "browser_lifecycle_generation_after": receipt.generation_after,
                "owns_runtime": False,
                "owns_canonical_state": False,
            },
        )

    @staticmethod
    def _normalize_command(
        command: ControlCommand | str,
        *,
        task_id: str,
    ) -> tuple[BrowserControlAction, str, str, str | None, dict[str, Any]]:
        if isinstance(command, ControlCommand):
            raw = command.name
            run_id = command.run_id
            resolved_task_id = command.task_id
            node_id = str(command.metadata.get("node_id") or "") or None
            arguments = dict(command.arguments)
        else:
            raw = str(command)
            run_id = ""
            resolved_task_id = task_id
            node_id = None
            arguments = {}
        normalized = raw.strip().lower().replace("_", "-")
        aliases = {
            "ensure": BrowserControlAction.ENSURE_STARTED,
            "attach": BrowserControlAction.ENSURE_STARTED,
            "health": BrowserControlAction.DIAGNOSE,
            "list-sessions": BrowserControlAction.LIST,
            "shutdown": BrowserControlAction.STOP,
        }
        try:
            action = aliases.get(normalized) or BrowserControlAction(normalized)
        except ValueError as error:
            raise ValueError(f"unknown browser control action: {raw}") from error
        return action, run_id, resolved_task_id, node_id, arguments

    @staticmethod
    def _require_session_command(command: BrowserSessionCommand | None) -> BrowserSessionCommand:
        if command is None:
            raise ValueError("browser session command is required")
        return command

    @staticmethod
    def _require_session_id(session_id: str) -> str:
        if not session_id:
            raise ValueError("browser_session_id is required")
        return session_id

    def _failure(
        self,
        action: BrowserControlAction,
        run_id: str,
        task_id: str,
        node_id: str | None,
        error: str,
    ) -> BrowserControlResult:
        event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.BROWSER_SESSION_LIFECYCLE,
            payload={"browser_control": {"action": str(action), "ok": False, "error": error}},
        )
        projection = self.canonical_ports.project(events=(event,)) if self.canonical_ports else None
        return BrowserControlResult(
            ok=False,
            action=action,
            event_record=event,
            projection=projection,
            error=error,
            metadata={
                "runtime_id": "zyra-browser-session-control-runtime",
                "owns_runtime": False,
                "owns_canonical_state": False,
            },
        )
