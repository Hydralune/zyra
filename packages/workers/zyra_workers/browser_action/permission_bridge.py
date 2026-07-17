from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from zyra_core import EventRecord
from zyra_runtime.permission import (
    BrowserActionPermissionConsumption,
    BrowserActionPermissionDecision,
    BrowserActionPermissionGate,
    BrowserActionPermissionInput,
)
from zyra_runtime.permission.models import PermissionEffect

from .models import (
    ActionPreflightReceipt,
    ActionRequest,
    ActionRiskAssessment,
    PermissionDisposition,
    digest_value,
)


class PermissionBridgeError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PermissionBridgeDecision:
    preflight: ActionPreflightReceipt
    permission_input: BrowserActionPermissionInput
    decision: BrowserActionPermissionDecision
    events: tuple[EventRecord, ...]

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    @property
    def ask_pending(self) -> bool:
        return bool(self.decision.guard.ask_pending)

    @property
    def effect(self) -> PermissionEffect:
        return self.decision.effect

    def public_dict(self) -> dict[str, Any]:
        return {
            "preflight_receipt_id": self.preflight.receipt_id,
            "action_id": self.preflight.action_id,
            "allowed": self.allowed,
            "ask_pending": self.ask_pending,
            "effect": str(self.effect),
            "decision_id": self.decision.decision_id,
            "request_id": self.decision.request_id,
            "tool_use_id": self.decision.tool_use_id,
            "event_ids": [event.event_id for event in self.events],
        }


@dataclass(frozen=True, slots=True)
class PermissionBridgeConsumption:
    preflight: ActionPreflightReceipt
    bridge_decision: PermissionBridgeDecision
    consumption: BrowserActionPermissionConsumption
    events: tuple[EventRecord, ...]

    @property
    def accepted(self) -> bool:
        return self.consumption.accepted and self.preflight.authorizes_execution

    def public_dict(self) -> dict[str, Any]:
        return {
            "preflight_receipt_id": self.preflight.receipt_id,
            "action_id": self.preflight.action_id,
            "accepted": self.accepted,
            "reason": self.consumption.reason,
            "decision_id": self.bridge_decision.decision.decision_id,
            "request_id": self.bridge_decision.decision.request_id,
            "tool_use_id": self.bridge_decision.decision.tool_use_id,
            "event_ids": [event.event_id for event in self.events],
        }


class BrowserActionPermissionBridge:
    """Browser metadata adapter to the existing M1-03A permission owner.

    The bridge creates no rule, decision, request, token or grant store.  It
    maps the complete 04C receipt material into ``BrowserActionPermissionInput``
    and delegates guard/consume to the 03A ``BrowserActionPermissionGate``.
    """

    def __init__(self, gate: BrowserActionPermissionGate, *, disabled: bool = False) -> None:
        self.gate = gate
        self.disabled = disabled

    def guard(
        self,
        *,
        request: ActionRequest,
        preflight: ActionPreflightReceipt,
    ) -> PermissionBridgeDecision:
        self._ensure_available()
        self._assert_binding(request, preflight)
        if preflight.assessment.permission == PermissionDisposition.DENY:
            raise PermissionBridgeError(
                "classifier_denied",
                "browser risk classifier denied the action before permission evaluation",
                details={"risk": str(preflight.assessment.risk), "reasons": list(preflight.assessment.reasons)},
            )
        permission_input = self._input(request, preflight)
        try:
            decision = self.gate.guard(permission_input)
        except Exception as exc:
            raise PermissionBridgeError(
                "permission_guard_failed",
                f"browser permission owner failed closed: {type(exc).__name__}: {exc}",
            ) from exc
        return PermissionBridgeDecision(
            preflight=preflight,
            permission_input=permission_input,
            decision=decision,
            events=tuple(decision.events),
        )

    def consume(
        self,
        bridge_decision: PermissionBridgeDecision,
        *,
        request: ActionRequest,
        fresh_preflight: ActionPreflightReceipt,
    ) -> PermissionBridgeConsumption:
        self._ensure_available()
        original = bridge_decision.preflight
        self._assert_binding(request, fresh_preflight)
        if fresh_preflight.request_digest != original.request_digest:
            raise PermissionBridgeError("permission_request_changed", "browser action request changed after approval")
        if fresh_preflight.arguments_digest != original.arguments_digest:
            raise PermissionBridgeError("permission_arguments_changed", "browser action arguments changed after approval")
        if fresh_preflight.registry_digest != original.registry_digest:
            raise PermissionBridgeError("permission_registry_changed", "browser action registry changed after approval")
        if selector_digest(fresh_preflight) != selector_digest(original):
            raise PermissionBridgeError("permission_selector_changed", "browser selector identity changed after approval")
        if fresh_preflight.network_receipt_id != original.network_receipt_id:
            raise PermissionBridgeError("permission_network_changed", "browser network receipt changed after approval")
        if fresh_preflight.file_receipt_id != original.file_receipt_id:
            raise PermissionBridgeError("permission_file_changed", "browser file receipt changed after approval")
        if fresh_preflight.secret_receipt_id != original.secret_receipt_id:
            raise PermissionBridgeError("permission_secret_changed", "browser secret receipt changed after approval")
        if fresh_preflight.clipboard_receipt_id != original.clipboard_receipt_id:
            raise PermissionBridgeError("permission_clipboard_changed", "browser clipboard receipt changed after approval")
        if fresh_preflight.form_receipt_id != original.form_receipt_id:
            raise PermissionBridgeError("permission_form_changed", "browser form receipt changed after approval")
        replay = self._input(request, fresh_preflight)
        try:
            consumption = self.gate.consume(bridge_decision.decision, replay)
        except Exception as exc:
            raise PermissionBridgeError(
                "permission_consume_failed",
                f"browser execution grant failed closed: {type(exc).__name__}: {exc}",
            ) from exc
        bound = fresh_preflight.with_permission(
            decision_id=bridge_decision.decision.decision_id,
            request_id=bridge_decision.decision.request_id,
            tool_use_id=bridge_decision.decision.tool_use_id,
            consumed=consumption.accepted,
        )
        return PermissionBridgeConsumption(
            preflight=bound,
            bridge_decision=bridge_decision,
            consumption=consumption,
            events=tuple(consumption.events),
        )

    def _input(self, request: ActionRequest, receipt: ActionPreflightReceipt) -> BrowserActionPermissionInput:
        definition = receipt.definition
        binding = receipt.selector_binding
        browser_metadata: dict[str, Any] = {
            "zyra_browser_action_id": request.identity.action_id,
            "zyra_browser_request_digest": receipt.request_digest,
            "zyra_browser_registry_digest": receipt.registry_digest,
            "zyra_browser_action_schema_identity": definition.identity,
            "zyra_browser_risk": str(receipt.assessment.risk),
            "zyra_browser_policy_version": receipt.assessment.policy_version,
            "zyra_browser_capabilities": list(receipt.assessment.capabilities),
            "zyra_browser_risk_tags": list(receipt.assessment.risk_tags),
            "zyra_browser_safety_flags": list(receipt.assessment.safety_flags),
            "zyra_browser_network_receipt_id": receipt.network_receipt_id,
            "zyra_browser_file_receipt_id": receipt.file_receipt_id,
            "zyra_browser_secret_receipt_id": receipt.secret_receipt_id,
            "zyra_browser_clipboard_receipt_id": receipt.clipboard_receipt_id,
            "zyra_browser_form_receipt_id": receipt.form_receipt_id,
            "zyra_browser_hook_receipt_ids": list(receipt.hook_receipt_ids),
        }
        if binding is not None:
            browser_metadata.update(
                {
                    "zyra_browser_selector_binding_digest": binding.identity_digest,
                    "zyra_browser_selector_ref": binding.selector_ref,
                    "zyra_browser_selector_revision_id": binding.selector_revision_id,
                    "zyra_browser_selector_generation": binding.selector_generation,
                    "zyra_browser_backend_node_id": binding.backend_node_id,
                    "zyra_browser_target_id": binding.target_id,
                    "zyra_browser_target_generation": binding.target_generation,
                    "zyra_browser_cdp_session_id": binding.cdp_session_id,
                    "zyra_browser_cdp_generation": binding.cdp_generation,
                    "zyra_browser_frame_id": binding.frame_id,
                    "zyra_browser_document_loader_id": binding.document_loader_id,
                }
            )
        return BrowserActionPermissionInput(
            step_index=request.identity.step_index,
            action=request.action,
            normalized_action=definition.name,
            backend=request.backend,
            arguments={
                **dict(receipt.public_arguments),
                "_zyra_browser_binding": browser_metadata,
            },
            source_action=definition.source_name,
            source_model=f"BrowserActionDefinition.v{definition.schema_version}",
            required_arguments=definition.required_arguments,
            optional_arguments=(*definition.optional_arguments, "_zyra_browser_binding"),
            current_url=request.current_url,
            target_url=request.target_url,
            explicit_tool_use_id=str(request.metadata.get("permission_tool_use_id") or ""),
            metadata=browser_metadata,
        )

    @staticmethod
    def _assert_binding(request: ActionRequest, receipt: ActionPreflightReceipt) -> None:
        if receipt.action_id != request.identity.action_id:
            raise PermissionBridgeError("action_identity_mismatch", "preflight receipt belongs to another action")
        if receipt.request_digest != request.request_digest:
            raise PermissionBridgeError("request_identity_mismatch", "preflight receipt belongs to another request")
        if receipt.arguments_digest != digest_value(receipt.public_arguments):
            raise PermissionBridgeError("public_arguments_digest_mismatch", "preflight public arguments were modified")
        if receipt.assessment.action != receipt.definition.name:
            raise PermissionBridgeError("risk_action_mismatch", "risk assessment belongs to another action")

    def _ensure_available(self) -> None:
        if self.disabled or self.gate is None:
            raise PermissionBridgeError("permission_bridge_disabled", "browser permission bridge is disabled")


def selector_digest(receipt: ActionPreflightReceipt) -> str:
    return receipt.selector_binding.identity_digest if receipt.selector_binding else ""
