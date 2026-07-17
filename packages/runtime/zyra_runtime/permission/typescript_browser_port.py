from __future__ import annotations

"""Fail-closed browser seam after the E02 TypeScript permission cutover.

Browser execution used to construct a second Python permission runtime.  E02
forbids that ownership.  The compatibility types below keep the established
browser interfaces importable, but they cannot evaluate policy or mint a
grant.  Browser effects must be entered through the TypeScript CodeWorker
path, where an ``E02CapabilityCoordinator`` decision is checked by
``TypeScriptPermissionReceiptPort`` at the physical tool boundary.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from zyra_core import EventRecord, EventType

from .canonical import canonicalize_arguments
from .models import PermissionEffect


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
        if self.step_index < 1 or not self.normalized_action.strip() or not self.backend.strip():
            raise ValueError("browser permission action identity is incomplete")
        object.__setattr__(self, "arguments", canonicalize_arguments(dict(self.arguments)))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def permission_arguments(self) -> dict[str, Any]:
        return canonicalize_arguments({
            **dict(self.arguments),
            "_zyra_browser_action": self.normalized_action,
            "_zyra_browser_backend": self.backend,
            "_zyra_browser_current_url": self.current_url,
            "_zyra_browser_target_url": self.target_url,
        })


@dataclass(frozen=True, slots=True)
class BrowserActionPermissionDecision:
    """Shape-only projection of a decision already made by TypeScript."""

    material: Any
    guard: Any
    events: tuple[EventRecord, ...] = ()

    @property
    def allowed(self) -> bool:
        return bool(self.guard.allowed)

    @property
    def effect(self) -> PermissionEffect:
        return PermissionEffect(str(self.guard.effect))

    @property
    def request_id(self) -> str:
        return str(self.guard.decision.request_id)

    @property
    def decision_id(self) -> str:
        return str(self.guard.decision.decision_id)

    @property
    def tool_use_id(self) -> str:
        return str(self.material.call.tool_call_id)

    def metadata(self) -> dict[str, str]:
        return {
            "permission_runtime_id": "typescript-e02-receipt-port",
            "permission_effect": str(self.effect),
            "permission_decision_id": self.decision_id,
            "permission_request_id": self.request_id,
            "permission_tool_use_id": self.tool_use_id,
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


class BrowserActionPermissionGate:
    """Removed Python evaluator facade; every entry fails closed by design."""

    session_id = ""
    custody_token = ""
    effective_mode = "typescript-only"

    @classmethod
    def for_worker_request(cls, *_args: Any, **_kwargs: Any) -> "BrowserActionPermissionGate":
        raise BrowserActionPermissionDisabled("E02CapabilityCoordinator decision receipt required")

    def guard(self, _action: BrowserActionPermissionInput) -> BrowserActionPermissionDecision:
        raise BrowserActionPermissionDisabled("Python browser permission evaluation removed by E02")

    def consume(
        self,
        _decision: BrowserActionPermissionDecision,
        _replay: BrowserActionPermissionInput,
    ) -> BrowserActionPermissionConsumption:
        raise BrowserActionPermissionDisabled("TypeScript execution receipt required")

    def metadata(self) -> dict[str, str]:
        return {
            "browser_permission_gate_runtime_id": "typescript-e02-receipt-port",
            "canonical_permission_owner": "typescript",
            "python_policy_fallback": "false",
        }


def browser_permission_setup_failure_events(
    request: Any,
    *,
    session_id: str,
    component: str,
    reason: str,
    code: str,
) -> tuple[EventRecord, EventRecord]:
    identity = {
        "session_id": session_id,
        "worker_request_id": str(getattr(request, "request_id", "")),
        "component": component,
        "reason": reason,
        "code": code,
        "canonical_permission_owner": "typescript",
        "python_policy_fallback": False,
    }
    common = {
        "run_id": str(getattr(request, "run_id", "")),
        "task_id": str(getattr(request, "task_id", "")),
        "node_id": getattr(request, "node_id", None),
        "event_type": EventType.AGENT_MESSAGE,
    }
    return (
        EventRecord(**common, payload={"permission": {**identity, "phase": "blocked"}}),
        EventRecord(**common, payload={"permission": {**identity, "phase": "replan_required"}}),
    )


__all__ = [name for name in globals() if name.startswith("BrowserAction") or name.startswith("browser_")]
