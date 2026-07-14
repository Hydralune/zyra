from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from .canonical import token_digest
from .models import (
    CommandEffect,
    GatewayCommandEnvelope,
)
from .permission_relay import PermissionRelayRequest, PermissionRuntimeDecision
from .integration_models import canonical_value, content_digest, freeze_mapping, stable_identifier
from .integration_policy import GatewaySurfacePolicyDecision


@dataclass(frozen=True, slots=True)
class PermissionInvocationBinding:
    binding_id: str
    call: Any
    grant: Any
    authority: Any
    execution_context: Any
    policy: GatewaySurfacePolicyDecision
    command_digest: str
    tool_call_id: str
    activated_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.binding_id or not self.tool_call_id or not self.command_digest:
            raise ValueError("permission invocation binding is incomplete")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class PermissionConsumptionReceipt:
    receipt_id: str
    binding_id: str
    tool_call_id: str
    command_digest: str
    grant_digest: str
    allowed: bool
    reason: str
    authority_type: str
    consumed_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.receipt_id or not self.binding_id:
            raise ValueError("permission consumption receipt is incomplete")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "binding_id": self.binding_id,
            "tool_call_id": self.tool_call_id,
            "command_digest": self.command_digest,
            "grant_digest": self.grant_digest,
            "allowed": self.allowed,
            "reason": self.reason,
            "authority_type": self.authority_type,
            "consumed_at": self.consumed_at,
            "metadata": canonical_value(self.metadata),
        }


class GatewayPermissionBridgeError(RuntimeError):
    pass


class GatewayPermissionBridge:
    """Adapts the existing ToolPermissionRuntime to GatewayPermissionRelay.

    The bridge owns no rules and mints no authority.  It presents exactly one
    in-flight ToolPermissionRuntime grant to the 05B-01 relay, validates it at
    the last side-effect boundary, and makes the resulting authorization
    single-use inside the gateway call.
    """

    def __init__(self, *, maximum_binding_seconds: float = 30.0) -> None:
        if maximum_binding_seconds <= 0:
            raise ValueError("maximum_binding_seconds must be positive")
        self.maximum_binding_seconds = maximum_binding_seconds
        self._local = threading.local()
        self._lock = threading.RLock()
        self._consumed: dict[str, PermissionConsumptionReceipt] = {}

    @contextlib.contextmanager
    def activate(
        self,
        *,
        call: Any,
        grant: Any,
        authority: Any,
        execution_context: Any,
        policy: GatewaySurfacePolicyDecision,
        command_digest: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Iterator[PermissionInvocationBinding]:
        tool_call_id = str(getattr(call, "tool_call_id", "") or "")
        if not tool_call_id:
            raise GatewayPermissionBridgeError("tool call id is required for gateway authorization")
        if getattr(self._local, "binding", None) is not None:
            raise GatewayPermissionBridgeError("nested gateway permission binding is denied")
        binding = PermissionInvocationBinding(
            binding_id=stable_identifier(
                "gateway-permission",
                tool_call_id,
                command_digest,
                time.time_ns(),
            ),
            call=call,
            grant=grant,
            authority=authority,
            execution_context=execution_context,
            policy=policy,
            command_digest=command_digest,
            tool_call_id=tool_call_id,
            metadata=metadata or {},
        )
        self._local.binding = binding
        self._local.evaluated = False
        self._local.relay_consumed = False
        self._local.receipt = None
        try:
            yield binding
        finally:
            self._local.binding = None
            self._local.evaluated = False
            self._local.relay_consumed = False
            self._local.receipt = None

    def evaluate(self, request: PermissionRelayRequest) -> PermissionRuntimeDecision:
        binding = self._require_binding()
        if self._local.evaluated:
            raise GatewayPermissionBridgeError("permission binding was evaluated more than once")
        self._assert_live(binding)
        self._assert_request_matches(binding, request)
        self._local.evaluated = True
        if binding.policy.hard_denied:
            receipt = self._receipt(binding, allowed=False, reason="gateway policy hard denied")
            self._store_receipt(receipt)
            self._local.receipt = receipt
            return PermissionRuntimeDecision(
                allowed=False,
                effect=CommandEffect.DENY,
                request_fingerprint=request.request_fingerprint,
                reason=receipt.reason,
                metadata={"bridge_receipt_id": receipt.receipt_id},
            )
        if not binding.policy.requires_permission and binding.policy.allowed:
            receipt = self._receipt(binding, allowed=True, reason="policy-classified low-risk action")
            self._store_receipt(receipt)
            self._local.receipt = receipt
            return PermissionRuntimeDecision(
                allowed=True,
                effect=CommandEffect.ALLOW,
                request_fingerprint=request.request_fingerprint,
                grant_material={
                    "bridge_binding_id": binding.binding_id,
                    "bridge_receipt_id": receipt.receipt_id,
                },
                expires_at=time.time() + self.maximum_binding_seconds,
                reason=receipt.reason,
                metadata={"bridge_receipt_id": receipt.receipt_id, "auto_allow": True},
            )
        if binding.grant is None:
            receipt = self._receipt(binding, allowed=False, reason="exact execution grant is missing")
            self._store_receipt(receipt)
            self._local.receipt = receipt
            return PermissionRuntimeDecision(
                allowed=False,
                effect=CommandEffect.ASK,
                request_fingerprint=request.request_fingerprint,
                reason=receipt.reason,
                metadata={"bridge_receipt_id": receipt.receipt_id},
            )
        authority = binding.authority
        validator = getattr(authority, "validate_and_consume", None)
        if not callable(validator):
            receipt = self._receipt(binding, allowed=False, reason="ToolPermissionRuntime authority is unavailable")
            self._store_receipt(receipt)
            self._local.receipt = receipt
            return PermissionRuntimeDecision(
                allowed=False,
                effect=CommandEffect.DENY,
                request_fingerprint=request.request_fingerprint,
                reason=receipt.reason,
                metadata={"bridge_receipt_id": receipt.receipt_id},
            )
        try:
            allowed = bool(validator(binding.call, binding.grant, binding.execution_context))
        except Exception as error:  # noqa: BLE001 - permission failures must close the boundary.
            receipt = self._receipt(
                binding,
                allowed=False,
                reason=f"permission grant validation failed: {type(error).__name__}",
            )
            self._store_receipt(receipt)
            self._local.receipt = receipt
            return PermissionRuntimeDecision(
                allowed=False,
                effect=CommandEffect.DENY,
                request_fingerprint=request.request_fingerprint,
                reason=receipt.reason,
                metadata={"bridge_receipt_id": receipt.receipt_id},
            )
        receipt = self._receipt(
            binding,
            allowed=allowed,
            reason="ToolPermissionRuntime consumed exact grant" if allowed else "exact grant was rejected or consumed",
        )
        self._store_receipt(receipt)
        self._local.receipt = receipt
        return PermissionRuntimeDecision(
            allowed=allowed,
            effect=CommandEffect.ALLOW if allowed else CommandEffect.DENY,
            request_fingerprint=request.request_fingerprint,
            grant_material={
                "bridge_binding_id": binding.binding_id,
                "bridge_receipt_id": receipt.receipt_id,
            }
            if allowed
            else None,
            expires_at=time.time() + self.maximum_binding_seconds if allowed else 0.0,
            reason=receipt.reason,
            metadata={
                "bridge_receipt_id": receipt.receipt_id,
                "tool_permission_runtime_owner": True,
            },
        )

    def validate_and_consume(
        self,
        request: PermissionRelayRequest,
        decision: PermissionRuntimeDecision,
        replay: GatewayCommandEnvelope,
    ) -> bool:
        binding = self._require_binding()
        self._assert_live(binding)
        self._assert_request_matches(binding, request)
        if not self._local.evaluated:
            raise GatewayPermissionBridgeError("permission decision was not evaluated")
        if self._local.relay_consumed:
            raise GatewayPermissionBridgeError("gateway relay authorization is single-use")
        receipt = self._local.receipt
        if not isinstance(receipt, PermissionConsumptionReceipt):
            raise GatewayPermissionBridgeError("permission consumption receipt is missing")
        if not decision.allowed or not receipt.allowed:
            return False
        if request.request_fingerprint != decision.request_fingerprint:
            return False
        if binding.command_digest != replay.identity_digest:
            return False
        grant_material = decision.grant_material
        if not isinstance(grant_material, Mapping):
            return False
        if grant_material.get("bridge_binding_id") != binding.binding_id:
            return False
        if grant_material.get("bridge_receipt_id") != receipt.receipt_id:
            return False
        self._local.relay_consumed = True
        return True

    def consume_non_command(
        self,
        *,
        call: Any,
        grant: Any,
        authority: Any,
        execution_context: Any,
        policy: GatewaySurfacePolicyDecision,
    ) -> PermissionConsumptionReceipt:
        tool_call_id = str(getattr(call, "tool_call_id", "") or "")
        binding = PermissionInvocationBinding(
            binding_id=stable_identifier(
                "gateway-permission",
                tool_call_id,
                policy.subject_digest,
                time.time_ns(),
            ),
            call=call,
            grant=grant,
            authority=authority,
            execution_context=execution_context,
            policy=policy,
            command_digest=policy.subject_digest,
            tool_call_id=tool_call_id,
            metadata={"non_command": True},
        )
        if policy.hard_denied:
            receipt = self._receipt(binding, allowed=False, reason="gateway policy hard denied")
            self._store_receipt(receipt)
            return receipt
        if not policy.requires_permission and policy.allowed:
            receipt = self._receipt(binding, allowed=True, reason="policy-classified low-risk action")
            self._store_receipt(receipt)
            return receipt
        if grant is None:
            receipt = self._receipt(binding, allowed=False, reason="exact execution grant is missing")
            self._store_receipt(receipt)
            return receipt
        validator = getattr(authority, "validate_and_consume", None)
        if not callable(validator):
            receipt = self._receipt(binding, allowed=False, reason="ToolPermissionRuntime authority is unavailable")
            self._store_receipt(receipt)
            return receipt
        try:
            allowed = bool(validator(call, grant, execution_context))
            reason = "ToolPermissionRuntime consumed exact grant" if allowed else "exact grant was rejected or consumed"
        except Exception as error:  # noqa: BLE001
            allowed = False
            reason = f"permission grant validation failed: {type(error).__name__}"
        receipt = self._receipt(binding, allowed=allowed, reason=reason)
        self._store_receipt(receipt)
        return receipt

    def receipt(self, receipt_id: str) -> PermissionConsumptionReceipt | None:
        with self._lock:
            return self._consumed.get(receipt_id)

    def receipts(self) -> tuple[PermissionConsumptionReceipt, ...]:
        with self._lock:
            return tuple(self._consumed.values())

    def descriptor(self) -> Mapping[str, Any]:
        with self._lock:
            count = len(self._consumed)
            allowed = sum(1 for item in self._consumed.values() if item.allowed)
        return {
            "owner": "ToolPermissionRuntime",
            "bridge": "GatewayPermissionBridge",
            "mints_authority": False,
            "stores_raw_grants": False,
            "maximum_binding_seconds": self.maximum_binding_seconds,
            "receipt_count": count,
            "allowed_receipt_count": allowed,
        }

    def _require_binding(self) -> PermissionInvocationBinding:
        binding = getattr(self._local, "binding", None)
        if not isinstance(binding, PermissionInvocationBinding):
            raise GatewayPermissionBridgeError("gateway permission call has no active binding")
        return binding

    def _assert_live(self, binding: PermissionInvocationBinding) -> None:
        if time.time() - binding.activated_at > self.maximum_binding_seconds:
            raise GatewayPermissionBridgeError("gateway permission binding expired")

    @staticmethod
    def _assert_request_matches(
        binding: PermissionInvocationBinding,
        request: PermissionRelayRequest,
    ) -> None:
        envelope = request.envelope
        if envelope.tool_use_id != binding.tool_call_id:
            raise GatewayPermissionBridgeError("gateway permission tool call binding changed")
        if request.policy.policy_digest != binding.policy.normalized.get(
            "base_policy_digest",
            request.policy.policy_digest,
        ):
            raise GatewayPermissionBridgeError("gateway command policy digest changed")
        if request.policy.command_digest != binding.command_digest:
            raise GatewayPermissionBridgeError("gateway command digest changed")

    @staticmethod
    def _grant_digest(grant: Any) -> str:
        if grant is None:
            return content_digest("missing")
        for field_name in ("grant_id", "execution_grant_id", "capability_id", "token"):
            value = getattr(grant, field_name, None)
            if value:
                return token_digest(str(value))
            if isinstance(grant, Mapping) and grant.get(field_name):
                return token_digest(str(grant[field_name]))
        return content_digest(
            {
                "grant_type": type(grant).__name__,
                "safe_identity": getattr(grant, "safe_identity", ""),
            }
        )

    def _receipt(
        self,
        binding: PermissionInvocationBinding,
        *,
        allowed: bool,
        reason: str,
    ) -> PermissionConsumptionReceipt:
        grant_digest = self._grant_digest(binding.grant)
        return PermissionConsumptionReceipt(
            receipt_id=stable_identifier(
                "gateway-grant-consumption",
                binding.binding_id,
                grant_digest,
                allowed,
            ),
            binding_id=binding.binding_id,
            tool_call_id=binding.tool_call_id,
            command_digest=binding.command_digest,
            grant_digest=grant_digest,
            allowed=allowed,
            reason=reason,
            authority_type=type(binding.authority).__name__,
            metadata={
                "policy_digest": binding.policy.policy_digest,
                "subject_digest": binding.policy.subject_digest,
                "raw_grant_persisted": False,
            },
        )

    def _store_receipt(self, receipt: PermissionConsumptionReceipt) -> None:
        with self._lock:
            existing = self._consumed.get(receipt.receipt_id)
            if existing is not None and existing != receipt:
                raise GatewayPermissionBridgeError("permission receipt collision")
            self._consumed[receipt.receipt_id] = receipt


__all__ = [
    "GatewayPermissionBridge",
    "GatewayPermissionBridgeError",
    "PermissionConsumptionReceipt",
    "PermissionInvocationBinding",
]
