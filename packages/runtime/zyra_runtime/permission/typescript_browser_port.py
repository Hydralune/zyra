from __future__ import annotations

"""Typed browser effect port for the canonical TypeScript E02 permission owner.

This module owns no policy, mode state machine, rule matching, risk decision,
approval resolution, or execution-grant store.  It maps a BrowserWorker action
to an exact E02 identity, forwards that identity to ``PermissionCoordinator``,
verifies the returned TypeScript allow receipt, and consumes one local typed
receipt immediately before the physical browser callback.
"""

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from zyra_core import EventRecord, EventType

from ..e02_ports import (
    TypeScriptExecutionPermit,
    TypeScriptPermissionReceiptError,
    TypeScriptPermissionReceiptPort,
)
from ..tools import ToolCall
from .canonical import arguments_digest, build_tool_identity, canonicalize_arguments
from .custody import (
    PermissionSessionCustodyBinding,
    PermissionSessionCustodyError,
    PermissionSessionCustodyReceipt,
    PermissionSessionCustodyStore,
)
from .models import (
    PermissionDecisionRecord,
    PermissionEffect,
    PermissionMode,
    PermissionRecoveryInput,
    PermissionRequestPhase,
    PermissionRequestRecord,
    PermissionRequestStatus,
    PermissionScope,
    PermissionScopeKind,
    ToolIdentity,
)
from .store import PermissionStateStore


BROWSER_ACTION_GATE_RUNTIME_ID = "zyra-browser-typescript-permission-port"
BROWSER_ACTION_GATE_OWNER_UNIT = "M1-R01-E02"
BROWSER_ACTION_IDENTITY_VERSION = "zyra-browser-action-v1"


class _E02PermissionPort(Protocol):
    def permission_enforce(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    def permission_claim(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    def permission_get(
        self,
        *,
        view: str = "summary",
        request_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


class BrowserActionPermissionError(RuntimeError):
    code = "browser_action_permission_error"


class BrowserActionPermissionDisabled(BrowserActionPermissionError):
    code = "browser_action_permission_disabled"

    def __init__(self, component: str) -> None:
        self.component = component
        super().__init__(f"browser action permission dependency is disabled: {component}")


class BrowserActionPermissionCustodyError(BrowserActionPermissionError):
    code = "browser_action_permission_custody_error"

    def __init__(self, cause: Exception) -> None:
        self.component = type(cause).__name__
        self.cause_code = str(getattr(cause, "code", self.code))
        super().__init__(str(cause))


class BrowserActionPermissionIdentityError(BrowserActionPermissionError):
    code = "browser_action_permission_identity_mismatch"


@dataclass(frozen=True, slots=True)
class BrowserActionPermissionInput:
    step_index: int
    action: str
    normalized_action: str
    backend: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    source_action: str = ""
    source_model: str = ""
    required_arguments: tuple[str, ...] = ()
    optional_arguments: tuple[str, ...] = ()
    current_url: str = ""
    target_url: str = ""
    explicit_tool_use_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.step_index < 1:
            raise ValueError("browser permission step_index must be positive")
        if not self.normalized_action.strip() or not self.backend.strip():
            raise ValueError("browser permission action and backend are required")
        object.__setattr__(self, "arguments", canonicalize_arguments(dict(self.arguments)))
        object.__setattr__(
            self,
            "required_arguments",
            tuple(str(item) for item in self.required_arguments),
        )
        object.__setattr__(
            self,
            "optional_arguments",
            tuple(str(item) for item in self.optional_arguments),
        )
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))

    @property
    def permission_arguments(self) -> dict[str, Any]:
        return canonicalize_arguments(
            {
                **dict(self.arguments),
                "_zyra_browser_action": self.normalized_action,
                "_zyra_browser_backend": self.backend,
                "_zyra_browser_current_url": self.current_url,
                "_zyra_browser_target_url": self.target_url,
            }
        )

    @property
    def capabilities(self) -> tuple[str, ...]:
        return _browser_action_capabilities(self)

    @property
    def network(self) -> bool:
        return "network" in self.capabilities

    @property
    def read_only(self) -> bool:
        return "read_only" in self.capabilities and not bool(
            set(self.capabilities).intersection(
                {"external_side_effect", "mutation", "upload", "download"}
            )
        )

    def schema(self) -> dict[str, Any]:
        names = sorted(set((*self.required_arguments, *self.optional_arguments)))
        return {
            "type": "object",
            "required": list(self.required_arguments),
            "properties": {name: {} for name in names},
            "additionalProperties": True,
            "x-zyra-browser-action": self.normalized_action,
            "x-zyra-browser-backend": self.backend,
            "x-upstream-source-action": self.source_action,
            "x-upstream-source-model": self.source_model,
        }


@dataclass(frozen=True, slots=True)
class _BrowserActionMaterial:
    action: BrowserActionPermissionInput
    call: ToolCall
    evaluation_payload: dict[str, Any]
    identity: ToolIdentity
    operation: str


@dataclass(frozen=True, slots=True)
class _BrowserPermissionTrace:
    recovery_alternatives: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _BrowserGuardResult:
    decision: PermissionDecisionRecord
    allowed: bool
    effect: PermissionEffect
    ask_pending: bool
    execution_grant: TypeScriptExecutionPermit | None
    pending_request: PermissionRequestRecord | None
    trace: _BrowserPermissionTrace


@dataclass(frozen=True, slots=True)
class BrowserActionPermissionDecision:
    """Python projection of a decision already committed by TypeScript."""

    material: _BrowserActionMaterial
    guard: _BrowserGuardResult
    events: tuple[EventRecord, ...] = ()
    typescript_permit_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.guard.allowed

    @property
    def effect(self) -> PermissionEffect:
        return self.guard.effect

    @property
    def request_id(self) -> str:
        return self.guard.decision.request_id

    @property
    def decision_id(self) -> str:
        return self.guard.decision.decision_id

    @property
    def tool_use_id(self) -> str:
        return self.material.call.tool_call_id

    def metadata(self) -> dict[str, str]:
        return {
            "permission_runtime_id": BROWSER_ACTION_GATE_RUNTIME_ID,
            "permission_effect": str(self.effect),
            "permission_decision_id": self.decision_id,
            "permission_request_id": self.request_id,
            "permission_tool_use_id": self.tool_use_id,
            "permission_arguments_digest": self.guard.decision.arguments_digest,
            "permission_action_allowed": str(self.allowed).lower(),
            "permission_typescript_permit_id": self.typescript_permit_id,
            "canonical_permission_owner": "typescript",
            "python_policy_fallback": "false",
        }


@dataclass(frozen=True, slots=True)
class BrowserActionPermissionConsumption:
    accepted: bool
    decision: BrowserActionPermissionDecision
    replay: BrowserActionPermissionInput
    events: tuple[EventRecord, ...] = ()
    reason: str = "typescript_receipt_consumed"

    @property
    def authorizes_execution(self) -> bool:
        return self.accepted

    def metadata(self) -> dict[str, str]:
        return {
            **self.decision.metadata(),
            "permission_execution_grant_consumed": str(self.accepted).lower(),
            "permission_action_boundary_reason": self.reason,
        }


class _TypeScriptApprovalQueueProjection:
    """Read-only compatibility projection for browser payload continuation."""

    def __init__(self, gate: "BrowserActionPermissionGate") -> None:
        self._gate = gate

    def get(self, request_id: str) -> PermissionRequestRecord | None:
        return self._gate._permission_request(str(request_id))

    def mark_delivered(
        self,
        request_id: str,
        *,
        expected_request_revision: int = 0,
        channel: str = "",
    ) -> PermissionRequestRecord:
        del expected_request_revision, channel
        record = self.get(request_id)
        if record is None:
            raise KeyError(request_id)
        if record.phase is not PermissionRequestPhase.DELIVERED:
            raise BrowserActionPermissionIdentityError(
                "TypeScript approval request is not in the delivered phase"
            )
        return record


class _TypeScriptPermissionRuntimeProjection:
    """Compatibility surface with no Python evaluator or resolver."""

    def __init__(self, gate: "BrowserActionPermissionGate") -> None:
        self.request_queue = _TypeScriptApprovalQueueProjection(gate)

    def metadata(self) -> dict[str, str]:
        return {
            "canonical_permission_owner": "typescript",
            "python_permission_role": "approval-transport-projection",
            "python_policy_fallback": "false",
        }


class BrowserActionPermissionGate:
    """Session-bound adapter to the canonical TypeScript permission owner."""

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        session_id: str,
        tool_use_id_base: str,
        workspace_root: str | Path,
        state_store: PermissionStateStore,
        custody: PermissionSessionCustodyReceipt,
        requested_mode: str,
        effective_mode: str,
        caller_bypass_ignored: bool,
        caller_auto_ignored: bool,
        port: _E02PermissionPort | None,
        port_factory: Callable[[str], _E02PermissionPort] | None,
        redaction_secrets: tuple[str, ...] = (),
    ) -> None:
        self.run_id = str(run_id)
        self.task_id = str(task_id)
        self.node_id = node_id
        self.worker_request_id = str(worker_request_id)
        self.session_id = str(session_id)
        self.tool_use_id_base = str(tool_use_id_base)
        self.workspace_root = Path(workspace_root).resolve()
        self.state_store = state_store
        self.custody = custody
        self.requested_mode = requested_mode
        self.persisted_mode = ""
        self.effective_mode = effective_mode
        self.caller_bypass_ignored = bool(caller_bypass_ignored)
        self.caller_auto_ignored = bool(caller_auto_ignored)
        self._port = port
        self._port_factory = port_factory
        self.redaction_secrets = tuple(
            dict.fromkeys(str(item) for item in redaction_secrets if str(item))
        )
        self.receipt_port = TypeScriptPermissionReceiptPort(
            run_id=self.run_id,
            task_id=self.task_id,
            session_id=self.session_id,
            worker_request_id=self.worker_request_id,
            workspace_root=self.workspace_root,
        )
        self.runtime = _TypeScriptPermissionRuntimeProjection(self)

    @classmethod
    def for_worker_request(
        cls,
        request: Any,
        *,
        workspace_root: str | Path,
        state_path: str | Path,
        e02_port: _E02PermissionPort | None = None,
        e02_port_factory: Callable[[str], _E02PermissionPort] | None = None,
    ) -> "BrowserActionPermissionGate":
        constraints = dict(getattr(request, "constraints", {}) or {})
        for field_name, component in (
            ("disable_browser_action_permission_gate", "BrowserActionPermissionGate"),
            ("disable_typescript_e02_permission_port", "TypeScriptE02ApiPort"),
        ):
            if constraints.get(field_name) is True:
                raise BrowserActionPermissionDisabled(component)
        if e02_port is None and e02_port_factory is None:
            raise BrowserActionPermissionDisabled("TypeScriptE02ApiPort")

        run_id = str(getattr(request, "run_id", ""))
        task_id = str(getattr(request, "task_id", ""))
        worker_request_id = str(getattr(request, "request_id", ""))
        if not run_id or not task_id or not worker_request_id:
            raise BrowserActionPermissionIdentityError(
                "browser permission request identity is incomplete"
            )
        node_id = getattr(request, "node_id", None)
        workspace = Path(workspace_root).resolve()
        session_id = str(constraints.get("permission_session_id") or "").strip()
        if not session_id:
            session_id = _default_session_id(
                run_id=run_id,
                task_id=task_id,
                workspace_root=workspace,
            )
        tool_use_id_base = str(constraints.get("permission_tool_use_id") or "").strip()
        state_store = PermissionStateStore(state_path)
        binding = PermissionSessionCustodyBinding(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            workspace_root=str(workspace),
        )
        presented_token = str(
            constraints.get("permission_session_custody_token")
            or constraints.get("session_custody_token")
            or ""
        )
        try:
            custody = PermissionSessionCustodyStore(state_store).claim(
                binding,
                presented_token=presented_token,
            )
        except PermissionSessionCustodyError as error:
            raise BrowserActionPermissionCustodyError(error) from error
        requested_mode = str(constraints.get("permission_mode") or "default")
        effective_mode, bypass_ignored, auto_ignored = _effective_mode(
            requested_mode,
            constraints,
        )
        return cls(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            worker_request_id=worker_request_id,
            session_id=session_id,
            tool_use_id_base=tool_use_id_base,
            workspace_root=workspace,
            state_store=state_store,
            custody=custody,
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            caller_bypass_ignored=bypass_ignored,
            caller_auto_ignored=auto_ignored,
            port=e02_port,
            port_factory=e02_port_factory,
            redaction_secrets=tuple(
                item for item in (presented_token, custody.token) if item
            ),
        )

    @property
    def custody_token(self) -> str:
        return self.custody.token

    def metadata(self) -> dict[str, str]:
        return {
            **self.custody.metadata(),
            **self.runtime.metadata(),
            **self.receipt_port.metadata(),
            "browser_permission_gate_runtime_id": BROWSER_ACTION_GATE_RUNTIME_ID,
            "browser_permission_gate_owner_unit": BROWSER_ACTION_GATE_OWNER_UNIT,
            "permission_runtime_session_id": self.session_id,
            "browser_permission_requested_mode": self.requested_mode,
            "browser_permission_persisted_mode": self.persisted_mode,
            "browser_permission_mode_source": "typescript_state_owner",
            "browser_permission_effective_mode": self.effective_mode,
            "browser_permission_caller_bypass_ignored": str(
                self.caller_bypass_ignored
            ).lower(),
            "browser_permission_caller_auto_ignored": str(
                self.caller_auto_ignored
            ).lower(),
            "canonical_permission_owner": "typescript",
            "python_policy_fallback": "false",
        }

    def guard(self, action: BrowserActionPermissionInput) -> BrowserActionPermissionDecision:
        material = self._material(action)
        port, owned = self._acquire_port()
        try:
            claim = self._invoke_port(
                port,
                "permission_claim",
                material.evaluation_payload,
            )
            if _canonical_owner(claim) and claim.get("claimed") is True:
                response = claim
                typescript_permit_id = str(claim.get("permit_id") or "")
            else:
                response = self._invoke_port(
                    port,
                    "permission_enforce",
                    material.evaluation_payload,
                )
                typescript_permit_id = ""
        finally:
            if owned:
                port.close()
        _require_typescript_response(response)
        raw_decision = _mapping(response.get("decision"))
        effect = _permission_effect(raw_decision.get("effect"))
        if response.get("claimed") is True and effect is not PermissionEffect.ALLOW:
            raise BrowserActionPermissionIdentityError(
                "TypeScript claimed permit did not return an ALLOW decision"
            )
        if response.get("allowed") is True and effect is not PermissionEffect.ALLOW:
            raise BrowserActionPermissionIdentityError(
                "TypeScript enforcement flags disagree with its decision"
            )
        self.effective_mode = _mode_text(raw_decision.get("mode"))
        self.persisted_mode = self.effective_mode
        projected = _project_decision(raw_decision, material)
        pending = _project_pending_request(
            _mapping(response.get("approval_request")),
            projected,
            material,
        )
        execution_grant: TypeScriptExecutionPermit | None = None
        if effect is PermissionEffect.ALLOW:
            try:
                execution_grant = self.receipt_port.accept(
                    raw_decision,
                    tool_call_id=material.call.tool_call_id,
                    tool_name=material.call.tool_name,
                    arguments=material.call.arguments,
                    namespace=material.identity.namespace,
                    server_id=material.identity.server_id,
                    operation=material.operation,
                )
            except TypeScriptPermissionReceiptError as error:
                raise BrowserActionPermissionIdentityError(str(error)) from error
        alternatives = _recovery_alternatives(raw_decision)
        guard = _BrowserGuardResult(
            decision=projected,
            allowed=effect is PermissionEffect.ALLOW,
            effect=effect,
            ask_pending=effect is PermissionEffect.ASK,
            execution_grant=execution_grant,
            pending_request=pending,
            trace=_BrowserPermissionTrace(recovery_alternatives=alternatives),
        )
        events: tuple[EventRecord, ...] = (
            _permission_event(
                run_id=self.run_id,
                task_id=self.task_id,
                node_id=self.node_id,
                session_id=self.session_id,
                worker_request_id=self.worker_request_id,
                kind="permission_decision",
                effect=str(effect),
                reason=projected.reason,
                tool_use_id=projected.tool_use_id,
                request_id=projected.request_id,
                decision_id=projected.decision_id,
                arguments_digest_value=projected.arguments_digest,
                payload={
                    "reason_code": projected.reason_code,
                    "recovery_alternatives": list(alternatives),
                    "canonical_permission_owner": "typescript",
                    "python_decision_fallback": False,
                },
            ),
        )
        if effect is PermissionEffect.ASK:
            events = (
                *events,
                _permission_event(
                    run_id=self.run_id,
                    task_id=self.task_id,
                    node_id=self.node_id,
                    session_id=self.session_id,
                    worker_request_id=self.worker_request_id,
                    kind="permission_requested",
                    effect="ask",
                    reason=projected.reason,
                    tool_use_id=projected.tool_use_id,
                    request_id=projected.request_id,
                    decision_id=projected.decision_id,
                    arguments_digest_value=projected.arguments_digest,
                    cause_event_id=events[-1].event_id,
                    payload={"transport": "typescript-e02-api-port"},
                ),
            )
        if effect is not PermissionEffect.ALLOW:
            blocked, recovery = _blocked_events(
                run_id=self.run_id,
                task_id=self.task_id,
                node_id=self.node_id,
                worker_request_id=self.worker_request_id,
                session_id=self.session_id,
                action=action,
                guard=guard,
                cause_event_id=events[-1].event_id,
            )
            events = (*events, blocked, recovery)
        return BrowserActionPermissionDecision(
            material=material,
            guard=guard,
            events=events,
            typescript_permit_id=typescript_permit_id,
        )

    def consume(
        self,
        decision: BrowserActionPermissionDecision,
        replay: BrowserActionPermissionInput | None = None,
    ) -> BrowserActionPermissionConsumption:
        selected = replay or decision.material.action
        permit = decision.guard.execution_grant
        if not decision.allowed or permit is None:
            return BrowserActionPermissionConsumption(
                accepted=False,
                decision=decision,
                replay=selected,
                reason="TypeScript decision did not issue an execution receipt",
            )
        try:
            replay_material = self._material(selected)
            accepted = self.receipt_port.validate_and_consume(
                replay_material.call,
                permit,
                None,
            )
        except Exception as error:  # noqa: BLE001 - the physical boundary fails closed.
            accepted = False
            reason = f"TypeScript receipt validation failed: {type(error).__name__}"
        else:
            reason = (
                "exact one-use TypeScript browser receipt consumed"
                if accepted
                else "TypeScript receipt identity mismatch or replay"
            )
        if accepted:
            events = (
                _permission_event(
                    run_id=self.run_id,
                    task_id=self.task_id,
                    node_id=self.node_id,
                    session_id=self.session_id,
                    worker_request_id=self.worker_request_id,
                    kind="permission_execution_grant_consumed",
                    effect="allow",
                    reason=reason,
                    tool_use_id=decision.tool_use_id,
                    request_id=decision.request_id,
                    decision_id=decision.decision_id,
                    arguments_digest_value=decision.guard.decision.arguments_digest,
                    cause_event_id=decision.events[-1].event_id if decision.events else "",
                    payload={
                        "canonical_permission_owner": "typescript",
                        "python_policy_fallback": False,
                    },
                ),
            )
        else:
            events = _rejected_events(
                run_id=self.run_id,
                task_id=self.task_id,
                node_id=self.node_id,
                worker_request_id=self.worker_request_id,
                session_id=self.session_id,
                action=selected,
                decision=decision,
                reason=reason,
                cause_event_id=decision.events[-1].event_id if decision.events else "",
            )
        return BrowserActionPermissionConsumption(
            accepted=accepted,
            decision=decision,
            replay=selected,
            events=events,
            reason=reason,
        )

    def _material(self, action: BrowserActionPermissionInput) -> _BrowserActionMaterial:
        if PermissionSessionCustodyStore.contains_capability_echo(
            {"browser_action_arguments": dict(action.arguments)},
            self.redaction_secrets,
        ):
            raise BrowserActionPermissionIdentityError(
                "browser action arguments cannot contain the session custody capability"
            )
        permission_arguments = action.permission_arguments
        identity = build_tool_identity(
            action.normalized_action,
            namespace="browser",
            server_id="",
            version=BROWSER_ACTION_IDENTITY_VERSION,
            schema=action.schema(),
        )
        digest_value = arguments_digest(permission_arguments)
        tool_use_id = action.explicit_tool_use_id or _tool_use_id(
            base=self.tool_use_id_base,
            session_id=self.session_id,
            run_id=self.run_id,
            task_id=self.task_id,
            step_index=action.step_index,
            action=action.normalized_action,
            arguments_digest_value=digest_value,
        )
        operation = _permission_operation(action)
        workspace_state = _workspace_state(action, self.workspace_root)
        metadata = {
            "tool_namespace": identity.namespace,
            "server_id": identity.server_id,
            "tool_version": identity.version,
            "schema_digest": identity.schema_digest,
            "browser_backend": action.backend,
            "browser_step_index": action.step_index,
            "browser_source_action": action.source_action,
            "browser_source_model": action.source_model,
            "capabilities": list(action.capabilities),
            "read_only": action.read_only,
            "network": action.network,
            "workspace_state": workspace_state,
            "annotations": {
                "readOnlyHint": action.read_only,
                "destructiveHint": bool(
                    set(action.capabilities).intersection({"mutation", "upload"})
                ),
                "openWorldHint": action.network,
                "idempotentHint": action.read_only,
            },
            "canonical_adapter_owner": "zyra-python-browser-effect-port",
            "canonical_permission_owner": "typescript",
            "python_decision_fallback": False,
        }
        call = ToolCall(
            run_id=self.run_id,
            task_id=self.task_id,
            node_id=self.node_id,
            tool_name=action.normalized_action,
            arguments=copy.deepcopy(permission_arguments),
            tool_call_id=tool_use_id,
            metadata={
                "tool_namespace": identity.namespace,
                "server_id": identity.server_id,
                "tool_version": identity.version,
                "browser_backend": action.backend,
            },
        )
        payload = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "session_revision": 0,
            "worker_request_id": self.worker_request_id,
            "tool_call_id": tool_use_id,
            "tool_name": action.normalized_action,
            "namespace": identity.namespace,
            "server_id": identity.server_id,
            "operation": operation,
            "workspace_root": str(self.workspace_root),
            "arguments": copy.deepcopy(permission_arguments),
            "metadata": metadata,
            "await_approval_delivery": True,
        }
        return _BrowserActionMaterial(
            action=action,
            call=call,
            evaluation_payload=payload,
            identity=identity,
            operation=operation,
        )

    def _acquire_port(self) -> tuple[_E02PermissionPort, bool]:
        port = self._port
        if port is not None:
            return port, False
        if self._port_factory is None:
            raise BrowserActionPermissionDisabled("TypeScriptE02ApiPort")
        return self._port_factory(self.effective_mode), True

    @staticmethod
    def _invoke_port(
        port: _E02PermissionPort,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        method = getattr(port, method_name, None)
        if not callable(method):
            raise BrowserActionPermissionDisabled(
                f"TypeScriptE02ApiPort.{method_name}"
            )
        value = method(*args, **kwargs)
        if not isinstance(value, Mapping):
            raise BrowserActionPermissionIdentityError(
                f"TypeScript E02 port returned a non-object for {method_name}"
            )
        return dict(value)

    def _port_call(self, method_name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        port, owned = self._acquire_port()
        try:
            return self._invoke_port(port, method_name, *args, **kwargs)
        finally:
            if owned:
                port.close()

    def _permission_request(self, request_id: str) -> PermissionRequestRecord | None:
        projection = self._port_call(
            "permission_get",
            view="request",
            request_id=request_id,
            limit=1,
        )
        _require_typescript_response(projection)
        requests = projection.get("requests")
        if not isinstance(requests, list) or not requests:
            return None
        return _project_request_envelope(
            _mapping(requests[0]),
            effective_mode=self.effective_mode,
        )


def browser_permission_setup_failure_events(
    request: Any,
    *,
    session_id: str,
    component: str,
    reason: str,
    code: str,
) -> tuple[EventRecord, EventRecord]:
    run_id = str(getattr(request, "run_id", ""))
    task_id = str(getattr(request, "task_id", ""))
    node_id = getattr(request, "node_id", None)
    worker_request_id = str(getattr(request, "request_id", ""))
    blocked = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="browser_action_permission_unavailable",
        effect="deny",
        reason=reason,
        payload={
            "component": component,
            "error_code": code,
            "action_execution_allowed": False,
            "human_intervention_count": 0,
            "canonical_permission_owner": "typescript",
            "python_decision_fallback": False,
        },
    )
    recovery = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="recovery_input",
        effect="deny",
        reason="restore the TypeScript E02 permission owner before retry",
        cause_event_id=blocked.event_id,
        payload={
            "component": component,
            "retryable": True,
            "alternatives": [
                "restore the TypeScript E02 API port",
                "use a deterministic local read without a browser effect",
            ],
            "action_execution_allowed": False,
            "human_intervention_count": 0,
        },
    )
    return blocked, recovery


def _project_decision(
    value: Mapping[str, Any],
    material: _BrowserActionMaterial,
) -> PermissionDecisionRecord:
    binding = _mapping(value.get("requestBinding"))
    effect = _permission_effect(value.get("effect"))
    fingerprint = str(value.get("requestFingerprint") or "")
    digest_value = str(value.get("finalArgumentsDigest") or "")
    scope = PermissionScope(
        kind=PermissionScopeKind.ACTION,
        session_id=str(binding.get("session_id") or ""),
        task_id=str(binding.get("task_id") or ""),
        run_id=str(binding.get("run_id") or ""),
        workspace_root=str(binding.get("workspace_root") or ""),
        tool_namespace=str(binding.get("namespace") or ""),
        tool_name=str(binding.get("tool_name") or ""),
        server_id=str(binding.get("server_id") or ""),
        argument_digest=digest_value,
        request_fingerprint=fingerprint,
        domains=tuple(filter(None, (_action_domain(material.action),))),
        metadata={"canonical_owner": "typescript"},
    )
    recovery = _project_recovery(value, material)
    risk = _mapping(value.get("risk"))
    return PermissionDecisionRecord(
        decision_id=str(value.get("decisionId") or ""),
        effect=effect,
        mode=_permission_mode(value.get("mode")),
        request_fingerprint=fingerprint,
        arguments_digest=digest_value,
        tool_use_id=material.call.tool_call_id,
        tool_identity=material.identity,
        session_id=str(binding.get("session_id") or ""),
        task_id=str(binding.get("task_id") or ""),
        run_id=str(binding.get("run_id") or ""),
        worker_request_id=str(binding.get("worker_request_id") or ""),
        reason_code=str(value.get("reasonCode") or "typescript_permission_decision"),
        reason=str(value.get("reason") or "TypeScript permission decision"),
        scope=scope,
        matched_rule_ids=tuple(str(item) for item in value.get("matchedRuleIds", []) if str(item)),
        request_id=str(value.get("continuationRequestId") or ""),
        rule_snapshot_id=str(value.get("policyDigest") or ""),
        mode_revision=_non_negative_int(value.get("modeRevision")),
        hook_evidence=tuple(
            _mapping(item) for item in value.get("hooks", []) if isinstance(item, Mapping)
        ),
        classifier_evidence=risk,
        recovery_input=recovery,
        created_at=str(value.get("evaluatedAt") or _now_iso()),
        metadata={
            "canonical_owner": "typescript",
            "policy_revision": _non_negative_int(value.get("policyRevision")),
            "human_intervention_count": _non_negative_int(
                value.get("humanInterventionCount")
            ),
            "python_decision_fallback": False,
        },
    )


def _project_recovery(
    value: Mapping[str, Any],
    material: _BrowserActionMaterial,
) -> PermissionRecoveryInput | None:
    raw = _mapping(value.get("recoveryInput"))
    if not raw and value.get("effect") == "allow":
        return None
    binding = _mapping(value.get("requestBinding"))
    alternatives = tuple(
        item if isinstance(item, dict) else {"kind": "instruction", "instruction": str(item)}
        for item in raw.get("alternatives", [])
    )
    if not alternatives:
        alternatives = tuple(
            {"kind": "instruction", "instruction": item}
            for item in _recovery_alternatives(value)
        )
    return PermissionRecoveryInput(
        recovery_input_id=str(raw.get("recovery_input_id") or raw.get("recoveryId") or "")
        or "e02-recovery-" + hashlib.sha256(
            str(value.get("decisionId") or "").encode("utf-8")
        ).hexdigest()[:24],
        decision_id=str(value.get("decisionId") or ""),
        session_id=str(binding.get("session_id") or ""),
        task_id=str(binding.get("task_id") or ""),
        run_id=str(binding.get("run_id") or ""),
        tool_use_id=material.call.tool_call_id,
        reason_code=str(value.get("reasonCode") or "typescript_permission_blocked"),
        retryable=value.get("effect") == "ask" or bool(raw.get("retryable")),
        alternatives=alternatives,
        constraints=_mapping(raw.get("constraints")),
        created_at=str(value.get("evaluatedAt") or _now_iso()),
        metadata={
            "canonical_owner": "typescript",
            "replan_required": bool(value.get("replanRequired")),
        },
    )


def _project_pending_request(
    envelope: Mapping[str, Any],
    decision: PermissionDecisionRecord,
    material: _BrowserActionMaterial,
) -> PermissionRequestRecord | None:
    if decision.effect is not PermissionEffect.ASK:
        return None
    if not envelope:
        raise BrowserActionPermissionIdentityError(
            "TypeScript ASK decision did not deliver an approval request"
        )
    return PermissionRequestRecord(
        request_id=str(envelope.get("request_id") or decision.request_id),
        session_id=decision.session_id,
        task_id=decision.task_id,
        run_id=decision.run_id,
        worker_request_id=decision.worker_request_id,
        tool_use_id=decision.tool_use_id,
        tool_identity=material.identity,
        arguments_digest=decision.arguments_digest,
        request_fingerprint=decision.request_fingerprint,
        scope=decision.scope,
        expires_at=str(envelope.get("expires_at") or _future_iso()),
        reason_code=decision.reason_code,
        reason=decision.reason,
        status=PermissionRequestStatus.PENDING,
        phase=PermissionRequestPhase.DELIVERED,
        revision=0,
        created_at=str(envelope.get("created_at") or decision.created_at),
        delivered_at=str(envelope.get("updated_at") or envelope.get("created_at") or decision.created_at),
        rule_snapshot_id=decision.rule_snapshot_id,
        mode=decision.mode,
        metadata={
            "canonical_owner": "typescript",
            "approval_envelope_id": str(envelope.get("envelope_id") or ""),
            "typescript_arguments_digest": decision.arguments_digest,
            "python_transport_arguments_digest": arguments_digest(
                material.call.arguments
            ),
            "projection_role": "browser_continuation_transport",
            "python_decision_fallback": False,
        },
    )


def _project_request_envelope(
    envelope: Mapping[str, Any],
    *,
    effective_mode: str,
) -> PermissionRequestRecord:
    status_text = str(envelope.get("status") or "")
    response_effect = str(envelope.get("response_effect") or "")
    if status_text == "responded" and response_effect == "allow":
        status = PermissionRequestStatus.APPROVED
        phase = PermissionRequestPhase.RESOLVED
        resolution = PermissionEffect.ALLOW
    elif status_text == "responded" and response_effect == "deny":
        status = PermissionRequestStatus.DENIED
        phase = PermissionRequestPhase.RESOLVED
        resolution = PermissionEffect.DENY
    elif status_text == "expired":
        status = PermissionRequestStatus.EXPIRED
        phase = PermissionRequestPhase.EXPIRED
        resolution = None
    elif status_text == "cancelled":
        status = PermissionRequestStatus.CANCELLED
        phase = PermissionRequestPhase.CANCELLED
        resolution = None
    else:
        status = PermissionRequestStatus.PENDING
        phase = PermissionRequestPhase.DELIVERED
        resolution = None
    binding = _mapping(envelope.get("request_binding"))
    fingerprint = str(
        envelope.get("request_fingerprint")
        or binding.get("request_fingerprint")
        or ""
    )
    digest_value = str(
        envelope.get("arguments_digest")
        or binding.get("arguments_digest")
        or ""
    )
    identity = ToolIdentity(
        namespace=str(binding.get("namespace") or envelope.get("namespace") or "browser"),
        name=str(binding.get("tool_name") or envelope.get("tool_name") or "browser_action"),
        server_id=str(binding.get("server_id") or envelope.get("server_id") or ""),
    )
    scope = PermissionScope(
        kind=PermissionScopeKind.ACTION,
        session_id=str(binding.get("session_id") or envelope.get("session_id") or ""),
        task_id=str(binding.get("task_id") or envelope.get("task_id") or ""),
        run_id=str(binding.get("run_id") or envelope.get("run_id") or ""),
        workspace_root=str(binding.get("workspace_root") or ""),
        tool_namespace=identity.namespace,
        tool_name=identity.name,
        server_id=identity.server_id,
        argument_digest=digest_value,
        request_fingerprint=fingerprint or digest_value,
        metadata={
            "canonical_owner": "typescript",
            "request_binding_projected": bool(binding),
        },
    )
    return PermissionRequestRecord(
        request_id=str(envelope.get("request_id") or ""),
        session_id=str(envelope.get("session_id") or ""),
        task_id=str(envelope.get("task_id") or ""),
        run_id=str(envelope.get("run_id") or ""),
        worker_request_id=str(envelope.get("worker_request_id") or ""),
        tool_use_id=str(envelope.get("tool_call_id") or ""),
        tool_identity=identity,
        arguments_digest=digest_value,
        request_fingerprint=fingerprint or digest_value,
        scope=scope,
        expires_at=str(envelope.get("expires_at") or _future_iso()),
        reason_code="typescript_approval_request",
        reason=str(envelope.get("reason") or "TypeScript approval request"),
        status=status,
        phase=phase,
        created_at=str(envelope.get("created_at") or _now_iso()),
        delivered_at=str(envelope.get("updated_at") or envelope.get("created_at") or _now_iso()),
        resolved_at=(
            str(envelope.get("updated_at") or _now_iso())
            if phase is PermissionRequestPhase.RESOLVED
            else None
        ),
        resolved_by=str(envelope.get("responder") or "typescript-api-operator"),
        resolution_channel="typescript-e02-api-port" if resolution else "",
        resolution_effect=resolution,
        mode=_permission_mode(effective_mode),
        metadata={
            "canonical_owner": "typescript",
            "approval_envelope_id": str(envelope.get("envelope_id") or ""),
            "typescript_arguments_digest": digest_value,
            "request_binding_projected": bool(binding),
            "python_decision_fallback": False,
        },
    )


def _blocked_events(
    *,
    run_id: str,
    task_id: str,
    node_id: str | None,
    worker_request_id: str,
    session_id: str,
    action: BrowserActionPermissionInput,
    guard: _BrowserGuardResult,
    cause_event_id: str,
) -> tuple[EventRecord, EventRecord]:
    decision = guard.decision
    blocked = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="browser_action_permission_blocked",
        effect=str(guard.effect),
        reason=decision.reason,
        tool_use_id=decision.tool_use_id,
        request_id=decision.request_id,
        decision_id=decision.decision_id,
        arguments_digest_value=decision.arguments_digest,
        cause_event_id=cause_event_id,
        payload={
            "action": action.normalized_action,
            "backend": action.backend,
            "reason_code": decision.reason_code,
            "ask_pending": guard.ask_pending,
            "action_execution_allowed": False,
            "human_intervention_count": 0,
            "canonical_permission_owner": "typescript",
        },
    )
    recovery = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="recovery_input",
        effect=str(guard.effect),
        reason="browser action stopped before its physical effect boundary",
        tool_use_id=decision.tool_use_id,
        request_id=decision.request_id,
        decision_id=decision.decision_id,
        arguments_digest_value=decision.arguments_digest,
        cause_event_id=blocked.event_id,
        payload={
            **(decision.recovery_input.to_dict() if decision.recovery_input else {}),
            "action_execution_allowed": False,
            "human_intervention_count": 0,
        },
    )
    return blocked, recovery


def _rejected_events(
    *,
    run_id: str,
    task_id: str,
    node_id: str | None,
    worker_request_id: str,
    session_id: str,
    action: BrowserActionPermissionInput,
    decision: BrowserActionPermissionDecision,
    reason: str,
    cause_event_id: str,
) -> tuple[EventRecord, EventRecord]:
    rejected = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="browser_action_execution_grant_rejected",
        effect="deny",
        reason=reason,
        tool_use_id=decision.tool_use_id,
        request_id=decision.request_id,
        decision_id=decision.decision_id,
        arguments_digest_value=decision.guard.decision.arguments_digest,
        cause_event_id=cause_event_id,
        payload={
            "action": action.normalized_action,
            "backend": action.backend,
            "action_execution_allowed": False,
            "human_intervention_count": 0,
        },
    )
    recovery = _permission_event(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        session_id=session_id,
        worker_request_id=worker_request_id,
        kind="recovery_input",
        effect="deny",
        reason="request a fresh TypeScript decision; never replay this receipt",
        tool_use_id=decision.tool_use_id,
        request_id=decision.request_id,
        decision_id=decision.decision_id,
        arguments_digest_value=decision.guard.decision.arguments_digest,
        cause_event_id=rejected.event_id,
        payload={
            "retryable": True,
            "action_execution_allowed": False,
            "human_intervention_count": 0,
        },
    )
    return rejected, recovery


def _permission_event(
    *,
    run_id: str,
    task_id: str,
    node_id: str | None,
    session_id: str,
    worker_request_id: str,
    kind: str,
    effect: str,
    reason: str,
    tool_use_id: str = "",
    request_id: str = "",
    decision_id: str = "",
    arguments_digest_value: str = "",
    cause_event_id: str = "",
    payload: Mapping[str, Any] | None = None,
) -> EventRecord:
    return EventRecord(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "query_session": {
                "session_id": session_id,
                "worker_request_id": worker_request_id,
                "phase": kind,
                "permission_runtime": {
                    "schema": "zyra.browser-action-permission.event.v2",
                    "kind": kind,
                    "runtime_id": BROWSER_ACTION_GATE_RUNTIME_ID,
                    "owner_unit": BROWSER_ACTION_GATE_OWNER_UNIT,
                    "session_id": session_id,
                    "worker_request_id": worker_request_id,
                    "tool_call_id": tool_use_id,
                    "request_id": request_id,
                    "decision_id": decision_id,
                    "arguments_digest": arguments_digest_value,
                    "effect": effect,
                    "reason": reason,
                    "cause_event_id": cause_event_id,
                    "canonical_permission_owner": "typescript",
                    "python_decision_fallback": False,
                    "payload": _safe_event_payload(payload or {}),
                },
            }
        },
    )


def _browser_action_capabilities(action: BrowserActionPermissionInput) -> tuple[str, ...]:
    normalized = action.normalized_action
    target_url = action.target_url or str(action.arguments.get("url") or action.current_url or "")
    scheme = urlparse(target_url).scheme.casefold() if target_url else ""
    local_resource = scheme in {"", "file", "workspace"}
    local_read_actions = {
        "open_url",
        "navigate",
        "extract_text",
        "snapshot_state",
        "search_page",
        "wait",
        "take_screenshot",
        "save_as_pdf",
        "collect_downloads",
        "scroll_page",
        "scroll_to_text",
        "go_back",
        "list_targets",
        "focus_target",
        "capture_trace",
    }
    local_virtual_actions = {"click_element", "input_text", "send_keys"}
    session_read_actions = {
        "snapshot_state",
        "search_page",
        "get_dropdown_options",
        "list_targets",
        "wait",
        "capture_trace",
    }
    capabilities: list[str] = ["browser_action"]
    if normalized in session_read_actions or (
        local_resource and normalized in local_read_actions
    ) or (
        action.backend == "static"
        and local_resource
        and normalized in local_virtual_actions
    ):
        capabilities.extend(("read_only", "local_browser_state"))
    else:
        capabilities.append("network")
        if normalized in {
            "click_element",
            "input_text",
            "upload_file",
            "send_keys",
            "evaluate_js",
            "open_url",
            "go_back",
        } or action.backend == "browser-use-agent":
            capabilities.append("external_side_effect")
        else:
            capabilities.append("read_only")
    if normalized == "upload_file":
        capabilities.extend(("upload", "external_side_effect"))
    if normalized == "collect_downloads":
        capabilities.append("download")
    if normalized == "evaluate_js":
        capabilities.extend(("mutation", "external_side_effect"))
    return tuple(dict.fromkeys(capabilities))


def _permission_operation(action: BrowserActionPermissionInput) -> str:
    if action.read_only:
        return "read"
    if set(action.capabilities).intersection({"mutation", "upload"}):
        return "write"
    return "execute"


def _workspace_state(action: BrowserActionPermissionInput, workspace_root: Path) -> dict[str, Any]:
    root = workspace_root.resolve()
    path_safe = True
    raw_path = str(action.arguments.get("path") or "").strip()
    if raw_path:
        try:
            candidate = Path(raw_path).expanduser()
            selected = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
            selected.relative_to(root)
        except (OSError, ValueError):
            path_safe = False
    for url in (action.target_url, str(action.arguments.get("url") or "")):
        parsed = urlparse(url)
        if parsed.scheme != "file":
            continue
        try:
            path_text = url2pathname(unquote(parsed.path))
            if parsed.netloc:
                path_text = f"//{parsed.netloc}{path_text}"
            Path(path_text).resolve().relative_to(root)
        except (OSError, ValueError):
            path_safe = False
    return {
        "workspace_root": str(root),
        "workspace_scoped": path_safe,
        "path_validated": path_safe,
        "read_before_write": action.read_only,
        "bounded_change": action.read_only,
        "external_egress": action.network,
        "outside_workspace": not path_safe,
    }


def _default_session_id(*, run_id: str, task_id: str, workspace_root: Path) -> str:
    encoded = json.dumps(
        {
            "schema": "zyra.browser-permission-session.v2",
            "run_id": run_id,
            "task_id": task_id,
            "workspace_root": str(workspace_root.resolve()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"browser-permission-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def _tool_use_id(
    *,
    base: str,
    session_id: str,
    run_id: str,
    task_id: str,
    step_index: int,
    action: str,
    arguments_digest_value: str,
) -> str:
    if base:
        return base if step_index == 1 else f"{base}:{step_index}"
    encoded = json.dumps(
        {
            "schema": "zyra.browser-permission-tool-use.v2",
            "session_id": session_id,
            "run_id": run_id,
            "task_id": task_id,
            "step_index": step_index,
            "action": action,
            "arguments_digest": arguments_digest_value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"browser-tool-use-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def _effective_mode(
    requested: str,
    constraints: Mapping[str, Any],
) -> tuple[str, bool, bool]:
    normalized = str(requested or "default").strip()
    lowered = normalized.casefold().replace("-", "_")
    bypass_requested = lowered in {
        "bypass",
        "bypasspermissions",
        "bypass_permissions",
        "dangerously_skip_permissions",
    } or constraints.get("permission_bypass_available") is True
    auto_requested = lowered in {"auto", "autonomous"} or constraints.get(
        "permission_auto_available"
    ) is True
    if bypass_requested or auto_requested:
        return "default", bypass_requested, auto_requested
    aliases = {
        "workspace": "default",
        "dont_ask": "dontAsk",
        "sealed": "sealed",
        "default": "default",
        "plan": "plan",
        "accept_edits": "acceptEdits",
        "acceptedits": "acceptEdits",
    }
    return aliases.get(lowered, "default"), False, False


def _permission_effect(value: Any) -> PermissionEffect:
    try:
        return PermissionEffect(str(value))
    except ValueError as error:
        raise BrowserActionPermissionIdentityError(
            f"TypeScript permission receipt has invalid effect {value!r}"
        ) from error


def _permission_mode(value: Any) -> PermissionMode:
    aliases = {
        "acceptEdits": "accept_edits",
        "dontAsk": "dont_ask",
        "bypassPermissions": "bypass",
    }
    text = aliases.get(str(value), str(value or "default"))
    try:
        return PermissionMode(text)
    except ValueError:
        return PermissionMode.DEFAULT


def _mode_text(value: Any) -> str:
    text = str(value or "default")
    return text if text in {
        "default",
        "acceptEdits",
        "dontAsk",
        "plan",
        "auto",
        "bypassPermissions",
        "sealed",
    } else "default"


def _recovery_alternatives(value: Mapping[str, Any]) -> tuple[str, ...]:
    recovery = _mapping(value.get("recoveryInput"))
    selected: list[str] = []
    for item in recovery.get("alternatives", []):
        if isinstance(item, Mapping):
            text = str(item.get("instruction") or item.get("reason") or "")
        else:
            text = str(item)
        if text:
            selected.append(text)
    if selected:
        return tuple(selected)
    if value.get("effect") == "ask":
        return ("approve the exact TypeScript request and retry unchanged",)
    if value.get("effect") == "deny":
        return ("remove the denied side effect and replan",)
    return ()


def _canonical_owner(value: Mapping[str, Any]) -> bool:
    return str(value.get("canonical_owner") or "") == "typescript.PermissionCoordinator"


def _require_typescript_response(value: Mapping[str, Any]) -> None:
    if not _canonical_owner(value):
        raise BrowserActionPermissionIdentityError(
            "browser physical execution requires the canonical TypeScript permission owner"
        )
    if value.get("python_decision_fallback") is not False:
        raise BrowserActionPermissionIdentityError(
            "TypeScript permission response did not prove Python fallback was disabled"
        )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _action_domain(action: BrowserActionPermissionInput) -> str:
    for value in (action.target_url, str(action.arguments.get("url") or ""), action.current_url):
        parsed = urlparse(value)
        if parsed.hostname:
            return parsed.hostname.casefold()
    return ""


_SENSITIVE_EVENT_KEYS = frozenset(
    {
        "arguments",
        "canonical_arguments",
        "raw_arguments",
        "authorization_token",
        "session_custody_token",
        "permission_session_custody_token",
        "token",
        "signature",
        "password",
        "credential",
        "cookie",
        "secret",
        "api_key",
    }
)


def _safe_event_payload(value: Any, *, key: str = "") -> Any:
    normalized = key.casefold().replace("-", "_")
    if normalized in _SENSITIVE_EVENT_KEYS or normalized.endswith("_custody_token"):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(name): _safe_event_payload(item, key=str(name))
            for name, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray, memoryview),
    ):
        return [_safe_event_payload(item, key=key) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        if name.casefold() in {
            "token",
            "session_custody_token",
            "permission_session_custody_token",
        }:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            output[name] = item
        elif isinstance(item, Sequence) and not isinstance(
            item,
            (str, bytes, bytearray, memoryview),
        ):
            output[name] = [str(child) for child in item]
    return output


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


__all__ = [
    "BROWSER_ACTION_GATE_OWNER_UNIT",
    "BROWSER_ACTION_GATE_RUNTIME_ID",
    "BrowserActionPermissionConsumption",
    "BrowserActionPermissionCustodyError",
    "BrowserActionPermissionDecision",
    "BrowserActionPermissionDisabled",
    "BrowserActionPermissionError",
    "BrowserActionPermissionGate",
    "BrowserActionPermissionIdentityError",
    "BrowserActionPermissionInput",
    "browser_permission_setup_failure_events",
]
