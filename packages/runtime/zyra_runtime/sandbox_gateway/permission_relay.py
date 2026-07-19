from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from zyra_runtime.permission.canonical import (
    arguments_digest,
    build_request_fingerprint,
    build_tool_identity,
)

from .canonical import digest, stable_id
from .constants import APPROVAL_DIGEST_PREFIX
from .errors import GatewayErrorCode, SandboxGatewayError
from .models import (
    CommandEffect,
    CommandPolicyDecision,
    GatewayCommandEnvelope,
    PermissionBinding,
)
from .state_store import GatewayStateStore


@dataclass(frozen=True, slots=True)
class PermissionRelayRequest:
    request_id: str
    request_fingerprint: str
    envelope: GatewayCommandEnvelope
    policy: CommandPolicyDecision
    interactive: bool
    sealed: bool
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "request_fingerprint": self.request_fingerprint,
            "envelope": self.envelope.to_dict(),
            "policy": self.policy.to_dict(),
            "interactive": self.interactive,
            "sealed": self.sealed,
            "created_at": self.created_at,
            "final_authority": "typescript.PermissionCoordinator",
        }


@dataclass(frozen=True, slots=True)
class PermissionRuntimeDecision:
    allowed: bool
    effect: CommandEffect
    request_fingerprint: str
    grant_material: Any = None
    expires_at: float = 0.0
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def grant_digest(self) -> str:
        if self.grant_material is None:
            return ""
        return digest(
            {"grant_material": self._project_grant(self.grant_material)},
            prefix=APPROVAL_DIGEST_PREFIX,
        )

    @staticmethod
    def _project_grant(value: Any) -> Any:
        if hasattr(value, "to_dict") and callable(value.to_dict):
            return value.to_dict()
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return {"type": type(value).__name__, "identity": repr(value)}


@dataclass(frozen=True, slots=True)
class PermissionTicket:
    binding: PermissionBinding
    runtime_decision: PermissionRuntimeDecision
    request: PermissionRelayRequest

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "request": self.request.to_dict(),
            "runtime_decision": {
                "allowed": self.runtime_decision.allowed,
                "effect": self.runtime_decision.effect.value,
                "request_fingerprint": self.runtime_decision.request_fingerprint,
                "grant_digest": self.runtime_decision.grant_digest,
                "expires_at": self.runtime_decision.expires_at,
                "reason": self.runtime_decision.reason,
                "metadata": dict(self.runtime_decision.metadata),
            },
        }


class ToolPermissionRuntimePort(Protocol):
    """Port implemented by the existing ToolPermissionRuntime adapter."""

    def evaluate(self, request: PermissionRelayRequest) -> PermissionRuntimeDecision:
        ...

    def validate_and_consume(
        self,
        request: PermissionRelayRequest,
        decision: PermissionRuntimeDecision,
        replay: GatewayCommandEnvelope,
    ) -> bool:
        ...


class CallbackToolPermissionRuntimePort:
    """Thin callback adapter; callbacks must delegate to ToolPermissionRuntime."""

    def __init__(
        self,
        *,
        evaluate: Callable[[PermissionRelayRequest], PermissionRuntimeDecision],
        validate_and_consume: Callable[
            [PermissionRelayRequest, PermissionRuntimeDecision, GatewayCommandEnvelope],
            bool,
        ],
        descriptor: Mapping[str, Any] | None = None,
    ) -> None:
        self._evaluate = evaluate
        self._consume = validate_and_consume
        self._descriptor = dict(descriptor or {})

    def evaluate(self, request: PermissionRelayRequest) -> PermissionRuntimeDecision:
        return self._evaluate(request)

    def validate_and_consume(
        self,
        request: PermissionRelayRequest,
        decision: PermissionRuntimeDecision,
        replay: GatewayCommandEnvelope,
    ) -> bool:
        return bool(self._consume(request, decision, replay))

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "adapter": "CallbackToolPermissionRuntimePort",
            "final_authority": "typescript.PermissionCoordinator",
            "can_decide_without_callback": False,
            **self._descriptor,
        }


class GatewayPermissionRelay:
    """Exact identity relay; it never substitutes for ToolPermissionRuntime."""

    def __init__(
        self,
        store: GatewayStateStore,
        permission_runtime: ToolPermissionRuntimePort,
        *,
        default_ttl_seconds: float = 120.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.permission_runtime = permission_runtime
        self.default_ttl_seconds = float(default_ttl_seconds)
        self.clock = clock

    def issue(
        self,
        envelope: GatewayCommandEnvelope,
        policy: CommandPolicyDecision,
        *,
        interactive: bool,
        sealed: bool,
    ) -> PermissionTicket:
        if policy.denied:
            raise SandboxGatewayError(
                GatewayErrorCode.POLICY_DENIED,
                policy.reason,
                operation="permission_issue",
                recovery=policy.recovery,
                metadata=policy.to_dict(),
            )
        fingerprint = self.request_fingerprint(envelope)
        request = PermissionRelayRequest(
            request_id=stable_id(
                "gateway-permission-request",
                envelope.command_id,
                fingerprint,
                policy.policy_digest,
            ),
            request_fingerprint=fingerprint,
            envelope=envelope,
            policy=policy,
            interactive=bool(interactive),
            sealed=bool(sealed),
        )
        try:
            decision = self.permission_runtime.evaluate(request)
        except Exception as error:
            raise SandboxGatewayError(
                GatewayErrorCode.PERMISSION_UNAVAILABLE,
                f"typescript.PermissionCoordinator evaluation failed closed: {type(error).__name__}",
                operation="permission_issue",
                retryable=True,
                recovery=("restore the session-owned typescript.PermissionCoordinator",),
            ) from error
        self._validate_runtime_decision(request, decision)
        if not decision.allowed or decision.effect is CommandEffect.DENY:
            raise SandboxGatewayError(
                GatewayErrorCode.POLICY_DENIED,
                decision.reason or "typescript.PermissionCoordinator denied the command",
                operation="permission_issue",
                metadata={"request_fingerprint": fingerprint},
            )
        if decision.grant_material is None or not decision.grant_digest:
            raise SandboxGatewayError(
                GatewayErrorCode.PERMISSION_UNAVAILABLE,
                "typescript.PermissionCoordinator allowed without issuing an execution grant",
                operation="permission_issue",
            )
        now = self.clock()
        expires_at = decision.expires_at or now + self.default_ttl_seconds
        if expires_at <= now:
            raise SandboxGatewayError(
                GatewayErrorCode.APPROVAL_EXPIRED,
                "typescript.PermissionCoordinator issued an already-expired execution grant",
                operation="permission_issue",
            )
        binding = PermissionBinding(
            binding_id=stable_id(
                "gateway-permission-binding",
                envelope.session_id,
                envelope.command_id,
                fingerprint,
                decision.grant_digest,
            ),
            session_id=envelope.session_id,
            command_id=envelope.command_id,
            tool_use_id=envelope.tool_use_id,
            request_fingerprint=fingerprint,
            command_digest=envelope.identity_digest,
            grant_digest=decision.grant_digest,
            effect=decision.effect,
            issued_at=now,
            expires_at=expires_at,
            metadata={
                "permission_owner": "typescript.PermissionCoordinator",
                "policy_digest": policy.policy_digest,
                "interactive": interactive,
                "sealed": sealed,
            },
        )
        persisted = self.store.save_permission_binding(binding)
        return PermissionTicket(
            binding=persisted,
            runtime_decision=decision,
            request=request,
        )

    def consume(
        self,
        ticket: PermissionTicket,
        replay: GatewayCommandEnvelope,
    ) -> PermissionBinding:
        binding = self.store.require_permission_binding(ticket.binding.binding_id)
        self._assert_replay(binding, ticket.request.envelope, replay)
        try:
            accepted = self.permission_runtime.validate_and_consume(
                ticket.request,
                ticket.runtime_decision,
                replay,
            )
        except Exception as error:
            raise SandboxGatewayError(
                GatewayErrorCode.APPROVAL_MISMATCH,
                f"typescript.PermissionCoordinator grant validation failed: {type(error).__name__}",
                operation="permission_consume",
            ) from error
        if not accepted:
            raise SandboxGatewayError(
                GatewayErrorCode.APPROVAL_MISMATCH,
                "typescript.PermissionCoordinator rejected grant identity or replay",
                operation="permission_consume",
            )
        consumption_id = stable_id(
            "gateway-permission-consumption",
            binding.binding_id,
            replay.command_id,
            replay.identity_digest,
            self.clock(),
        )
        return self.store.consume_permission_binding(
            binding.binding_id,
            consumption_id=consumption_id,
            expected_grant_digest=ticket.runtime_decision.grant_digest,
            expected_command_digest=replay.identity_digest,
        )

    @staticmethod
    def request_fingerprint(envelope: GatewayCommandEnvelope) -> str:
        schema = {
            "type": "object",
            "required": [
                "executable",
                "argv",
                "cwd",
                "environment_digest",
                "workspace_id",
                "owner_epoch",
            ],
            "additionalProperties": False,
        }
        identity = build_tool_identity(
            "sandbox_command",
            namespace="gateway",
            version="v1",
            schema=schema,
        )
        argument_digest = arguments_digest(envelope.permission_material())
        return build_request_fingerprint(
            identity,
            argument_digest,
            session_id=envelope.session_id,
            tool_use_id=envelope.tool_use_id,
            run_id=envelope.run_id,
            task_id=envelope.task_id,
        )

    @staticmethod
    def _validate_runtime_decision(
        request: PermissionRelayRequest,
        decision: PermissionRuntimeDecision,
    ) -> None:
        if decision.request_fingerprint != request.request_fingerprint:
            raise SandboxGatewayError(
                GatewayErrorCode.APPROVAL_MISMATCH,
                "typescript.PermissionCoordinator decision is bound to different request material",
                operation="permission_issue",
            )
        if decision.allowed and decision.effect is CommandEffect.DENY:
            raise SandboxGatewayError(
                GatewayErrorCode.PERMISSION_UNAVAILABLE,
                "typescript.PermissionCoordinator returned an internally inconsistent decision",
                operation="permission_issue",
            )
        if not decision.allowed and decision.effect is CommandEffect.ALLOW:
            raise SandboxGatewayError(
                GatewayErrorCode.PERMISSION_UNAVAILABLE,
                "typescript.PermissionCoordinator returned an internally inconsistent decision",
                operation="permission_issue",
            )

    @staticmethod
    def _assert_replay(
        binding: PermissionBinding,
        approved: GatewayCommandEnvelope,
        replay: GatewayCommandEnvelope,
    ) -> None:
        if binding.consumed:
            raise SandboxGatewayError(
                GatewayErrorCode.APPROVAL_REPLAY,
                "permission binding was already consumed",
                operation="permission_consume",
            )
        if binding.expired:
            raise SandboxGatewayError(
                GatewayErrorCode.APPROVAL_EXPIRED,
                "permission binding expired",
                operation="permission_consume",
            )
        if approved.command_id != replay.command_id:
            raise SandboxGatewayError(
                GatewayErrorCode.COMMAND_MUTATED,
                "command id changed after permission evaluation",
                operation="permission_consume",
            )
        if binding.command_digest != replay.identity_digest:
            raise SandboxGatewayError(
                GatewayErrorCode.COMMAND_MUTATED,
                "executable, argv, cwd, or environment changed after permission evaluation",
                operation="permission_consume",
            )
        if approved.permission_material() != replay.permission_material():
            raise SandboxGatewayError(
                GatewayErrorCode.COMMAND_MUTATED,
                "permission-bound command material changed after approval",
                operation="permission_consume",
            )
